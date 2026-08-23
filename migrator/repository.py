from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class GitCheckpoint:
    sha: str
    branch: str | None
    dirty: bool


class GitRepository:
    """Thin Git boundary for safe repository-scale migrations.

    The migrator can execute in a detached temporary worktree so failed attempts
    never need to mutate the developer's current checkout. Applying changes back
    to the source repository remains an explicit user action.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        if not (self.root / ".git").exists():
            completed = self._run("rev-parse", "--git-dir", check=False)
            if completed.returncode != 0:
                raise ValueError(f"not a Git repository: {self.root}")

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=check,
        )

    def head_sha(self) -> str:
        return self._run("rev-parse", "HEAD").stdout.strip()

    def branch(self) -> str | None:
        result = self._run("symbolic-ref", "--short", "-q", "HEAD", check=False)
        return result.stdout.strip() or None

    def status_porcelain(self) -> str:
        return self._run("status", "--porcelain=v1").stdout

    def checkpoint(self) -> GitCheckpoint:
        return GitCheckpoint(self.head_sha(), self.branch(), bool(self.status_porcelain().strip()))

    def ensure_clean(self) -> None:
        status = self.status_porcelain().strip()
        if status:
            raise RuntimeError(
                "source checkout has uncommitted changes; commit/stash them or use an isolated copy"
            )

    def diff(self, base: str = "HEAD") -> str:
        return self._run("diff", "--binary", base).stdout

    def write_patch(self, path: str | Path, base: str = "HEAD") -> Path:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(self.diff(base), encoding="utf-8")
        return output

    @contextlib.contextmanager
    def isolated_worktree(self, ref: str = "HEAD") -> Iterator[Path]:
        """Create a detached temporary worktree and remove it on exit."""
        self.ensure_clean()
        temp_root = Path(tempfile.mkdtemp(prefix="agentic-migrator-"))
        worktree = temp_root / "repo"
        try:
            self._run("worktree", "add", "--detach", str(worktree), ref)
            yield worktree
        finally:
            self._run("worktree", "remove", "--force", str(worktree), check=False)
            shutil.rmtree(temp_root, ignore_errors=True)

    def apply_patch(self, patch_path: str | Path, *, check_only: bool = False) -> None:
        command = ["apply", "--check"] if check_only else ["apply", "--3way"]
        self._run(*command, str(Path(patch_path).resolve()))

    def hard_reset(self, checkpoint: GitCheckpoint, *, allow_destructive: bool = False) -> None:
        if not allow_destructive:
            raise RuntimeError("hard reset is destructive; pass allow_destructive=True explicitly")
        self._run("reset", "--hard", checkpoint.sha)
        self._run("clean", "-fd")
