"""An atomic quota shared by a parent run and its delegated child runs."""

from __future__ import annotations

import asyncio
import time


class AggregateRunBudget:
    """Charge actual parent and child work against one deadline and ceiling."""

    def __init__(
        self,
        *,
        max_iterations: int,
        max_tool_calls: int,
        max_children: int,
        deadline_monotonic: float,
    ) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (max_iterations, max_tool_calls, max_children)
        ):
            raise ValueError("aggregate limits must be nonnegative integers")
        self.max_iterations = max_iterations
        self.max_tool_calls = max_tool_calls
        self.max_children = max_children
        self.deadline_monotonic = deadline_monotonic
        self.iterations_used = 0
        self.tool_calls_used = 0
        self._child_calls: set[str] = set()
        self._lock = asyncio.Lock()
        self._child_condition = asyncio.Condition(self._lock)
        self._active_children = 0

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_monotonic - time.monotonic())

    async def take_iteration(self) -> bool:
        async with self._lock:
            if self.remaining_seconds <= 0 or self.iterations_used >= self.max_iterations:
                return False
            self.iterations_used += 1
            return True

    async def take_tool_slots(self, requested: int) -> int:
        if isinstance(requested, bool) or not isinstance(requested, int) or requested < 0:
            raise ValueError("requested tool slots must be a nonnegative integer")
        async with self._lock:
            if self.remaining_seconds <= 0:
                return 0
            available = max(0, self.max_tool_calls - self.tool_calls_used)
            permitted = min(requested, available)
            self.tool_calls_used += permitted
            return permitted

    async def reserve_child(self, call_id: str, *, ceiling: int | None = None) -> bool:
        if not call_id:
            raise ValueError("a child reservation requires a call id")
        if ceiling is not None and (
            isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < 0
        ):
            raise ValueError("child ceiling must be a nonnegative integer")
        async with self._lock:
            if (
                self.remaining_seconds <= 0
                or call_id in self._child_calls
                or len(self._child_calls) >= (
                    self.max_children if ceiling is None else min(self.max_children, ceiling)
                )
            ):
                return False
            self._child_calls.add(call_id)
            return True

    async def take_child_slot(self, ceiling: int) -> bool:
        if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < 0:
            raise ValueError("child concurrency ceiling must be a nonnegative integer")
        effective = min(ceiling, self.max_children)
        async with self._child_condition:
            while self.remaining_seconds > 0 and effective > 0:
                if self._active_children < effective:
                    self._active_children += 1
                    return True
                try:
                    await asyncio.wait_for(
                        self._child_condition.wait(), timeout=self.remaining_seconds
                    )
                except TimeoutError:
                    break
            return False

    async def release_child_slot(self) -> None:
        async with self._child_condition:
            if self._active_children <= 0:
                raise RuntimeError("no child concurrency slot is active")
            self._active_children -= 1
            self._child_condition.notify_all()
