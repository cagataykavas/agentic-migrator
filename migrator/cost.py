from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


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


class CostLedger:
    """Append-only JSONL usage ledger with optional pre-call budget enforcement."""

    def __init__(self, path: str | Path = "artifacts/llm_usage.jsonl") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def calculate_cost(price: ModelPrice, input_tokens: int, output_tokens: int) -> float:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts cannot be negative")
        return (
            input_tokens / 1_000_000 * price.input_per_million
            + output_tokens / 1_000_000 * price.output_per_million
        )

    def check_budget(
        self,
        price: ModelPrice,
        input_tokens: int,
        output_tokens: int,
        policy: BudgetPolicy,
    ) -> BudgetStatus:
        current = self.summary()
        projected_calls = int(current["calls"]) + 1
        projected_input = int(current["input_tokens"]) + input_tokens
        projected_output = int(current["output_tokens"]) + output_tokens
        projected_cost = float(current["cost_usd"]) + self.calculate_cost(
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
        if budget is not None:
            status = self.check_budget(price, input_tokens, output_tokens, budget)
            if not status.allowed:
                raise BudgetExceeded("; ".join(status.reasons))

        event = UsageEvent(
            operation=operation,
            model=price.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=self.calculate_cost(price, input_tokens, output_tokens),
            created_at=datetime.now(UTC).isoformat(),
            metadata=metadata or {},
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(event), sort_keys=True) + "\n")
        return event

    def events(self) -> list[UsageEvent]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(UsageEvent(**json.loads(line)))
        return rows

    def summary(self) -> dict[str, float | int]:
        events = self.events()
        return {
            "calls": len(events),
            "input_tokens": sum(item.input_tokens for item in events),
            "output_tokens": sum(item.output_tokens for item in events),
            "cost_usd": round(sum(item.cost_usd for item in events), 8),
        }
