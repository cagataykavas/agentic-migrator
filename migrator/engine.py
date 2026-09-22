from __future__ import annotations

from collections import Counter
from collections.abc import Callable

from .convergence import ConvergencePolicy, RepairConvergenceGuard
from .llm import RepairContext, RuleSynthesizer
from .models import MigrationTrace, TestResult
from .rules import RuleStore, apply_rules
from .test_guard import unified_diff, validate_test_repair

TestRunner = Callable[[str, str], TestResult]


class MigrationConvergenceError(RuntimeError):
    """Raised with structured evidence when the repair loop stops making progress."""

    def __init__(self, reason: str, trace: MigrationTrace) -> None:
        self.reason = reason
        self.trace = trace
        super().__init__(f"migration stopped: {reason} after {trace.attempts} attempts")


class MigrationEngine:
    def __init__(
        self,
        rule_store: RuleStore,
        synthesizer: RuleSynthesizer,
        test_runner: TestRunner,
        *,
        max_attempts: int = 6,
        repeated_test_failure_threshold: int = 3,
        convergence_policy: ConvergencePolicy | None = None,
    ) -> None:
        self.rule_store = rule_store
        self.synthesizer = synthesizer
        self.test_runner = test_runner
        self.max_attempts = max_attempts
        self.repeated_test_failure_threshold = repeated_test_failure_threshold
        self.convergence_policy = convergence_policy or ConvergencePolicy(
            max_state_occurrences=max(2, repeated_test_failure_threshold),
            max_unique_states=max_attempts,
        )

    def migrate(self, source: str, test_source: str) -> tuple[str, str, MigrationTrace]:
        trace = MigrationTrace()
        active_test_source = test_source
        failure_signatures: Counter[str] = Counter()
        convergence = RepairConvergenceGuard(self.convergence_policy)

        for attempt in range(1, self.max_attempts + 1):
            trace.attempts = attempt
            active_rules = self.rule_store.load()
            candidate, applied = apply_rules(source, active_rules)
            trace.applied_rules.extend(rule_id for rule_id in applied if rule_id not in trace.applied_rules)

            result = self.test_runner(candidate, active_test_source)
            if result.passed:
                return candidate, active_test_source, trace

            signature = f"{result.kind.value}:{result.name}:{result.stderr.strip()}"
            failure_signatures[signature] += 1
            trace.failures.append({
                "attempt": attempt,
                "name": result.name,
                "kind": result.kind.value,
                "stderr": result.stderr,
            })
            convergence_state = convergence.observe(
                attempt=attempt,
                candidate=candidate,
                test_source=active_test_source,
                test_result=result,
                active_rule_ids=tuple(rule.id for rule in active_rules),
            )
            trace.convergence_states.append(convergence_state.to_dict())

            context = RepairContext(
                source=source,
                candidate=candidate,
                test_result=result,
                active_rule_ids=[rule.id for rule in active_rules],
            )

            proposed_rule = self.synthesizer.propose_rule(context)
            if proposed_rule is not None:
                before = result
                trial_rules = self.rule_store.load() + [proposed_rule]
                trial_candidate, _ = apply_rules(source, trial_rules)
                after = self.test_runner(trial_candidate, active_test_source)
                if after.passed or self._improved(before, after):
                    proposed_rule.success_count += 1
                    self.rule_store.upsert(proposed_rule)
                    trace.learned_rules.append(proposed_rule.id)
                    continue
                proposed_rule.failure_count += 1

            if failure_signatures[signature] >= self.repeated_test_failure_threshold:
                proposed_test = self.synthesizer.propose_test_repair(context, active_test_source)
                if proposed_test is not None:
                    allowed, reason = validate_test_repair(active_test_source, proposed_test)
                    if allowed:
                        diff = unified_diff(active_test_source, proposed_test)
                        trial = self.test_runner(candidate, proposed_test)
                        if trial.passed or self._improved(result, trial):
                            active_test_source = proposed_test
                            trace.test_repairs.append(diff)
                            continue
                    trace.failures.append({"attempt": attempt, "test_repair_rejected": reason})

            if convergence_state.stalled:
                trace.termination_reason = convergence_state.reason
                raise MigrationConvergenceError(convergence_state.reason or "stalled", trace)

        trace.termination_reason = "attempt_budget_exhausted"
        raise MigrationConvergenceError(trace.termination_reason, trace)

    @staticmethod
    def _improved(before: TestResult, after: TestResult) -> bool:
        if after.passed:
            return True
        # A different, more local failure after a rule application can represent progress.
        priority = {"syntax": 0, "import": 1, "runtime": 2, "test_harness": 2, "assertion": 3, "unknown": 0}
        return priority.get(after.kind.value, 0) > priority.get(before.kind.value, 0)
