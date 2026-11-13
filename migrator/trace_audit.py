from __future__ import annotations

import json
import math
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

MAX_TRACE_BYTES = 8_000_000
MAX_TRACE_SPANS = 10_000


class TraceEvidenceError(ValueError):
    """Raised when trace evidence cannot be parsed safely."""


@dataclass(frozen=True)
class TraceIssue:
    code: str
    message: str
    span_id: str | None = None


@dataclass(frozen=True)
class TraceAuditReport:
    trace_id: str | None
    spans: int
    roots: int
    error_spans: int
    issues: tuple[TraceIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "trace_id": self.trace_id,
            "spans": self.spans,
            "roots": self.roots,
            "error_spans": self.error_spans,
            "issues": [asdict(issue) for issue in self.issues],
        }


@dataclass(frozen=True)
class _Span:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    started_at_ns: int
    ended_at_ns: int
    status: str
    events: tuple[tuple[str, int], ...]


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TraceEvidenceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _string(value: Any, field: str, line: int) -> str:
    if not isinstance(value, str) or not value:
        raise TraceEvidenceError(f"line {line}: {field} must be a non-empty string")
    return value


def _integer(value: Any, field: str, line: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TraceEvidenceError(f"line {line}: {field} must be a non-negative integer")
    return value


def _parse_span(payload: Any, line: int) -> _Span:
    if not isinstance(payload, dict):
        raise TraceEvidenceError(f"line {line}: span must be a JSON object")
    trace_id = _string(payload.get("trace_id"), "trace_id", line)
    span_id = _string(payload.get("span_id"), "span_id", line)
    parent = payload.get("parent_span_id")
    if parent is not None:
        parent = _string(parent, "parent_span_id", line)
    name = _string(payload.get("name"), "name", line)
    started = _integer(payload.get("started_at_ns"), "started_at_ns", line)
    ended = _integer(payload.get("ended_at_ns"), "ended_at_ns", line)
    if ended < started:
        raise TraceEvidenceError(f"line {line}: ended_at_ns precedes started_at_ns")
    status = _string(payload.get("status"), "status", line)
    if status not in {"ok", "error"}:
        raise TraceEvidenceError(f"line {line}: status must be 'ok' or 'error'")
    if not isinstance(payload.get("attributes"), dict):
        raise TraceEvidenceError(f"line {line}: attributes must be an object")

    duration = payload.get("duration_ms")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise TraceEvidenceError(f"line {line}: duration_ms must be a finite number")
    if not math.isfinite(duration):
        raise TraceEvidenceError(f"line {line}: duration_ms must be a finite number")
    expected_duration = (ended - started) / 1_000_000
    if not math.isclose(
        float(duration), expected_duration, rel_tol=0.0, abs_tol=0.000_001
    ):
        raise TraceEvidenceError(f"line {line}: duration_ms does not match span timestamps")

    event_payloads = payload.get("events")
    if not isinstance(event_payloads, list):
        raise TraceEvidenceError(f"line {line}: events must be an array")
    events: list[tuple[str, int]] = []
    for event_index, event in enumerate(event_payloads):
        field = f"events[{event_index}]"
        if not isinstance(event, dict):
            raise TraceEvidenceError(f"line {line}: {field} must be an object")
        event_name = _string(event.get("name"), f"{field}.name", line)
        timestamp = _integer(event.get("timestamp_ns"), f"{field}.timestamp_ns", line)
        if not isinstance(event.get("attributes"), dict):
            raise TraceEvidenceError(f"line {line}: {field}.attributes must be an object")
        events.append((event_name, timestamp))
    return _Span(trace_id, span_id, parent, name, started, ended, status, tuple(events))


def _read_spans(path: Path) -> list[_Span]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise TraceEvidenceError(f"cannot inspect trace: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise TraceEvidenceError("trace must be a regular, non-symlink file")
    if metadata.st_size > MAX_TRACE_BYTES:
        raise TraceEvidenceError(f"trace exceeds {MAX_TRACE_BYTES} bytes")

    spans: list[_Span] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                if not raw_line.strip():
                    raise TraceEvidenceError(f"line {line_number}: blank lines are not allowed")
                if len(spans) >= MAX_TRACE_SPANS:
                    raise TraceEvidenceError(f"trace exceeds {MAX_TRACE_SPANS} spans")
                try:
                    payload = json.loads(
                        raw_line,
                        object_pairs_hook=_strict_object,
                        parse_constant=lambda value, current_line=line_number: (
                            _ for _ in ()
                        ).throw(
                            TraceEvidenceError(
                                f"line {current_line}: non-finite number {value}"
                            )
                        ),
                    )
                except json.JSONDecodeError as exc:
                    raise TraceEvidenceError(
                        f"line {line_number}: invalid JSON: {exc.msg}"
                    ) from exc
                spans.append(_parse_span(payload, line_number))
    except (OSError, UnicodeDecodeError) as exc:
        raise TraceEvidenceError(f"cannot read UTF-8 trace: {exc}") from exc
    if not spans:
        raise TraceEvidenceError("trace contains no spans")
    return spans


def audit_trace(
    path: str | Path,
    *,
    required_span_names: tuple[str, ...] = (),
) -> TraceAuditReport:
    """Validate structural lineage and timing invariants in recorder JSONL evidence."""

    if any(not isinstance(name, str) or not name for name in required_span_names):
        raise TraceEvidenceError("required span names must be non-empty strings")
    if len(set(required_span_names)) != len(required_span_names):
        raise TraceEvidenceError("required span names must not contain duplicates")

    spans = _read_spans(Path(path))
    issues: set[TraceIssue] = set()
    by_id: dict[str, _Span] = {}
    for span in spans:
        if span.span_id in by_id:
            issues.add(
                TraceIssue("span.duplicate_id", "span ID appears more than once", span.span_id)
            )
        else:
            by_id[span.span_id] = span

    trace_ids = {span.trace_id for span in spans}
    if len(trace_ids) != 1:
        issues.add(TraceIssue("trace.mixed_ids", "evidence contains multiple trace IDs"))

    roots = [span for span in spans if span.parent_span_id is None]
    if len(roots) != 1:
        issues.add(
            TraceIssue("trace.root_count", f"expected exactly one root span, found {len(roots)}")
        )

    for span in spans:
        parent_id = span.parent_span_id
        if parent_id is not None:
            parent = by_id.get(parent_id)
            if parent is None:
                issues.add(
                    TraceIssue("span.orphan", f"parent span {parent_id!r} is missing", span.span_id)
                )
            elif span.started_at_ns < parent.started_at_ns or span.ended_at_ns > parent.ended_at_ns:
                issues.add(
                    TraceIssue(
                        "span.outside_parent",
                        "child timestamps are not contained by parent",
                        span.span_id,
                    )
                )
        for event_name, timestamp in span.events:
            if timestamp < span.started_at_ns or timestamp > span.ended_at_ns:
                issues.add(
                    TraceIssue(
                        "event.outside_span",
                        f"event {event_name!r} timestamp is outside its span",
                        span.span_id,
                    )
                )

        ancestry: set[str] = {span.span_id}
        current = span
        while current.parent_span_id is not None:
            if current.parent_span_id in ancestry:
                issues.add(
                    TraceIssue("span.parent_cycle", "parent lineage contains a cycle", span.span_id)
                )
                break
            ancestry.add(current.parent_span_id)
            parent = by_id.get(current.parent_span_id)
            if parent is None:
                break
            current = parent

    observed_names = {span.name for span in spans}
    for required_name in required_span_names:
        if required_name not in observed_names:
            issues.add(
                TraceIssue(
                    "trace.required_span_missing",
                    f"required span {required_name!r} is missing",
                )
            )

    trace_id = next(iter(trace_ids)) if len(trace_ids) == 1 else None
    return TraceAuditReport(
        trace_id=trace_id,
        spans=len(spans),
        roots=len(roots),
        error_spans=sum(span.status == "error" for span in spans),
        issues=tuple(
            sorted(
                issues,
                key=lambda issue: (issue.code, issue.span_id or "", issue.message),
            )
        ),
    )
