"""Transport-independent live events for WebSocket and Feishu adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from suiteharness.runtime.scopes import AgentContext, RequestScope
from suiteharness.sessions import SessionIdentity
from suiteharness.sessions.models import canonical_json

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class AgentEventType(str, Enum):
    MESSAGE_START = "message_start"
    MESSAGE_DELTA = "message_delta"
    MESSAGE_END = "message_end"
    TOOL_INTENT = "tool_intent"
    TOOL_RESULT = "tool_result"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_RESOLVED = "approval_resolved"
    STATUS = "status"
    ERROR = "error"
    CANCEL = "cancel"


class AgentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    event_id: str
    identity: SessionIdentity
    run_id: str
    sequence: int = Field(ge=1)
    type: AgentEventType
    payload: JsonValue = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("event_id", "run_id")
    @classmethod
    def validate_ids(cls, value: str, info: object) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError(f"invalid {getattr(info, 'field_name', 'identifier')}: {value!r}")
        return value

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value: JsonValue) -> JsonValue:
        canonical_json(value)
        return value

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class AgentRunContext:
    """Authenticated runtime/session pairing passed to an agent coordinator."""

    agent: AgentContext
    session: SessionIdentity
    run_id: str
    route_id: str

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.run_id):
            raise ValueError("invalid run_id")
        if not self.route_id.strip():
            raise ValueError("route_id must not be blank")
        path = self.agent.path
        expected = (
            path.tenant_id,
            path.product_id,
            path.agent_id,
            path.session_id,
            self.agent.request.principal_id,
        )
        actual = (
            self.session.tenant_id,
            self.session.product_id,
            self.session.agent_id,
            self.session.session_id,
            self.session.principal_id,
        )
        if actual != expected:
            raise ValueError("session identity must match the authenticated AgentContext")

    @property
    def request_scope(self) -> RequestScope:
        return self.agent.request
