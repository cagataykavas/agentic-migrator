from pathlib import Path

import pytest

from migrator.sandbox import SandboxViolation
from migrator.verification import RepositoryVerificationSuite, VerificationStep


def test_verification_suite_runs_in_order_and_captures_evidence(tmp_path: Path) -> None:
    (tmp_path / "candidate.py").write_text("VALUE = 2\n", encoding="utf-8")
    suite = RepositoryVerificationSuite(
        [
            'python -c "from candidate import VALUE; assert VALUE == 2"',
            "python -m compileall -q candidate.py",
        ],
        timeout_seconds=5,
    )

    report = suite.run(tmp_path)

    assert report.passed is True
    assert len(report.steps) == 2
    assert all(step["passed"] for step in report.steps)
    assert Path(report.steps[0]["command"][0]).name == "python"


def test_verification_suite_stops_after_failure(tmp_path: Path) -> None:
    suite = RepositoryVerificationSuite(
        [
            'python -c "raise SystemExit(3)"',
            'python -c "open(\'should-not-exist\', \'w\').close()"',
        ]
    )

    report = suite.run(tmp_path)

    assert report.passed is False
    assert report.stopped_early is True
    assert len(report.steps) == 1
    assert report.steps[0]["returncode"] == 3
    assert not (tmp_path / "should-not-exist").exists()


def test_verification_timeout_is_a_structured_failure(tmp_path: Path) -> None:
    suite = RepositoryVerificationSuite(
        ['python -c "import time; time.sleep(2)"'], timeout_seconds=0.05
    )

    report = suite.run(tmp_path)

    assert report.passed is False
    assert report.steps[0]["timed_out"] is True
    assert report.steps[0]["returncode"] == 124


def test_verification_rejects_non_allowlisted_executable(tmp_path: Path) -> None:
    suite = RepositoryVerificationSuite(["bash -c true"])

    with pytest.raises(SandboxViolation, match="not allowed"):
        suite.run(tmp_path)


def test_verification_rejects_allowlisted_basename_with_attacker_path(tmp_path: Path) -> None:
    fake_python = tmp_path / "python"
    fake_python.write_text("malicious", encoding="utf-8")
    suite = RepositoryVerificationSuite([f"{fake_python} -V"])

    with pytest.raises(SandboxViolation, match="trusted allowlisted binary"):
        suite.run(tmp_path)


def test_empty_verification_command_is_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        VerificationStep.parse("   ", 1)
