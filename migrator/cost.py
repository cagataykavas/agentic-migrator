from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class ModelPrice:
    model: str
    input_per_million: float
    output_per_million: float


@dataclass(frozen=True)
class UsageEvent:
    operation: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    created_at: str
    metadata: dict[str, str]


class CostLedger:
    """Append-only JSONL usage ledger for optional LLM-assisted repair calls."""

    def __init__(self, path: str | Path = "artifacts/llm_usage.jsonl") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def calculate_cost(price: ModelPrice, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens / 1_000_000 * price.input_per_million
            + output_tokens / 1_000_000 * price.output_per_million
        )

    def record(
        self,
        operation: str,
        price: ModelPrice,
        input_tokens: int,
        output_tokens: int,
        metadata: dict[str, str] | None = None,
    ) -> UsageEvent:
        event = UsageEvent(
            operation=operation,
            model=price.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=self.calculate_cost(price, input_tokens, output_tokens),
            created_at=datetime.now(timezone.utc).isoformat(),
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
