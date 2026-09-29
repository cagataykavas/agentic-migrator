import json
import subprocess
from pathlib import Path

import pytest

from migrator.repository import GitRepository
from migrator.verification_stability import (
    StabilityAuditError,
    StabilityPolicy,
    audit_patch_stability,
    main,
)


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def repository_with_patch(tmp_path: Path, *, replacement: str = "VALUE = 2\n") -> tuple[Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init")
    (root / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(root, "add", "candidate.py")
    git(
        root,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=Test",
        "commit",
        "-m",
        "initial",
    )
    (root / "candidate.py").write_text(replacement, encoding="utf-8")
    patch = tmp_path / "candidate.patch"
    patch.write_text(git(root, "diff", "--binary", "HEAD").stdout, encoding="utf-8")
    git(root, "restore", "candidate.py")
    return root, patch


def policy(*commands: str, **overrides: object) -> StabilityPolicy:
    values = {"commands": commands, "trials": 2, "timeout_seconds": 5.0, **overrides}
    return StabilityPolicy(**values)


def test_stable_patch_is_accepted_in_fresh_worktrees(tmp_path: Path) -> None:
    root, patch = repository_with_patch(tmp_path)
    report = audit_patch_stability(
        base_repository=GitRepository(root),
        patch_path=patch,
        policy=policy('python -c "from candidate import VALUE; assert VALUE == 2"'),
    )

    assert report.accepted
    assert report.pass_count == 2
    assert report.outcome_variant_count == 1
    assert report.required_passes == 2
    assert len({trial.candidate_before_sha256 for trial in report.trials}) == 1
    assert all(not trial.mutated_candidate for trial in report.trials)


def test_failed_command_rejects_pass_quorum(tmp_path: Path) -> None:
    root, patch = repository_with_patch(tmp_path)
    report = audit_patch_stability(
        base_repository=GitRepository(root),
        patch_path=patch,
        policy=policy('python -c "raise SystemExit(7)"'),
    )

    assert not report.accepted
    assert report.pass_count == 0
    assert report.reason_codes == ("PASS_QUORUM_NOT_MET",)


def test_verification_mutation_is_rejected_even_when_command_exits_zero(tmp_path: Path) -> None:
    root, patch = repository_with_patch(tmp_path)
    report = audit_patch_stability(
        base_repository=GitRepository(root),
        patch_path=patch,
        policy=policy("python -c \"open('candidate.py', 'a').write('MUTATED = 1\\n')\""),
    )

    assert not report.accepted
    assert "VERIFICATION_MUTATED_CANDIDATE" in report.reason_codes
    assert "PASS_QUORUM_NOT_MET" in report.reason_codes
    assert all(trial.mutated_candidate for trial in report.trials)


def test_stopped_sequence_is_reported_as_incomplete(tmp_path: Path) -> None:
    root, patch = repository_with_patch(tmp_path)
    report = audit_patch_stability(
        base_repository=GitRepository(root),
        patch_path=patch,
        policy=policy('python -c "raise SystemExit(1)"', "python -c \"print('never')\""),
    )

    assert report.reason_codes == (
        "INCOMPLETE_VERIFICATION_SEQUENCE",
        "PASS_QUORUM_NOT_MET",
    )
    assert all(trial.completed_steps == 1 for trial in report.trials)


def test_raw_output_is_hashed_not_exposed(tmp_path: Path) -> None:
    root, patch = repository_with_patch(tmp_path)
    secret = "sensitive-tool-output"
    report = audit_patch_stability(
        base_repository=GitRepository(root),
        patch_path=patch,
        policy=policy(f"python -c \"print('{secret}')\""),
    )

    serialized = json.dumps(report.as_dict(), sort_keys=True)
    assert report.accepted
    assert secret not in serialized
    assert "stdout" not in serialized


def test_truncated_output_fails_closed_with_explicit_reason(tmp_path: Path) -> None:
    root, patch = repository_with_patch(tmp_path)
    report = audit_patch_stability(
        base_repository=GitRepository(root),
        patch_path=patch,
        policy=policy("python -c \"print('x' * 100)\"", max_output_bytes=10),
    )

    assert not report.accepted
    assert report.reason_codes == (
        "OUTPUT_TRUNCATION_DETECTED",
        "PASS_QUORUM_NOT_MET",
    )
    assert all(trial.output_truncated for trial in report.trials)


def test_strict_output_policy_detects_nondeterministic_output(tmp_path: Path) -> None:
    root, patch = repository_with_patch(tmp_path)
    report = audit_patch_stability(
        base_repository=GitRepository(root),
        patch_path=patch,
        policy=policy(
            'python -c "import secrets; print(secrets.token_hex())"',
            max_output_variants=1,
        ),
    )

    assert not report.accepted
    assert report.pass_count == 2
    assert report.output_variant_count == 2
    assert report.reason_codes == ("OUTPUT_VARIANT_BUDGET_EXCEEDED",)


def test_output_variation_is_observable_but_optional_by_default(tmp_path: Path) -> None:
    root, patch = repository_with_patch(tmp_path)
    report = audit_patch_stability(
        base_repository=GitRepository(root),
        patch_path=patch,
        policy=policy('python -c "import secrets; print(secrets.token_hex())"'),
    )

    assert report.accepted
    assert report.output_variant_count == 2


def test_evidence_identity_is_deterministic_for_stable_run(tmp_path: Path) -> None:
    root, patch = repository_with_patch(tmp_path)
    active_policy = policy("python -c \"print('stable')\"")
    first = audit_patch_stability(
        base_repository=GitRepository(root), patch_path=patch, policy=active_policy
    )
    second = audit_patch_stability(
        base_repository=GitRepository(root), patch_path=patch, policy=active_policy
    )

    assert first.as_dict() == second.as_dict()
    assert len(first.evidence_sha256) == 64


def test_dirty_source_checkout_fails_closed(tmp_path: Path) -> None:
    root, patch = repository_with_patch(tmp_path)
    (root / "uncommitted.txt").write_text("dirty", encoding="utf-8")

    with pytest.raises(RuntimeError, match="uncommitted changes"):
        audit_patch_stability(
            base_repository=GitRepository(root),
            patch_path=patch,
            policy=policy('python -c "print(1)"'),
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"commands": ()},
        {"commands": ("python -V",), "trials": 1},
        {"commands": ("python -V",), "trials": 2, "min_passes": 3},
        {"commands": ("python -V",), "trials": 2, "max_outcome_variants": 3},
        {"commands": ("python -V",), "trials": 2, "timeout_seconds": float("nan")},
    ],
)
def test_invalid_policy_is_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises((ValueError, TypeError)):
        StabilityPolicy(**kwargs)


