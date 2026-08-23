from pathlib import Path

from migrator.cost import CostLedger, ModelPrice
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
