from __future__ import annotations

import json
from pathlib import Path

from migrator.metrics import MigrationRunRecord, aggregate_metrics, markdown_summary


def demo_records() -> list[MigrationRunRecord]:
    """Synthetic portfolio benchmark.

    These records are intentionally synthetic: they demonstrate reporting and
    metric semantics without implying performance measured on proprietary code.
    """
    return [
        MigrationRunRecord(
            migration_id="api-timeout-001",
            source_language="python-legacy-api",
            target_language="python-modern-api",
            passed=True,
            attempts=1,
            duration_seconds=0.18,
            input_lines=84,
            output_lines=84,
            applied_rules=("builtin.rename_timeout_kwarg.v1",),
        ),
        MigrationRunRecord(
            migration_id="client-import-002",
            source_language="python-legacy-api",
            target_language="python-modern-api",
            passed=True,
            attempts=2,
            duration_seconds=0.82,
            input_lines=132,
            output_lines=134,
            applied_rules=("builtin.rewrite_client_import.v1", "learned.client_factory.v1"),
            learned_rules=("learned.client_factory.v1",),
            llm_rule_requests=1,
            failure_kinds=("import",),
        ),
        MigrationRunRecord(
            migration_id="client-import-003",
            source_language="python-legacy-api",
            target_language="python-modern-api",
            passed=True,
            attempts=1,
            duration_seconds=0.22,
            input_lines=165,
            output_lines=167,
            applied_rules=("builtin.rewrite_client_import.v1", "learned.client_factory.v1"),
        ),
        MigrationRunRecord(
            migration_id="fixture-layout-004",
            source_language="python-legacy-api",
            target_language="python-modern-api",
            passed=True,
            attempts=4,
            duration_seconds=1.61,
            input_lines=241,
            output_lines=245,
            applied_rules=("builtin.rename_timeout_kwarg.v1",),
            test_repairs=1,
            llm_rule_requests=1,
            llm_test_requests=1,
            failure_kinds=("assertion", "test_harness", "test_harness"),
        ),
        MigrationRunRecord(
            migration_id="unsupported-semantic-005",
            source_language="python-legacy-api",
            target_language="python-modern-api",
            passed=False,
            attempts=6,
            duration_seconds=2.74,
            input_lines=318,
            output_lines=320,
            applied_rules=("builtin.rewrite_client_import.v1",),
            llm_rule_requests=3,
            regression_failures=1,
            failure_kinds=("runtime", "assertion", "assertion"),
        ),
    ]


def main() -> None:
    output_dir = Path("artifacts")
    output_dir.mkdir(exist_ok=True)

    metrics = aggregate_metrics(demo_records())
    payload = metrics.as_dict()
    (output_dir / "benchmark.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "benchmark.md").write_text(markdown_summary(metrics), encoding="utf-8")

    print(markdown_summary(metrics))
    print("\nWrote artifacts/benchmark.json and artifacts/benchmark.md")


if __name__ == "__main__":
    main()
