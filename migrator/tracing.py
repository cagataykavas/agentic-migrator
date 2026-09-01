from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SpanRecord:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    started_at_ns: int
    ended_at_ns: int | None = None
    status: str = "ok"
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration_ms(self) -> float | None:
        if self.ended_at_ns is None:
            return None
        return (self.ended_at_ns - self.started_at_ns) / 1_000_000

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["duration_ms"] = self.duration_ms
        return payload


class TraceRecorder:
    """Small structured tracer for migration runs.

    It intentionally has no mandatory dependency on an observability vendor. Records can
    be written as JSONL for CI artifacts, while ``OpenTelemetryBridge`` can mirror spans
    into a real OpenTelemetry provider when the optional package is installed.
    """

    def __init__(self, *, trace_id: str | None = None, output: str | Path | None = None) -> None:
        self.trace_id = trace_id or uuid.uuid4().hex
        self.output = Path(output) if output is not None else None
        self.records: list[SpanRecord] = []
        self._stack: list[SpanRecord] = []

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[SpanRecord]:
        parent = self._stack[-1] if self._stack else None
        record = SpanRecord(
            trace_id=self.trace_id,
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=parent.span_id if parent else None,
            name=name,
            started_at_ns=time.time_ns(),
            attributes=dict(attributes),
        )
        self._stack.append(record)
        try:
            yield record
        except Exception as exc:
            record.status = "error"
            record.attributes["exception.type"] = type(exc).__name__
            record.attributes["exception.message"] = str(exc)
            raise
        finally:
            record.ended_at_ns = time.time_ns()
            self._stack.pop()
            self.records.append(record)
            if self.output is not None:
                self.output.parent.mkdir(parents=True, exist_ok=True)
                with self.output.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record.as_dict(), sort_keys=True) + "\n")

    def event(self, name: str, **attributes: Any) -> None:
        if not self._stack:
            raise RuntimeError("events must be emitted inside an active span")
        self._stack[-1].events.append(
            {"name": name, "timestamp_ns": time.time_ns(), "attributes": dict(attributes)}
        )

    def summary(self) -> dict[str, Any]:
        durations = [record.duration_ms for record in self.records if record.duration_ms is not None]
        return {
            "trace_id": self.trace_id,
            "spans": len(self.records),
            "errors": sum(record.status == "error" for record in self.records),
            "total_recorded_ms": round(sum(durations), 3),
            "names": sorted({record.name for record in self.records}),
        }


class OpenTelemetryBridge:
    """Optional adapter that mirrors a migration span into OpenTelemetry.

    Importing this module never requires OpenTelemetry. Calling ``span`` becomes a no-op
    context manager when the package is absent, which keeps the core library lightweight.
    """

    def __init__(self, instrumentation_name: str = "agentic-migrator") -> None:
        self.instrumentation_name = instrumentation_name
        try:
            from opentelemetry import trace  # type: ignore[import-not-found]
        except ImportError:
            self._tracer = None
        else:
            self._tracer = trace.get_tracer(instrumentation_name)

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[object | None]:
        if self._tracer is None:
            yield None
            return
        with self._tracer.start_as_current_span(name, attributes=attributes) as span:
            yield span
