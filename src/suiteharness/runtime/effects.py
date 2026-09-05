"""Async ownership of runtime side effects.

An :class:`EffectScope` is the lifecycle primitive used by the new scoped runtime.
Anything that must be undone -- a tool registration, hook, route, task, process or
provider connection -- is registered here at the point where it becomes visible.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeVar

T = TypeVar("T")
Disposer = Callable[[], Awaitable[None] | None]


class EffectScopeState(str, Enum):
    """The deliberately small state machine of an effect owner."""

    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


class EffectScopeClosedError(RuntimeError):
    """Raised when an effect is added after shutdown has started."""


@dataclass(frozen=True, slots=True)
class EffectFailure:
    """One disposer that failed while the remaining disposers kept running."""

    label: str
    error: BaseException


class EffectCleanupError(RuntimeError):
    """Aggregate cleanup error that preserves every failed disposer."""

    def __init__(self, scope_name: str, failures: tuple[EffectFailure, ...]) -> None:
        self.scope_name = scope_name
        self.failures = failures
        labels = ", ".join(failure.label for failure in failures)
        super().__init__(f"effect scope {scope_name!r} failed to clean: {labels}")


@dataclass(frozen=True, slots=True)
class _OwnedEffect:
    label: str
    dispose: Disposer


class EffectScope:
    """Own a LIFO stack of synchronous and asynchronous cleanup callbacks.

    ``close`` is idempotent and safe to call concurrently.  A failing disposer is
    recorded but never prevents the remaining stack from being unwound.  Repeated
    callers observe the same cleanup result without running a disposer twice.
    """

    def __init__(self, name: str, *, parent: EffectScope | None = None) -> None:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("effect scope name must not be empty")
        self._name = clean_name
        self._parent = parent
        self._state = EffectScopeState.OPEN
        self._effects: list[_OwnedEffect] = []
        self._close_lock = asyncio.Lock()
        self._closed = asyncio.Event()
        self._cleanup_error: EffectCleanupError | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def parent(self) -> EffectScope | None:
        return self._parent

    @property
    def state(self) -> EffectScopeState:
        return self._state

    @property
    def closed(self) -> bool:
        return self._state is EffectScopeState.CLOSED

    def callback(self, label: str, dispose: Disposer) -> None:
        """Register a disposer without introducing an await/cancellation gap."""

        clean_label = label.strip()
        if not clean_label:
            raise ValueError("effect label must not be empty")
        if not callable(dispose):
            raise TypeError("effect disposer must be callable")
        if self._state is not EffectScopeState.OPEN:
            raise EffectScopeClosedError(
                f"cannot add effect {clean_label!r}: scope {self._name!r} is {self._state.value}"
            )
        self._effects.append(_OwnedEffect(clean_label, dispose))

    async def enter(self, label: str, resource: AbstractAsyncContextManager[T]) -> T:
        """Enter an async resource and atomically transfer its exit to this scope.

        If shutdown starts while ``__aenter__`` is waiting, the newly acquired
        resource is immediately exited instead of being leaked or published.
        """

        if self._state is not EffectScopeState.OPEN:
            raise EffectScopeClosedError(
                f"cannot enter effect {label!r}: scope {self._name!r} is {self._state.value}"
            )
        value = await resource.__aenter__()

        async def exit_resource() -> None:
            await resource.__aexit__(None, None, None)

        try:
            self.callback(label, exit_resource)
        except BaseException:
            await exit_resource()
            raise
        return value

    def child(self, name: str) -> EffectScope:
        """Create a child whose shutdown is owned by this scope."""

        child = EffectScope(name, parent=self)
        try:
            self.callback(f"scope:{child.name}", child.close)
        except BaseException:
            # No resource has been installed in a brand-new child, so there is no
            # asynchronous cleanup to schedule on this failure path.
            raise
        return child

    async def close(self) -> None:
        """Unwind once in reverse order, continuing after individual failures."""

        wait_for_owner = False
        effects: tuple[_OwnedEffect, ...] = ()
        async with self._close_lock:
            if self._state is EffectScopeState.CLOSED:
                if self._cleanup_error is not None:
                    raise self._cleanup_error
                return
            if self._state is EffectScopeState.CLOSING:
                wait_for_owner = True
            else:
                self._state = EffectScopeState.CLOSING
                effects = tuple(reversed(self._effects))
                self._effects.clear()

        if wait_for_owner:
            await self._closed.wait()
            if self._cleanup_error is not None:
                raise self._cleanup_error
            return

        failures: list[EffectFailure] = []
        try:
            for effect in effects:
                try:
                    result = effect.dispose()
                    if inspect.isawaitable(result):
                        await result
                except (KeyboardInterrupt, SystemExit):
                    raise
                except BaseException as exc:  # cancellation also must not strand later effects
                    failures.append(EffectFailure(effect.label, exc))
        finally:
            self._state = EffectScopeState.CLOSED
            if failures:
                self._cleanup_error = EffectCleanupError(self._name, tuple(failures))
            self._closed.set()

        if self._cleanup_error is not None:
            raise self._cleanup_error

    async def __aenter__(self) -> EffectScope:
        if self._state is not EffectScopeState.OPEN:
            raise EffectScopeClosedError(
                f"cannot enter scope {self._name!r}: it is {self._state.value}"
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> bool:
        try:
            await self.close()
        except EffectCleanupError as cleanup:
            if exc is None:
                raise
            # Keep both the body failure and cleanup diagnostics through
            # explicit chaining; callers can inspect the structured failures.
            raise cleanup from exc
        return False
