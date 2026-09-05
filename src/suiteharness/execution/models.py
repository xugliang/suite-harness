"""Strict, provider-neutral models for the non-replaceable execution boundary."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from enum import Enum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    field_validator,
    model_validator,
)

from suiteharness.runtime.scopes import RequestScope, ScopeKind, ScopePath

_NAME = re.compile(r"^[a-z][a-z0-9]*(?:[._:/-][a-z0-9]+)*$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ORIGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _name(value: str, label: str) -> str:
    if not _NAME.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _identifier(value: str, label: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def canonical_json(value: JsonValue) -> bytes:
    """Return the sole canonical representation used for approval binding.

    JSON has no NaN or infinity values.  Rejecting those values here also avoids
    different runtimes hashing the same apparent arguments differently.
    """

    def reject_non_finite(item: object) -> None:
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("JSON values must not contain NaN or infinity")
        if isinstance(item, dict):
            for child in item.values():
                reject_non_finite(child)
        elif isinstance(item, list):
            for child in item:
                reject_non_finite(child)

    reject_non_finite(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def arguments_digest(arguments: dict[str, JsonValue]) -> str:
    """Digest exactly the canonical arguments that a handler will receive."""

    return hashlib.sha256(canonical_json(arguments)).hexdigest()


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
        frozen=True,
        strict=True,
    )


class ToolEffect(str, Enum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    EXTERNAL = "external"


class RunStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BUDGET_EXCEEDED = "budget_exceeded"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class ToolCallStatus(str, Enum):
    SUCCEEDED = "succeeded"
    DENIED = "denied"
    FAILED = "failed"
    BUDGET_EXCEEDED = "budget_exceeded"


class ToolAuthorization(str, Enum):
    """Root policy result for one already-resolved, schema-valid tool call."""

    DEFAULT = "default"
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class FailureCode(str, Enum):
    INVALID_SCOPE = "invalid_scope"
    RUN_CONFLICT = "run_conflict"
    GRANT_NOT_FOUND = "grant_not_found"
    GRANT_SCOPE_MISMATCH = "grant_scope_mismatch"
    GRANT_EXPIRED = "grant_expired"
    TOOL_NOT_FOUND = "tool_not_found"
    TOOL_NOT_GRANTED = "tool_not_granted"
    CAPABILITY_DENIED = "capability_denied"
    POLICY_DENIED = "policy_denied"
    READ_ONLY_DENIED = "read_only_denied"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_INVALID = "approval_invalid"
    TOOL_INPUT_INVALID = "tool_input_invalid"
    RUN_INPUT_TOO_LARGE = "run_input_too_large"
    TOOL_ARGUMENTS_TOO_LARGE = "tool_arguments_too_large"
    TOOL_RESULT_TOO_LARGE = "tool_result_too_large"
    FINAL_OUTPUT_TOO_LARGE = "final_output_too_large"
    WORKFLOW_STATE_TOO_LARGE = "workflow_state_too_large"
    DUPLICATE_CALL_ID = "duplicate_call_id"
    INVALID_TOOL_RESULT = "invalid_tool_result"
    TOOL_ERROR = "tool_error"
    WORKFLOW_ERROR = "workflow_error"
    PROMPT_ERROR = "prompt_error"
    REFLECTION_ERROR = "reflection_error"
    BUDGET_POLICY_EXCEEDED = "budget_policy_exceeded"
    ITERATION_BUDGET_EXCEEDED = "iteration_budget_exceeded"
    TOOL_BUDGET_EXCEEDED = "tool_budget_exceeded"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"


class WorkflowDecisionKind(str, Enum):
    FINAL = "final"
    TOOLS = "tools"


class AuditEventType(str, Enum):
    RUN_STARTED = "run_started"
    STRATEGY_DECISION = "strategy_decision"
    TOOL_INTENT = "tool_intent"
    TOOL_DENIED = "tool_denied"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    RUN_FINISHED = "run_finished"


class ToolIdentity(_FrozenModel):
    """Stable security identity for one tool implementation contract.

    ``ToolSpec.name`` is a model-facing alias.  Authorization, approval, and
    auditing bind this complete identity so a more-specific registration using
    the same alias cannot inherit another tool's authority.
    """

    namespace: str
    name: str
    origin: str
    version: str

    @field_validator("namespace", "name")
    @classmethod
    def validate_names(cls, value: str, info: object) -> str:
        return _name(value, getattr(info, "field_name", "name"))

    @field_validator("origin")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        if not _ORIGIN.fullmatch(value):
            raise ValueError(f"invalid tool origin: {value!r}")
        return value

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not _VERSION.fullmatch(value):
            raise ValueError(f"invalid tool version: {value!r}")
        return value

    @property
    def canonical_id(self) -> str:
        """Return a collision-resistant identifier for logs and wire formats."""

        payload = canonical_json(
            {
                "name": self.name,
                "namespace": self.namespace,
                "origin": self.origin,
                "version": self.version,
            }
        )
        return f"tool:sha256:{hashlib.sha256(payload).hexdigest()}"

    @classmethod
    def scoped(
        cls,
        scope: ScopePath,
        name: str,
        *,
        version: str = "1",
    ) -> ToolIdentity:
        """Build the deterministic default identity for a scoped registration."""

        if scope.kind is ScopeKind.ROOT:
            origin = "scope:root"
        elif scope.kind is ScopeKind.TENANT:
            origin = f"scope:tenant:{scope.tenant_id}"
        elif scope.kind is ScopeKind.PRODUCT:
            origin = f"scope:product:{scope.tenant_id}:{scope.product_id}"
        else:
            raise ValueError("tool identities require root, tenant, or product scope")
        return cls(namespace=scope.kind.value, name=name, origin=origin, version=version)


class ToolSpec(_FrozenModel):
    """Declarative tool metadata.  Effects are interpreted only by the runner."""

    name: str
    description: str = ""
    effects: frozenset[ToolEffect]
    required_capabilities: frozenset[str] = frozenset()
    input_schema: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _name(value, "tool name")

    @field_validator("required_capabilities")
    @classmethod
    def validate_capabilities(cls, value: frozenset[str]) -> frozenset[str]:
        for item in value:
            _name(item, "capability")
        return value

    @field_validator("input_schema")
    @classmethod
    def validate_input_schema(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        canonical_json(value)
        return value

    @model_validator(mode="after")
    def validate_effects(self) -> ToolSpec:
        if not self.effects:
            raise ValueError("a tool must declare at least one effect")
        if not self.effects & {ToolEffect.READ, ToolEffect.WRITE}:
            raise ValueError("a tool must declare exactly one of read or write")
        if ToolEffect.DESTRUCTIVE in self.effects and ToolEffect.WRITE not in self.effects:
            raise ValueError("destructive tools must also declare the write effect")
        if ToolEffect.READ in self.effects and ToolEffect.WRITE in self.effects:
            raise ValueError("read and write effects are mutually exclusive")
        return self

    @property
    def is_read(self) -> bool:
        return ToolEffect.READ in self.effects and ToolEffect.WRITE not in self.effects


class ExecutionBudget(_FrozenModel):
    max_iterations: int = Field(default=16, ge=1, le=10_000)
    max_tool_calls: int = Field(default=32, ge=0, le=100_000)
    max_parallel_calls: int = Field(default=4, ge=1, le=1_024)
    timeout_seconds: float = Field(default=120.0, gt=0.0, le=86_400.0)


class CapabilityGrant(_FrozenModel):
    """Control-plane-issued grant bound to exact tool identities."""

    grant_id: str
    tenant_id: str
    product_id: str
    tool_identities: frozenset[ToolIdentity]
    capabilities: frozenset[str] = frozenset()
    principal_id: str | None = None
    issued_at: datetime
    expires_at: datetime

    @field_validator("grant_id", "tenant_id", "product_id")
    @classmethod
    def validate_ids(cls, value: str, info: object) -> str:
        label = getattr(info, "field_name", "identifier")
        return _identifier(value, label)

    @field_validator("principal_id")
    @classmethod
    def validate_principal(cls, value: str | None) -> str | None:
        return None if value is None else _identifier(value, "principal_id")

    @field_validator("capabilities")
    @classmethod
    def validate_names(cls, value: frozenset[str], info: object) -> frozenset[str]:
        label = getattr(info, "field_name", "name")
        for item in value:
            _name(item, label)
        return value

    @field_validator("issued_at", "expires_at")
    @classmethod
    def validate_timestamp(cls, value: datetime, info: object) -> datetime:
        return _utc(value, getattr(info, "field_name", "timestamp"))

    @model_validator(mode="after")
    def validate_window(self) -> CapabilityGrant:
        if self.expires_at <= self.issued_at:
            raise ValueError("grant expires_at must be after issued_at")
        return self

    @property
    def tool_names(self) -> frozenset[str]:
        """Return aliases for presentation only; never use this for authorization."""

        return frozenset(identity.name for identity in self.tool_identities)


class ApprovalTarget(_FrozenModel):
    """The exact authenticated call an approval may authorize.

    Keeping the complete product/agent path prevents a token approved for one
    agent session from being replayed by another session in the same product.
    Approval issuer metadata and validity timestamps deliberately live outside
    this value so a store can compare the target atomically before consuming it.
    """

    scope: ScopePath
    principal_id: str
    run_id: str
    call_id: str
    tool_identity: ToolIdentity
    arguments_digest: str

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: ScopePath) -> ScopePath:
        if value.kind not in {ScopeKind.PRODUCT, ScopeKind.AGENT}:
            raise ValueError("approval target must use a product or agent scope")
        return value

    @field_validator("principal_id", "run_id", "call_id")
    @classmethod
    def validate_ids(cls, value: str, info: object) -> str:
        return _identifier(value, getattr(info, "field_name", "identifier"))

    @field_validator("arguments_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("arguments_digest must be a lowercase SHA-256 hex digest")
        return value


class ApprovalBinding(_FrozenModel):
    """Issuer claims around one exact, single-use :class:`ApprovalTarget`."""

    target: ApprovalTarget
    approved_by: str
    issued_at: datetime
    expires_at: datetime
    single_use: Literal[True] = True

    @field_validator("approved_by")
    @classmethod
    def validate_ids(cls, value: str, info: object) -> str:
        return _identifier(value, getattr(info, "field_name", "identifier"))

    @field_validator("issued_at", "expires_at")
    @classmethod
    def validate_timestamp(cls, value: datetime, info: object) -> datetime:
        return _utc(value, getattr(info, "field_name", "timestamp"))

    @model_validator(mode="after")
    def validate_window(self) -> ApprovalBinding:
        if self.expires_at <= self.issued_at:
            raise ValueError("approval expires_at must be after issued_at")
        return self


class ApprovalCredential(_FrozenModel):
    token: SecretStr = Field(min_length=32)
    binding: ApprovalBinding


class ToolIntent(_FrozenModel):
    call_id: str
    tool_name: str
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    approval_token: SecretStr | None = None

    @field_validator("call_id")
    @classmethod
    def validate_call_id(cls, value: str) -> str:
        return _identifier(value, "call_id")

    @field_validator("tool_name")
    @classmethod
    def validate_tool_name(cls, value: str) -> str:
        return _name(value, "tool_name")

    @field_validator("arguments")
    @classmethod
    def validate_arguments(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        canonical_json(value)
        return value


class RunFailure(_FrozenModel):
    code: FailureCode
    message: str
    retryable: bool = False
    iteration: int | None = Field(default=None, ge=1)
    call_id: str | None = None
    tool_name: str | None = None


class ToolObservation(_FrozenModel):
    call_id: str
    tool_name: str
    status: ToolCallStatus
    result: JsonValue | None = None
    failure: RunFailure | None = None
    started_at: datetime
    finished_at: datetime

    @field_validator("started_at", "finished_at")
    @classmethod
    def validate_timestamps(cls, value: datetime, info: object) -> datetime:
        return _utc(value, getattr(info, "field_name", "timestamp"))

    @field_validator("result")
    @classmethod
    def validate_result(cls, value: JsonValue | None) -> JsonValue | None:
        canonical_json(value)
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> ToolObservation:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if self.status is ToolCallStatus.SUCCEEDED and self.failure is not None:
            raise ValueError("successful tool observations cannot carry a failure")
        if self.status is not ToolCallStatus.SUCCEEDED and self.failure is None:
            raise ValueError("unsuccessful tool observations require a failure")
        return self


class PromptMessage(_FrozenModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class PromptEnvelope(_FrozenModel):
    messages: tuple[PromptMessage, ...] = ()
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        canonical_json(value)
        return value


class ConversationMessage(_FrozenModel):
    """One server-reconstructed message from the isolated durable transcript."""

    role: Literal["user", "assistant"]
    content: JsonValue
    principal_id: str | None = None
    occurred_at: datetime

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: JsonValue) -> JsonValue:
        canonical_json(value)
        return value

    @field_validator("principal_id")
    @classmethod
    def validate_principal_id(cls, value: str | None) -> str | None:
        return None if value is None else _identifier(value, "conversation principal_id")

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime) -> datetime:
        return _utc(value, "conversation occurred_at")

    @model_validator(mode="after")
    def validate_actor(self) -> ConversationMessage:
        if self.role == "user" and self.principal_id is None:
            raise ValueError("a user conversation message requires a trusted principal_id")
        if self.role == "assistant" and self.principal_id is not None:
            raise ValueError("an assistant conversation message cannot claim a principal_id")
        return self


class ConversationContext(_FrozenModel):
    """Bounded context reconstructed by the server, never accepted from a client."""

    messages: tuple[ConversationMessage, ...] = ()
    truncated: bool = False


class WorkflowFrame(_FrozenModel):
    iteration: int = Field(ge=1)
    observations: tuple[ToolObservation, ...] = ()
    available_tools: tuple[ToolSpec, ...] = ()
    workflow_state: JsonValue = None
    tool_calls_used: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0.0)

    @field_validator("workflow_state")
    @classmethod
    def validate_state(cls, value: JsonValue) -> JsonValue:
        canonical_json(value)
        return value


class WorkflowDecision(_FrozenModel):
    kind: WorkflowDecisionKind
    intents: tuple[ToolIntent, ...] = ()
    output: JsonValue | None = None
    workflow_state: JsonValue = None

    @field_validator("output", "workflow_state")
    @classmethod
    def validate_json_fields(cls, value: JsonValue | None) -> JsonValue | None:
        canonical_json(value)
        return value

    @model_validator(mode="after")
    def validate_kind(self) -> WorkflowDecision:
        if self.kind is WorkflowDecisionKind.TOOLS and not self.intents:
            raise ValueError("a tools decision requires at least one intent")
        if self.kind is WorkflowDecisionKind.FINAL and self.intents:
            raise ValueError("a final decision cannot contain tool intents")
        return self

    @classmethod
    def final(cls, output: JsonValue, *, state: JsonValue = None) -> WorkflowDecision:
        return cls(kind=WorkflowDecisionKind.FINAL, output=output, workflow_state=state)

    @classmethod
    def tools(
        cls,
        *intents: ToolIntent,
        state: JsonValue = None,
    ) -> WorkflowDecision:
        return cls(
            kind=WorkflowDecisionKind.TOOLS,
            intents=tuple(intents),
            workflow_state=state,
        )


class RunRequest(_FrozenModel):
    run_id: str
    scope: RequestScope
    grant_id: str
    input: JsonValue
    conversation: ConversationContext = Field(default_factory=ConversationContext)
    read_only: bool = False
    budget: ExecutionBudget = Field(default_factory=ExecutionBudget)

    @field_validator("run_id", "grant_id")
    @classmethod
    def validate_ids(cls, value: str, info: object) -> str:
        return _identifier(value, getattr(info, "field_name", "identifier"))

    @field_validator("input")
    @classmethod
    def validate_input(cls, value: JsonValue) -> JsonValue:
        canonical_json(value)
        return value

    @model_validator(mode="after")
    def validate_scope(self) -> RunRequest:
        if self.scope.path.kind not in {ScopeKind.PRODUCT, ScopeKind.AGENT}:
            raise ValueError("run request must use a product or agent RequestScope")
        return self


class RunResult(_FrozenModel):
    run_id: str
    status: RunStatus
    output: JsonValue | None = None
    failure: RunFailure | None = None
    observations: tuple[ToolObservation, ...] = ()
    iterations_used: int = Field(ge=0)
    tool_calls_used: int = Field(ge=0)
    started_at: datetime
    finished_at: datetime

    @field_validator("started_at", "finished_at")
    @classmethod
    def validate_timestamps(cls, value: datetime, info: object) -> datetime:
        return _utc(value, getattr(info, "field_name", "timestamp"))

    @field_validator("output")
    @classmethod
    def validate_output(cls, value: JsonValue | None) -> JsonValue | None:
        canonical_json(value)
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> RunResult:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if self.status is RunStatus.SUCCEEDED and self.failure is not None:
            raise ValueError("successful runs cannot carry a failure")
        if self.status is not RunStatus.SUCCEEDED and self.failure is None:
            raise ValueError("unsuccessful runs require a failure")
        return self


class AuditEvent(_FrozenModel):
    event_id: str
    event_type: AuditEventType
    occurred_at: datetime
    tenant_id: str
    product_id: str
    principal_id: str
    request_id: str
    correlation_id: str
    purpose: str
    run_id: str
    channel_id: str = "internal"
    call_id: str | None = None
    tool_name: str | None = None
    tool_identity: ToolIdentity | None = None
    data: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime) -> datetime:
        return _utc(value, "occurred_at")

    @field_validator(
        "event_id", "tenant_id", "product_id", "principal_id", "run_id", "channel_id"
    )
    @classmethod
    def validate_ids(cls, value: str, info: object) -> str:
        return _identifier(value, getattr(info, "field_name", "identifier"))

    @field_validator("request_id", "correlation_id")
    @classmethod
    def validate_optional_ids(cls, value: str, info: object) -> str:
        if not value:
            return value
        return _identifier(value, getattr(info, "field_name", "identifier"))

    @field_validator("purpose")
    @classmethod
    def validate_purpose(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("purpose must not be empty")
        return value

    @field_validator("call_id")
    @classmethod
    def validate_call_id(cls, value: str | None) -> str | None:
        return None if value is None else _identifier(value, "call_id")

    @field_validator("tool_name")
    @classmethod
    def validate_tool_name(cls, value: str | None) -> str | None:
        return None if value is None else _name(value, "tool_name")

    @model_validator(mode="after")
    def validate_tool_binding(self) -> AuditEvent:
        if self.tool_identity is not None:
            if self.tool_name is None:
                raise ValueError("tool_name is required when tool_identity is present")
            if self.tool_identity.name != self.tool_name:
                raise ValueError("tool_name must match tool_identity.name")
        return self

    @field_validator("data")
    @classmethod
    def validate_data(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        canonical_json(value)
        return value
