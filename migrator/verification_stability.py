from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from .repository import GitRepository
from .verification import RepositoryVerificationSuite, VerificationReport


class StabilityAuditError(RuntimeError):
    """Fail-closed audit error with a stable, non-sensitive reason code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class StabilityPolicy:
    commands: tuple[str, ...]
    trials: int = 3
    min_passes: int | None = None
    timeout_seconds: float = 120.0
    max_output_bytes: int = 256_000
    max_patch_bytes: int = 4_000_000
    max_files: int = 20_000
    max_tree_bytes: int = 256_000_000
    max_outcome_variants: int = 1
    max_output_variants: int | None = None
    reject_output_truncation: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.commands, tuple) or not self.commands:
            raise ValueError("commands must be a non-empty tuple")
        if not all(isinstance(command, str) and command.strip() for command in self.commands):
            raise ValueError("commands must contain non-empty strings")
        if len(set(self.commands)) != len(self.commands):
            raise ValueError("commands must be unique")
        if (
            isinstance(self.trials, bool)
            or not isinstance(self.trials, int)
            or not 2 <= self.trials <= 20
        ):
            raise ValueError("trials must be between 2 and 20")
        quorum = self.required_passes
        if (
            isinstance(quorum, bool)
            or not isinstance(quorum, int)
            or not 1 <= quorum <= self.trials
        ):
            raise ValueError("min_passes must be between 1 and trials")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or not 0.01 <= self.timeout_seconds <= 3_600
        ):
            raise ValueError("timeout_seconds must be between 0.01 and 3600")
        for name, value, upper in (
            ("max_output_bytes", self.max_output_bytes, 16_000_000),
            ("max_patch_bytes", self.max_patch_bytes, 64_000_000),
            ("max_files", self.max_files, 100_000),
            ("max_tree_bytes", self.max_tree_bytes, 2_000_000_000),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
                raise ValueError(f"{name} must be between 1 and {upper}")
        for name, value in (
            ("max_outcome_variants", self.max_outcome_variants),
            ("max_output_variants", self.max_output_variants),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= self.trials
            ):
                raise ValueError(f"{name} must be between 1 and trials")
        if not isinstance(self.reject_output_truncation, bool):
            raise TypeError("reject_output_truncation must be boolean")

    @property
    def required_passes(self) -> int:
        return self.trials if self.min_passes is None else self.min_passes

    def evidence(self) -> dict[str, object]:
        return {
            "command_sha256": [_text_digest(command) for command in self.commands],
            "trials": self.trials,
            "min_passes": self.required_passes,
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "max_patch_bytes": self.max_patch_bytes,
            "max_files": self.max_files,
            "max_tree_bytes": self.max_tree_bytes,
            "max_outcome_variants": self.max_outcome_variants,
            "max_output_variants": self.max_output_variants,
            "reject_output_truncation": self.reject_output_truncation,
        }

    @property
    def digest_sha256(self) -> str:
        return _digest(self.evidence())


@dataclass(frozen=True)
class TrialEvidence:
    trial: int
    passed: bool
    mutated_candidate: bool
    output_truncated: bool
    completed_steps: int
    outcome_sha256: str
    output_sha256: str
    candidate_before_sha256: str
    candidate_after_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "trial": self.trial,
            "passed": self.passed,
            "mutated_candidate": self.mutated_candidate,
            "output_truncated": self.output_truncated,
            "completed_steps": self.completed_steps,
            "outcome_sha256": self.outcome_sha256,
            "output_sha256": self.output_sha256,
            "candidate_before_sha256": self.candidate_before_sha256,
            "candidate_after_sha256": self.candidate_after_sha256,
        }


@dataclass(frozen=True)
class StabilityReport:
    schema_version: int
    accepted: bool
    reason_codes: tuple[str, ...]
    base_commit_sha: str
    patch_sha256: str
    policy_sha256: str
    pass_count: int
    required_passes: int
    outcome_variant_count: int
    output_variant_count: int
    trials: tuple[TrialEvidence, ...]
    evidence_sha256: str

    def body(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "accepted": self.accepted,
            "reason_codes": list(self.reason_codes),
            "base_commit_sha": self.base_commit_sha,
            "patch_sha256": self.patch_sha256,
            "policy_sha256": self.policy_sha256,
            "pass_count": self.pass_count,
            "required_passes": self.required_passes,
            "outcome_variant_count": self.outcome_variant_count,
            "output_variant_count": self.output_variant_count,
            "trials": [trial.as_dict() for trial in self.trials],
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.body(), "evidence_sha256": self.evidence_sha256}

    def with_evidence_digest(self) -> StabilityReport:
        return replace(self, evidence_sha256=_digest(self.body()))


class SuiteFactory(Protocol):
    def __call__(self, policy: StabilityPolicy) -> RepositoryVerificationSuite: ...


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bytes_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _candidate_snapshot(root: Path, policy: StabilityPolicy) -> str:
    process = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        raise StabilityAuditError("CANDIDATE_ENUMERATION_FAILED")
    raw_paths = process.stdout.split(b"\0")
    if raw_paths and raw_paths[-1] == b"":
        raw_paths.pop()
    try:
        paths = sorted(path.decode("utf-8") for path in raw_paths)
    except UnicodeDecodeError as exc:
        raise StabilityAuditError("NON_UTF8_CANDIDATE_PATH") from exc
    if not paths:
        raise StabilityAuditError("EMPTY_CANDIDATE_TREE")
    if len(paths) != len(set(paths)):
        raise StabilityAuditError("DUPLICATE_CANDIDATE_PATH")
    if len(paths) > policy.max_files:
        raise StabilityAuditError("CANDIDATE_FILE_BUDGET_EXCEEDED")

    digest = hashlib.sha256()
    total_bytes = 0
    resolved_root = root.resolve()
    for relative in paths:
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts or not relative_path.parts:
            raise StabilityAuditError("UNSAFE_CANDIDATE_PATH")
        path = (resolved_root / relative_path).resolve()
        if not path.is_relative_to(resolved_root):
            raise StabilityAuditError("CANDIDATE_PATH_ESCAPE")
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise StabilityAuditError("CANDIDATE_READ_FAILED") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise StabilityAuditError("CANDIDATE_SYMLINK_REJECTED")
        if not stat.S_ISREG(metadata.st_mode):
            raise StabilityAuditError("CANDIDATE_SPECIAL_FILE_REJECTED")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise StabilityAuditError("CANDIDATE_READ_FAILED") from exc
        total_bytes += len(content)
        if total_bytes > policy.max_tree_bytes:
            raise StabilityAuditError("CANDIDATE_TREE_BUDGET_EXCEEDED")
        path_bytes = relative.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(4, "big"))
        digest.update(path_bytes)
        digest.update(stat.S_IMODE(metadata.st_mode).to_bytes(4, "big"))
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def _step_evidence(
    report: VerificationReport,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    outcomes: list[dict[str, object]] = []
    outputs: list[dict[str, object]] = []
    for step in report.steps:
        outcomes.append(
            {
                "name": step["name"],
                "passed": step["passed"],
                "returncode": step["returncode"],
                "timed_out": step["timed_out"],
                "output_truncated": step["output_truncated"],
            }
        )
        outputs.append(
            {
                "name": step["name"],
                "stdout_sha256": _text_digest(str(step["stdout"])),
                "stderr_sha256": _text_digest(str(step["stderr"])),
            }
        )
    return outcomes, outputs


def _default_suite(policy: StabilityPolicy) -> RepositoryVerificationSuite:
    return RepositoryVerificationSuite(
        list(policy.commands),
        timeout_seconds=policy.timeout_seconds,
        max_output_bytes=policy.max_output_bytes,
    )


def audit_patch_stability(
    *,
    base_repository: GitRepository,
    patch_path: str | Path,
    policy: StabilityPolicy,
    suite_factory: SuiteFactory = _default_suite,
) -> StabilityReport:
    """Replay and verify one patch in independent worktrees before promotion."""
    base_repository.ensure_clean()
    base_sha = base_repository.head_sha()
    patch = Path(patch_path).resolve()
    try:
        patch_bytes = patch.read_bytes()
    except OSError as exc:
        raise StabilityAuditError("PATCH_READ_FAILED") from exc
    if not patch_bytes:
        raise StabilityAuditError("EMPTY_PATCH")
    if len(patch_bytes) > policy.max_patch_bytes:
        raise StabilityAuditError("PATCH_BUDGET_EXCEEDED")
    patch_sha = _bytes_digest(patch_bytes)

    trial_evidence: list[TrialEvidence] = []
    for trial in range(1, policy.trials + 1):
        with base_repository.isolated_worktree(base_sha) as worktree:
            candidate = GitRepository(worktree)
            try:
                candidate.apply_patch(patch)
            except subprocess.CalledProcessError as exc:
                raise StabilityAuditError("PATCH_APPLICATION_FAILED") from exc
            before = _candidate_snapshot(worktree, policy)
            report = suite_factory(policy).run(worktree)
            after = _candidate_snapshot(worktree, policy)
            outcomes, outputs = _step_evidence(report)
            truncated = any(bool(step["output_truncated"]) for step in report.steps)
            completed = len(report.steps)
            passed = (
                report.passed
                and completed == len(policy.commands)
                and before == after
                and not (policy.reject_output_truncation and truncated)
            )
            trial_evidence.append(
                TrialEvidence(
                    trial=trial,
                    passed=passed,
                    mutated_candidate=before != after,
                    output_truncated=truncated,
                    completed_steps=completed,
                    outcome_sha256=_digest(outcomes),
                    output_sha256=_digest(outputs),
                    candidate_before_sha256=before,
                    candidate_after_sha256=after,
                )
            )

    pass_count = sum(trial.passed for trial in trial_evidence)
    outcome_variants = len({trial.outcome_sha256 for trial in trial_evidence})
    output_variants = len({trial.output_sha256 for trial in trial_evidence})
    reasons: list[str] = []
    if any(trial.mutated_candidate for trial in trial_evidence):
        reasons.append("VERIFICATION_MUTATED_CANDIDATE")
    if policy.reject_output_truncation and any(trial.output_truncated for trial in trial_evidence):
        reasons.append("OUTPUT_TRUNCATION_DETECTED")
    if any(trial.completed_steps != len(policy.commands) for trial in trial_evidence):
        reasons.append("INCOMPLETE_VERIFICATION_SEQUENCE")
    if pass_count < policy.required_passes:
        reasons.append("PASS_QUORUM_NOT_MET")
    if outcome_variants > policy.max_outcome_variants:
        reasons.append("OUTCOME_VARIANT_BUDGET_EXCEEDED")
    if policy.max_output_variants is not None and output_variants > policy.max_output_variants:
        reasons.append("OUTPUT_VARIANT_BUDGET_EXCEEDED")

    report = StabilityReport(
        schema_version=1,
        accepted=not reasons,
        reason_codes=tuple(reasons),
        base_commit_sha=base_sha,
        patch_sha256=patch_sha,
        policy_sha256=policy.digest_sha256,
        pass_count=pass_count,
        required_passes=policy.required_passes,
        outcome_variant_count=outcome_variants,
        output_variant_count=output_variants,
        trials=tuple(trial_evidence),
        evidence_sha256="",
    )
    return report.with_evidence_digest()


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, allow_nan=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit repeated migration verification stability")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--patch", required=True)
    parser.add_argument("--command", action="append", required=True)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--min-passes", type=int)
    parser.add_argument("--max-output-variants", type=int)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        policy = StabilityPolicy(
            commands=tuple(args.command),
            trials=args.trials,
            min_passes=args.min_passes,
            timeout_seconds=args.timeout,
            max_output_variants=args.max_output_variants,
        )
        report = audit_patch_stability(
            base_repository=GitRepository(args.repository),
            patch_path=args.patch,
            policy=policy,
        )
        _atomic_write(Path(args.output), report.as_dict())
        return 0 if report.accepted else 2
    except (StabilityAuditError, ValueError, TypeError, RuntimeError) as exc:
        code = exc.code if isinstance(exc, StabilityAuditError) else "INVALID_AUDIT_INPUT"
        _atomic_write(
            Path(args.output),
            {"accepted": False, "error_code": code, "schema_version": 1},
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
