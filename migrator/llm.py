from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import Rule, TestResult


@dataclass(frozen=True)
class RepairContext:
    source: str
    candidate: str
    test_result: TestResult
    active_rule_ids: list[str]


class RuleSynthesizer(Protocol):
    def propose_rule(self, context: RepairContext) -> Rule | None:
        ...

    def propose_test_repair(self, context: RepairContext, test_source: str) -> str | None:
        ...


class DemoRuleSynthesizer:
    """Deterministic stand-in showing the contract expected from an LLM wrapper.

    Production integrations should force structured JSON output and validate it before
    creating a Rule. This demo deliberately avoids requiring an API key.
    """

    def propose_rule(self, context: RepairContext) -> Rule | None:
        message = f"{context.test_result.stderr}\n{context.test_result.stdout}".lower()
        if "unexpected keyword argument 'timeout_seconds'" in message:
            return Rule(
                id="learned.rename_timeout_kwarg.v1",
                pattern="timeout_seconds=",
                replacement="timeout=",
                reason="target API expects timeout instead of timeout_seconds",
                created_by="llm-demo",
            )
        return None

    def propose_test_repair(self, context: RepairContext, test_source: str) -> str | None:
        # Demonstration only: no automatic weakening or assertion deletion.
        if "legacy_client" in test_source and "module not found" in context.test_result.stderr.lower():
            return test_source.replace("legacy_client", "modern_client")
        return None
