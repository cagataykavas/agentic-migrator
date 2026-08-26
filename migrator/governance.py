from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum


class RuleState(str, Enum):
    QUARANTINED = "quarantined"
    CANARY = "canary"
    PROMOTED = "promoted"
    DISABLED = "disabled"


@dataclass(frozen=True)
class RuleEvaluation:
    rule_id: str
    validation_successes: int
    validation_failures: int
    regression_failures: int
    distinct_migrations: int
    exact_reuse_count: int
    state: RuleState = RuleState.QUARANTINED

    @property
    def observations(self) -> int:
        return self.validation_successes + self.validation_failures

    @property
    def success_rate(self) -> float:
        if not self.observations:
            return 0.0
        return self.validation_successes / self.observations


@dataclass(frozen=True)
class PromotionPolicy:
    canary_min_successes: int = 2
    promote_min_successes: int = 5
    promote_min_distinct_migrations: int = 3
    promote_min_success_rate: float = 0.95
    max_regression_failures_for_canary: int = 0
    max_regression_failures_for_promotion: int = 0
    disable_failure_rate: float = 0.30
    disable_min_observations: int = 5


@dataclass(frozen=True)
class GovernanceDecision:
    rule_id: str
    previous_state: RuleState
    next_state: RuleState
    reasons: tuple[str, ...]


class RuleGovernor:
    """Decide when a learned migration rule is safe enough to reuse broadly.

    The governor is independent from the LLM. It only consumes validation
    evidence. This keeps promotion policy deterministic and auditable.
    """

    def __init__(self, policy: PromotionPolicy | None = None) -> None:
        self.policy = policy or PromotionPolicy()

    def decide(self, evaluation: RuleEvaluation) -> GovernanceDecision:
        reasons: list[str] = []
        failure_rate = 1.0 - evaluation.success_rate

        if (
            evaluation.observations >= self.policy.disable_min_observations
            and failure_rate >= self.policy.disable_failure_rate
        ):
            reasons.append("failure_rate_exceeds_disable_threshold")
            return GovernanceDecision(
                evaluation.rule_id,
                evaluation.state,
                RuleState.DISABLED,
                tuple(reasons),
            )

        if evaluation.regression_failures > 0:
            reasons.append("regression_failure_observed")
            if evaluation.state is RuleState.PROMOTED:
                reasons.append("promoted_rule_must_be_quarantined")
                return GovernanceDecision(
                    evaluation.rule_id,
                    evaluation.state,
                    RuleState.QUARANTINED,
                    tuple(reasons),
                )

        can_promote = (
            evaluation.validation_successes >= self.policy.promote_min_successes
            and evaluation.distinct_migrations
            >= self.policy.promote_min_distinct_migrations
            and evaluation.success_rate >= self.policy.promote_min_success_rate
            and evaluation.regression_failures
            <= self.policy.max_regression_failures_for_promotion
        )
        if can_promote:
            reasons.extend(
                [
                    "minimum_success_count_met",
                    "cross_migration_reuse_demonstrated",
                    "success_rate_above_promotion_threshold",
                    "no_blocking_regression_failure",
                ]
            )
            return GovernanceDecision(
                evaluation.rule_id,
                evaluation.state,
                RuleState.PROMOTED,
                tuple(reasons),
            )

        can_canary = (
            evaluation.validation_successes >= self.policy.canary_min_successes
            and evaluation.regression_failures
            <= self.policy.max_regression_failures_for_canary
        )
        if can_canary and evaluation.state is RuleState.QUARANTINED:
            reasons.extend(
                [
                    "minimum_canary_success_count_met",
                    "no_blocking_regression_failure",
                ]
            )
            return GovernanceDecision(
                evaluation.rule_id,
                evaluation.state,
                RuleState.CANARY,
                tuple(reasons),
            )

        reasons.append("insufficient_evidence_for_broader_reuse")
        return GovernanceDecision(
            evaluation.rule_id,
            evaluation.state,
            evaluation.state,
            tuple(reasons),
        )


def summarize_decisions(
    evaluations: Iterable[RuleEvaluation],
    *,
    governor: RuleGovernor | None = None,
) -> list[GovernanceDecision]:
    active_governor = governor or RuleGovernor()
    return [active_governor.decide(evaluation) for evaluation in evaluations]


def demo() -> None:
    evaluations = [
        RuleEvaluation(
            rule_id="learned.rename_timeout_kwarg.v1",
            validation_successes=8,
            validation_failures=0,
            regression_failures=0,
            distinct_migrations=5,
            exact_reuse_count=7,
            state=RuleState.CANARY,
        ),
        RuleEvaluation(
            rule_id="learned.unsafe_constructor_rewrite.v1",
            validation_successes=4,
            validation_failures=3,
            regression_failures=1,
            distinct_migrations=2,
            exact_reuse_count=1,
            state=RuleState.CANARY,
        ),
    ]
    for decision in summarize_decisions(evaluations):
        print(decision)


if __name__ == "__main__":
    demo()
