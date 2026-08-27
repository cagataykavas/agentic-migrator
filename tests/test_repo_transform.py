from __future__ import annotations

import json
import subprocess
from pathlib import Path

from migrator.ast_rules import ImportRewrite, KeywordRewrite
from migrator.cli import main
from migrator.repo_transform import build_repository_transform_plan


def test_repository_plan_tracks_changes_without_touching_source(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    source_path = package / "client.py"
    source_path.write_text(
        "# migration comment must survive\n"
        "import legacy_client\n\n"
        "def fetch():\n"
        "    return legacy_client.request(timeout_seconds=5)\n",
        encoding="utf-8",
    )
    original = source_path.read_text(encoding="utf-8")

    plan = build_repository_transform_plan(
        tmp_path,
        import_rewrites=(ImportRewrite("legacy_client", "modern_client"),),
        keyword_rewrites=(
            KeywordRewrite("modern_client.request", "timeout_seconds", "timeout"),
        ),
        high_risk_imports=("legacy_client",),
    )

    assert plan.changed_files == 1
    assert plan.breaking_files == ()
    result = next(item for item in plan.files if item.path == "pkg/client.py")
    assert result.status == "changed"
    assert result.risk_score >= 30
    assert any(item.startswith("import:") for item in result.applied_rules)

    migrated = plan.changeset.read("pkg/client.py")
    assert "# migration comment must survive" in migrated
    assert "import modern_client" in migrated
    assert "modern_client.request(timeout=5)" in migrated
    assert source_path.read_text(encoding="utf-8") == original


def test_repository_plan_skips_invalid_python(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("def broken(:\n", encoding="utf-8")

    plan = build_repository_transform_plan(tmp_path)

    result = next(item for item in plan.files if item.path == "bad.py")
    assert result.status == "skipped_invalid_syntax"
    assert plan.changed_files == 0
    assert plan.skipped_files == 1


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )


def test_migrate_repo_cli_exports_patch_without_mutating_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source_path = repo / "app.py"
    original = (
        "# keep me\n"
        "import legacy_client\n\n"
        "def run():\n"
        "    return legacy_client.request(timeout_seconds=5)\n"
    )
    source_path.write_text(original, encoding="utf-8")

    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "ci@example.invalid")
    _git(repo, "config", "user.name", "CI")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "initial")

    patch = tmp_path / "migration.patch"
    manifest = tmp_path / "migration.json"
    code = main(
        [
            "migrate-repo",
            str(repo),
            "--import-rewrite",
            "legacy_client=modern_client",
            "--keyword-rewrite",
            "modern_client.request:timeout_seconds=timeout",
            "--patch",
            str(patch),
            "--manifest",
            str(manifest),
        ]
    )

    assert code == 0
    assert source_path.read_text(encoding="utf-8") == original
    patch_text = patch.read_text(encoding="utf-8")
    assert "+import modern_client" in patch_text
    assert "modern_client.request(timeout=5)" in patch_text

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["summary"]["changed_files"] == 1
    assert payload["summary"]["breaking_files"] == []
    assert payload["files"][0]["status"] == "changed"
