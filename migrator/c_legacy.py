from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class FunctionSpan:
    name: str
    start_line: int
    end_line: int
    line_count: int


@dataclass(frozen=True)
class LegacyFinding:
    rule_id: str
    severity: str
    category: str
    line: int
    message: str
    evidence: str
    recommendation: str
    behavior_sensitive: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class BehaviorFact:
    fact_id: str
    description: str
    matched: bool
    evidence_lines: tuple[int, ...]
    evidence: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


_FUNCTION_HEADER = re.compile(
    r"(?m)^[ \t]*(?:[A-Za-z_]\w*[ \t]+)+(?P<name>[A-Za-z_]\w*)[ \t]*"
    r"\([^;{}]*\)[ \t]*\{"
)


def _line_number(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _line_text(source: str, line: int) -> str:
    lines = source.splitlines()
    if 1 <= line <= len(lines):
        return lines[line - 1].strip()
    return ""


def _mask_comments(source: str) -> str:
    """Replace C comments with spaces while preserving newlines and string literals."""
    result = list(source)
    index = 0
    quote: str | None = None
    escaped = False
    while index < len(source):
        char = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
            index += 1
            continue
        if char == "/" and nxt == "/":
            cursor = index
            while cursor < len(source) and source[cursor] != "\n":
                result[cursor] = " "
                cursor += 1
            index = cursor
            continue
        if char == "/" and nxt == "*":
            result[index] = " "
            result[index + 1] = " "
            cursor = index + 2
            while cursor < len(source) - 1:
                if source[cursor] == "*" and source[cursor + 1] == "/":
                    result[cursor] = " "
                    result[cursor + 1] = " "
                    cursor += 2
                    break
                if source[cursor] != "\n":
                    result[cursor] = " "
                cursor += 1
            index = cursor
            continue
        index += 1
    return "".join(result)


def _brace_end(source: str, opening_offset: int) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    in_line_comment = False
    in_block_comment = False
    index = opening_offset
    while index < len(source):
        char = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""
        if char == "\n":
            in_line_comment = False
        if in_line_comment:
            index += 1
            continue
        if in_block_comment:
            if char == "*" and nxt == "/":
                in_block_comment = False
                index += 2
            else:
                index += 1
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char == "/" and nxt == "/":
            in_line_comment = True
            index += 2
            continue
        if char == "/" and nxt == "*":
            in_block_comment = True
            index += 2
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def function_spans(source: str) -> tuple[FunctionSpan, ...]:
    masked = _mask_comments(source)
    spans: list[FunctionSpan] = []
    for match in _FUNCTION_HEADER.finditer(masked):
        opening = masked.find("{", match.start(), match.end())
        end = _brace_end(source, opening)
        if end is None:
            continue
        start_line = _line_number(source, match.start())
        end_line = _line_number(source, end)
        spans.append(
            FunctionSpan(
                name=match.group("name"),
                start_line=start_line,
                end_line=end_line,
                line_count=end_line - start_line + 1,
            )
        )
    return tuple(spans)


def _regex_findings(
    source: str,
    pattern: re.Pattern[str],
    *,
    rule_id: str,
    severity: str,
    category: str,
    message: str,
    recommendation: str,
    behavior_sensitive: bool = False,
) -> list[LegacyFinding]:
    findings: list[LegacyFinding] = []
    masked = _mask_comments(source)
    for match in pattern.finditer(masked):
        line = _line_number(source, match.start())
        findings.append(
            LegacyFinding(
                rule_id=rule_id,
                severity=severity,
                category=category,
                line=line,
                message=message,
                evidence=_line_text(source, line),
                recommendation=recommendation,
                behavior_sensitive=behavior_sensitive,
            )
        )
    return findings


def _sequential_toggle_findings(source: str) -> list[LegacyFinding]:
    """Detect adjacent if-statements that toggle 1→2 then 2→1 in one pass."""
    lines = source.splitlines()
    findings: list[LegacyFinding] = []
    first_pattern = re.compile(
        r"if\s*\([^)]*\b(?P<var>[A-Za-z_]\w*)\s*==\s*1[^)]*\).*\b(?P=var)\s*=\s*2\s*;"
    )
    for index, line in enumerate(lines):
        first = first_pattern.search(line)
        if first is None:
            continue
        variable = first.group("var")
        second_pattern = re.compile(
            rf"if\s*\([^)]*\b{re.escape(variable)}\s*==\s*2[^)]*\).*"
            rf"\b{re.escape(variable)}\s*=\s*1\s*;"
        )
        for lookahead in range(index + 1, min(index + 4, len(lines))):
            if second_pattern.search(lines[lookahead]):
                findings.append(
                    LegacyFinding(
                        rule_id="C.STATE.SEQUENTIAL_TOGGLE",
                        severity="high",
                        category="state-machine",
                        line=index + 1,
                        message=(
                            f"{variable!r} can be changed from 1→2 and then 2→1 by "
                            "sequential independent if-statements in the same loop iteration."
                        ),
                        evidence=f"{line.strip()} | {lines[lookahead].strip()}",
                        recommendation=(
                            "Represent ownership/equipped state separately or use an if/else "
                            "transition so one user action produces one state change."
                        ),
                        behavior_sensitive=True,
                    )
                )
                break
    return findings


def analyze_legacy_c(source: str, *, path: str = "legacy.c") -> dict[str, object]:
    findings: list[LegacyFinding] = []
    findings.extend(
        _regex_findings(
            source,
            re.compile(r"\bsystem\s*\(\s*\"cls\"\s*\)"),
            rule_id="C.PORTABILITY.SYSTEM_CLS",
            severity="medium",
            category="portability",
            message="Windows-specific shell clear command couples gameplay to one terminal.",
            recommendation="Move screen clearing behind a platform/presentation boundary.",
        )
    )
    findings.extend(
        _regex_findings(
            source,
            re.compile(r"\bsrand\s*\(\s*time\s*\("),
            rule_id="C.RNG.TIME_SEEDED_GLOBAL",
            severity="medium",
            category="reproducibility",
            message="Process-global RNG is seeded from wall-clock time.",
            recommendation=(
                "Inject explicit RNG state/seed so tests and migration regressions are repeatable."
            ),
            behavior_sensitive=True,
        )
    )
    findings.extend(
        _regex_findings(
            source,
            re.compile(r"\brand\s*\(\s*\)\s*%\s*[^;,)]+"),
            rule_id="C.RNG.MODULO_RAND",
            severity="low",
            category="randomness",
            message="Gameplay distribution is encoded through rand() modulo expressions.",
            recommendation=(
                "Inventory each probability/reward distribution before replacing RNG mechanics; "
                "migration tests should preserve intended ranges."
            ),
            behavior_sensitive=True,
        )
    )
    findings.extend(
        _regex_findings(
            source,
            re.compile(r"\bbattle\s*=\s*[A-Za-z_]\w*\s*\*\s*[A-Za-z_]\w*\s*;"),
            rule_id="C.STATE.PRODUCT_SENTINEL",
            severity="high",
            category="state-machine",
            message="Combat state is encoded as the product of mutable health values.",
            recommendation=(
                "Replace arithmetic sentinel state with explicit combat-result/state values while "
                "regression-testing win/death/flee turn ordering."
            ),
            behavior_sensitive=True,
        )
    )
    findings.extend(
        _regex_findings(
            source,
            re.compile(r"\bscanf\s*\([^\n;]*\"%s\""),
            rule_id="C.INPUT.UNBOUNDED_SCANF_STRING",
            severity="high",
            category="input-safety",
            message="Unbounded %s input can overrun its destination buffer.",
            recommendation="Use fgets plus bounded parsing, or provide an explicit scanf width.",
        )
    )
    findings.extend(_sequential_toggle_findings(source))

    spans = function_spans(source)
    for span in spans:
        if span.line_count >= 120:
            findings.append(
                LegacyFinding(
                    rule_id="C.ARCH.MONOLITHIC_FUNCTION",
                    severity="high" if span.line_count >= 250 else "medium",
                    category="architecture",
                    line=span.start_line,
                    message=f"Function {span.name!r} spans {span.line_count} lines.",
                    evidence=f"{span.name}: lines {span.start_line}-{span.end_line}",
                    recommendation=(
                        "Extract behavior boundaries behind tests before structural migration; "
                        "avoid a single LLM rewrite of the entire function."
                    ),
                    behavior_sensitive=True,
                )
            )

    masked = _mask_comments(source)
    discriminator_matches = list(re.finditer(r"\bif\s*\(\s*n\s*==?\s*\d+", masked))
    if len(discriminator_matches) >= 3:
        line = _line_number(source, discriminator_matches[0].start())
        findings.append(
            LegacyFinding(
                rule_id="C.DOMAIN.MAGIC_DISCRIMINATOR",
                severity="medium",
                category="domain-model",
                line=line,
                message=(
                    "Repeated numeric discriminator 'n' controls domain-specific behavior."
                ),
                evidence=_line_text(source, line),
                recommendation=(
                    "Introduce an enum/domain object only after tests capture each numeric case's "
                    "stats, rewards and side effects."
                ),
                behavior_sensitive=True,
            )
        )

    severity_order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda item: (severity_order.get(item.severity, 9), item.line, item.rule_id))
    return {
        "schema_version": 1,
        "path": path,
        "language": "c",
        "lines": len(source.splitlines()),
        "functions": [asdict(span) for span in spans],
        "summary": {
            "findings": len(findings),
            "high": sum(item.severity == "high" for item in findings),
            "medium": sum(item.severity == "medium" for item in findings),
            "low": sum(item.severity == "low" for item in findings),
            "behavior_sensitive": sum(item.behavior_sensitive for item in findings),
        },
        "findings": [item.as_dict() for item in findings],
    }


