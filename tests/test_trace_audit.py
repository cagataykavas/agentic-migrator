from __future__ import annotations

import json
from pathlib import Path

import pytest

from migrator.cli import main
from migrator.trace_audit import TraceEvidenceError, audit_trace
from migrator.tracing import TraceRecorder


def _span(
    span_id: str,
    *,
    parent: str | None = None,
    trace_id: str = "trace-1",
    name: str = "migration.step",
    started: int = 100,
    ended: int = 200,
    events: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent,
        "name": name,
        "started_at_ns": started,
        "ended_at_ns": ended,
        "status": "ok",
        "attributes": {},
        "events": events or [],
        "duration_ms": (ended - started) / 1_000_000,
    }


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_audits_real_nested_recorder_output(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    recorder = TraceRecorder(trace_id="run-42", output=path)
    with recorder.span("repository.migrate"):
        recorder.event("scan.complete", files=3)
        with recorder.span("validation.run"):
            pass

    report = audit_trace(
        path,
        required_span_names=("repository.migrate", "validation.run"),
    )

    assert report.ok
    assert report.trace_id == "run-42"
    assert report.spans == 2
    assert report.roots == 1


def test_reports_orphan_and_multiple_roots(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    _write(path, [_span("root"), _span("orphan", parent="missing")])

    report = audit_trace(path)

    assert not report.ok
    assert {issue.code for issue in report.issues} == {"span.orphan"}


def test_rejects_duplicate_span_ids(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    _write(path, [_span("same"), _span("same", parent="same")])

    report = audit_trace(path)

    assert "span.duplicate_id" in {issue.code for issue in report.issues}
    assert "span.parent_cycle" in {issue.code for issue in report.issues}


def test_rejects_mixed_trace_ids(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    _write(
        path,
        [
            _span("root"),
            _span("child", parent="root", trace_id="trace-2", started=120, ended=180),
        ],
    )

    report = audit_trace(path)

    assert report.trace_id is None
    assert "trace.mixed_ids" in {issue.code for issue in report.issues}


def test_checks_child_and_event_time_containment(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    _write(
        path,
        [
            _span("root", started=100, ended=200),
            _span(
                "child",
                parent="root",
                started=90,
                ended=180,
                events=[{"name": "too-late", "timestamp_ns": 190, "attributes": {}}],
            ),
        ],
    )

    report = audit_trace(path)

    codes = {issue.code for issue in report.issues}
    assert "span.outside_parent" in codes
    assert "event.outside_span" in codes


def test_detects_parent_cycle(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    _write(
        path,
        [
            _span("a", parent="b", started=100, ended=200),
            _span("b", parent="a", started=100, ended=200),
        ],
    )

    report = audit_trace(path)

    assert "span.parent_cycle" in {issue.code for issue in report.issues}
    assert "trace.root_count" in {issue.code for issue in report.issues}


def test_requires_declared_control_plane_spans(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    _write(path, [_span("root", name="repository.migrate")])

    report = audit_trace(path, required_span_names=("repository.migrate", "validation.run"))

    assert [issue.code for issue in report.issues] == ["trace.required_span_missing"]


def test_rejects_duration_inconsistent_with_timestamps(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    row = _span("root")
    row["duration_ms"] = 99
    _write(path, [row])

    with pytest.raises(TraceEvidenceError, match="duration_ms does not match"):
        audit_trace(path)


def test_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text('{"span_id":"a","span_id":"b"}\n', encoding="utf-8")

    with pytest.raises(TraceEvidenceError, match="duplicate JSON key"):
        audit_trace(path)


def test_cli_writes_failure_report_and_returns_two(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    output = tmp_path / "audit.json"
    _write(path, [_span("root", name="repository.migrate")])

    exit_code = main(
        [
            "trace-audit",
            str(path),
            "--require-span",
            "validation.run",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert not report["ok"]
    assert report["issues"][0]["code"] == "trace.required_span_missing"
