"""Persistence contract for resumable company-server sessions."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import JsonValue

from suiteharness.sessions.models import (
    SessionCheckpoint,
    SessionIdentity,
    SessionRecord,
    SessionRunClaimState,
    SessionSnapshot,
    SessionStatus,
    TranscriptEvent,
    TranscriptEventType,
)


class SessionStore(Protocol):
    async def create(self, identity: SessionIdentity) -> SessionRecord: ...

    async def get(self, identity: SessionIdentity) -> SessionRecord | None: ...

    async def claim_run(
        self,
        identity: SessionIdentity,
        *,
        run_id: str,
        owner_token: str,
        lease_seconds: float,
    ) -> SessionRunClaimState: ...

    async def finalize_run(
        self,
        identity: SessionIdentity,
        *,
        run_id: str,
        owner_token: str,
        lease_seconds: float,
    ) -> bool: ...

    async def append(
        self,
        identity: SessionIdentity,
        *,
        event_id: str,
        event_type: TranscriptEventType,
        payload: JsonValue = None,
        idempotency_key: str | None = None,
        expected_revision: int | None = None,
        occurred_at: datetime | None = None,
    ) -> TranscriptEvent: ...

    async def save_checkpoint(
        self,
        identity: SessionIdentity,
        *,
        checkpoint_id: str,
        workflow_state: JsonValue,
        expected_revision: int,
        idempotency_key: str,
    ) -> SessionCheckpoint: ...

    async def set_status(
        self,
        identity: SessionIdentity,
        status: SessionStatus,
        *,
        expected_revision: int,
    ) -> SessionRecord: ...

    async def recent_transcript(
        self,
        identity: SessionIdentity,
        *,
        limit: int,
    ) -> tuple[TranscriptEvent, ...]: ...

    async def resume(self, identity: SessionIdentity) -> SessionSnapshot | None: ...
