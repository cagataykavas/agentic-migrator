from pathlib import Path

from migrator.ast_rules import conservative_example
from migrator.cli import main
from migrator.gitops import MigrationWorkspace
from migrator.sandbox import LocalSandbox


def test_cli_scan_writes_markdown(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("import b\nprint('a')\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("VALUE = 1\n", encoding="utf-8")
    output = tmp_path / "plan.md"

    code = main(["scan", str(tmp_path), "--output", str(output)])

    assert code == 0
    text = output.read_text(encoding="utf-8")
    assert "Repository migration plan" in text
    assert "a.py" in text
    assert "b.py" in text


def test_structural_example_avoids_comment_rewrite() -> None:
    source = "# legacy_client should stay in this comment\nimport legacy_client\nlegacy_client.request(timeout_seconds=5)\n"
    transformed, applied = conservative_example(source)

    assert "# legacy_client should stay in this comment" in transformed
    assert "import modern_client" in transformed
    assert "timeout=5" in transformed
    assert applied


def test_workspace_diff_and_rollback() -> None:
    workspace = MigrationWorkspace({"app.py": "value = 1\n"})
    workspace.write("app.py", "value = 2\n")
    changes = workspace.proposed_changes()

    assert len(changes) == 1
    assert "-value = 1" in changes[0].diff
    assert "+value = 2" in changes[0].diff

    workspace.rollback("app.py")
    assert workspace.proposed_changes() == []


def test_local_sandbox_runs_bounded_command() -> None:
    sandbox = LocalSandbox(timeout_seconds=3)
    result = sandbox.run(
        ["python", "check.py"],
        files={"check.py": "print('sandbox-ok')\n"},
    )

    assert result.passed
    assert "sandbox-ok" in result.stdout
