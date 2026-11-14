import json
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from migrator.cli import main
from migrator.cost import (
    BudgetExceeded,
    BudgetPolicy,
    CostLedger,
    LedgerIntegrityError,
    ModelPrice,
)
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
    assert summary["ledger_bytes"] > 0
    assert len(summary["ledger_sha256"]) == 64
    assert stat.S_IMODE((tmp_path / "usage.jsonl").stat().st_mode) == 0o600


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


def test_concurrent_records_cannot_overspend_shared_call_budget(tmp_path: Path):
    ledger = CostLedger(tmp_path / "usage.jsonl")
    price = ModelPrice("demo", input_per_million=1.0, output_per_million=1.0)
    policy = BudgetPolicy(max_calls=1, max_input_tokens=100, max_output_tokens=100, max_cost_usd=1)
    barrier = Barrier(12)

    def attempt(index: int) -> str:
        barrier.wait()
        try:
            ledger.record("repair", price, 1, 1, {"attempt": str(index)}, budget=policy)
        except BudgetExceeded:
            return "blocked"
        return "recorded"

    with ThreadPoolExecutor(max_workers=12) as executor:
        outcomes = list(executor.map(attempt, range(12)))

    assert outcomes.count("recorded") == 1
    assert outcomes.count("blocked") == 11
    assert ledger.summary()["calls"] == 1


def test_corrupt_or_truncated_ledger_fails_closed_without_append(tmp_path: Path):
    path = tmp_path / "usage.jsonl"
    original = b'{"operation":"partial"'
    path.write_bytes(original)
    ledger = CostLedger(path)
    price = ModelPrice("demo", input_per_million=1.0, output_per_million=1.0)

    with pytest.raises(LedgerIntegrityError, match="truncated"):
        ledger.record("repair", price, 1, 1)

    assert path.read_bytes() == original


def test_duplicate_ledger_fields_are_rejected(tmp_path: Path):
    path = tmp_path / "usage.jsonl"
    path.write_text(
        '{"operation":"repair","operation":"shadow","model":"demo",'
        '"input_tokens":1,"output_tokens":1,"cost_usd":0.1,'
        '"created_at":"2026-11-14T19:00:00+00:00","metadata":{}}\n',
        encoding="utf-8",
    )

    with pytest.raises(LedgerIntegrityError, match="duplicate ledger field"):
        CostLedger(path).summary()


def test_symlinked_ledger_is_rejected_without_touching_target(tmp_path: Path):
    target = tmp_path / "target.jsonl"
    target.write_bytes(b"")
    link = tmp_path / "usage.jsonl"
    link.symlink_to(target)
    price = ModelPrice("demo", input_per_million=1.0, output_per_million=1.0)

    with pytest.raises(LedgerIntegrityError, match="non-symlink"):
        CostLedger(link).record("repair", price, 1, 1)

    assert target.read_bytes() == b""


def test_non_numeric_price_and_policy_are_rejected_cleanly(tmp_path: Path):
    ledger = CostLedger(tmp_path / "usage.jsonl")
    invalid_price = ModelPrice("demo", input_per_million="unknown", output_per_million=1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="input_per_million"):
        ledger.record("repair", invalid_price, 1, 1)

    invalid_policy = BudgetPolicy(max_cost_usd="unbounded")  # type: ignore[arg-type]
    price = ModelPrice("demo", input_per_million=1.0, output_per_million=1.0)
    with pytest.raises(ValueError, match="max_cost_usd"):
        ledger.record("repair", price, 1, 1, budget=invalid_policy)


def test_cost_summary_cli_reports_integrity_failure_without_traceback(tmp_path: Path, capsys):
    path = tmp_path / "usage.jsonl"
    path.write_text("not-json\n", encoding="utf-8")

    assert main(["cost-summary", "--ledger", str(path)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert "ledger line 1" in payload["error"]
