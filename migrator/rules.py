from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX platforms.
    fcntl = None  # type: ignore[assignment]

from .models import Rule

_THREAD_LOCKS: dict[Path, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock_for(path: Path) -> threading.RLock:
    resolved = path.resolve()
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(resolved, threading.RLock())


class RuleStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        self._thread_lock = _thread_lock_for(self.path)
        with self._locked(exclusive=True):
            if not self.path.exists():
                self._save_unlocked([])

    def load(self) -> list[Rule]:
        with self._locked(exclusive=False):
            return self._load_unlocked()

    def _load_unlocked(self) -> list[Rule]:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return [Rule(**item) for item in payload]

    def save(self, rules: list[Rule]) -> None:
        with self._locked(exclusive=True):
            self._save_unlocked(rules)

    def _save_unlocked(self, rules: list[Rule]) -> None:
        payload = json.dumps([asdict(rule) for rule in rules], indent=2)
        existing_mode = (
            stat.S_IMODE(self.path.stat().st_mode) if self.path.exists() else 0o644
        )
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            os.chmod(temporary_path, existing_mode)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            self._sync_parent_directory()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary_path.unlink(missing_ok=True)

    def upsert(self, rule: Rule) -> None:
        with self._locked(exclusive=True):
            rules = self._load_unlocked()
            by_id = {item.id: item for item in rules}
            by_id[rule.id] = rule
            self._save_unlocked(list(by_id.values()))

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        """Serialize store access in-process and, on POSIX, between processes."""

        with self._thread_lock:
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                if fcntl is not None:
                    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                    fcntl.flock(descriptor, operation)
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def _sync_parent_directory(self) -> None:
        try:
            descriptor = os.open(self.path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            # Some filesystems/platforms do not support directory fsync.
            pass
        finally:
            os.close(descriptor)


def apply_rules(source: str, rules: list[Rule]) -> tuple[str, list[str]]:
    output = source
    applied: list[str] = []
    for rule in rules:
        if rule.pattern in output:
            output = output.replace(rule.pattern, rule.replacement)
            applied.append(rule.id)
    return output, applied
