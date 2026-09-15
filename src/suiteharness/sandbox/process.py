"""Bounded asyncio subprocess supervision for sandbox backends."""

from __future__ import annotations

from suiteharness.sandbox.protocols import SandboxProcessTransport

import asyncio
import contextlib
import os
import signal
import subprocess
from collections.abc import Mapping
from pathlib import Path

from .models import ProcessResult, SandboxUnavailable
from .protocols import SandboxSession

_READ_CHUNK_BYTES = 64 * 1024
_PROBE_OUTPUT_BYTES = 64 * 1024


class _OutputBudget:
    def __init__(self, limit: int) -> None:
        self._remaining = limit
        self._lock = asyncio.Lock()
        self.truncated = False

    async def keep(self, chunk: bytes) -> bytes:
        async with self._lock:
            if not chunk:
                return b""
            take = min(len(chunk), self._remaining)
            self._remaining -= take
            if take != len(chunk):
                self.truncated = True
            return chunk[:take]


async def _drain(
    stream: asyncio.StreamReader | None,
    budget: _OutputBudget,
) -> bytes:
    if stream is None:
        return b""
    chunks: list[bytes] = []
    while True:
        chunk = await stream.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        kept = await budget.keep(chunk)
        if kept:
            chunks.append(kept)
    return b"".join(chunks)


async def _feed_stdin(process: asyncio.subprocess.Process, data: bytes | None) -> None:
    if process.stdin is None:
        return
    try:
        if data:
            process.stdin.write(data)
            await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        process.stdin.close()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await process.stdin.wait_closed()


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name != "nt":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    else:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
        return
    except TimeoutError:
        pass
    if os.name != "nt":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    else:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    with contextlib.suppress(ProcessLookupError):
        await process.wait()


class AsyncioProcessTransport(SandboxProcessTransport):
    """Execute argv directly, drain both pipes, and enforce time/output bounds.

    This supervisor is trusted host code. It never invokes a command shell and
    treats ``environment`` as an overlay on the service process environment.
    """

    async def open_session(self, argv: tuple[str, ...], *, environment: Mapping[str, str],
                           timeout_seconds: float, output_bytes: int) -> SandboxSession:
        process_environment = os.environ.copy()
        process_environment.update(environment)
        options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                   else {"start_new_session": True})
        process = await asyncio.create_subprocess_exec(
            *argv, env=process_environment, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=output_bytes + 1, **options,
        )
        return _PersistentProcessSession(process, timeout_seconds, output_bytes)

    async def probe(self, argv: tuple[str, ...], *, timeout_seconds: float) -> ProcessResult:
        return await self.execute(
            argv,
            working_directory=None,
            environment={},
            stdin=None,
            timeout_seconds=timeout_seconds,
            output_bytes=_PROBE_OUTPUT_BYTES,
        )

    async def execute(
        self,
        argv: tuple[str, ...],
        *,
        working_directory: Path | None,
        environment: Mapping[str, str],
        stdin: bytes | None,
        timeout_seconds: float,
        output_bytes: int,
    ) -> ProcessResult:
        process_environment = os.environ.copy()
        process_environment.update(environment)
        creation_flags = 0
        kwargs: dict[str, object] = {}
        if os.name == "nt":
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=working_directory,
            env=process_environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creation_flags,
            **kwargs,
        )
        budget = _OutputBudget(output_bytes)
        stdout_task = asyncio.create_task(_drain(process.stdout, budget))
        stderr_task = asyncio.create_task(_drain(process.stderr, budget))
        stdin_task = asyncio.create_task(_feed_stdin(process, stdin))
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        except TimeoutError:
            timed_out = True
            await _stop_process(process)
        except asyncio.CancelledError:
            await _stop_process(process)
            raise
        finally:
            await stdin_task
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        return ProcessResult(
            exit_code=None if timed_out else process.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            output_truncated=budget.truncated,
        )


class _PersistentProcessSession(SandboxSession):
    def __init__(self, process, timeout_seconds, output_bytes):
        self._process = process
        self._limit = output_bytes
        self._closed = asyncio.Event()
        self._closing = False
        self._stderr = asyncio.create_task(self._drain_stderr())
        self._watcher = asyncio.create_task(self._watch(timeout_seconds))

    async def _drain_stderr(self):
        # Drain continuously without retaining potentially secret diagnostics.
        while await self._process.stderr.read(65536):
            pass

    async def _watch(self, timeout_seconds):
        try:
            await asyncio.wait_for(self._process.wait(), timeout_seconds)
        except TimeoutError:
            pass
        finally:
            await self.close()

    async def send_line(self, payload):
        if self._closing or len(payload) > self._limit or b'\n' in payload or b'\r' in payload:
            raise SandboxUnavailable("persistent session closed or invalid outbound line")
        try:
            self._process.stdin.write(payload + b'\n')
            await asyncio.wait_for(self._process.stdin.drain(), 10)
        except (OSError, TimeoutError) as exc:
            await self.close()
            raise SandboxUnavailable("persistent stdin failed") from exc

    async def receive_line(self):
        try:
            line = await self._process.stdout.readline()
            if len(line) > self._limit + 1:
                raise ValueError("oversized line")
            return line.rstrip(b'\r\n') if line else None
        except (ValueError, OSError) as exc:
            await self.close()
            raise SandboxUnavailable("persistent stdout exceeded bounds or failed") from exc

    async def close(self):
        if self._closing:
            return
        self._closing = True
        try:
            self._process.stdin.close()
            await _stop_process(self._process)
            self._stderr.cancel()
            await asyncio.gather(self._stderr, return_exceptions=True)
        finally:
            self._closed.set()

    async def wait_closed(self):
        await self._closed.wait()


__all__ = ["AsyncioProcessTransport"]
