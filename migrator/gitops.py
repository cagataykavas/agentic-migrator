from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import difflib
import hashlib
from typing import Mapping


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    sha256: str
    content: str


@dataclass(frozen=True)
class ProposedChange:
    path: str
    before_sha256: str
    after_sha256: str
    diff: str


class MigrationWorkspace:
    """In-memory change set with explicit before/after hashes and rollback support."""

    def __init__(self, files: Mapping[str, str]) -> None:
        self._original = dict(files)
        self._current = dict(files)

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @classmethod
    def from_directory(cls, root: str | Path, pattern: str = "*.py") -> "MigrationWorkspace":
        root_path = Path(root)
        files = {
            path.relative_to(root_path).as_posix(): path.read_text(encoding="utf-8")
            for path in root_path.rglob(pattern)
            if ".git" not in path.parts and ".venv" not in path.parts
        }
        return cls(files)

    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._current))

    def read(self, path: str) -> str:
        return self._current[path]

    def write(self, path: str, content: str) -> None:
        if path not in self._current:
            raise KeyError(f"unknown migration path: {path}")
        self._current[path] = content

    def rollback(self, path: str | None = None) -> None:
        if path is None:
            self._current = dict(self._original)
            return
        self._current[path] = self._original[path]

    def snapshot(self, path: str) -> FileSnapshot:
        content = self._current[path]
        return FileSnapshot(path, self._hash(content), content)

    def proposed_changes(self) -> list[ProposedChange]:
        changes: list[ProposedChange] = []
        for path in self.paths():
            before = self._original[path]
            after = self._current[path]
            if before == after:
                continue
            diff = "".join(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                )
            )
            changes.append(
                ProposedChange(
                    path=path,
                    before_sha256=self._hash(before),
                    after_sha256=self._hash(after),
                    diff=diff,
                )
            )
        return changes

    def materialize(self, root: str | Path) -> None:
        root_path = Path(root)
        for path, content in self._current.items():
            destination = root_path / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
