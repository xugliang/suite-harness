from __future__ import annotations

import asyncio
import sys

from suiteharness.sandbox import AsyncioProcessTransport


def test_process_transport_captures_stdout_stderr_and_stdin() -> None:
    script = (
        "import sys; data=sys.stdin.buffer.read(); "
        "sys.stdout.buffer.write(data); sys.stderr.write('err')"
    )
    result = asyncio.run(
        AsyncioProcessTransport().execute(
            (sys.executable, "-c", script),
            working_directory=None,
            environment={},
            stdin=b"hello",
            timeout_seconds=5,
            output_bytes=1024,
        )
    )

    assert result.exit_code == 0
    assert result.stdout == b"hello"
    assert result.stderr == b"err"
    assert not result.timed_out


def test_process_transport_enforces_a_combined_output_limit() -> None:
    result = asyncio.run(
        AsyncioProcessTransport().execute(
            (sys.executable, "-c", "print('x' * 10000)"),
            working_directory=None,
            environment={},
            stdin=None,
            timeout_seconds=5,
            output_bytes=1024,
        )
    )

    assert len(result.stdout) + len(result.stderr) == 1024
    assert result.output_truncated


def test_process_transport_terminates_on_timeout() -> None:
    result = asyncio.run(
        AsyncioProcessTransport().execute(
            (sys.executable, "-c", "import time; time.sleep(30)"),
            working_directory=None,
            environment={},
            stdin=None,
            timeout_seconds=0.05,
            output_bytes=1024,
        )
    )

    assert result.exit_code is None
    assert result.timed_out
