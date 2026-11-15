from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Final

_SENSITIVE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+[^\s]+"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|passwd)\b\s*[:=]\s*(['\"])[^'\"\r\n]+\1"
    ),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:]+:[^\s/@]+@[^\s]+"),
)
_RULE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")


@dataclass(frozen=True)
class RepairContextPolicy:
    """Server-owned limits for data leaving the deterministic migration boundary."""

    max_source_bytes: int = 64 * 1024
    max_candidate_bytes: int = 64 * 1024
    max_diagnostics_bytes: int = 16 * 1024
    max_rule_ids: int = 256
    max_rule_id_bytes: int = 16 * 1024

    def __post_init__(self) -> None:
        limits = asdict(self)
        if any(value < 1 for value in limits.values()):
            raise ValueError("repair context limits must be positive")


@dataclass(frozen=True)
class RepairContextDecision:
    accepted: bool
    reasons: tuple[str, ...]
    source_bytes: int
    candidate_bytes: int
    diagnostics_bytes: int
    rule_ids: int
    rule_id_bytes: int
    evidence_sha256: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _utf8(value: str) -> bytes:
    return value.encode("utf-8")


def _contains_sensitive_material(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SENSITIVE_PATTERNS)


def evaluate_repair_context(
    *,
    source: str,
    candidate: str,
    stdout: str,
    stderr: str,
    active_rule_ids: list[str],
    policy: RepairContextPolicy | None = None,
) -> RepairContextDecision:
    """Return bounded, content-addressed evidence without echoing outbound content."""

    active_policy = policy or RepairContextPolicy()
    diagnostics = f"{stderr}\n{stdout}"
    source_bytes = len(_utf8(source))
    candidate_bytes = len(_utf8(candidate))
    diagnostics_bytes = len(_utf8(diagnostics))
    rule_id_bytes = sum(len(_utf8(rule_id)) for rule_id in active_rule_ids)

    reasons: list[str] = []
    if source_bytes > active_policy.max_source_bytes:
        reasons.append("source_budget_exceeded")
    if candidate_bytes > active_policy.max_candidate_bytes:
        reasons.append("candidate_budget_exceeded")
    if diagnostics_bytes > active_policy.max_diagnostics_bytes:
        reasons.append("diagnostics_budget_exceeded")
    if len(active_rule_ids) > active_policy.max_rule_ids:
        reasons.append("rule_count_budget_exceeded")
    if rule_id_bytes > active_policy.max_rule_id_bytes:
        reasons.append("rule_id_budget_exceeded")
    if any(not _RULE_ID.fullmatch(rule_id) for rule_id in active_rule_ids):
        reasons.append("invalid_rule_id")

    for field_name, value in (
        ("source", source),
        ("candidate", candidate),
        ("diagnostics", diagnostics),
    ):
        if _contains_sensitive_material(value):
            reasons.append(f"sensitive_material:{field_name}")

    evidence = {
        "policy": asdict(active_policy),
        "source_sha256": hashlib.sha256(_utf8(source)).hexdigest(),
        "candidate_sha256": hashlib.sha256(_utf8(candidate)).hexdigest(),
        "diagnostics_sha256": hashlib.sha256(_utf8(diagnostics)).hexdigest(),
        "rule_ids_sha256": hashlib.sha256(
            json.dumps(active_rule_ids, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "reasons": reasons,
    }
    evidence_sha256 = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return RepairContextDecision(
        accepted=not reasons,
        reasons=tuple(reasons),
        source_bytes=source_bytes,
        candidate_bytes=candidate_bytes,
        diagnostics_bytes=diagnostics_bytes,
        rule_ids=len(active_rule_ids),
        rule_id_bytes=rule_id_bytes,
        evidence_sha256=evidence_sha256,
    )
