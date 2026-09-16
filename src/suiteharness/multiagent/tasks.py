"""Owned, durable state contract for bounded delegated agent runs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from suiteharness.runtime.scopes import RequestScope


class AgentTaskStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


TERMINAL_TASK_STATUSES = frozenset(
    {
        AgentTaskStatus.SUCCEEDED,
        AgentTaskStatus.FAILED,
        AgentTaskStatus.CANCELLED,
        AgentTaskStatus.TIMED_OUT,
    }
)


class AgentTaskIdempotencyConflictError(ValueError):
    """One parent tool call was reused with a different role or task digest."""


class AgentTaskCancelledError(RuntimeError):
    """The parent-run cancellation fence forbids any new child task."""


@dataclass(frozen=True, slots=True)
class AgentTaskRecord:
    task_id: str
    tenant_id: str
    product_id: str
    principal_id: str
    parent_run_id: str
    call_id: str
    role_id: str
    task_digest: str
    child_run_id: str
    child_agent_id: str
    child_session_id: str
    status: AgentTaskStatus
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: str | None = None
    failure: str | None = None


class AgentTaskStore(ABC):
    """Task state is keyed by authenticated scope, never a bare model task ID."""

    @abstractmethod
    async def create(
        self,
        parent_scope: RequestScope,
        parent_run_id: str,
        call_id: str,
        role_id: str,
        task_digest: str,
    ) -> AgentTaskRecord: ...

    @abstractmethod
    async def get_by_call(
        self,
        parent_scope: RequestScope,
        parent_run_id: str,
        call_id: str,
    ) -> AgentTaskRecord | None: ...

    @abstractmethod
    async def get_owned(
        self,
        parent_scope: RequestScope,
        task_id: str,
        *,
        parent_run_id: str,
    ) -> AgentTaskRecord | None: ...

    @abstractmethod
    async def transition(
        self,
        parent_scope: RequestScope,
        task_id: str,
        expected: AgentTaskStatus,
        target: AgentTaskStatus,
        *,
        parent_run_id: str,
        result: str | None = None,
        failure: str | None = None,
    ) -> AgentTaskRecord | None: ...

    @abstractmethod
    async def cancel_children(self, parent_scope: RequestScope, parent_run_id: str) -> int: ...

    @abstractmethod
    async def recover_interrupted(self) -> int: ...


__all__ = [
    "AgentTaskCancelledError",
    "AgentTaskIdempotencyConflictError",
    "AgentTaskRecord",
    "AgentTaskStatus",
    "AgentTaskStore",
    "TERMINAL_TASK_STATUSES",
]
