from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from migrator.cli import main

SOURCE = """# comment must survive the structural migration
import legacy_client


def fetch_record():
    return legacy_client.request(timeout_seconds=5)
"""


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def run_demo(output: Path) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    patch = output / "test-gated-migration.patch"
    manifest = output / "test-gated-migration.json"

    with tempfile.TemporaryDirectory(prefix="migration-gate-demo-") as temporary:
        repository = Path(temporary) / "legacy-repository"
        repository.mkdir()
        (repository / "client.py").write_text(SOURCE, encoding="utf-8")
        git(repository, "init", "-b", "main")
        git(repository, "config", "user.email", "demo@example.invalid")
        git(repository, "config", "user.name", "Migration Demo")
        git(repository, "add", "client.py")
        git(repository, "commit", "-m", "legacy checkpoint")
        checkpoint = git_output(repository, "rev-parse", "HEAD")

        exit_code = main(
            [
                "migrate-repo",
                str(repository),
                "--import-rewrite",
                "legacy_client=modern_client",
                "--keyword-rewrite",
                "modern_client.request:timeout_seconds=timeout",
                "--verify-command",
                "python -m compileall -q .",
                "--require-verification",
                "--manifest",
                str(manifest),
                "--patch",
                str(patch),
            ]
        )
        if exit_code != 0:
            raise RuntimeError(f"test-gated migration failed with exit code {exit_code}")
        if git_output(repository, "rev-parse", "HEAD") != checkpoint:
            raise RuntimeError("source repository checkpoint changed")
        if (repository / "client.py").read_text(encoding="utf-8") != SOURCE:
            raise RuntimeError("source checkout was mutated")

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    return {
        "status": payload["status"],
        "changed_files": payload["summary"]["changed_files"],
        "verification_passed": payload["verification"]["passed"],
        "patch_applies": payload["patch_validation"]["applies_to_source_checkpoint"],
        "patch_bytes": patch.stat().st_size,
        "manifest": str(manifest),
        "patch": str(patch),
    }


def git_output(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def main_cli() -> None:
    parser = argparse.ArgumentParser(description="Produce test-gated repository migration evidence")
    parser.add_argument("--output", type=Path, default=Path("artifacts/test-gated-demo"))
    args = parser.parse_args()
    print(json.dumps(run_demo(args.output), indent=2))


if __name__ == "__main__":
    main_cli()
