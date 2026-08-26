from __future__ import annotations

import sys
from pathlib import Path

import pytest

from migrator.gitops import MigrationWorkspace
from migrator.sandbox import LocalSandbox, SandboxViolation


def _python_sandbox(*, max_output_bytes: int = 256_000) -> LocalSandbox:
    return LocalSandbox(
        allowed_executables=(Path(sys.executable).name,),
        max_output_bytes=max_output_bytes,
    )


def test_sandbox_executes_allowed_command_with_materialized_files():
    sandbox = _python_sandbox()
    result = sandbox.run(
        [sys.executable, "-c", "print(open('input.txt', encoding='utf-8').read())"],
        files={"input.txt": "migration-ok"},
    )

    assert result.passed
    assert result.stdout.strip() == "migration-ok"
    assert not result.output_truncated


def test_sandbox_rejects_non_allowlisted_executable():
    sandbox = _python_sandbox()
    with pytest.raises(SandboxViolation, match="not allowed"):
        sandbox.run(["definitely-not-python", "--version"])


def test_sandbox_rejects_path_escape():
    sandbox = _python_sandbox()
    with pytest.raises(SandboxViolation, match="unsafe sandbox path"):
        sandbox.run(
            [sys.executable, "-c", "print('never runs')"],
            files={"../escape.py": "print('bad')"},
        )


def test_sandbox_caps_captured_output():
    sandbox = _python_sandbox(max_output_bytes=64)
    result = sandbox.run([sys.executable, "-c", "print('x' * 1000)"])

    assert result.passed
    assert result.output_truncated
    assert "output truncated by sandbox" in result.stdout


def test_workspace_tracks_create_update_delete_and_rollback():
    workspace = MigrationWorkspace(
        {
            "pkg/update.py": "VALUE = 1\n",
            "pkg/delete.py": "OLD = True\n",
        }
    )

    workspace.write("pkg/update.py", "VALUE = 2\n")
    workspace.delete("pkg/delete.py")
    workspace.create("pkg/create.py", "NEW = True\n")

    manifest = workspace.manifest()
    assert manifest["changed_files"] == 3
    assert manifest["creates"] == 1
    assert manifest["updates"] == 1
    assert manifest["deletes"] == 1
    assert {item["operation"] for item in manifest["changes"]} == {
        "create",
        "update",
        "delete",
    }

    workspace.rollback("pkg/create.py")
    workspace.rollback("pkg/update.py")
    assert workspace.read("pkg/update.py") == "VALUE = 1\n"
    assert "pkg/create.py" not in workspace.paths()

    workspace.rollback()
    assert workspace.paths() == ("pkg/delete.py", "pkg/update.py")


def test_workspace_materialize_applies_deletions_and_creations(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "old.py").write_text("OLD = 1\n", encoding="utf-8")

    workspace = MigrationWorkspace({"old.py": "OLD = 1\n"})
    workspace.delete("old.py")
    workspace.create("nested/new.py", "NEW = 2\n")
    workspace.materialize(root)

    assert not (root / "old.py").exists()
    assert (root / "nested/new.py").read_text(encoding="utf-8") == "NEW = 2\n"


def test_workspace_rejects_unsafe_paths():
    with pytest.raises(ValueError, match="unsafe migration path"):
        MigrationWorkspace({"../escape.py": "bad"})
