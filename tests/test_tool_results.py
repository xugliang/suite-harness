import asyncio
import pytest
from suiteharness.execution.results import SQLiteToolResultStore
from suiteharness.runtime import RequestScope, ScopePath


def scope(seller="seller", session="chat"):
    return RequestScope(path=ScopePath.agent("company", "product", "agent", session), principal_id=seller)


def test_reference_is_durable_scoped_and_paginated(tmp_path):
    async def run():
        store = SQLiteToolResultStore(tmp_path / "results.sqlite3")
        ref = await store.put(scope(), "run1", "中文" * 100)
        reopened = SQLiteToolResultStore(tmp_path / "results.sqlite3")
        first = await reopened.read(scope(), "run1", ref["result_id"], offset=0, limit=7)
        second = await reopened.read(scope(), "run1", ref["result_id"], offset=first["next_offset"], limit=1000)
        assert first["content"] + second["content"] == "中文" * 100
        for other, run_id in [(scope("other"), "run1"), (scope(session="other"), "run1"), (scope(), "run2")]:
            with pytest.raises(LookupError):
                await reopened.read(other, run_id, ref["result_id"])
    asyncio.run(run())


def test_store_quota_is_bounded_and_does_not_evict_active_results(tmp_path):
    async def run():
        store = SQLiteToolResultStore(tmp_path / "results.sqlite3", max_total_bytes=10)
        first = await store.put(scope(), "run1", "123456")
        with pytest.raises(ValueError, match="quota"):
            await store.put(scope(), "run1", "abcdef")
        assert (await store.read(scope(), "run1", first["result_id"]))["content"] == "123456"
    asyncio.run(run())
