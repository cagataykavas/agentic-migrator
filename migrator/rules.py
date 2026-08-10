from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .models import Rule


class RuleStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")

    def load(self) -> list[Rule]:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return [Rule(**item) for item in payload]

    def save(self, rules: list[Rule]) -> None:
        self.path.write_text(json.dumps([asdict(rule) for rule in rules], indent=2), encoding="utf-8")

    def upsert(self, rule: Rule) -> None:
        rules = self.load()
        by_id = {item.id: item for item in rules}
        by_id[rule.id] = rule
        self.save(list(by_id.values()))


def apply_rules(source: str, rules: list[Rule]) -> tuple[str, list[str]]:
    output = source
    applied: list[str] = []
    for rule in rules:
        if rule.pattern in output:
            output = output.replace(rule.pattern, rule.replacement)
            applied.append(rule.id)
    return output, applied
