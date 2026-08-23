import subprocess
from pathlib import Path

from migrator.repository import GitRepository


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def test_isolated_worktree_does_not_mutate_source_checkout(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    git(repo_root, "init")
    (repo_root / "module.py").write_text("VALUE = 1\n")
    git(repo_root, "add", "module.py")
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            "initial",
        ],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )

    repository = GitRepository(repo_root)
    checkpoint = repository.checkpoint()
    assert checkpoint.dirty is False

    with repository.isolated_worktree() as worktree:
        (worktree / "module.py").write_text("VALUE = 2\n")
        assert (worktree / "module.py").read_text() == "VALUE = 2\n"

    assert (repo_root / "module.py").read_text() == "VALUE = 1\n"
    assert repository.head_sha() == checkpoint.sha
