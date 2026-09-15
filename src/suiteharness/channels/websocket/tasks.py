"""Bounded background work owned by one connection, independent of its protocol."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any


class ConnectionTaskGroup:
    """Keep a receiver live while owning and joining its background operations.

    Factories are called only when capacity is available. Names are diagnostic,
    not ownership keys: a queued callback cannot remove a newer same-name task.
    Operation failures are retrieved immediately and re-raised by the next
    ``try_start`` or ``aclose``; protocol-specific error reporting stays with
    the caller. ASGI adapters retain responsibility for shielding their cleanup
    from their host's cancellation scope.
    """

    def __init__(self, *, max_concurrent: int) -> None:
        if isinstance(max_concurrent, bool) or not isinstance(max_concurrent, int) or max_concurrent < 1:
            raise ValueError("max_concurrent must be a positive integer")
        self._limit = max_concurrent
        self._tasks: set[asyncio.Task[None]] = set()
        self._failures: list[BaseException] = []
        self._closed = False

    @property
    def active_count(self) -> int:
        self._reap_finished()
        return len(self._tasks)

    def try_start(
        self,
        operation: Callable[[], Coroutine[Any, Any, None]],
        *,
        name: str | None = None,
    ) -> bool:
        self._reap_finished()
        self._raise_failures()
        if self._closed:
            raise RuntimeError("connection task group is closed")
        if len(self._tasks) >= self._limit:
            return False
        task = asyncio.create_task(operation(), name=name)
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return True

    async def aclose(self) -> None:
        """Reject new work, cancel all active work, and wait for its cleanup."""
        self._closed = True
        pending = tuple(self._tasks)
        for task in pending:
            if not task.done():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._reap_finished()
        self._raise_failures()

    def _reap_finished(self) -> None:
        for task in tuple(self._tasks):
            if task.done():
                self._finished(task)

    def _finished(self, task: asyncio.Task[None]) -> None:
        if task not in self._tasks:
            return
        self._tasks.remove(task)
        if not task.cancelled():
            failure = task.exception()
            if failure is not None:
                self._failures.append(failure)

    def _raise_failures(self) -> None:
        if not self._failures:
            return
        failures, self._failures = self._failures, []
        if len(failures) == 1:
            raise failures[0]
        raise BaseExceptionGroup("connection operations failed", failures)


__all__ = ["ConnectionTaskGroup"]
