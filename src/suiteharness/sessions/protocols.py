"""Persistence contract for resumable company-server sessions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import JsonValue

from suiteharness.sessions.models import (
    RunResumePoint,
    RunStopOutcome,
    SessionCheckpoint,
    SessionIdentity,
    SessionRecord,
    SessionRunClaimState,
    SessionSnapshot,
    SessionStatus,
    TranscriptEvent,
    TranscriptEventType,
    validate_transcript_page,
)


class SessionStore(ABC):
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
    async def request_stop(
        self,
        identity: SessionIdentity,
        *,
        run_id: str,
    ) -> RunStopOutcome: ...

    @abstractmethod
    async def stop_requested(
        self,
        identity: SessionIdentity,
        *,
        run_id: str,
    ) -> bool: ...

    @abstractmethod
    async def active_run_ids(
        self,
        identity: SessionIdentity,
        *,
        limit: int = 4,
    ) -> tuple[str, ...]: ...

    @abstractmethod
    async def save_resume_point(
        self, identity: SessionIdentity, point: RunResumePoint,
    ) -> RunResumePoint | None: ...

    @abstractmethod
    async def get_resume_point(
        self, identity: SessionIdentity, *, run_id: str,
    ) -> RunResumePoint | None: ...

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

    async def transcript_page(
        self,
        identity: SessionIdentity,
        *,
        limit: int,
        before_sequence: int | None = None,
    ) -> tuple[TranscriptEvent, ...]:
        """Return the newest page before an exclusive sequence, in ascending order.

        Legacy adapters must explicitly signal lack of support, including on
        the first page, so callers can expose a non-paginated history fallback.
        """
        validate_transcript_page(limit, before_sequence)
        raise NotImplementedError("this session store does not support transcript cursors")

    @abstractmethod
    async def resume(self, identity: SessionIdentity) -> SessionSnapshot | None: ...
