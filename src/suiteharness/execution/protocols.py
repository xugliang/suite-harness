"""Replaceable strategy contracts and non-strategy execution dependencies."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import JsonValue

from suiteharness.execution.events import WorkflowEvent
from suiteharness.execution.models import (
    ApprovalBinding,
    ApprovalTarget,
    AuditEvent,
    CapabilityGrant,
    ConversationMessage,
    PromptEnvelope,
    PromptMessage,
    RunRequest,
    ToolAuthorization,
    ToolIdentity,
    ToolSpec,
    WorkflowDecision,
    WorkflowFrame,
)
from suiteharness.runtime.effects import EffectScope
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
    """Structural callable contract so plain async functions remain supported."""
    async def __call__(
        self,
        context: ToolCallContext,
        arguments: dict[str, JsonValue],
    ) -> JsonValue: ...


class ToolInputValidator(Protocol):
    """Structural view of third-party jsonschema validator instances."""
    def validate(self, instance: object) -> None: ...


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    registration_scope: ScopePath
    identity: ToolIdentity
    spec: ToolSpec
    handler: ToolHandler
    input_validator: ToolInputValidator


class ToolRegistry(ABC):
    """Root-owned registry; callers can only resolve through a RequestScope."""

    @abstractmethod
    def resolve(self, scope: RequestScope, tool_name: str) -> RegisteredTool | None: ...


class ScopedToolRegistry(ToolRegistry):
    """Root-owned registry surface exposed narrowly during trusted install."""

    @abstractmethod
    def register_owned(
        self,
        scope: ScopePath,
        effects: EffectScope,
        spec: ToolSpec,
        handler: ToolHandler,
    ) -> object: ...


class CapabilityAuthority(ABC):
    @abstractmethod
    async def resolve(self, grant_id: str) -> CapabilityGrant | None: ...

    @abstractmethod
    async def bind_activation(
        self,
        tenant_id: str,
        product_ids: tuple[str, ...],
        owner_token: str,
    ) -> None:
        """Make subsequent grants belong to one kernel-owned activation."""
        raise NotImplementedError

    @abstractmethod
    async def release_activation(self, owner_token: str) -> None:
        """Revoke grants and remove scopes owned by an exact activation."""
        raise NotImplementedError


class ApprovalStore(ABC):
    @abstractmethod
    async def consume_if_matches(
        self,
        token: str,
        expected: ApprovalTarget,
    ) -> ApprovalBinding | None: ...


class InteractiveApprovalBroker(ABC):
    """Ask an authenticated interactive channel about one exact pending call."""

    @abstractmethod
    async def request_approval(
        self,
        request: RunRequest,
        target: ApprovalTarget,
        spec: ToolSpec,
        arguments: dict[str, JsonValue],
    ) -> ApprovalBinding | None: ...


class ToolAuthorizationPolicy(ABC):
    """Trusted host policy; products and model output cannot replace it."""

    @abstractmethod
    def evaluate(
        self,
        request: RunRequest,
        identity: ToolIdentity,
        spec: ToolSpec,
        arguments: dict[str, JsonValue],
    ) -> ToolAuthorization: ...


class AuditJournal(ABC):
    @abstractmethod
    async def append(self, event: AuditEvent) -> None: ...


class Workflow(ABC):
    async def stream(
        self, request: RunRequest, frame: WorkflowFrame, prompt: PromptEnvelope | None,
    ) -> AsyncIterator[WorkflowEvent | WorkflowDecision]:
        """Default for atomic business steps; incremental workflows override this."""
        yield await self.next(request, frame, prompt)

    @abstractmethod
    async def next(
        self,
        request: RunRequest,
        frame: WorkflowFrame,
        prompt: PromptEnvelope | None,
    ) -> WorkflowDecision: ...


class PromptStrategy(ABC):
    """Generic bounded conversation renderer, customizable by subclasses."""

    system_prompt: str = (
        "You are an enterprise AI agent. Follow company policy, use only the tools "
        "provided to you, and never claim that a tool ran before receiving its result."
    )

    async def render(self, request: RunRequest, frame: WorkflowFrame) -> PromptEnvelope:
        del frame
        participant_aliases: dict[str, str] = {}
        history: list[PromptMessage] = []
        for item in request.conversation.messages:
            alias: str | None = None
            if item.role == "user" and item.principal_id != request.scope.principal_id:
                assert item.principal_id is not None
                alias = participant_aliases.setdefault(
                    item.principal_id,
                    f"participant-{len(participant_aliases) + 1}",
                )
            history.append(self._history_message(item, participant_alias=alias))
        memory_messages = ()
        if request.conversation.working_summary or request.conversation.long_term_context:
            memory_messages = (PromptMessage(role='assistant', content=json.dumps({
                'context_type': 'conversation_summary',
                'trust': 'untrusted',
                'working_summary': request.conversation.working_summary,
                'long_term_context': request.conversation.long_term_context,
                'degraded': request.conversation.memory_degraded,
            }, ensure_ascii=False), context_category="conversation_summary"),)
        return PromptEnvelope(
            messages=(
                PromptMessage(role="system", content=self.system_prompt, context_category="system_prompt"),
                *memory_messages,
                *history,
                PromptMessage(role="user", content=self._text(request.input), context_category="current_question"),
            ),
            metadata={
                "run_id": request.run_id,
                "tenant_id": request.scope.tenant_id,
                "product_id": request.scope.product_id,
                "conversation_truncated": request.conversation.truncated,
                "memory_degraded": request.conversation.memory_degraded,
            },
        )

    @classmethod
    def _history_message(
        cls,
        message: ConversationMessage,
        *,
        participant_alias: str | None,
    ) -> PromptMessage:
        content = cls._text(message.content)
        if participant_alias is not None:
            # Stable only inside this bounded prompt window: the external model
            # can distinguish speakers without receiving company principal ids.
            content = f"[authenticated {participant_alias}]\n{content}"
        return PromptMessage(role=message.role, content=content, context_category="history_memory")

    @staticmethod
    def _text(value: object) -> str:
        return (
            value
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )


class ReflectionStrategy(ABC):
    async def review(
        self,
        request: RunRequest,
        frame: WorkflowFrame,
        decision: WorkflowDecision,
    ) -> WorkflowDecision:
        """Accept the decision unchanged unless a reviewer overrides this hook."""
        return decision


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
    skills: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.workflow, Workflow):
            raise TypeError("workflow does not implement Workflow")
        if self.prompt is not None and not isinstance(self.prompt, PromptStrategy):
            raise TypeError("prompt does not implement PromptStrategy")
        if self.reflection is not None and not isinstance(self.reflection, ReflectionStrategy):
            raise TypeError("reflection does not implement ReflectionStrategy")
        if self.skills is not None:
            from suiteharness.skills.protocols import SkillProvider
            if not isinstance(self.skills, SkillProvider):
                raise TypeError("skills must implement SkillProvider")

    @classmethod
    def from_bindings(cls, bindings: ServiceBindings) -> ExecutionStrategies:
        from suiteharness.skills.services import SKILL_PROVIDER
        return cls(
            workflow=bindings.resolve(WORKFLOW),
            prompt=(bindings.resolve(PROMPT_STRATEGY) if bindings.contains(PROMPT_STRATEGY) else None),
            reflection=(
                bindings.resolve(REFLECTION_STRATEGY)
                if bindings.contains(REFLECTION_STRATEGY)
                else None
            ),
            skills=bindings.resolve(SKILL_PROVIDER) if bindings.contains(SKILL_PROVIDER) else None,
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
