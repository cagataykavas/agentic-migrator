from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Iterable


def digest(content: bytes) -> str:
    return sha256(content).hexdigest()


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    existed: bool
    sha256: str | None
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class FileChange:
    path: str
    operation: str
    before_sha256: str | None
    after_sha256: str | None
    bytes_before: int
    bytes_after: int


@dataclass
class MigrationManifest:
    root: str
    snapshots: list[FileSnapshot] = field(default_factory=list)
    changes: list[FileChange] = field(default_factory=list)

    @property
    def changed_files(self) -> int:
        return len(self.changes)


class MigrationWorkspace:
    """Apply a bounded file plan while retaining enough information to roll it back.

    Paths are resolved relative to one repository root and attempts to escape that root
    are rejected. The implementation is intentionally filesystem-local; Git commits and
    pull requests can sit one layer above it.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def _resolve(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"path escapes migration root: {relative_path}") from exc
        return candidate

    def snapshot(self, paths: Iterable[str]) -> MigrationManifest:
        manifest = MigrationManifest(root=str(self.root))
        for relative in sorted(set(paths)):
            path = self._resolve(relative)
            if path.exists():
                if not path.is_file():
                    raise ValueError(f"only files can be snapshotted: {relative}")
                content = path.read_bytes()
                manifest.snapshots.append(
                    FileSnapshot(
                        path=relative,
                        existed=True,
                        sha256=digest(content),
                        content=content,
                    )
                )
            else:
                manifest.snapshots.append(
                    FileSnapshot(path=relative, existed=False, sha256=None, content=b"")
                )
        return manifest

    def apply_text_plan(
        self,
        updates: dict[str, str | None],
        *,
        encoding: str = "utf-8",
    ) -> MigrationManifest:
        """Apply a plan where text means create/update and None means delete."""
        manifest = self.snapshot(updates.keys())
        snapshots = {snapshot.path: snapshot for snapshot in manifest.snapshots}

        for relative, new_text in updates.items():
            path = self._resolve(relative)
            before = snapshots[relative]

            if new_text is None:
                if path.exists():
                    path.unlink()
                after_content = b""
                operation = "delete" if before.existed else "noop"
                after_hash = None
            else:
                after_content = new_text.encode(encoding)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(after_content)
                after_hash = digest(after_content)
                if not before.existed:
                    operation = "create"
                elif before.sha256 == after_hash:
                    operation = "noop"
                else:
                    operation = "update"

            if operation != "noop":
                manifest.changes.append(
                    FileChange(
                        path=relative,
                        operation=operation,
                        before_sha256=before.sha256,
                        after_sha256=after_hash,
                        bytes_before=len(before.content),
                        bytes_after=len(after_content),
                    )
                )
        return manifest

    def rollback(self, manifest: MigrationManifest) -> None:
        if Path(manifest.root).resolve() != self.root:
            raise ValueError("manifest belongs to a different workspace")

        for snapshot in manifest.snapshots:
            path = self._resolve(snapshot.path)
            if snapshot.existed:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(snapshot.content)
            elif path.exists():
                path.unlink()

    def verify_manifest(self, manifest: MigrationManifest) -> dict[str, bool]:
        """Check whether current files still match the manifest's post-change hashes."""
        expected = {change.path: change.after_sha256 for change in manifest.changes}
        result: dict[str, bool] = {}
        for relative, expected_hash in expected.items():
            path = self._resolve(relative)
            if expected_hash is None:
                result[relative] = not path.exists()
                continue
            result[relative] = path.is_file() and digest(path.read_bytes()) == expected_hash
        return result
