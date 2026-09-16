"""Aggregate parent/child execution limits must be atomic and non-renewable."""

import asyncio
import time

from suiteharness.multiagent.budget import AggregateRunBudget


def test_parent_and_parallel_children_share_iteration_and_tool_quota():
    async def check():
        budget = AggregateRunBudget(
            max_iterations=3,
            max_tool_calls=2,
            max_children=2,
            deadline_monotonic=time.monotonic() + 10,
        )
        assert await budget.take_iteration()
        assert await asyncio.gather(budget.take_iteration(), budget.take_iteration()) == [True, True]
        assert not await budget.take_iteration()
        assert await asyncio.gather(budget.take_tool_slots(1), budget.take_tool_slots(1)) == [1, 1]
        assert await budget.take_tool_slots(1) == 0

    asyncio.run(check())


def test_child_quota_counts_total_children_not_only_running_children():
    async def check():
        budget = AggregateRunBudget(
            max_iterations=4,
            max_tool_calls=4,
            max_children=2,
            deadline_monotonic=time.monotonic() + 10,
        )
        assert await budget.reserve_child("call-one")
        assert await budget.reserve_child("call-two")
        assert not await budget.reserve_child("call-three")
        assert not await budget.reserve_child("call-one")

    asyncio.run(check())


def test_deadline_denies_new_work_even_with_unused_quota():
    async def check():
        budget = AggregateRunBudget(
            max_iterations=4,
            max_tool_calls=4,
            max_children=2,
            deadline_monotonic=time.monotonic() - 1,
        )
        assert not await budget.take_iteration()
        assert await budget.take_tool_slots(1) == 0
        assert not await budget.reserve_child("call-one")

    asyncio.run(check())


def test_product_child_limit_can_narrow_framework_ceiling():
    async def check():
        budget = AggregateRunBudget(
            max_iterations=4, max_tool_calls=4, max_children=2,
            deadline_monotonic=time.monotonic() + 10,
        )
        assert await budget.reserve_child("first", ceiling=1)
        assert not await budget.reserve_child("second", ceiling=1)

    asyncio.run(check())


def test_zero_child_ceiling_denies_reservation():
    async def check():
        budget = AggregateRunBudget(
            max_iterations=4, max_tool_calls=4, max_children=2,
            deadline_monotonic=time.monotonic() + 10,
        )
        assert not await budget.reserve_child("first", ceiling=0)

    asyncio.run(check())


def test_child_concurrency_slot_waits_for_prior_child_to_finish():
    async def check():
        budget = AggregateRunBudget(
            max_iterations=4, max_tool_calls=4, max_children=2,
            deadline_monotonic=time.monotonic() + 10,
        )
        assert await budget.take_child_slot(1)
        second = asyncio.create_task(budget.take_child_slot(1))
        done, _ = await asyncio.wait({second}, timeout=0.05)
        assert not done
        await budget.release_child_slot()
        assert await second
        await budget.release_child_slot()

    asyncio.run(check())
