"""Persistence contract for resumable company-server sessions."""

from __future__ import annotations

from abc import ABC, abstractmethod

from datetime import datetime


from pydantic import JsonValue

from suiteharness.sessions.models import (
    SessionCheckpoint,
    SessionClearResult,
    SessionIdentity,
    SessionRecord,
    SessionRunClaimState,
    SessionSnapshot,
    SessionStatus,
    TranscriptEvent,
    TranscriptEventType,
)


class SessionStore(ABC):
    async def clear_conversation(self, identity: SessionIdentity, *,
                                 idempotency_key: str) -> SessionClearResult:
        """Erase bodies atomically, retaining monotonic fences and digest tombstones.

        Existing third-party stores remain instantiable but must explicitly
        implement this capability; unsupported cleanup must never claim success.
        """
        raise NotImplementedError('this session store does not support conversation clearing')

    async def pending_clear(self, identity: SessionIdentity) -> bool:
        return False

    async def finish_clear(self, identity: SessionIdentity, *, idempotency_key: str,
                           failed_layers: tuple[str, ...] = ()) -> None:
        raise NotImplementedError('this session store does not support conversation clearing')

    @abstractmethod
    async def create(self, identity: SessionIdentity) -> SessionRecord: ...

    @abstractmethod
    async def get(self, identity: SessionIdentity) -> SessionRecord | None: ...

    @abstractmethod
    async def claim_run(
        self,
        identity: SessionIdentity,
        *,
        run_id: str,
        owner_token: str,
        lease_seconds: float,
    ) -> SessionRunClaimState: ...

    @abstractmethod
    async def finalize_run(
        self,
        identity: SessionIdentity,
        *,
        run_id: str,
        owner_token: str,
        lease_seconds: float,
    ) -> bool: ...

    @abstractmethod
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

    @abstractmethod
    async def save_checkpoint(
        self,
        identity: SessionIdentity,
        *,
        checkpoint_id: str,
        workflow_state: JsonValue,
        expected_revision: int,
        idempotency_key: str,
    ) -> SessionCheckpoint: ...

    @abstractmethod
    async def set_status(
        self,
        identity: SessionIdentity,
        status: SessionStatus,
        *,
        expected_revision: int,
    ) -> SessionRecord: ...

    @abstractmethod
    async def recent_transcript(
        self,
        identity: SessionIdentity,
        *,
        limit: int,
    ) -> tuple[TranscriptEvent, ...]: ...

    @abstractmethod
    async def resume(self, identity: SessionIdentity) -> SessionSnapshot | None: ...
