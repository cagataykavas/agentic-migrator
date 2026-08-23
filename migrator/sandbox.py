from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import subprocess
import tempfile
import time
from typing import Mapping, Sequence


@dataclass(frozen=True)
class SandboxResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool

    @property
    def passed(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class LocalSandbox:
    """Run validation commands with bounded time and an isolated temporary cwd.

    This is a portfolio reference adapter, not a hardened hostile-code sandbox.
    Production execution should use stronger isolation such as a locked-down
    container, VM, seccomp profile or dedicated remote worker pool.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        environment_allowlist: Sequence[str] = ("PATH", "SYSTEMROOT", "WINDIR"),
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.environment_allowlist = tuple(environment_allowlist)

    def _environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key in self.environment_allowlist
        }
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if extra:
            env.update({str(key): str(value) for key, value in extra.items()})
        return env

    def run(
        self,
        command: Sequence[str],
        *,
        files: Mapping[str, str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> SandboxResult:
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="agentic-migrator-") as tmp:
            root = Path(tmp)
            for relative, content in (files or {}).items():
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")

            try:
                process = subprocess.run(
                    list(command),
                    cwd=root,
                    env=self._environment(env),
                    text=True,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
                return SandboxResult(
                    command=tuple(command),
                    returncode=process.returncode,
                    stdout=process.stdout,
                    stderr=process.stderr,
                    duration_seconds=time.perf_counter() - started,
                    timed_out=False,
                )
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
                return SandboxResult(
                    command=tuple(command),
                    returncode=124,
                    stdout=stdout,
                    stderr=stderr,
                    duration_seconds=time.perf_counter() - started,
                    timed_out=True,
                )
