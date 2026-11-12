from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest

from migrator.models import Rule
from migrator.rules import RuleStore


def _upsert_rule(path: str, rule_id: str, start: multiprocessing.synchronize.Event) -> None:
    store = RuleStore(path)
    start.wait(timeout=10)
    store.upsert(
        Rule(
            id=rule_id,
            pattern=f"legacy_{rule_id}",
            replacement=f"modern_{rule_id}",
            reason="concurrent acceptance test",
        )
    )


@pytest.mark.skipif(os.name != "posix", reason="inter-process locking uses POSIX flock")
def test_concurrent_upserts_do_not_lose_accepted_rules(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    RuleStore(path)
    context = multiprocessing.get_context("fork")
    start = context.Event()
    processes = [
        context.Process(target=_upsert_rule, args=(str(path), f"rule-{index}", start))
        for index in range(12)
    ]

    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=15)

    assert all(process.exitcode == 0 for process in processes)
    assert {rule.id for rule in RuleStore(path).load()} == {
        f"rule-{index}" for index in range(12)
    }


def test_failed_atomic_replace_preserves_previous_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rules.json"
    store = RuleStore(path)
    original = Rule("stable", "old", "new", "known-good rule")
    store.save([original])

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError(f"cannot replace {source} with {destination}")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="cannot replace"):
        store.save([Rule("candidate", "a", "b", "not committed")])

    assert RuleStore(path).load() == [original]
    assert not list(tmp_path.glob(".rules.json.*.tmp"))


def test_save_preserves_existing_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    store = RuleStore(path)
    path.chmod(0o640)

    store.save([Rule("stable", "old", "new", "mode test")])

    assert path.stat().st_mode & 0o777 == 0o640
