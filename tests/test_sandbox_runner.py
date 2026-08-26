from migrator.models import FailureKind
from migrator.runners import PytestSandboxRunner
from migrator.sandbox import LocalSandbox


def test_sandboxed_pytest_runner_passes_candidate_without_touching_checkout():
    runner = PytestSandboxRunner(LocalSandbox(timeout_seconds=10))
    candidate = "def add(a, b):\n    return a + b\n"
    tests = (
        "from candidate import add\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
    )

    result = runner(candidate, tests)
    assert result.passed
    assert result.kind is FailureKind.UNKNOWN


def test_sandboxed_pytest_runner_classifies_assertion_failure():
    runner = PytestSandboxRunner(LocalSandbox(timeout_seconds=10))
    candidate = "def add(a, b):\n    return a - b\n"
    tests = (
        "from candidate import add\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
    )

    result = runner(candidate, tests)
    assert not result.passed
    assert result.kind is FailureKind.ASSERTION
