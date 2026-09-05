from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from suiteharness.execution import (
    AuditConflictError,
    AuditEvent,
    AuditEventType,
    SQLiteAuditJournal,
)


def _event(event_id: str, *, product: str = "sales", run: str = "run-1") -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        event_type=AuditEventType.RUN_STARTED,
        occurred_at=datetime.now(UTC),
        tenant_id="acme",
        product_id=product,
        principal_id="user-1",
        channel_id="web",
        request_id="request-1",
        correlation_id="correlation-1",
        purpose="test",
        run_id=run,
    )


def test_sqlite_audit_survives_restart_and_filters_scope(tmp_path: Path) -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        path = (tmp_path / "state" / "audit.sqlite3").resolve()
        first = SQLiteAuditJournal(path)
        await first.append(_event("event-1"))
        await first.append(_event("event-2", product="support", run="run-2"))
        await first.close()
        second = SQLiteAuditJournal(path)
        selected = await second.events(tenant_id="acme", product_id="sales")
        await second.close()
        return selected

    selected = asyncio.run(exercise())
    assert [item.event_id for item in selected] == ["event-1"]
    assert selected[0].channel_id == "web"


def test_sqlite_audit_rejects_duplicate_event_id(tmp_path: Path) -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        journal = SQLiteAuditJournal((tmp_path / "audit.sqlite3").resolve())
        await journal.append(_event("event-1"))
        with pytest.raises(AuditConflictError):
            await journal.append(_event("event-1"))
        events = await journal.events()
        await journal.close()
        return events

    assert len(asyncio.run(exercise())) == 1


def test_sqlite_audit_requires_an_absolute_path(tmp_path: Path) -> None:
    del tmp_path
    with pytest.raises(ValueError, match="absolute"):
        SQLiteAuditJournal("audit.sqlite3")
