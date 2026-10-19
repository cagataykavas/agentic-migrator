from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ._output_capture import capture_process


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
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive and finite")
        if (
            isinstance(max_output_bytes, bool)
            or not isinstance(max_output_bytes, int)
            or max_output_bytes < 1
        ):
            raise ValueError("max_output_bytes must be a positive integer")
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
        sibling = Path(sys.executable).with_name(executable)
        resolved = sibling if sibling.is_file() else shutil.which(executable)
        if resolved is None:
            raise SandboxViolation(f"allowed executable {executable!r} is not installed")
        if (
            normalized[0] != executable
            and Path(normalized[0]).resolve() != Path(resolved).resolve()
        ):
            raise SandboxViolation("executable path does not match the trusted allowlisted binary")
        normalized = (str(resolved), *normalized[1:])
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

    def run(
        self,
        command: Sequence[str],
        *,
        files: Mapping[str, str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> SandboxResult:
        argv = self._validate_command(command)
        with tempfile.TemporaryDirectory(prefix="agentic-migrator-") as tmp:
            root = Path(tmp).resolve()
            for relative, content in (files or {}).items():
                safe_relative = self._safe_relative_path(relative)
                destination = (root / safe_relative).resolve()
                if not destination.is_relative_to(root):
                    raise SandboxViolation(f"sandbox path escapes temporary root: {relative!r}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")

            return self.run_in_directory(argv, root, env=env)

    def run_in_directory(
        self,
        command: Sequence[str],
        directory: str | Path,
        *,
        env: Mapping[str, str] | None = None,
    ) -> SandboxResult:
        """Run a bounded command in an existing isolated repository directory."""
        argv = self._validate_command(command)
        root = Path(directory).resolve()
        if not root.is_dir():
            raise SandboxViolation(f"verification directory does not exist: {root}")
        started = time.perf_counter()
        process = subprocess.Popen(
            argv,
            cwd=root,
            env=self._environment(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            shell=False,
            start_new_session=os.name == "posix",
        )
        returncode, stdout, stderr, timed_out, truncated = capture_process(
            process,
            timeout_seconds=self.policy.timeout_seconds,
            max_output_bytes=self.policy.max_output_bytes,
        )
        return SandboxResult(
            command=argv,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=time.perf_counter() - started,
            timed_out=timed_out,
            output_truncated=truncated,
        )
