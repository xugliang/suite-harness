"""Replaceable strategy contracts and non-strategy execution dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import JsonValue

from suiteharness.execution.models import (
    ApprovalBinding,
    ApprovalTarget,
    AuditEvent,
    CapabilityGrant,
    PromptEnvelope,
    RunRequest,
    ToolAuthorization,
    ToolIdentity,
    ToolSpec,
    WorkflowDecision,
    WorkflowFrame,
)
from suiteharness.runtime.scopes import (
    RequestScope,
    ScopeKind,
    ScopePath,
    ServiceBindings,
    ServiceKey,
)


@dataclass(frozen=True, slots=True)
class ToolCallContext:
    scope: RequestScope
    run_id: str
    call_id: str
    tool_identity: ToolIdentity
    tool: ToolSpec
    remaining_seconds: float


@runtime_checkable
class ToolHandler(Protocol):
    async def __call__(
        self,
        context: ToolCallContext,
        arguments: dict[str, JsonValue],
    ) -> JsonValue: ...


class ToolInputValidator(Protocol):
    def validate(self, instance: object) -> None: ...


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    registration_scope: ScopePath
    identity: ToolIdentity
    spec: ToolSpec
    handler: ToolHandler
    input_validator: ToolInputValidator


class ToolRegistry(Protocol):
    """Root-owned registry; callers can only resolve through a RequestScope."""

    def resolve(self, scope: RequestScope, tool_name: str) -> RegisteredTool | None: ...


class CapabilityAuthority(Protocol):
    async def resolve(self, grant_id: str) -> CapabilityGrant | None: ...

    async def bind_activation(
        self,
        tenant_id: str,
        product_ids: tuple[str, ...],
        owner_token: str,
    ) -> None:
        """Make subsequent grants belong to one kernel-owned activation."""

    async def release_activation(self, owner_token: str) -> None:
        """Revoke grants and remove scopes owned by an exact activation."""


class ApprovalStore(Protocol):
    async def consume_if_matches(
        self,
        token: str,
        expected: ApprovalTarget,
    ) -> ApprovalBinding | None: ...


class InteractiveApprovalBroker(Protocol):
    """Ask an authenticated interactive channel about one exact pending call."""

    async def request_approval(
        self,
        request: RunRequest,
        target: ApprovalTarget,
        spec: ToolSpec,
        arguments: dict[str, JsonValue],
    ) -> ApprovalBinding | None: ...


class ToolAuthorizationPolicy(Protocol):
    """Trusted host policy; products and model output cannot replace it."""

    def evaluate(
        self,
        request: RunRequest,
        identity: ToolIdentity,
        spec: ToolSpec,
        arguments: dict[str, JsonValue],
    ) -> ToolAuthorization: ...


class AuditJournal(Protocol):
    async def append(self, event: AuditEvent) -> None: ...


@runtime_checkable
class Workflow(Protocol):
    async def next(
        self,
        request: RunRequest,
        frame: WorkflowFrame,
        prompt: PromptEnvelope | None,
    ) -> WorkflowDecision: ...


@runtime_checkable
class PromptStrategy(Protocol):
    async def render(self, request: RunRequest, frame: WorkflowFrame) -> PromptEnvelope: ...


@runtime_checkable
class ReflectionStrategy(Protocol):
    async def review(
        self,
        request: RunRequest,
        frame: WorkflowFrame,
        decision: WorkflowDecision,
    ) -> WorkflowDecision: ...


# Strategies may be supplied by a product.  There is intentionally no ServiceKey
# for ExecutionRunner: the trusted host constructs and owns that security boundary.
WORKFLOW = ServiceKey(
    "execution.workflow",
    Workflow,
    ScopeKind.ROOT,
    frozenset({ScopeKind.PRODUCT}),
)
PROMPT_STRATEGY = ServiceKey(
    "execution.prompt",
    PromptStrategy,
    ScopeKind.ROOT,
    frozenset({ScopeKind.PRODUCT}),
)
REFLECTION_STRATEGY = ServiceKey(
    "execution.reflection",
    ReflectionStrategy,
    ScopeKind.ROOT,
    frozenset({ScopeKind.PRODUCT}),
)


@dataclass(frozen=True, slots=True)
class ExecutionStrategies:
    workflow: Workflow
    prompt: PromptStrategy | None = None
    reflection: ReflectionStrategy | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.workflow, Workflow):
            raise TypeError("workflow does not implement Workflow")
        if self.prompt is not None and not isinstance(self.prompt, PromptStrategy):
            raise TypeError("prompt does not implement PromptStrategy")
        if self.reflection is not None and not isinstance(self.reflection, ReflectionStrategy):
            raise TypeError("reflection does not implement ReflectionStrategy")

    @classmethod
    def from_bindings(cls, bindings: ServiceBindings) -> ExecutionStrategies:
        return cls(
            workflow=bindings.resolve(WORKFLOW),
            prompt=(bindings.resolve(PROMPT_STRATEGY) if bindings.contains(PROMPT_STRATEGY) else None),
            reflection=(
                bindings.resolve(REFLECTION_STRATEGY)
                if bindings.contains(REFLECTION_STRATEGY)
                else None
            ),
        )


def registration_candidates(scope: RequestScope) -> tuple[ScopePath, ...]:
    """Return deterministic most-specific-to-root lookup scopes."""

    path = scope.path
    product = (
        path
        if path.kind is ScopeKind.PRODUCT
        else ScopePath.product(
            scope.tenant_id,
            scope.product_id,
        )
    )
    return (product, ScopePath.tenant(scope.tenant_id), ScopePath.root())
