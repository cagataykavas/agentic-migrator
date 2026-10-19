import io
import os
import sys
import tracemalloc
from pathlib import Path

import pytest

from migrator._output_capture import BoundedOutput
from migrator.sandbox import LocalSandbox
from migrator.verification import RepositoryVerificationSuite


def sandbox(**changes):
    return LocalSandbox(
        allowed_executables=(Path(sys.executable).name,),
        **changes,
    )


def test_capture_retains_bounded_memory_while_draining_large_stream():
    stream = io.BytesIO(b"x" * (16 * 1024 * 1024))
    capture = BoundedOutput(128)
    tracemalloc.start()
    try:
        capture.drain(stream)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert bytes(capture.prefix) == b"x" * 128
    assert capture.truncated is True
    assert peak < 512 * 1024
    assert stream.closed


@pytest.mark.parametrize("size,truncated", [(63, False), (64, False), (65, True)])
def test_output_limit_boundary(size, truncated):
    result = sandbox(max_output_bytes=64).run(
        [sys.executable, "-c", f"import os; os.write(1, b'x' * {size})"]
    )

    assert result.passed
    assert result.output_truncated is truncated
    assert result.stdout.split("\n")[0] == "x" * min(size, 64)


def test_both_large_pipes_are_drained_without_deadlock(tmp_path):
    result = sandbox(max_output_bytes=128, timeout_seconds=10).run_in_directory(
        [
            sys.executable,
            "-c",
            (
                "import os; "
                "[(os.write(1, b'o' * 65536), os.write(2, b'e' * 65536)) "
                "for _ in range(128)]"
            ),
        ],
        tmp_path,
    )

    assert result.passed
    assert result.output_truncated
    assert result.stdout == "o" * 128 + "\n...[output truncated by sandbox]"
    assert result.stderr == "e" * 128 + "\n...[output truncated by sandbox]"


def test_nonzero_exit_is_preserved_with_truncated_output():
    result = sandbox(max_output_bytes=64).run(
        [sys.executable, "-c", "print('x'*1000); raise SystemExit(7)"]
    )
    assert result.returncode == 7
    assert not result.passed
    assert result.output_truncated


def test_timeout_preserves_partial_output_and_kills_child():
    result = sandbox(max_output_bytes=64, timeout_seconds=0.5).run(
        [sys.executable, "-c", "import time; print('before-timeout'); time.sleep(30)"]
    )
    assert result.returncode == 124
    assert result.timed_out
    assert not result.passed
    assert "before-timeout" in result.stdout
    assert result.duration_seconds < 5


@pytest.mark.skipif(os.name != "posix", reason="process group cleanup is POSIX-specific")
def test_descendant_inheriting_pipes_cannot_extend_deadline():
    result = sandbox(max_output_bytes=64, timeout_seconds=0.5).run(
        [
            sys.executable,
            "-c",
            (
                "import subprocess,sys; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                "print('parent-finished')"
            ),
        ]
    )
    assert result.timed_out
    assert result.returncode == 124
    assert "parent-finished" in result.stdout
    assert result.duration_seconds < 5


def test_invalid_utf8_is_replaced_instead_of_raising():
    result = sandbox(max_output_bytes=64).run(
        [sys.executable, "-c", "import os; os.write(1, b'\\xff\\xfeok')"]
    )
    assert result.passed
    assert result.stdout == "\ufffd\ufffdok"


def test_universal_newline_behavior_is_preserved():
    result = sandbox().run([sys.executable, "-c", "import os; os.write(1, b'a\\r\\nb\\rc\\n')"])
    assert result.passed
    assert result.stdout == "a\nb\nc\n"


def test_empty_output_is_successful_without_truncation():
    result = sandbox().run([sys.executable, "-c", "pass"])
    assert result.passed
    assert result.stdout == result.stderr == ""
    assert not result.output_truncated


def test_incomplete_utf8_prefix_has_explicit_truncation():
    result = sandbox(max_output_bytes=2).run(
        [sys.executable, "-c", "import os; os.write(1, '€'.encode())"]
    )
    assert result.passed
    assert result.output_truncated
    assert result.stdout == "\ufffd\n...[output truncated by sandbox]"


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_timeout_policy_must_be_positive_and_finite(timeout):
    with pytest.raises(ValueError, match="timeout_seconds"):
        sandbox(timeout_seconds=timeout)


@pytest.mark.parametrize("limit", [0, -1, 1.5, True])
def test_output_policy_must_be_positive_integer(limit):
    with pytest.raises(ValueError, match="max_output_bytes"):
        sandbox(max_output_bytes=limit)


def test_verification_report_propagates_truncated_evidence(tmp_path):
    suite = RepositoryVerificationSuite(["python -c \"print('x'*1000)\""], max_output_bytes=64)
    report = suite.run(tmp_path)

    assert report.passed
    assert report.steps[0]["output_truncated"] is True
    assert len(report.steps[0]["stdout"]) < 128
