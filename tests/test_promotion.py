from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from migrator.promotion import (
    PromotionError,
    PromotionPolicy,
    issue_promotion_receipt,
    snapshot_tree,
    verify_promotion_receipt,
)
from migrator.repository import GitRepository


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repositories(tmp_path: Path) -> tuple[GitRepository, GitRepository, Path]:
    base = tmp_path / "base"
    base.mkdir()
    git(base, "init", "-b", "main")
    git(base, "config", "user.email", "test@example.invalid")
    git(base, "config", "user.name", "Promotion Test")
    (base / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    (base / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (base / "test_module.py").write_text(
        "from module import VALUE\n\ndef test_value():\n    assert VALUE in {1, 2}\n",
        encoding="utf-8",
    )
    git(base, "add", ".")
    git(base, "commit", "-m", "base")

    candidate = tmp_path / "candidate"
    git(tmp_path, "clone", str(base), str(candidate))
    (candidate / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    patch = tmp_path / "candidate.patch"
    patch.write_bytes(
        subprocess.run(
            ["git", "diff", "--binary", "HEAD"],
            cwd=candidate,
            check=True,
            capture_output=True,
        ).stdout
    )
    return GitRepository(base), GitRepository(candidate), patch


def policy(**overrides: object) -> PromotionPolicy:
    values: dict[str, object] = {
        "required_commands": (
            'python -c "from module import VALUE; assert VALUE == 2"',
            "python -m compileall -q module.py",
        ),
        "timeout_seconds": 5.0,
        "max_output_bytes": 10_000,
    }
    values.update(overrides)
    return PromotionPolicy(**values)  # type: ignore[arg-type]


def issue(
    repositories: tuple[GitRepository, GitRepository, Path],
    selected_policy: PromotionPolicy | None = None,
):
    base, candidate, patch = repositories
    return issue_promotion_receipt(
        base_repository=base,
        candidate_repository=candidate,
        patch_path=patch,
        policy=selected_policy or policy(),
    )


def test_issues_and_verifies_content_addressed_receipt(repositories) -> None:
    base, _, patch = repositories
    receipt = issue(repositories)
    decision = verify_promotion_receipt(
        receipt=receipt,
        base_repository=base,
        patch_path=patch,
        target_base_sha=base.head_sha(),
        policy=policy(),
    )
    assert decision.accepted is True
    assert decision.reason_codes == ()
    assert decision.candidate_tree_sha256 == receipt.candidate_tree_sha256
    assert len(receipt.evidence_sha256) == 64
    json.dumps({"receipt": receipt.as_dict(), "decision": decision.as_dict()})


def test_receipt_is_deterministic_for_same_candidate(repositories) -> None:
    assert issue(repositories) == issue(repositories)


def test_source_repository_is_never_mutated(repositories) -> None:
    base, _, _ = repositories
    before = snapshot_tree(base, policy())
    issue(repositories)
    assert snapshot_tree(base, policy()) == before
    assert (base.root / "module.py").read_text() == "VALUE = 1\n"


def test_rejects_candidate_drift_after_receipt(repositories) -> None:
    base, candidate, patch = repositories
    receipt = issue(repositories)
    (candidate.root / "module.py").write_text("VALUE = 3\n")
    drift_patch = patch.parent / "drift.patch"
    drift_patch.write_bytes(
        subprocess.run(
            ["git", "diff", "--binary", "HEAD"],
            cwd=candidate.root,
            check=True,
            capture_output=True,
        ).stdout
    )
    decision = verify_promotion_receipt(
        receipt=receipt,
        base_repository=base,
        patch_path=drift_patch,
        target_base_sha=base.head_sha(),
        policy=policy(),
    )
    assert decision.accepted is False
    assert "PATCH_DIGEST_MISMATCH" in decision.reason_codes


def test_rejects_advanced_target_base(repositories) -> None:
    base, _, patch = repositories
    receipt = issue(repositories)
    (base.root / "other.py").write_text("OTHER = 1\n")
    git(base.root, "add", "other.py")
    git(base.root, "commit", "-m", "advance base")
    decision = verify_promotion_receipt(
        receipt=receipt,
        base_repository=base,
        patch_path=patch,
        target_base_sha=base.head_sha(),
        policy=policy(),
    )
    assert decision.accepted is False
    assert "TARGET_BASE_ADVANCED" in decision.reason_codes


def test_rejects_tampered_patch(repositories) -> None:
    base, _, patch = repositories
    receipt = issue(repositories)
    patch.write_bytes(patch.read_bytes() + b"\n# tampered\n")
    decision = verify_promotion_receipt(
        receipt=receipt,
        base_repository=base,
        patch_path=patch,
        target_base_sha=base.head_sha(),
        policy=policy(),
    )
    assert decision.accepted is False
    assert "PATCH_DIGEST_MISMATCH" in decision.reason_codes


def test_rejects_tampered_receipt(repositories) -> None:
    base, _, patch = repositories
    receipt = replace(issue(repositories), candidate_total_bytes=1)
    decision = verify_promotion_receipt(
        receipt=receipt,
        base_repository=base,
        patch_path=patch,
        target_base_sha=base.head_sha(),
        policy=policy(),
    )
    assert decision.accepted is False
    assert "RECEIPT_DIGEST_MISMATCH" in decision.reason_codes


def test_rejects_policy_drift(repositories) -> None:
    base, _, patch = repositories
    receipt = issue(repositories)
    changed = policy(max_patch_bytes=3_000_000)
    decision = verify_promotion_receipt(
        receipt=receipt,
        base_repository=base,
        patch_path=patch,
        target_base_sha=base.head_sha(),
        policy=changed,
    )
    assert decision.accepted is False
    assert "PROMOTION_POLICY_MISMATCH" in decision.reason_codes


def test_rejects_failed_verification(repositories) -> None:
    with pytest.raises(PromotionError, match="VERIFICATION_FAILED"):
        issue(repositories, policy(required_commands=('python -c "raise SystemExit(4)"',)))


def test_rejects_truncated_verification_output(repositories) -> None:
    noisy = "python -c \"print('x' * 1000)\""
    with pytest.raises(PromotionError, match="VERIFICATION_OUTPUT_TRUNCATED"):
        issue(repositories, policy(required_commands=(noisy,), max_output_bytes=50))


def test_rejects_tracked_mutation_during_verification(repositories) -> None:
    command = "python -c \"open('module.py', 'w').write('VALUE = 9\\n')\""
    with pytest.raises(PromotionError, match="CANDIDATE_CHANGED_DURING_VERIFICATION"):
        issue(repositories, policy(required_commands=(command,)))


def test_rejects_untracked_mutation_during_verification(repositories) -> None:
    command = "python -c \"open('new.py', 'w').write('NEW = 1\\n')\""
    with pytest.raises(PromotionError, match="CANDIDATE_CHANGED_DURING_VERIFICATION"):
        issue(repositories, policy(required_commands=(command,)))


def test_ignores_gitignored_verification_cache(repositories) -> None:
    command = (
        "python -c \"import os; os.makedirs('__pycache__'); "
        "open('__pycache__/cache.pyc', 'wb').write(b'cache')\""
    )
    receipt = issue(repositories, policy(required_commands=(command,)))
    assert receipt.verification_gate_count == 1


def test_rejects_candidate_not_reproducible_from_patch(repositories) -> None:
    _, candidate, _ = repositories
    (candidate.root / "untracked.py").write_text("SURPRISE = True\n")
    with pytest.raises(PromotionError, match="PATCH_CANDIDATE_BINDING_MISMATCH"):
        issue(repositories)


def test_rejects_symlink_in_candidate(repositories) -> None:
    _, candidate, _ = repositories
    os.symlink("module.py", candidate.root / "alias.py")
    with pytest.raises(PromotionError, match="SYMLINK_NOT_PROMOTABLE"):
        issue(repositories)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"max_files": 1}, "TREE_FILE_BUDGET_EXCEEDED"),
        ({"max_file_bytes": 4}, "TREE_FILE_SIZE_BUDGET_EXCEEDED"),
        ({"max_tree_bytes": 10}, "TREE_BYTE_BUDGET_EXCEEDED"),
        ({"max_patch_bytes": 10}, "PATCH_BYTE_BUDGET_EXCEEDED"),
    ],
)
def test_enforces_resource_budgets(repositories, overrides, expected: str) -> None:
    with pytest.raises(PromotionError, match=expected):
        issue(repositories, policy(**overrides))


