from pathlib import Path

import pytest

from migrator.convergence import (
    ConvergenceInputError,
    ConvergencePolicy,
    RepairConvergenceGuard,
)
from migrator.engine import MigrationConvergenceError, MigrationEngine
from migrator.llm import RepairContext
from migrator.models import FailureKind, Rule, TestResult
from migrator.rules import RuleStore


def _failure(message: str = "still broken") -> TestResult:
    return TestResult(
        False,
        "migration_test",
        stderr=message,
        kind=FailureKind.ASSERTION,
    )


def test_repeated_state_is_stopped_with_content_addressed_evidence():
    guard = RepairConvergenceGuard(ConvergencePolicy(max_state_occurrences=3))

    observations = [
        guard.observe(
            attempt=attempt,
            candidate="VALUE = 1\n",
            test_source="assert VALUE == 2\n",
            test_result=_failure(),
            active_rule_ids=("rule-b", "rule-a"),
        )
        for attempt in range(1, 4)
    ]

    assert [item.stalled for item in observations] == [False, False, True]
    assert observations[-1].reason == "repeated_repair_state"
    assert observations[-1].state_occurrences == 3
    assert observations[-1].active_rule_ids == ("rule-a", "rule-b")
    assert len(observations[-1].state_sha256) == 64


def test_two_state_oscillation_is_detected_before_occurrence_limit():
    guard = RepairConvergenceGuard(ConvergencePolicy(max_state_occurrences=5))
    observations = []
    for attempt, candidate in enumerate(("A", "B", "A", "B"), start=1):
        observations.append(
            guard.observe(
                attempt=attempt,
                candidate=candidate,
                test_source="tests",
                test_result=_failure(),
            )
        )

    assert observations[-1].stalled is True
    assert observations[-1].reason == "two_state_oscillation"
    assert observations[-1].unique_state_count == 2


def test_unique_state_budget_is_fail_closed():
    guard = RepairConvergenceGuard(ConvergencePolicy(max_state_occurrences=5, max_unique_states=2))

    first = guard.observe(
        attempt=1, candidate="A", test_source="tests", test_result=_failure("one")
    )
    second = guard.observe(
        attempt=2, candidate="B", test_source="tests", test_result=_failure("two")
    )
    third = guard.observe(
        attempt=3, candidate="C", test_source="tests", test_result=_failure("three")
    )

    assert first.stalled is False
    assert second.stalled is False
    assert third.reason == "unique_state_budget_exceeded"


def test_non_consecutive_attempts_and_passing_results_are_rejected():
    guard = RepairConvergenceGuard()

    with pytest.raises(ConvergenceInputError, match="consecutive"):
        guard.observe(
            attempt=2,
            candidate="candidate",
            test_source="tests",
            test_result=_failure(),
        )
    with pytest.raises(ConvergenceInputError, match="failed"):
        guard.observe(
            attempt=1,
            candidate="candidate",
            test_source="tests",
            test_result=TestResult(True, "passed"),
        )


class _NoRepairSynthesizer:
    def propose_rule(self, context: RepairContext) -> Rule | None:
        return None

    def propose_test_repair(self, context: RepairContext, test_source: str) -> str | None:
        return None


def test_engine_stops_repeated_failed_state_before_attempt_budget(tmp_path: Path):
    def runner(candidate: str, test_source: str) -> TestResult:
        return _failure()

    engine = MigrationEngine(
        RuleStore(tmp_path / "rules.json"),
        _NoRepairSynthesizer(),
        runner,
        max_attempts=6,
        repeated_test_failure_threshold=3,
    )

    with pytest.raises(MigrationConvergenceError) as captured:
        engine.migrate("VALUE = 1\n", "assert VALUE == 2\n")

    error = captured.value
    assert error.reason == "repeated_repair_state"
    assert error.trace.attempts == 3
    assert error.trace.termination_reason == "repeated_repair_state"
    assert len(error.trace.convergence_states) == 3


class _HarnessRepairSynthesizer(_NoRepairSynthesizer):
    def propose_test_repair(self, context: RepairContext, test_source: str) -> str | None:
        return test_source + "# fixed harness\n"


def test_engine_allows_threshold_test_repair_before_stopping(tmp_path: Path):
    def runner(candidate: str, test_source: str) -> TestResult:
        if "fixed harness" in test_source:
            return TestResult(True, "migration_test", kind=FailureKind.ASSERTION)
        return _failure()

    engine = MigrationEngine(
        RuleStore(tmp_path / "rules.json"),
        _HarnessRepairSynthesizer(),
        runner,
        max_attempts=6,
        repeated_test_failure_threshold=3,
    )

    _, test_source, trace = engine.migrate("VALUE = 1\n", "assert VALUE == 2\n")

    assert "fixed harness" in test_source
    assert trace.attempts == 4
    assert trace.termination_reason is None
    assert len(trace.convergence_states) == 3
    assert len(trace.test_repairs) == 1


def test_attempt_budget_exhaustion_has_structured_reason(tmp_path: Path):
    engine = MigrationEngine(
        RuleStore(tmp_path / "rules.json"),
        _NoRepairSynthesizer(),
        lambda candidate, tests: _failure(candidate),
        max_attempts=2,
        repeated_test_failure_threshold=3,
    )

    with pytest.raises(MigrationConvergenceError) as captured:
        engine.migrate("VALUE = 1\n", "assert VALUE == 2\n")

    assert captured.value.reason == "attempt_budget_exhausted"
    assert captured.value.trace.termination_reason == "attempt_budget_exhausted"