def _behavior_fact(
    source: str,
    *,
    fact_id: str,
    description: str,
    patterns: tuple[re.Pattern[str], ...],
) -> BehaviorFact:
    evidence_lines: list[int] = []
    evidence: list[str] = []
    for pattern in patterns:
        match = pattern.search(source)
        if match is None:
            continue
        line = _line_number(source, match.start())
        evidence_lines.append(line)
        evidence.append(_line_text(source, line))
    return BehaviorFact(
        fact_id=fact_id,
        description=description,
        matched=len(evidence) == len(patterns),
        evidence_lines=tuple(evidence_lines),
        evidence=tuple(evidence),
    )


def probe_shrek_rpg_behavior(source: str) -> dict[str, object]:
    """Explicit preservation probes for the public student RPG case study.

    These probes intentionally encode facts from the untouched historical source.
    They are not generic C-analysis heuristics; their purpose is to prevent the
    modernization case study itself from drifting away from what the old game did.
    """
    facts = (
        _behavior_fact(
            source,
            fact_id="difficulty.skill_budget",
            description="Skill budget uses (5 - difficulty) * 5.",
            patterns=(re.compile(r"\bSTONKS\s*=\s*\(\s*5\s*-\s*notgud\s*\)\s*\*\s*5"),),
        ),
        _behavior_fact(
            source,
            fact_id="encounter.zombie_70",
            description="Zombie branch covers rolls <= 70.",
            patterns=(re.compile(r"\bif\s*\(\s*n\s*<=\s*70\s*\)"),),
        ),
        _behavior_fact(
            source,
            fact_id="encounter.werewolf_25",
            description="Werewolf branch covers 70 < n <= 95.",
            patterns=(re.compile(r"n\s*>\s*70\s*&&\s*n\s*<=\s*95"),),
        ),
        _behavior_fact(
            source,
            fact_id="encounter.shrek_5",
            description="Shrek branch covers rolls > 95.",
            patterns=(re.compile(r"\bif\s*\(\s*n\s*>\s*95\s*\)"),),
        ),
        _behavior_fact(
            source,
            fact_id="shrek.identity",
            description="Encounter text names Mighty .:SHREK:..",
            patterns=(re.compile(r"Mighty \.\:SHREK\:\. appears!"),),
        ),
        _behavior_fact(
            source,
            fact_id="shrek.stats",
            description="Shrek has 800 HP and base attack 15.",
            patterns=(
                re.compile(r"ENEMYHP\s*=\s*800"),
                re.compile(r"battledamage\s*=\s*15"),
            ),
        ),
        _behavior_fact(
            source,
            fact_id="shrek.onion_reward",
            description="Defeating Shrek increments onion by one.",
            patterns=(re.compile(r"\bonion\s*=\s*onion\s*\+\s*1"),),
        ),
        _behavior_fact(
            source,
            fact_id="level.threshold",
            description="Initial XP threshold is 70 and increases by 40.",
            patterns=(
                re.compile(r"\bGOLDENEXPERIUNCU\s*=\s*70"),
                re.compile(r"GOLDENEXPERIUNCU\s*=\s*GOLDENEXPERIUNCU\s*\+\s*40"),
            ),
        ),
        _behavior_fact(
            source,
            fact_id="armor.chestplate",
            description="Chestplate costs 500 gold + 4 wolfskins and adds 5 stamina.",
            patterns=(
                re.compile(r"STONKS\s*>=\s*500"),
                re.compile(r"wolfskin\s*>=\s*4"),
                re.compile(r"c\s*=\s*c\s*\+\s*5"),
            ),
        ),
    )
    return {
        "case_study": "legacy-student-rpg-original.c",
        "facts": [fact.as_dict() for fact in facts],
        "matched": sum(fact.matched for fact in facts),
        "total": len(facts),
        "all_matched": all(fact.matched for fact in facts),
    }


