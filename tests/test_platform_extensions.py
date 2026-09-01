from __future__ import annotations

import json
from pathlib import Path

from migrator.adapters import AdapterRegistry
from migrator.parallel import MigrationTask, ParallelMigrationExecutor
from migrator.tracing import TraceRecorder


def test_trace_recorder_writes_nested_jsonl(tmp_path: Path) -> None:
    output = tmp_path / "trace.jsonl"
    recorder = TraceRecorder(trace_id="demo-trace", output=output)

    with recorder.span("repository.migrate", files=2):
        recorder.event("scan.complete", python_files=2)
        with recorder.span("file.migrate", path="app.py"):
            pass

    assert recorder.summary()["spans"] == 2
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert {row["name"] for row in rows} == {"repository.migrate", "file.migrate"}
    child = next(row for row in rows if row["name"] == "file.migrate")
    parent = next(row for row in rows if row["name"] == "repository.migrate")
    assert child["parent_span_id"] == parent["span_id"]


def test_parallel_executor_is_deterministic_and_defers_high_risk() -> None:
    executor = ParallelMigrationExecutor(lambda value: value.upper(), max_workers=2)
    tasks = [
        MigrationTask("b.py", "beta", risk_score=0.1),
        MigrationTask("a.py", "alpha", risk_score=0.2),
        MigrationTask("danger.py", "gamma", risk_score=0.95),
    ]

    results, review = executor.run(tasks)

    assert [result.task_id for result in results] == ["a.py", "b.py"]
    assert [result.value for result in results] == ["ALPHA", "BETA"]
    assert [task.task_id for task in review] == ["danger.py"]
    assert executor.summary(results, review)["review_required"] == 1


def test_parallel_executor_captures_worker_failures() -> None:
    def worker(value: str) -> str:
        if value == "boom":
            raise RuntimeError("migration failed")
        return value

    executor = ParallelMigrationExecutor(worker)
    results, review = executor.run(
        [MigrationTask("ok", "fine"), MigrationTask("bad", "boom")]
    )

    assert not review
    by_id = {result.task_id: result for result in results}
    assert by_id["ok"].success
    assert not by_id["bad"].success
    assert by_id["bad"].error_type == "RuntimeError"


def test_adapter_registry_validates_python_and_detects_java() -> None:
    registry = AdapterRegistry()

    assert registry.validate("service.py", "def run():\n    return 1\n").valid
    invalid = registry.validate("service.py", "def broken(:\n    pass\n")
    assert not invalid.valid
    assert "SyntaxError" in invalid.diagnostics[0]

    unit = registry.source_unit("PaymentService.java", "class PaymentService {\r\n}\r\n")
    assert unit.language == "java"
    assert "\r" not in unit.content
    assert registry.validate(unit.path, unit.content).valid


def test_adapter_registry_rejects_unknown_suffix() -> None:
    registry = AdapterRegistry()
    try:
        registry.adapter_for("schema.sql")
    except ValueError as exc:
        assert "no migration adapter" in str(exc)
    else:
        raise AssertionError("unknown suffix should not resolve to an adapter")
