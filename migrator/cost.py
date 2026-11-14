from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    fcntl = None  # type: ignore[assignment]

MAX_LEDGER_BYTES = 4_000_000
MAX_LEDGER_EVENTS = 100_000
MAX_EVENT_BYTES = 32_000
_EVENT_KEYS = {
    "operation",
    "model",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "created_at",
    "metadata",
}


@dataclass(frozen=True)
class ModelPrice:
    model: str
    input_per_million: float
    output_per_million: float


@dataclass(frozen=True)
class BudgetPolicy:
    max_calls: int = 20
    max_input_tokens: int = 200_000
    max_output_tokens: int = 50_000
    max_cost_usd: float = 5.0


@dataclass(frozen=True)
class BudgetStatus:
    allowed: bool
    reasons: tuple[str, ...]
    projected_calls: int
    projected_input_tokens: int
    projected_output_tokens: int
    projected_cost_usd: float


@dataclass(frozen=True)
class UsageEvent:
    operation: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    created_at: str
    metadata: dict[str, str]


class BudgetExceeded(RuntimeError):
    pass


class LedgerIntegrityError(RuntimeError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise LedgerIntegrityError(f"duplicate ledger field: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise LedgerIntegrityError(f"non-finite ledger number: {value}")


def _validate_count(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LedgerIntegrityError(f"{label} must be a non-negative integer")
    return value


def _validate_event(payload: object, line_number: int) -> UsageEvent:
    if not isinstance(payload, dict) or set(payload) != _EVENT_KEYS:
        raise LedgerIntegrityError(f"ledger line {line_number} has an invalid schema")
    operation = payload["operation"]
    model = payload["model"]
    if not isinstance(operation, str) or not operation.strip() or len(operation) > 128:
        raise LedgerIntegrityError(f"ledger line {line_number} has an invalid operation")
    if not isinstance(model, str) or not model.strip() or len(model) > 128:
        raise LedgerIntegrityError(f"ledger line {line_number} has an invalid model")
    input_tokens = _validate_count(payload["input_tokens"], "input_tokens")
    output_tokens = _validate_count(payload["output_tokens"], "output_tokens")
    cost = payload["cost_usd"]
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost):
        raise LedgerIntegrityError(f"ledger line {line_number} has an invalid cost")
    if cost < 0:
        raise LedgerIntegrityError(f"ledger line {line_number} has a negative cost")
    created_at = payload["created_at"]
    if not isinstance(created_at, str):
        raise LedgerIntegrityError(f"ledger line {line_number} has an invalid timestamp")
    try:
        timestamp = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise LedgerIntegrityError(f"ledger line {line_number} has an invalid timestamp") from exc
    if timestamp.tzinfo is None:
        raise LedgerIntegrityError(f"ledger line {line_number} timestamp must include a timezone")
    metadata = payload["metadata"]
    if (
        not isinstance(metadata, dict)
        or len(metadata) > 64
        or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or len(key) > 128
            or len(value) > 1024
            for key, value in metadata.items()
        )
    ):
        raise LedgerIntegrityError(f"ledger line {line_number} has invalid metadata")
    return UsageEvent(
        operation=operation,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=float(cost),
        created_at=created_at,
        metadata=metadata,
    )


class CostLedger:
    """A bounded, locked JSONL ledger with atomic pre-call budget admission."""

    def __init__(self, path: str | Path = "artifacts/llm_usage.jsonl") -> None:
        self.path = Path(path)

    @contextmanager
    def _locked_file(self, *, write: bool) -> Iterator[BinaryIO]:
        if fcntl is None:
            raise LedgerIntegrityError("atomic ledger locking requires a POSIX host")
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = (os.O_RDWR | os.O_CREAT) if write else os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as exc:
            if not write and exc.errno == errno.ENOENT:
                raise FileNotFoundError from exc
            raise LedgerIntegrityError("ledger must be an accessible non-symlink file") from exc
        handle = os.fdopen(descriptor, "r+b" if write else "rb")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if write else fcntl.LOCK_SH)
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise LedgerIntegrityError("ledger must be a regular file")
            yield handle
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    @staticmethod
    def _read_locked(handle: BinaryIO) -> tuple[list[UsageEvent], bytes]:
        handle.seek(0)
        content = handle.read(MAX_LEDGER_BYTES + 1)
        if len(content) > MAX_LEDGER_BYTES:
            raise LedgerIntegrityError(f"ledger exceeds {MAX_LEDGER_BYTES} bytes")
        if content and not content.endswith(b"\n"):
            raise LedgerIntegrityError("ledger has a truncated final record")
        raw_lines = content.splitlines()
        if len(raw_lines) > MAX_LEDGER_EVENTS:
            raise LedgerIntegrityError(f"ledger exceeds {MAX_LEDGER_EVENTS} events")
        events: list[UsageEvent] = []
        for line_number, raw_line in enumerate(raw_lines, 1):
            if not raw_line or len(raw_line) > MAX_EVENT_BYTES:
                raise LedgerIntegrityError(f"ledger line {line_number} has an invalid size")
            try:
                payload = json.loads(
                    raw_line.decode("utf-8"),
                    object_pairs_hook=_reject_duplicate_keys,
                    parse_constant=_reject_nonfinite,
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LedgerIntegrityError(f"ledger line {line_number} is invalid JSON") from exc
            events.append(_validate_event(payload, line_number))
        return events, content

    def _snapshot(self) -> tuple[list[UsageEvent], bytes]:
        try:
            with self._locked_file(write=False) as handle:
                return self._read_locked(handle)
        except FileNotFoundError:
            return [], b""

    @staticmethod
    def _validate_price(price: ModelPrice) -> None:
        if not isinstance(price.model, str) or not price.model.strip() or len(price.model) > 128:
            raise ValueError("model must contain 1-128 characters")
        for label, value in (
            ("input_per_million", price.input_per_million),
            ("output_per_million", price.output_per_million),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{label} must be a finite non-negative number")

    @staticmethod
    def _validate_policy(policy: BudgetPolicy) -> None:
        for label, value in (
            ("max_calls", policy.max_calls),
            ("max_input_tokens", policy.max_input_tokens),
            ("max_output_tokens", policy.max_output_tokens),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} must be a non-negative integer")
        if (
            isinstance(policy.max_cost_usd, bool)
            or not isinstance(policy.max_cost_usd, (int, float))
            or not math.isfinite(policy.max_cost_usd)
            or policy.max_cost_usd < 0
        ):
            raise ValueError("max_cost_usd must be a finite non-negative number")

    @classmethod
    def calculate_cost(cls, price: ModelPrice, input_tokens: int, output_tokens: int) -> float:
        cls._validate_price(price)
        if (
            isinstance(input_tokens, bool)
            or isinstance(output_tokens, bool)
            or not isinstance(input_tokens, int)
            or not isinstance(output_tokens, int)
            or input_tokens < 0
            or output_tokens < 0
        ):
            raise ValueError("token counts must be non-negative integers")
        return (
            input_tokens / 1_000_000 * price.input_per_million
            + output_tokens / 1_000_000 * price.output_per_million
        )

    @classmethod
    def _budget_status(
        cls,
        events: list[UsageEvent],
        price: ModelPrice,
        input_tokens: int,
        output_tokens: int,
        policy: BudgetPolicy,
    ) -> BudgetStatus:
        cls._validate_policy(policy)
        projected_calls = len(events) + 1
        projected_input = sum(item.input_tokens for item in events) + input_tokens
        projected_output = sum(item.output_tokens for item in events) + output_tokens
        projected_cost = sum(item.cost_usd for item in events) + cls.calculate_cost(
            price,
            input_tokens,
            output_tokens,
        )
        reasons: list[str] = []
        if projected_calls > policy.max_calls:
            reasons.append("call budget exceeded")
        if projected_input > policy.max_input_tokens:
            reasons.append("input-token budget exceeded")
        if projected_output > policy.max_output_tokens:
            reasons.append("output-token budget exceeded")
        if projected_cost > policy.max_cost_usd:
            reasons.append("cost budget exceeded")
        return BudgetStatus(
            allowed=not reasons,
            reasons=tuple(reasons),
            projected_calls=projected_calls,
            projected_input_tokens=projected_input,
            projected_output_tokens=projected_output,
            projected_cost_usd=round(projected_cost, 8),
        )

    def check_budget(
        self,
        price: ModelPrice,
        input_tokens: int,
        output_tokens: int,
        policy: BudgetPolicy,
    ) -> BudgetStatus:
        events, _ = self._snapshot()
        return self._budget_status(events, price, input_tokens, output_tokens, policy)

    def record(
        self,
        operation: str,
        price: ModelPrice,
        input_tokens: int,
        output_tokens: int,
        metadata: dict[str, str] | None = None,
        *,
        budget: BudgetPolicy | None = None,
    ) -> UsageEvent:
        if not isinstance(operation, str) or not operation.strip() or len(operation) > 128:
            raise ValueError("operation must contain 1-128 characters")
        event = UsageEvent(
            operation=operation,
            model=price.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=self.calculate_cost(price, input_tokens, output_tokens),
            created_at=datetime.now(UTC).isoformat(),
            metadata=dict(metadata or {}),
        )
        _validate_event(asdict(event), 1)
        line = (
            json.dumps(
                asdict(event), sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
            + b"\n"
        )
        if len(line) > MAX_EVENT_BYTES:
            raise ValueError(f"usage event exceeds {MAX_EVENT_BYTES} bytes")

        with self._locked_file(write=True) as handle:
            events, content = self._read_locked(handle)
            if budget is not None:
                status = self._budget_status(events, price, input_tokens, output_tokens, budget)
                if not status.allowed:
                    raise BudgetExceeded("; ".join(status.reasons))
            if len(events) >= MAX_LEDGER_EVENTS:
                raise LedgerIntegrityError(f"ledger exceeds {MAX_LEDGER_EVENTS} events")
            if len(content) + len(line) > MAX_LEDGER_BYTES:
                raise LedgerIntegrityError(f"ledger exceeds {MAX_LEDGER_BYTES} bytes")
            handle.seek(0, os.SEEK_END)
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        return event

    def events(self) -> list[UsageEvent]:
        events, _ = self._snapshot()
        return events

    def summary(self) -> dict[str, float | int | str]:
        events, content = self._snapshot()
        return {
            "calls": len(events),
            "input_tokens": sum(item.input_tokens for item in events),
            "output_tokens": sum(item.output_tokens for item in events),
            "cost_usd": round(sum(item.cost_usd for item in events), 8),
            "ledger_bytes": len(content),
            "ledger_sha256": hashlib.sha256(content).hexdigest(),
        }
