from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

from .gitops import ProposedChange


@dataclass(frozen=True)
class ProposalPolicy:
    max_changed_files: int = 8
    max_changed_lines: int = 300
    allow_deletes: bool = False
    allow_test_changes: bool = False
    allowed_roots: tuple[str, ...] = ()
    protected_prefixes: tuple[str, ...] = (
        ".github/workflows/",
        ".git/",
        ".env",
        "secrets/",
    )

    def __post_init__(self) -> None:
        if self.max_changed_files < 1 or self.max_changed_lines < 1:
            raise ValueError("change budgets must be positive")


@dataclass(frozen=True)
class ChangeEvidence:
    path: str
    operation: str
    added_lines: int
    removed_lines: int


@dataclass(frozen=True)
class ProposalDecision:
    accepted: bool
    reasons: tuple[str, ...]
    changed_files: int
    changed_lines: int
    evidence: tuple[ChangeEvidence, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "reasons": list(self.reasons),
            "changed_files": self.changed_files,
            "changed_lines": self.changed_lines,
            "evidence": [asdict(item) for item in self.evidence],
        }


def _normalize_path(raw: str) -> str:
    path = PurePosixPath(raw.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"unsafe proposal path: {raw!r}")
    normalized = path.as_posix()
    if normalized in {"", "."}:
        raise ValueError(f"unsafe proposal path: {raw!r}")
    return normalized


def _line_counts(diff: str) -> tuple[int, int]:
    added = sum(line.startswith("+") and not line.startswith("+++") for line in diff.splitlines())
    removed = sum(line.startswith("-") and not line.startswith("---") for line in diff.splitlines())
    return added, removed


def evaluate_proposal(
    changes: Sequence[ProposedChange],
    *,
    policy: ProposalPolicy | None = None,
) -> ProposalDecision:
    """Apply server-owned scope and diff budgets to an agent repair proposal."""
    active_policy = policy or ProposalPolicy()
    if not changes:
        return ProposalDecision(False, ("empty_proposal",), 0, 0, ())

    normalized = [_normalize_path(change.path) for change in changes]
    if len(normalized) != len(set(normalized)):
        raise ValueError("proposal paths must be unique")

    reasons: list[str] = []
    evidence: list[ChangeEvidence] = []
    for path, change in sorted(zip(normalized, changes, strict=True), key=lambda item: item[0]):
        if change.operation not in {"create", "update", "delete"}:
            reasons.append(f"unsupported_operation:{path}")
        if change.operation == "delete" and not active_policy.allow_deletes:
            reasons.append(f"delete_not_allowed:{path}")
        if any(
            path == prefix.rstrip("/") or path.startswith(prefix)
            for prefix in active_policy.protected_prefixes
        ):
            reasons.append(f"protected_path:{path}")
        if not active_policy.allow_test_changes and (
            path.startswith(("tests/", "test/")) or "/tests/" in path
        ):
            reasons.append(f"test_change_not_allowed:{path}")
        if active_policy.allowed_roots and not any(
            path == root.rstrip("/") or path.startswith(root.rstrip("/") + "/")
            for root in active_policy.allowed_roots
        ):
            reasons.append(f"outside_allowed_roots:{path}")

        added, removed = _line_counts(change.diff)
        evidence.append(ChangeEvidence(path, change.operation, added, removed))

    changed_lines = sum(item.added_lines + item.removed_lines for item in evidence)
    if len(changes) > active_policy.max_changed_files:
        reasons.append("changed_file_budget_exceeded")
    if changed_lines > active_policy.max_changed_lines:
        reasons.append("changed_line_budget_exceeded")

    return ProposalDecision(
        accepted=not reasons,
        reasons=tuple(reasons),
        changed_files=len(changes),
        changed_lines=changed_lines,
        evidence=tuple(evidence),
    )
