from __future__ import annotations

from dataclasses import dataclass

from .models import FailureKind, TestResult
from .sandbox import LocalSandbox


@dataclass(frozen=True)
class PytestSandboxRunner:
    """Adapt the bounded local sandbox to MigrationEngine's TestRunner contract.

    Candidate source is exposed as ``candidate.py`` and the active harness as
    ``test_candidate.py``. The test source can therefore import the migrated module
    with ``from candidate import ...`` without touching the developer checkout.
    """

    sandbox: LocalSandbox
    pytest_args: tuple[str, ...] = ("-q", "test_candidate.py")

    def __call__(self, candidate_source: str, test_source: str) -> TestResult:
        result = self.sandbox.run(
            ["pytest", *self.pytest_args],
            files={
                "candidate.py": candidate_source,
                "test_candidate.py": test_source,
            },
        )
        combined = f"{result.stdout}\n{result.stderr}"
        return TestResult(
            passed=result.passed,
            name="sandboxed_pytest",
            stdout=result.stdout,
            stderr=result.stderr,
            kind=self._classify(combined, timed_out=result.timed_out),
        )

    @staticmethod
    def _classify(output: str, *, timed_out: bool) -> FailureKind:
        if timed_out:
            return FailureKind.RUNTIME
        if "SyntaxError" in output or "IndentationError" in output:
            return FailureKind.SYNTAX
        if "ModuleNotFoundError" in output or "ImportError" in output:
            return FailureKind.IMPORT
        if "fixture" in output and "not found" in output:
            return FailureKind.TEST_HARNESS
        if "AssertionError" in output or " failed" in output.lower():
            return FailureKind.ASSERTION
        if "ERROR" in output or "Traceback" in output:
            return FailureKind.RUNTIME
        return FailureKind.UNKNOWN
