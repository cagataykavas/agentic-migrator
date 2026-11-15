from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from migrator.context_policy import RepairContextPolicy, evaluate_repair_context
from migrator.engine import MigrationEngine
from migrator.llm import RepairContext
from migrator.models import FailureKind, Rule, TestResult
from migrator.rules import RuleStore


def _decision(**overrides):
    values = {
        "source": "client.request()",
        "candidate": "client.request()",
        "stdout": "",
        "stderr": "TypeError: unsupported call",
        "active_rule_ids": ["builtin.rename.v1"],
    }
    values.update(overrides)
    return evaluate_repair_context(**values)


def test_safe_context_is_accepted_with_content_bound_evidence():
    first = _decision()
    second = _decision(candidate="client.request(timeout=5)")

    assert first.accepted
    assert first.reasons == ()
    assert len(first.evidence_sha256) == 64
    assert first.evidence_sha256 != second.evidence_sha256
    assert "client.request" not in str(first.to_dict())


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("source", 'password = "not-a-real-credential"', "sensitive_material:source"),
        ("candidate", "Authorization: Bearer synthetic-value", "sensitive_material:candidate"),
        (
            "stderr",
            "connection failed for service://demo:not-a-real-credential@example.invalid/db",
            "sensitive_material:diagnostics",
        ),
    ],
)
def test_high_confidence_sensitive_markers_fail_closed(field, value, reason):
    decision = _decision(**{field: value})

    assert not decision.accepted
    assert reason in decision.reasons
    assert value not in str(decision.to_dict())


def test_all_outbound_sections_have_independent_budgets():
    policy = RepairContextPolicy(
        max_source_bytes=3,
        max_candidate_bytes=3,
        max_diagnostics_bytes=3,
        max_rule_ids=1,
        max_rule_id_bytes=3,
    )
    decision = evaluate_repair_context(
        source="source",
        candidate="candidate",
        stdout="output",
        stderr="error",
        active_rule_ids=["one", "two"],
        policy=policy,
    )

    assert decision.reasons == (
        "source_budget_exceeded",
        "candidate_budget_exceeded",
        "diagnostics_budget_exceeded",
        "rule_count_budget_exceeded",
        "rule_id_budget_exceeded",
    )


def test_rule_identifiers_are_bounded_and_structured():
    decision = _decision(active_rule_ids=["valid.rule", "contains whitespace"])

    assert not decision.accepted
    assert decision.reasons == ("invalid_rule_id",)


def test_policy_rejects_non_positive_limits():
    with pytest.raises(ValueError, match="positive"):
        replace(RepairContextPolicy(), max_source_bytes=0)


class RecordingSynthesizer:
    def __init__(self) -> None:
        self.rule_calls = 0
        self.test_calls = 0

    def propose_rule(self, context: RepairContext) -> Rule | None:
        self.rule_calls += 1
        return None

    def propose_test_repair(self, context: RepairContext, test_source: str) -> str | None:
        self.test_calls += 1
        return None


def test_engine_never_calls_synthesizer_when_context_is_rejected(tmp_path: Path):
    synthesizer = RecordingSynthesizer()

    def failing_runner(candidate: str, test_source: str) -> TestResult:
        return TestResult(
            False,
            "migration",
            stderr="failure",
            kind=FailureKind.RUNTIME,
        )

    engine = MigrationEngine(
        RuleStore(tmp_path / "rules.json"),
        synthesizer,
        failing_runner,
        repair_context_policy=RepairContextPolicy(max_source_bytes=4),
    )

    with pytest.raises(RuntimeError, match=r"repair context rejected: source_budget_exceeded"):
        engine.migrate("source-too-large", "assert True")

    assert synthesizer.rule_calls == 0
    assert synthesizer.test_calls == 0
