from pathlib import Path

from migrator.engine import MigrationEngine
from migrator.llm import DemoRuleSynthesizer
from migrator.models import FailureKind, TestResult
from migrator.rules import RuleStore
from migrator.test_guard import validate_test_repair


def fake_runner(candidate: str, _: str) -> TestResult:
    if "timeout_seconds=" in candidate:
        return TestResult(False, "api_call", stderr="TypeError: unexpected keyword argument 'timeout_seconds'", kind=FailureKind.RUNTIME)
    return TestResult(True, "api_call", kind=FailureKind.ASSERTION)


def test_engine_learns_and_persists_rule(tmp_path: Path):
    store = RuleStore(tmp_path / "rules.json")
    engine = MigrationEngine(store, DemoRuleSynthesizer(), fake_runner)
    candidate, _, trace = engine.migrate("client.request(timeout_seconds=5)", "assert True")
    assert "timeout=5" in candidate
    assert "learned.rename_timeout_kwarg.v1" in trace.learned_rules
    assert any(rule.id == "learned.rename_timeout_kwarg.v1" for rule in store.load())


def test_guard_rejects_deleted_assertion():
    allowed, _ = validate_test_repair("assert result == 3\n", "result = 3\n")
    assert not allowed