def test_rejects_dirty_base_repository(repositories) -> None:
    base, _, _ = repositories
    (base.root / "module.py").write_text("DIRTY = True\n")
    with pytest.raises(RuntimeError, match="uncommitted changes"):
        issue(repositories)


def test_rejects_candidate_based_on_different_commit(repositories) -> None:
    base, candidate, _ = repositories
    git(candidate.root, "config", "user.email", "test@example.invalid")
    git(candidate.root, "config", "user.name", "Promotion Test")
    git(candidate.root, "add", "module.py")
    git(candidate.root, "commit", "-m", "different base")
    patch = candidate.root.parent / "empty.patch"
    patch.write_text("not a patch")
    with pytest.raises(PromotionError, match="CANDIDATE_BASE_MISMATCH"):
        issue_promotion_receipt(
            base_repository=base,
            candidate_repository=candidate,
            patch_path=patch,
            policy=policy(),
        )


@pytest.mark.parametrize(
    "commands",
    [(), ("",), ("python -V", "python -V"), ('python -c "',)],
)
def test_rejects_invalid_policy_commands(commands: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        PromotionPolicy(required_commands=commands)


@pytest.mark.parametrize("timeout", [True, float("nan"), float("inf"), 0.0, 3_601])
def test_rejects_invalid_timeout_policy(timeout: object) -> None:
    with pytest.raises(ValueError):
        PromotionPolicy(required_commands=("python -V",), timeout_seconds=timeout)  # type: ignore[arg-type]
