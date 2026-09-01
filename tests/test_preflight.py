from __future__ import annotations

import json
from pathlib import Path

from migrator.preflight import (
    discover_sources,
    estimate_preflight_risk,
    run_repository_preflight,
)


def test_discovery_uses_language_registry_and_ignores_vendor_dirs(tmp_path: Path) -> None:
    (tmp_path / "service.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (tmp_path / "Risk.java").write_text("class Risk { }\n", encoding="utf-8")
    (tmp_path / "client.js").write_text("export const x = 1;\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("ignore me\n", encoding="utf-8")
    vendor = tmp_path / "node_modules"
    vendor.mkdir()
    (vendor / "vendored.js").write_text("export const bad = 1;\n", encoding="utf-8")

    sources = discover_sources(tmp_path)

    assert [item.path for item in sources] == ["Risk.java", "client.js", "service.py"]
    assert {item.language for item in sources} == {"java", "javascript", "python"}


def test_explicit_high_risk_pattern_moves_file_to_review_lane(tmp_path: Path) -> None:
    core = tmp_path / "core"
    core.mkdir()
    (core / "payments.py").write_text("def settle():\n    return True\n", encoding="utf-8")
    (tmp_path / "utility.py").write_text("VALUE = 1\n", encoding="utf-8")

    report = run_repository_preflight(
        tmp_path,
        max_workers=2,
        automatic_risk_ceiling=0.25,
        high_risk_patterns=("core/*",),
    )

    assert [item.path for item in report.review_lane] == ["core/payments.py"]
    assert [item.path for item in report.validated] == ["utility.py"]
    assert report.execution["review_required"] == 1


def test_invalid_supported_source_is_reported_not_worker_failure(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")

    report = run_repository_preflight(tmp_path)

    assert len(report.validated) == 1
    assert not report.validated[0].valid
    assert "SyntaxError" in report.validated[0].diagnostics[0]
    assert not report.failed_workers


def test_preflight_writes_structured_trace(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    trace_path = tmp_path / "artifacts" / "trace.jsonl"

    report = run_repository_preflight(tmp_path, trace_output=trace_path)
    rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]

    assert report.trace["errors"] == 0
    assert {row["name"] for row in rows} == {
        "preflight.discover",
        "preflight.parallel_validate",
        "preflight.repository",
    }


def test_risk_estimator_is_bounded_and_transparent() -> None:
    risk, matched = estimate_preflight_risk(
        relative_path="core/huge.py",
        line_count=50_000,
        high_risk_patterns=("core/*",),
    )
    assert risk == 1.0
    assert matched == "core/*"
