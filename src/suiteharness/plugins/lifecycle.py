"""Fiber-style ownership for every plugin side effect."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

from suiteharness.plugins.errors import PluginErrorCode, PluginLifecycleError

Disposer = Callable[[], Awaitable[None] | None]


class FiberState(str, Enum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class CleanupFailure:
    label: str
    error: BaseException


class PluginCleanupError(PluginLifecycleError):
    def __init__(self, fiber_id: str, failures: tuple[CleanupFailure, ...]) -> None:
        self.fiber_id = fiber_id
        self.failures = failures
        super().__init__(
            PluginErrorCode.CLEANUP_FAILED,
            "one or more plugin effect disposers failed",
            fiber_id=fiber_id,
            labels=tuple(item.label for item in failures),
        )


@dataclass(frozen=True, slots=True)
class _Effect:
    label: str
    disposer: Disposer


class PluginFiber:
    """Concurrent-safe LIFO effect owner inspired by Cordis Fiber semantics."""

    def __init__(self, fiber_id: str) -> None:
        if not fiber_id.strip():
            raise ValueError("fiber_id must not be blank")
        self._fiber_id = fiber_id
        self._state = FiberState.OPEN
        self._effects: list[_Effect] = []
        self._state_lock = threading.RLock()
        self._close_lock = asyncio.Lock()
        self._closed = asyncio.Event()
        self._cleanup_error: PluginCleanupError | None = None

    @property
    def fiber_id(self) -> str:
        return self._fiber_id

    @property
    def state(self) -> FiberState:
        return self._state

    def own(self, label: str, disposer: Disposer) -> None:
        if not label.strip():
            raise ValueError("effect label must not be blank")
        if not callable(disposer):
            raise TypeError("effect disposer must be callable")
        with self._state_lock:
            if self._state is not FiberState.OPEN:
                raise PluginLifecycleError(
                    PluginErrorCode.ACTIVATION_FAILED,
                    "cannot attach an effect to a closing plugin fiber",
                    fiber_id=self._fiber_id,
                    label=label,
                )
            self._effects.append(_Effect(label, disposer))

    async def close(self) -> None:
        wait = False
        effects: tuple[_Effect, ...] = ()
        async with self._close_lock:
            with self._state_lock:
                if self._state is FiberState.CLOSED:
                    if self._cleanup_error is not None:
                        raise self._cleanup_error
                    return
                if self._state is FiberState.CLOSING:
                    wait = True
                else:
                    self._state = FiberState.CLOSING
                    effects = tuple(reversed(self._effects))
                    self._effects.clear()
        if wait:
            await self._closed.wait()
            if self._cleanup_error is not None:
                raise self._cleanup_error
            return

        failures: list[CleanupFailure] = []
        try:
            for effect in effects:
                try:
                    result = effect.disposer()
                    if inspect.isawaitable(result):
                        await result
                except (KeyboardInterrupt, SystemExit):
                    raise
                except BaseException as exc:
                    failures.append(CleanupFailure(effect.label, exc))
        finally:
            with self._state_lock:
                self._state = FiberState.CLOSED
                if failures:
                    self._cleanup_error = PluginCleanupError(
                        self._fiber_id, tuple(failures)
                    )
            self._closed.set()
        if self._cleanup_error is not None:
            raise self._cleanup_error


__all__ = [
    "CleanupFailure",
    "Disposer",
    "FiberState",
    "PluginCleanupError",
    "PluginFiber",
]