def markdown_report(report: dict[str, object], behavior: dict[str, object] | None = None) -> str:
    summary = report["summary"]
    lines = [
        "# Legacy C migration preflight",
        "",
        f"- Path: `{report['path']}`",
        f"- Lines: {report['lines']}",
        f"- Findings: {summary['findings']}",
        f"- High / medium / low: {summary['high']} / {summary['medium']} / {summary['low']}",
        "",
        "## Findings",
        "",
        "| Severity | Line | Rule | Finding | Behavior-sensitive |",
        "|---|---:|---|---|---|",
    ]
    for finding in report["findings"]:
        lines.append(
            "| {severity} | {line} | `{rule}` | {message} | {sensitive} |".format(
                severity=finding["severity"],
                line=finding["line"],
                rule=finding["rule_id"],
                message=str(finding["message"]).replace("|", "\\|"),
                sensitive="yes" if finding["behavior_sensitive"] else "no",
            )
        )
    if behavior is not None:
        lines.extend(
            [
                "",
                "## Historical behavior probes",
                "",
                f"Matched: **{behavior['matched']}/{behavior['total']}**",
                "",
                "| Fact | Preserved in source probe | Description |",
                "|---|---|---|",
            ]
        )
        for fact in behavior["facts"]:
            lines.append(
                f"| `{fact['fact_id']}` | {'yes' if fact['matched'] else 'NO'} | "
                f"{fact['description']} |"
            )
    return "\n".join(lines) + "\n"


def write_reports(
    report: dict[str, object],
    *,
    json_path: str | Path,
    markdown_path: str | Path,
    behavior: dict[str, object] | None = None,
) -> None:
    json_destination = Path(json_path)
    markdown_destination = Path(markdown_path)
    json_destination.parent.mkdir(parents=True, exist_ok=True)
    markdown_destination.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(report)
    if behavior is not None:
        payload["behavior_probe"] = behavior
    json_destination.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    markdown_destination.write_text(markdown_report(report, behavior), encoding="utf-8")
