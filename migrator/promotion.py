from __future__ import annotations

import hashlib
import json
import math
import shlex
import stat
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .repository import GitRepository
from .verification import RepositoryVerificationSuite, VerificationReport

SHA1_LENGTH = 40


class PromotionError(RuntimeError):
    """Fail-closed promotion error with a stable, non-sensitive reason code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PromotionPolicy:
    required_commands: tuple[str, ...]
    timeout_seconds: float = 120.0
    max_output_bytes: int = 256_000
    max_patch_bytes: int = 4_000_000
    max_files: int = 20_000
    max_file_bytes: int = 16_000_000
    max_tree_bytes: int = 256_000_000
    reject_truncated_output: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.required_commands, tuple) or not self.required_commands:
            raise ValueError("at least one verification command is required")
        if not all(isinstance(command, str) for command in self.required_commands):
            raise ValueError("verification commands must be strings")
        try:
            parsed = [tuple(shlex.split(command)) for command in self.required_commands]
        except ValueError as exc:
            raise ValueError("verification command quoting is invalid") from exc
        if any(not command for command in parsed):
            raise ValueError("verification commands cannot be empty")
        if len(set(parsed)) != len(parsed):
            raise ValueError("verification commands must be unique")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or not 0.01 <= self.timeout_seconds <= 3_600
        ):
            raise ValueError("timeout_seconds must be between 0.01 and 3600")
        if not isinstance(self.reject_truncated_output, bool):
            raise TypeError("reject_truncated_output must be boolean")
        for name, value, lower, upper in (
            ("max_output_bytes", self.max_output_bytes, 1, 16_000_000),
            ("max_patch_bytes", self.max_patch_bytes, 1, 64_000_000),
            ("max_files", self.max_files, 1, 100_000),
            ("max_file_bytes", self.max_file_bytes, 1, 256_000_000),
            ("max_tree_bytes", self.max_tree_bytes, 1, 2_000_000_000),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                raise ValueError(f"{name} must be between {lower} and {upper}")

    def normalized_commands(self) -> tuple[tuple[str, ...], ...]:
        return tuple(_normalize_command(tuple(shlex.split(raw))) for raw in self.required_commands)

    def evidence(self) -> dict[str, object]:
        return {
            "required_commands": [list(command) for command in self.normalized_commands()],
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "max_patch_bytes": self.max_patch_bytes,
            "max_files": self.max_files,
            "max_file_bytes": self.max_file_bytes,
            "max_tree_bytes": self.max_tree_bytes,
            "reject_truncated_output": self.reject_truncated_output,
        }

    @property
    def digest(self) -> str:
        return _digest(self.evidence())


@dataclass(frozen=True)
class TreeSnapshot:
    digest_sha256: str
    file_count: int
    total_bytes: int


@dataclass(frozen=True)
class PromotionReceipt:
    schema_version: int
    base_commit_sha: str
    candidate_tree_sha256: str
    candidate_file_count: int
    candidate_total_bytes: int
    patch_sha256: str
    patch_bytes: int
    verification_sha256: str
    verification_gate_count: int
    policy_sha256: str
    evidence_sha256: str

    def body(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "base_commit_sha": self.base_commit_sha,
            "candidate_tree_sha256": self.candidate_tree_sha256,
            "candidate_file_count": self.candidate_file_count,
            "candidate_total_bytes": self.candidate_total_bytes,
            "patch_sha256": self.patch_sha256,
            "patch_bytes": self.patch_bytes,
            "verification_sha256": self.verification_sha256,
            "verification_gate_count": self.verification_gate_count,
            "policy_sha256": self.policy_sha256,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.body(), "evidence_sha256": self.evidence_sha256}

    def with_evidence_digest(self) -> PromotionReceipt:
        return replace(self, evidence_sha256=_digest(self.body()))


@dataclass(frozen=True)
class PromotionDecision:
    accepted: bool
    reason_codes: tuple[str, ...]
    receipt_sha256: str
    candidate_tree_sha256: str | None
    patch_sha256: str | None
    evidence_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "reason_codes": list(self.reason_codes),
            "receipt_sha256": self.receipt_sha256,
            "candidate_tree_sha256": self.candidate_tree_sha256,
            "patch_sha256": self.patch_sha256,
            "evidence_sha256": self.evidence_sha256,
        }


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validate_git_sha(value: str) -> None:
    if len(value) != SHA1_LENGTH or any(character not in "0123456789abcdef" for character in value):
        raise PromotionError("INVALID_BASE_COMMIT")


def _normalize_command(command: tuple[str, ...]) -> tuple[str, ...]:
    if not command:
        raise PromotionError("EMPTY_VERIFICATION_COMMAND")
    return (Path(command[0]).name, *command[1:])


def _git_paths(repository: GitRepository) -> tuple[str, ...]:
    process = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=repository.root,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        raise PromotionError("TREE_ENUMERATION_FAILED")
    raw_paths = process.stdout.split(b"\0")
    if raw_paths and not raw_paths[-1]:
        raw_paths.pop()
    try:
        paths = tuple(path.decode("utf-8") for path in raw_paths)
    except UnicodeDecodeError as exc:
        raise PromotionError("NON_UTF8_REPOSITORY_PATH") from exc
    if len(paths) != len(set(paths)):
        raise PromotionError("DUPLICATE_REPOSITORY_PATH")
    return tuple(sorted(paths))


def snapshot_tree(repository: GitRepository, policy: PromotionPolicy) -> TreeSnapshot:
    """Hash tracked and non-ignored candidate files without exposing their contents."""

    paths = _git_paths(repository)
    if not paths:
        raise PromotionError("EMPTY_CANDIDATE_TREE")
    if len(paths) > policy.max_files:
        raise PromotionError("TREE_FILE_BUDGET_EXCEEDED")

    tree_hash = hashlib.sha256()
    total_bytes = 0
    for relative in paths:
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
            raise PromotionError("UNSAFE_REPOSITORY_PATH")
        path_bytes = relative.encode("utf-8")
        if len(path_bytes) > 4_096:
            raise PromotionError("REPOSITORY_PATH_BUDGET_EXCEEDED")
        destination = repository.root / candidate
        try:
            metadata = destination.lstat()
        except OSError as exc:
            raise PromotionError("CANDIDATE_FILE_UNREADABLE") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise PromotionError("SYMLINK_NOT_PROMOTABLE")
        if not stat.S_ISREG(metadata.st_mode):
            raise PromotionError("SPECIAL_FILE_NOT_PROMOTABLE")
        if metadata.st_size > policy.max_file_bytes:
            raise PromotionError("TREE_FILE_SIZE_BUDGET_EXCEEDED")
        total_bytes += metadata.st_size
        if total_bytes > policy.max_tree_bytes:
            raise PromotionError("TREE_BYTE_BUDGET_EXCEEDED")

        file_hash = hashlib.sha256()
        bytes_read = 0
        try:
            with destination.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1_048_576), b""):
                    file_hash.update(chunk)
                    bytes_read += len(chunk)
            final_metadata = destination.lstat()
        except OSError as exc:
            raise PromotionError("CANDIDATE_FILE_UNREADABLE") from exc
        if (
            bytes_read != metadata.st_size
            or final_metadata.st_size != metadata.st_size
            or final_metadata.st_mtime_ns != metadata.st_mtime_ns
            or final_metadata.st_ino != metadata.st_ino
        ):
            raise PromotionError("CANDIDATE_CHANGED_DURING_SNAPSHOT")
        mode = "100755" if metadata.st_mode & stat.S_IXUSR else "100644"
        record = json.dumps(
            [relative, mode, metadata.st_size, file_hash.hexdigest()],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        tree_hash.update(len(record).to_bytes(8, "big"))
        tree_hash.update(record)
    return TreeSnapshot(tree_hash.hexdigest(), len(paths), total_bytes)


def _read_patch(path: str | Path, policy: PromotionPolicy) -> bytes:
    try:
        patch = Path(path).read_bytes()
    except OSError as exc:
        raise PromotionError("PATCH_UNREADABLE") from exc
    if not patch:
        raise PromotionError("EMPTY_PATCH")
    if len(patch) > policy.max_patch_bytes:
        raise PromotionError("PATCH_BYTE_BUDGET_EXCEEDED")
    return patch


def _verification_evidence(
    report: VerificationReport, policy: PromotionPolicy
) -> dict[str, object]:
    if not report.passed or report.stopped_early:
        raise PromotionError("VERIFICATION_FAILED")
    if len(report.steps) != len(policy.required_commands):
        raise PromotionError("VERIFICATION_GATE_SET_MISMATCH")
    expected = policy.normalized_commands()
    evidence_steps: list[dict[str, object]] = []
    for index, (step, expected_command) in enumerate(zip(report.steps, expected, strict=True), 1):
        command_raw = step.get("command")
        if not isinstance(command_raw, (tuple, list)) or not all(
            isinstance(item, str) for item in command_raw
        ):
            raise PromotionError("MALFORMED_VERIFICATION_EVIDENCE")
        command = _normalize_command(tuple(command_raw))
        if command != expected_command:
            raise PromotionError("VERIFICATION_COMMAND_MISMATCH")
        if (
            step.get("passed") is not True
            or step.get("returncode") != 0
            or step.get("timed_out") is not False
        ):
            raise PromotionError("VERIFICATION_FAILED")
        if policy.reject_truncated_output and step.get("output_truncated") is not False:
            raise PromotionError("VERIFICATION_OUTPUT_TRUNCATED")
        stdout = step.get("stdout")
        stderr = step.get("stderr")
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            raise PromotionError("MALFORMED_VERIFICATION_EVIDENCE")
        evidence_steps.append(
            {
                "name": f"verification-{index:02d}",
                "command_sha256": _digest(list(command)),
                "stdout_sha256": _sha256_bytes(stdout.encode("utf-8")),
                "stderr_sha256": _sha256_bytes(stderr.encode("utf-8")),
            }
        )
    return {"passed": True, "steps": evidence_steps}


def _project_patch(
    base_repository: GitRepository,
    patch_path: str | Path,
    policy: PromotionPolicy,
) -> TreeSnapshot:
    try:
        base_repository.apply_patch(patch_path, check_only=True)
    except subprocess.CalledProcessError as exc:
        raise PromotionError("PATCH_DOES_NOT_APPLY_TO_BASE") from exc
    with base_repository.isolated_worktree() as worktree:
        projected = GitRepository(worktree)
        try:
            projected.apply_patch(patch_path)
        except subprocess.CalledProcessError as exc:
            raise PromotionError("PATCH_PROJECTION_FAILED") from exc
        return snapshot_tree(projected, policy)


def issue_promotion_receipt(
    *,
    base_repository: GitRepository,
    candidate_repository: GitRepository,
    patch_path: str | Path,
    policy: PromotionPolicy,
) -> PromotionReceipt:
    """Run required gates and issue a receipt for the exact immutable candidate."""

    base_repository.ensure_clean()
    base_sha = base_repository.head_sha()
    _validate_git_sha(base_sha)
    if candidate_repository.head_sha() != base_sha:
        raise PromotionError("CANDIDATE_BASE_MISMATCH")
    patch = _read_patch(patch_path, policy)
    before = snapshot_tree(candidate_repository, policy)
    suite = RepositoryVerificationSuite(
        list(policy.required_commands),
        timeout_seconds=policy.timeout_seconds,
        max_output_bytes=policy.max_output_bytes,
    )
    report = suite.run(candidate_repository.root)
    verification = _verification_evidence(report, policy)
    after = snapshot_tree(candidate_repository, policy)
    if before != after:
        raise PromotionError("CANDIDATE_CHANGED_DURING_VERIFICATION")
    projected = _project_patch(base_repository, patch_path, policy)
    if projected != after:
        raise PromotionError("PATCH_CANDIDATE_BINDING_MISMATCH")

    receipt = PromotionReceipt(
        schema_version=1,
        base_commit_sha=base_sha,
        candidate_tree_sha256=after.digest_sha256,
        candidate_file_count=after.file_count,
        candidate_total_bytes=after.total_bytes,
        patch_sha256=_sha256_bytes(patch),
        patch_bytes=len(patch),
        verification_sha256=_digest(verification),
        verification_gate_count=len(verification["steps"]),
        policy_sha256=policy.digest,
        evidence_sha256="",
    )
    return receipt.with_evidence_digest()


def verify_promotion_receipt(
    *,
    receipt: PromotionReceipt,
    base_repository: GitRepository,
    patch_path: str | Path,
    target_base_sha: str,
    policy: PromotionPolicy,
) -> PromotionDecision:
    """Revalidate policy, target base, patch and projected candidate before promotion."""

    reasons: set[str] = set()
    candidate_digest: str | None = None
    patch_digest: str | None = None
    if receipt.schema_version != 1:
        reasons.add("UNSUPPORTED_RECEIPT_SCHEMA")
    if receipt.evidence_sha256 != _digest(receipt.body()):
        reasons.add("RECEIPT_DIGEST_MISMATCH")
    try:
        _validate_git_sha(target_base_sha)
    except PromotionError as exc:
        reasons.add(exc.code)
    if receipt.policy_sha256 != policy.digest:
        reasons.add("PROMOTION_POLICY_MISMATCH")
    if receipt.base_commit_sha != target_base_sha:
        reasons.add("TARGET_BASE_ADVANCED")
    try:
        base_repository.ensure_clean()
    except RuntimeError:
        reasons.add("BASE_REPOSITORY_DIRTY")
    if base_repository.head_sha() != target_base_sha:
        reasons.add("TARGET_BASE_ADVANCED")
    try:
        patch = _read_patch(patch_path, policy)
        patch_digest = _sha256_bytes(patch)
        if patch_digest != receipt.patch_sha256 or len(patch) != receipt.patch_bytes:
            reasons.add("PATCH_DIGEST_MISMATCH")
    except PromotionError as exc:
        reasons.add(exc.code)
    if not reasons:
        try:
            projected = _project_patch(base_repository, patch_path, policy)
            candidate_digest = projected.digest_sha256
            if (
                projected.digest_sha256 != receipt.candidate_tree_sha256
                or projected.file_count != receipt.candidate_file_count
                or projected.total_bytes != receipt.candidate_total_bytes
            ):
                reasons.add("CANDIDATE_TREE_MISMATCH")
        except PromotionError as exc:
            reasons.add(exc.code)

    body: dict[str, Any] = {
        "accepted": not reasons,
        "reason_codes": sorted(reasons),
        "receipt_sha256": receipt.evidence_sha256,
        "candidate_tree_sha256": candidate_digest,
        "patch_sha256": patch_digest,
    }
    decision = PromotionDecision(
        accepted=not reasons,
        reason_codes=tuple(sorted(reasons)),
        receipt_sha256=receipt.evidence_sha256,
        candidate_tree_sha256=candidate_digest,
        patch_sha256=patch_digest,
        evidence_sha256=_digest(body),
    )
    return decision
