from __future__ import annotations

import os
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SandboxPolicy:
    timeout_seconds: float = 20.0
    max_output_bytes: int = 256_000
    allowed_executables: tuple[str, ...] = (
        "python",
        "python3",
        "pytest",
        "ruff",
    )
    environment_allowlist: tuple[str, ...] = (
        "PATH",
        "PYTHONPATH",
        "HOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SYSTEMROOT",
        "WINDIR",
    )


@dataclass(frozen=True)
class SandboxResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
    output_truncated: bool = False

    @property
    def passed(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class SandboxViolation(RuntimeError):
    pass


class LocalSandbox:
    """Run validation commands with bounded resources in a temporary cwd.

    The runner never invokes a shell, strips most inherited environment variables,
    enforces an executable allowlist, validates materialized paths, applies a timeout,
    and caps captured output.

    This remains a portfolio reference process boundary rather than a hardened
    hostile-code sandbox. Truly untrusted migrations should run inside a locked-down
    container, VM, seccomp profile, or dedicated remote worker pool.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        environment_allowlist: Sequence[str] | None = None,
        allowed_executables: Sequence[str] | None = None,
        max_output_bytes: int = 256_000,
    ) -> None:
        defaults = SandboxPolicy()
        self.policy = SandboxPolicy(
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            allowed_executables=tuple(allowed_executables or defaults.allowed_executables),
            environment_allowlist=tuple(environment_allowlist or defaults.environment_allowlist),
        )

    @staticmethod
    def _safe_relative_path(raw: str) -> Path:
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts:
            raise SandboxViolation(f"unsafe sandbox path: {raw!r}")
        if not path.parts:
            raise SandboxViolation("sandbox path cannot be empty")
        return path

    def _validate_command(self, command: Sequence[str]) -> tuple[str, ...]:
        if not command:
            raise SandboxViolation("command cannot be empty")
        normalized = tuple(str(item) for item in command)
        executable = Path(normalized[0]).name
        if executable not in self.policy.allowed_executables:
            allowed = ", ".join(self.policy.allowed_executables)
            raise SandboxViolation(f"executable {executable!r} is not allowed; allowed: {allowed}")
        return normalized

    def _environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key in self.policy.environment_allowlist
        }
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"

        if extra:
            forbidden = sorted(set(extra) - set(self.policy.environment_allowlist))
            if forbidden:
                raise SandboxViolation(
                    "environment override contains non-allowlisted keys: " + ", ".join(forbidden)
                )
            env.update({str(key): str(value) for key, value in extra.items()})
        return env

    def _truncate(self, text: str) -> tuple[str, bool]:
        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) <= self.policy.max_output_bytes:
            return text, False
        clipped = encoded[: self.policy.max_output_bytes].decode("utf-8", errors="replace")
        return clipped + "\n...[output truncated by sandbox]", True

    def run(
        self,
        command: Sequence[str],
        *,
        files: Mapping[str, str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> SandboxResult:
        argv = self._validate_command(command)
        started = time.perf_counter()

        with tempfile.TemporaryDirectory(prefix="agentic-migrator-") as tmp:
            root = Path(tmp).resolve()
            for relative, content in (files or {}).items():
                safe_relative = self._safe_relative_path(relative)
                destination = (root / safe_relative).resolve()
                if not destination.is_relative_to(root):
                    raise SandboxViolation(f"sandbox path escapes temporary root: {relative!r}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")

            try:
                process = subprocess.run(
                    argv,
                    cwd=root,
                    env=self._environment(env),
                    text=True,
                    capture_output=True,
                    shell=False,
                    timeout=self.policy.timeout_seconds,
                    check=False,
                )
                stdout, stdout_truncated = self._truncate(process.stdout)
                stderr, stderr_truncated = self._truncate(process.stderr)
                return SandboxResult(
                    command=argv,
                    returncode=process.returncode,
                    stdout=stdout,
                    stderr=stderr,
                    duration_seconds=time.perf_counter() - started,
                    timed_out=False,
                    output_truncated=stdout_truncated or stderr_truncated,
                )
            except subprocess.TimeoutExpired as exc:
                stdout_value = exc.stdout or ""
                stderr_value = exc.stderr or ""
                if isinstance(stdout_value, bytes):
                    stdout_value = stdout_value.decode("utf-8", errors="replace")
                if isinstance(stderr_value, bytes):
                    stderr_value = stderr_value.decode("utf-8", errors="replace")
                stdout, stdout_truncated = self._truncate(stdout_value)
                stderr, stderr_truncated = self._truncate(stderr_value)
                return SandboxResult(
                    command=argv,
                    returncode=124,
                    stdout=stdout,
                    stderr=stderr,
                    duration_seconds=time.perf_counter() - started,
                    timed_out=True,
                    output_truncated=stdout_truncated or stderr_truncated,
                )
