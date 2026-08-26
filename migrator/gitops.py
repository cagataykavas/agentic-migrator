from __future__ import annotations

import difflib
import hashlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    sha256: str
    content: str


@dataclass(frozen=True)
class ProposedChange:
    path: str
    operation: str
    before_sha256: str | None
    after_sha256: str | None
    diff: str

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


class MigrationWorkspace:
    """In-memory repository change set with hashes, diffs, and rollback support."""

    def __init__(self, files: Mapping[str, str]) -> None:
        normalized = {self._normalize_path(path): content for path, content in files.items()}
        self._original = dict(normalized)
        self._current = dict(normalized)

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_path(raw: str) -> str:
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError(f"unsafe migration path: {raw!r}")
        normalized = path.as_posix()
        if normalized in {".", ""}:
            raise ValueError(f"unsafe migration path: {raw!r}")
        return normalized

    @staticmethod
    def _destination(root: Path, relative: str) -> Path:
        destination = (root / relative).resolve()
        if not destination.is_relative_to(root):
            raise ValueError(f"migration path escapes destination root: {relative!r}")
        return destination

    @classmethod
    def from_directory(cls, root: str | Path, pattern: str = "*.py") -> MigrationWorkspace:
        root_path = Path(root).resolve()
        files = {
            path.relative_to(root_path).as_posix(): path.read_text(encoding="utf-8")
            for path in root_path.rglob(pattern)
            if ".git" not in path.parts and ".venv" not in path.parts
        }
        return cls(files)

    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._current))

    def read(self, path: str) -> str:
        return self._current[self._normalize_path(path)]

    def write(self, path: str, content: str) -> None:
        normalized = self._normalize_path(path)
        if normalized not in self._current:
            raise KeyError(f"unknown migration path: {normalized}")
        self._current[normalized] = content

    def create(self, path: str, content: str) -> None:
        normalized = self._normalize_path(path)
        if normalized in self._current or normalized in self._original:
            raise FileExistsError(f"migration path already exists: {normalized}")
        self._current[normalized] = content

    def delete(self, path: str) -> None:
        normalized = self._normalize_path(path)
        if normalized not in self._current:
            raise KeyError(f"unknown migration path: {normalized}")
        del self._current[normalized]

    def rollback(self, path: str | None = None) -> None:
        if path is None:
            self._current = dict(self._original)
            return

        normalized = self._normalize_path(path)
        if normalized in self._original:
            self._current[normalized] = self._original[normalized]
        else:
            self._current.pop(normalized, None)

    def snapshot(self, path: str) -> FileSnapshot:
        normalized = self._normalize_path(path)
        content = self._current[normalized]
        return FileSnapshot(normalized, self._hash(content), content)

    @staticmethod
    def _diff(path: str, before: str, after: str) -> str:
        return "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )

    def proposed_changes(self) -> list[ProposedChange]:
        changes: list[ProposedChange] = []
        all_paths = sorted(set(self._original) | set(self._current))

        for path in all_paths:
            before = self._original.get(path)
            after = self._current.get(path)
            if before == after:
                continue

            if before is None and after is not None:
                operation = "create"
                before_sha = None
                after_sha = self._hash(after)
                diff = self._diff(path, "", after)
            elif before is not None and after is None:
                operation = "delete"
                before_sha = self._hash(before)
                after_sha = None
                diff = self._diff(path, before, "")
            else:
                operation = "update"
                assert before is not None and after is not None
                before_sha = self._hash(before)
                after_sha = self._hash(after)
                diff = self._diff(path, before, after)

            changes.append(
                ProposedChange(
                    path=path,
                    operation=operation,
                    before_sha256=before_sha,
                    after_sha256=after_sha,
                    diff=diff,
                )
            )
        return changes

    def manifest(self) -> dict[str, object]:
        changes = self.proposed_changes()
        return {
            "changed_files": len(changes),
            "creates": sum(item.operation == "create" for item in changes),
            "updates": sum(item.operation == "update" for item in changes),
            "deletes": sum(item.operation == "delete" for item in changes),
            "changes": [item.as_dict() for item in changes],
        }

    def materialize(self, root: str | Path) -> None:
        root_path = Path(root).resolve()
        root_path.mkdir(parents=True, exist_ok=True)

        for path in sorted(set(self._original) - set(self._current)):
            destination = self._destination(root_path, path)
            if destination.exists() and destination.is_file():
                destination.unlink()

        for path, content in self._current.items():
            destination = self._destination(root_path, path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