def test_empty_and_oversized_patch_fail_closed(tmp_path: Path) -> None:
    root, patch = repository_with_patch(tmp_path)
    patch.write_bytes(b"")
    with pytest.raises(StabilityAuditError, match="EMPTY_PATCH"):
        audit_patch_stability(
            base_repository=GitRepository(root),
            patch_path=patch,
            policy=policy('python -c "print(1)"'),
        )

    patch.write_bytes(b"x" * 11)
    with pytest.raises(StabilityAuditError, match="PATCH_BUDGET_EXCEEDED"):
        audit_patch_stability(
            base_repository=GitRepository(root),
            patch_path=patch,
            policy=policy('python -c "print(1)"', max_patch_bytes=10),
        )


def test_cli_has_distinct_success_policy_and_malformed_exit_codes(tmp_path: Path) -> None:
    root, patch = repository_with_patch(tmp_path)
    accepted = tmp_path / "accepted.json"
    rejected = tmp_path / "rejected.json"
    malformed = tmp_path / "malformed.json"

    assert (
        main(
            [
                "--repository",
                str(root),
                "--patch",
                str(patch),
                "--trials",
                "2",
                "--command",
                'python -c "print(1)"',
                "--output",
                str(accepted),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--repository",
                str(root),
                "--patch",
                str(patch),
                "--trials",
                "2",
                "--command",
                'python -c "raise SystemExit(1)"',
                "--output",
                str(rejected),
            ]
        )
        == 2
    )
    assert (
        main(
            [
                "--repository",
                str(root),
                "--patch",
                str(tmp_path / "missing.patch"),
                "--trials",
                "2",
                "--command",
                'python -c "print(1)"',
                "--output",
                str(malformed),
            ]
        )
        == 3
    )

    assert json.loads(accepted.read_text())["accepted"] is True
    assert json.loads(rejected.read_text())["accepted"] is False
    assert json.loads(malformed.read_text())["error_code"] == "PATCH_READ_FAILED"
