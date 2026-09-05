from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from suiteharness.mcp import HttpResumeState
from suiteharness.persistence import (
    SQLiteDatabase,
    SQLiteMcpHttpResumeStore,
    SQLiteProductStateStore,
    StateRevisionConflictError,
)


def test_mcp_resume_is_restart_safe_scoped_and_endpoint_bound(tmp_path: Path) -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        path = (tmp_path / "mcp.sqlite3").resolve()
        first_database = SQLiteDatabase(path)
        resume = SQLiteMcpHttpResumeStore(SQLiteProductStateStore(first_database))
        saved = await resume.save(
            tenant_id="tenant-a",
            product_id="product-a",
            server_id="knowledge",
            endpoint="https://mcp.example.cn/v1",
            state=HttpResumeState(session_id="session-1", last_event_id="event-9"),
            expected_revision=0,
            idempotency_key="save-1",
        )
        await first_database.close()

        second_database = SQLiteDatabase(path)
        reopened = SQLiteMcpHttpResumeStore(SQLiteProductStateStore(second_database))
        loaded = await reopened.load(
            tenant_id="tenant-a",
            product_id="product-a",
            server_id="knowledge",
            endpoint="https://mcp.example.cn/v1",
        )
        different_product = await reopened.load(
            tenant_id="tenant-a",
            product_id="product-b",
            server_id="knowledge",
            endpoint="https://mcp.example.cn/v1",
        )
        different_endpoint = await reopened.load(
            tenant_id="tenant-a",
            product_id="product-a",
            server_id="knowledge",
            endpoint="https://other.example.cn/v1",
        )
        await second_database.close()
        return saved, loaded, different_product, different_endpoint, path.read_bytes()

    saved, loaded, different_product, different_endpoint, raw_database = asyncio.run(exercise())
    assert saved.revision == 1
    assert loaded is not None and loaded.state == saved.state
    assert different_product is None
    assert different_endpoint is None
    assert b"https://mcp.example.cn/v1" not in raw_database
    assert b"Authorization" not in raw_database
    assert b"approval_token" not in raw_database


def test_mcp_resume_uses_cas_and_validates_wire_values() -> None:
    async def exercise() -> None:
        database = SQLiteDatabase(":memory:")
        resume = SQLiteMcpHttpResumeStore(SQLiteProductStateStore(database))
        arguments = {
            "tenant_id": "tenant-a",
            "product_id": "product-a",
            "server_id": "knowledge",
            "endpoint": "https://mcp.example.cn/v1",
        }
        await resume.save(
            **arguments,
            state=HttpResumeState(session_id="session-1"),
            expected_revision=0,
            idempotency_key="first",
        )
        with pytest.raises(StateRevisionConflictError):
            await resume.save(
                **arguments,
                state=HttpResumeState(session_id="session-2"),
                expected_revision=0,
                idempotency_key="stale",
            )
        with pytest.raises(ValueError, match="session_id"):
            await resume.save(
                **arguments,
                state=HttpResumeState(session_id="bad\nvalue"),
                expected_revision=1,
                idempotency_key="bad-session",
            )
        with pytest.raises(ValueError, match="HTTPS"):
            await resume.load(**(arguments | {"endpoint": "http://mcp.example.cn/v1"}))
        await database.close()

    asyncio.run(exercise())
