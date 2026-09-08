from __future__ import annotations

import shlex
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .sandbox import LocalSandbox, SandboxResult


@dataclass(frozen=True)
class VerificationStep:
    name: str
    command: tuple[str, ...]

    @classmethod
    def parse(cls, raw: str, index: int) -> VerificationStep:
        command = tuple(shlex.split(raw))
        if not command:
            raise ValueError("verification command cannot be empty")
        return cls(name=f"verification-{index:02d}", command=command)


@dataclass
class VerificationReport:
    steps: list[dict[str, object]] = field(default_factory=list)
    stopped_early: bool = False

    @property
    def passed(self) -> bool:
        return bool(self.steps) and all(bool(step["passed"]) for step in self.steps)

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "stopped_early": self.stopped_early,
            "steps": self.steps,
        }


class RepositoryVerificationSuite:
    """Execute ordered, shell-free validation steps against a migrated worktree."""

    def __init__(
        self,
        commands: list[str],
        *,
        timeout_seconds: float = 120.0,
        max_output_bytes: int = 256_000,
        allowed_executables: tuple[str, ...] = (
            "python",
            "python3",
            "pytest",
            "ruff",
            "npm",
            "go",
            "cargo",
            "mvn",
            "gradle",
        ),
    ) -> None:
        self.steps = [VerificationStep.parse(raw, index) for index, raw in enumerate(commands, 1)]
        self.runner = LocalSandbox(
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            allowed_executables=allowed_executables,
        )

    @staticmethod
    def _step_result(step: VerificationStep, result: SandboxResult) -> dict[str, object]:
        payload = asdict(result)
        payload["name"] = step.name
        payload["passed"] = result.passed
        return payload

    def run(self, worktree: str | Path) -> VerificationReport:
        report = VerificationReport()
        for index, step in enumerate(self.steps):
            result = self.runner.run_in_directory(step.command, worktree)
            report.steps.append(self._step_result(step, result))
            if not result.passed:
                report.stopped_early = index < len(self.steps) - 1
                break
        return report
