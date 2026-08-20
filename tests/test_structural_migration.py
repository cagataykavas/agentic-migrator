from pathlib import Path

from migrator.ast_rules import (
    AttributeRewrite,
    ImportRewrite,
    KeywordRewrite,
    transform_source,
)
from migrator.project_scan import build_inventory, migration_plan


def test_ast_rule_rewrites_code_but_not_strings():
    source = (
        "import legacy_client\n"
        "message = 'legacy_client.request(timeout_seconds=5)'\n"
        "result = legacy_client.request(timeout_seconds=5)\n"
    )
    output, applied = transform_source(
        source,
        import_rewrites=(ImportRewrite("legacy_client", "modern_client"),),
        keyword_rewrites=(
            KeywordRewrite("legacy_client.request", "timeout_seconds", "timeout"),
        ),
    )
    assert "import modern_client" in output
    assert "'legacy_client.request(timeout_seconds=5)'" in output
    # Function still uses legacy_client because import rewriting does not silently
    # rename every Name node; this demonstrates conservative rule boundaries.
    assert "legacy_client.request(timeout=5)" in output
    assert applied


def test_attribute_chain_can_be_rewritten():
    source = "error_type = legacy.errors.Timeout\n"
    output, applied = transform_source(
        source,
        attribute_rewrites=(
            AttributeRewrite("legacy.errors.Timeout", "modern.TimeoutError"),
        ),
    )
    assert "modern.TimeoutError" in output
    assert any(item.startswith("attribute:") for item in applied)


def test_project_scan_builds_local_dependency_plan(tmp_path: Path):
    package = tmp_path / "demo"
    package.mkdir()
    (package / "core.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (package / "service.py").write_text(
        "from demo import core\n\ndef run():\n    return core.value()\n",
        encoding="utf-8",
    )
    (package / "__init__.py").write_text("", encoding="utf-8")

    inventory = build_inventory(tmp_path)
    assert "demo/core.py" in inventory.files
    assert "demo/service.py" in inventory.files

    plan = migration_plan(inventory)
    order = {unit.path: unit.order for unit in plan}
    assert order["demo/core.py"] < order["demo/service.py"]
