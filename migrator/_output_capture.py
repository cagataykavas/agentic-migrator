"""Bounded pipe capture for the local validation subprocess boundary."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from typing import BinaryIO


class BoundedOutput:
    """Drain a binary stream while retaining at most ``limit`` raw bytes.

    Discarding excess bytes still drains the pipe, preventing child processes
    from deadlocking when stdout and stderr both exceed the pipe capacity.
    Each instance has exactly one writer thread; callers inspect it after join.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.prefix = bytearray()
        self.truncated = False
        self.error: OSError | None = None

    def drain(self, stream: BinaryIO) -> None:
        try:
            while block := stream.read(64 * 1024):
                remaining = self.limit - len(self.prefix)
                self.prefix.extend(block[:remaining])
                if len(block) > remaining:
                    self.truncated = True
        except OSError as error:
            self.error = error
        finally:
            stream.close()

    def text(self) -> str:
        value = self.prefix.decode("utf-8", errors="replace")
        # Preserve the universal-newline behavior of the former text=True path.
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        if self.truncated:
            value += "\n...[output truncated by sandbox]"
        return value


def capture_process(
    process: subprocess.Popen,
    *,
    timeout_seconds: float,
    max_output_bytes: int,
) -> tuple[int, str, str, bool, bool]:
    """Capture both pipes within one deadline, including inherited-pipe EOF.

    On POSIX the caller must start a new session. The whole process group is
    killed on timeout so ordinary descendants cannot keep the pipes open.
    Other platforms retain direct-child termination only.
    """
    outputs = [BoundedOutput(max_output_bytes), BoundedOutput(max_output_bytes)]
    threads = [
        threading.Thread(target=output.drain, args=(stream,), daemon=True)
        for output, stream in zip(outputs, (process.stdout, process.stderr), strict=True)
    ]
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    for thread in threads:
        thread.start()
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        timed_out = any(thread.is_alive() for thread in threads)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        if timed_out or process.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            process.wait()
        for thread in threads:
            thread.join(timeout=1.0)

    if any(thread.is_alive() for thread in threads):
        # Escaped descendants can outlive a local process group. Do not block
        # forever or issue a success result from incomplete capture.
        raise RuntimeError("sandbox output pipes did not close after termination")
    for output in outputs:
        if output.error is not None:
            raise output.error
    return (
        124 if timed_out else process.returncode,
        outputs[0].text(),
        outputs[1].text(),
        timed_out,
        any(output.truncated for output in outputs),
    )
