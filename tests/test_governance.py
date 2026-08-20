from migrator.governance import (
    RuleEvaluation,
    RuleGovernor,
    RuleState,
)


def test_strong_rule_is_promoted():
    evaluation = RuleEvaluation(
        rule_id="learned.good.v1",
        validation_successes=8,
        validation_failures=0,
        regression_failures=0,
        distinct_migrations=4,
        exact_reuse_count=6,
        state=RuleState.CANARY,
    )
    decision = RuleGovernor().decide(evaluation)
    assert decision.next_state is RuleState.PROMOTED


def test_high_failure_rate_disables_rule():
    evaluation = RuleEvaluation(
        rule_id="learned.bad.v1",
        validation_successes=3,
        validation_failures=3,
        regression_failures=0,
        distinct_migrations=3,
        exact_reuse_count=1,
        state=RuleState.CANARY,
    )
    decision = RuleGovernor().decide(evaluation)
    assert decision.next_state is RuleState.DISABLED


def test_promoted_rule_with_regression_is_quarantined():
    evaluation = RuleEvaluation(
        rule_id="learned.regressed.v1",
        validation_successes=12,
        validation_failures=0,
        regression_failures=1,
        distinct_migrations=8,
        exact_reuse_count=10,
        state=RuleState.PROMOTED,
    )
    decision = RuleGovernor().decide(evaluation)
    assert decision.next_state is RuleState.QUARANTINED


def test_new_rule_moves_to_canary_after_minimum_evidence():
    evaluation = RuleEvaluation(
        rule_id="learned.new.v1",
        validation_successes=2,
        validation_failures=0,
        regression_failures=0,
        distinct_migrations=1,
        exact_reuse_count=0,
        state=RuleState.QUARANTINED,
    )
    decision = RuleGovernor().decide(evaluation)
    assert decision.next_state is RuleState.CANARY
