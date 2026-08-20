from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from statistics import mean
from typing import Iterable

from .models import MigrationTrace


@dataclass(frozen=True)
class MigrationRunRecord:
    """Compact, serializable summary of one migration execution.

    The migration engine intentionally keeps a detailed trace. This record is a
    reporting projection used by dashboards, CI summaries and benchmark runs.
    """

    migration_id: str
    source_language: str
    target_language: str
    passed: bool
    attempts: int
    duration_seconds: float
    input_lines: int
    output_lines: int
    applied_rules: tuple[str, ...] = ()
    learned_rules: tuple[str, ...] = ()
    test_repairs: int = 0
    llm_rule_requests: int = 0
    llm_test_requests: int = 0
    regression_failures: int = 0
    failure_kinds: tuple[str, ...] = ()

    @classmethod
    def from_trace(
        cls,
        *,
        migration_id: str,
        source_language: str,
        target_language: str,
        passed: bool,
        duration_seconds: float,
        input_lines: int,
        output_lines: int,
        trace: MigrationTrace,
        llm_rule_requests: int = 0,
        llm_test_requests: int = 0,
        regression_failures: int = 0,
    ) -> "MigrationRunRecord":
        failure_kinds = tuple(
            str(item.get("kind", "unknown"))
            for item in trace.failures
            if isinstance(item, dict) and "kind" in item
        )
        return cls(
            migration_id=migration_id,
            source_language=source_language,
            target_language=target_language,
            passed=passed,
            attempts=trace.attempts,
            duration_seconds=duration_seconds,
            input_lines=input_lines,
            output_lines=output_lines,
            applied_rules=tuple(trace.applied_rules),
            learned_rules=tuple(trace.learned_rules),
            test_repairs=len(trace.test_repairs),
            llm_rule_requests=llm_rule_requests,
            llm_test_requests=llm_test_requests,
            regression_failures=regression_failures,
            failure_kinds=failure_kinds,
        )


@dataclass
class MigrationMetrics:
    total_runs: int = 0
    passed_runs: int = 0
    failed_runs: int = 0
    first_pass_successes: int = 0
    deterministic_only_successes: int = 0
    runs_using_learned_rules: int = 0
    runs_learning_new_rules: int = 0
    runs_repairing_tests: int = 0
    llm_rule_requests: int = 0
    llm_test_requests: int = 0
    regression_failures: int = 0
    total_duration_seconds: float = 0.0
    average_attempts: float = 0.0
    average_duration_seconds: float = 0.0
    average_input_lines: float = 0.0
    average_output_lines: float = 0.0
    rule_usage: Counter[str] = field(default_factory=Counter)
    learned_rule_usage: Counter[str] = field(default_factory=Counter)
    failure_kinds: Counter[str] = field(default_factory=Counter)

    @property
    def pass_rate(self) -> float:
        return self.passed_runs / self.total_runs if self.total_runs else 0.0

    @property
    def first_pass_rate(self) -> float:
        return self.first_pass_successes / self.total_runs if self.total_runs else 0.0

    @property
    def llm_avoidance_rate(self) -> float:
        """Fraction of successful runs that required no LLM repair request."""
        if not self.passed_runs:
            return 0.0
        llm_assisted = min(self.passed_runs, self.llm_rule_requests + self.llm_test_requests)
        return max(0.0, 1.0 - llm_assisted / self.passed_runs)

    @property
    def learned_rule_reuse_rate(self) -> float:
        if not self.total_runs:
            return 0.0
        return self.runs_using_learned_rules / self.total_runs

    def as_dict(self) -> dict[str, object]:
        return {
            "total_runs": self.total_runs,
            "passed_runs": self.passed_runs,
            "failed_runs": self.failed_runs,
            "pass_rate": round(self.pass_rate, 4),
            "first_pass_rate": round(self.first_pass_rate, 4),
            "llm_avoidance_rate": round(self.llm_avoidance_rate, 4),
            "learned_rule_reuse_rate": round(self.learned_rule_reuse_rate, 4),
            "deterministic_only_successes": self.deterministic_only_successes,
            "runs_learning_new_rules": self.runs_learning_new_rules,
            "runs_repairing_tests": self.runs_repairing_tests,
            "llm_rule_requests": self.llm_rule_requests,
            "llm_test_requests": self.llm_test_requests,
            "regression_failures": self.regression_failures,
            "average_attempts": round(self.average_attempts, 3),
            "average_duration_seconds": round(self.average_duration_seconds, 3),
            "average_input_lines": round(self.average_input_lines, 1),
            "average_output_lines": round(self.average_output_lines, 1),
            "top_rules": self.rule_usage.most_common(10),
            "top_learned_rules": self.learned_rule_usage.most_common(10),
            "failure_kinds": dict(self.failure_kinds),
        }


def aggregate_metrics(records: Iterable[MigrationRunRecord]) -> MigrationMetrics:
    runs = list(records)
    metrics = MigrationMetrics()
    if not runs:
        return metrics

    metrics.total_runs = len(runs)
    metrics.passed_runs = sum(run.passed for run in runs)
    metrics.failed_runs = metrics.total_runs - metrics.passed_runs
    metrics.first_pass_successes = sum(run.passed and run.attempts == 1 for run in runs)
    metrics.deterministic_only_successes = sum(
        run.passed and run.llm_rule_requests == 0 and run.llm_test_requests == 0
        for run in runs
    )
    metrics.runs_using_learned_rules = sum(
        any(rule_id.startswith("learned.") for rule_id in run.applied_rules)
        for run in runs
    )
    metrics.runs_learning_new_rules = sum(bool(run.learned_rules) for run in runs)
    metrics.runs_repairing_tests = sum(run.test_repairs > 0 for run in runs)
    metrics.llm_rule_requests = sum(run.llm_rule_requests for run in runs)
    metrics.llm_test_requests = sum(run.llm_test_requests for run in runs)
    metrics.regression_failures = sum(run.regression_failures for run in runs)
    metrics.total_duration_seconds = sum(run.duration_seconds for run in runs)
    metrics.average_attempts = mean(run.attempts for run in runs)
    metrics.average_duration_seconds = mean(run.duration_seconds for run in runs)
    metrics.average_input_lines = mean(run.input_lines for run in runs)
    metrics.average_output_lines = mean(run.output_lines for run in runs)

    for run in runs:
        metrics.rule_usage.update(run.applied_rules)
        metrics.learned_rule_usage.update(run.learned_rules)
        metrics.failure_kinds.update(run.failure_kinds)

    return metrics


def markdown_summary(metrics: MigrationMetrics) -> str:
    """Generate a CI-friendly Markdown summary without external dependencies."""
    top_rules = metrics.rule_usage.most_common(5)
    top_rule_lines = "\n".join(
        f"| `{rule_id}` | {count} |" for rule_id, count in top_rules
    ) or "| _none_ | 0 |"

    return f"""# Agentic Migrator benchmark

| Metric | Value |
|---|---:|
| Runs | {metrics.total_runs} |
| Pass rate | {metrics.pass_rate:.1%} |
| First-pass success | {metrics.first_pass_rate:.1%} |
| LLM avoidance | {metrics.llm_avoidance_rate:.1%} |
| Learned-rule reuse | {metrics.learned_rule_reuse_rate:.1%} |
| Average attempts | {metrics.average_attempts:.2f} |
| Average duration | {metrics.average_duration_seconds:.2f}s |
| Test-repair runs | {metrics.runs_repairing_tests} |
| Regression failures | {metrics.regression_failures} |

## Most-used rules

| Rule | Uses |
|---|---:|
{top_rule_lines}
"""
