from pathlib import Path

import pytest

from migrator.cost import BudgetExceeded, BudgetPolicy, CostLedger, ModelPrice
from migrator.semantic_diff import compare_public_api


def test_semantic_diff_detects_breaking_signature_change():
    before = """
def convert(source, timeout=10):
    return source

class Client:
    def send(self, payload):
        return payload
"""
    after = """
def convert(source, timeout, retries=2):
    return source

class Client:
    def send(self, payload):
        return payload
"""
    diff = compare_public_api(before, after)
    assert diff.breaking
    assert diff.changed[0][0].name == "convert"


def test_cost_ledger_is_append_only_and_summarizes(tmp_path: Path):
    ledger = CostLedger(tmp_path / "usage.jsonl")
    price = ModelPrice("demo-model", input_per_million=2.0, output_per_million=8.0)
    first = ledger.record("rule_synthesis", price, 1000, 250, {"migration_id": "m1"})
    ledger.record("test_repair", price, 500, 100, {"migration_id": "m1"})

    assert first.cost_usd > 0
    summary = ledger.summary()
    assert summary["calls"] == 2
    assert summary["input_tokens"] == 1500
    assert summary["output_tokens"] == 350
    assert summary["cost_usd"] > 0


def test_cost_budget_blocks_call_before_ledger_mutation(tmp_path: Path):
    ledger = CostLedger(tmp_path / "usage.jsonl")
    price = ModelPrice("expensive-demo", input_per_million=10.0, output_per_million=30.0)
    budget = BudgetPolicy(
        max_calls=1,
        max_input_tokens=1000,
        max_output_tokens=100,
        max_cost_usd=0.02,
    )

    status = ledger.check_budget(price, 500, 100, budget)
    assert status.allowed
    ledger.record("rule_synthesis", price, 500, 100, budget=budget)

    with pytest.raises(BudgetExceeded, match="call budget exceeded"):
        ledger.record("rule_synthesis", price, 10, 10, budget=budget)

    assert ledger.summary()["calls"] == 1


def test_cost_budget_reports_multiple_projected_limit_failures(tmp_path: Path):
    ledger = CostLedger(tmp_path / "usage.jsonl")
    price = ModelPrice("demo", input_per_million=100.0, output_per_million=100.0)
    policy = BudgetPolicy(
        max_calls=5,
        max_input_tokens=100,
        max_output_tokens=50,
        max_cost_usd=0.01,
    )

    status = ledger.check_budget(price, 200, 100, policy)
    assert not status.allowed
    assert "input-token budget exceeded" in status.reasons
    assert "output-token budget exceeded" in status.reasons
    assert "cost budget exceeded" in status.reasons
