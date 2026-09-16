"""Trusted, same-product agent delegation behind an ordinary tool call."""

from __future__ import annotations

import asyncio
import hashlib
import json
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from pydantic import JsonValue

from suiteharness.execution.models import (
    AuditEvent,
    AuditEventType,
    CapabilityGrant,
    ExecutionBudget,
    PromptEnvelope,
    PromptMessage,
    RunRequest,
    RunResult,
    RunStatus,
    ToolIdentity,
)
from suiteharness.execution.protocols import (
    AuditJournal,
    ExecutionStrategies,
    PromptStrategy,
    ToolCallContext,
    ToolHandlerDenied,
    ToolRegistry,
    Workflow,
)
from suiteharness.runtime.scopes import ProductContext, RequestScope

from .roles import ConfiguredAgentRoleRegistry
from .tasks import (
    TERMINAL_TASK_STATUSES,
    AgentTaskCancelledError,
    AgentTaskIdempotencyConflictError,
    AgentTaskRecord,
    AgentTaskStatus,
    AgentTaskStore,
)

_MAX_TASK_BYTES = 4096
_MAX_RESULT_BYTES = 8192


class DelegationDenied(ToolHandlerDenied):
    """The caller cannot authorize the requested delegated role or tool set."""


class DelegationAuthority(Protocol):
    async def resolve(self, grant_id: str) -> CapabilityGrant | None: ...

    async def issue_child(
        self,
        parent_grant_id: str,
        scope: object,
        *,
        tool_identities: tuple[ToolIdentity, ...],
        capabilities: frozenset[str],
        principal_id: str,
        lifetime: timedelta,
    ) -> CapabilityGrant: ...

    async def revoke(self, grant_id: str) -> bool: ...


class TrustedChildActivation(Protocol):
    def product(self, product_id: str) -> ProductContext: ...

    def strategies(self, product_id: str) -> ExecutionStrategies: ...

    async def run_trusted_child(
        self,
        request: RunRequest,
        strategies: ExecutionStrategies,
        aggregate_budget: object,
    ) -> RunResult: ...


class DelegationService(ABC):
    @abstractmethod
    async def delegate(
        self, context: ToolCallContext, role_id: str, task: str
    ) -> JsonValue: ...


class DisabledDelegationService(DelegationService):
    async def delegate(
        self, context: ToolCallContext, role_id: str, task: str
    ) -> JsonValue:
        del context, role_id, task
        raise DelegationDenied("agent delegation is disabled")


class RoleRoutedWorkflow(Workflow):
    """Custom opt-in workflows must explicitly honor a child role's model route."""

    @abstractmethod
    def with_role_route(self, role_id: str, model_route: str) -> Workflow: ...


class _RolePromptStrategy(PromptStrategy):
    def __init__(self, base: PromptStrategy | None, instruction: str) -> None:
        self._base = base or PromptStrategy()
        self._instruction = instruction

    async def render(self, request: RunRequest, frame: object) -> PromptEnvelope:
        envelope = await self._base.render(request, frame)
        if not isinstance(envelope, PromptEnvelope):
            raise TypeError("product prompt returned an invalid envelope")
        return envelope.model_copy(update={
            "messages": (
                *envelope.messages[:1],
                PromptMessage(role="system", content=self._instruction),
                *envelope.messages[1:],
            )
        })


class ControlledDelegationService(DelegationService):
    """Issue a narrowed child grant and re-enter the fixed product runner."""

    def __init__(
        self,
        *,
        activation: TrustedChildActivation,
        roles: ConfiguredAgentRoleRegistry,
        tools: ToolRegistry,
        authority: DelegationAuthority,
        tasks: AgentTaskStore,
        journal: AuditJournal,
    ) -> None:
        self._activation = activation
        self._roles = roles
        self._tools = tools
        self._authority = authority
        self._tasks = tasks
        self._journal = journal

    async def delegate(
        self, context: ToolCallContext, role_id: str, task: str
    ) -> JsonValue:
        if not isinstance(context, ToolCallContext) or context.grant_id is None:
            raise DelegationDenied("a trusted parent run and grant are required")
        if context.cancel_signal is not None and context.cancel_signal.is_set():
            raise DelegationDenied("parent run was cancelled")
        if (
            not isinstance(task, str)
            or not task.strip()
            or len(task.encode("utf-8")) > _MAX_TASK_BYTES
        ):
            raise DelegationDenied("delegated task exceeds its bounded input")
        parent_scope = context.scope
        role = self._roles.resolve(parent_scope.product_id, role_id)
        policy = self._roles.policy(parent_scope.product_id)
        if role is None or policy is None:
            raise DelegationDenied("delegated role is not enabled for this product")
        if context.aggregate_budget is None or context.aggregate_budget.remaining_seconds <= 0:
            raise DelegationDenied("parent aggregate budget is unavailable")
        protected = self._tools.resolve(parent_scope, "agent.delegate")
        if protected is None or protected.identity != context.tool_identity:
            raise DelegationDenied("delegation tool identity is not trusted")
        parent_grant = await self._authority.resolve(context.grant_id)
        now = datetime.now(UTC)
        if (
            parent_grant is None
            or parent_grant.tenant_id != parent_scope.tenant_id
            or parent_grant.product_id != parent_scope.product_id
            or parent_grant.principal_id != parent_scope.principal_id
            or context.tool_identity not in parent_grant.tool_identities
            or not parent_grant.issued_at <= now < parent_grant.expires_at
        ):
            raise DelegationDenied("parent grant cannot authorize this delegation")
        product = self._activation.product(parent_scope.product_id)
        if product.path.tenant_id != parent_scope.tenant_id:
            raise DelegationDenied("parent scope does not match the active product")
        identities: list[ToolIdentity] = []
        capabilities: set[str] = set()
        for alias in role.allowed_tools:
            registered = self._tools.resolve(parent_scope, alias)
            if registered is None:
                raise DelegationDenied("configured role tool is not registered")
            if (
                registered.identity not in parent_grant.tool_identities
                or registered.spec.required_capabilities - parent_grant.capabilities
            ):
                continue
            identities.append(registered.identity)
            capabilities.update(registered.spec.required_capabilities)
        # A replay must not reveal a previous child's output under a new lease,
        # narrower tools, or another authenticated owner/channel/session.
        task_digest = hashlib.sha256(json.dumps({
            "task": task,
            "role_id": role.role_id,
            "role_instruction": role.instruction,
            "model_route": role.model_route,
            "grant_id": context.grant_id,
            "allowed_tools": sorted(identity.canonical_id for identity in identities),
            "read_only": context.read_only,
            "agent_id": parent_scope.path.agent_id,
            "session_id": parent_scope.path.session_id,
            "session_owner_id": parent_scope.effective_session_owner_id,
            "principal_id": parent_scope.principal_id,
            "channel_id": parent_scope.channel_id,
            "roles": sorted(parent_scope.roles),
            "purpose": parent_scope.purpose,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        prior = await self._tasks.get_by_call(
            parent_scope, context.run_id, context.call_id
        )
        if prior is not None:
            if prior.role_id != role.role_id or prior.task_digest != task_digest:
                raise DelegationDenied("delegation call conflicts with its recorded task")
            if prior.status in TERMINAL_TASK_STATUSES:
                return self._recorded_result(prior)
            raise DelegationDenied("delegation call is already in progress")
        remaining = min(
            context.remaining_seconds,
            context.aggregate_budget.remaining_seconds,
            (parent_grant.expires_at - now).total_seconds(),
            45.0,
        ) - 0.1
        if remaining <= 0.1:
            raise DelegationDenied("parent deadline has expired")
        if not await context.aggregate_budget.reserve_child(
            context.call_id, ceiling=policy.max_children
        ):
            raise DelegationDenied("parent child quota is exhausted")

        try:
            record = await self._tasks.create(
                parent_scope, context.run_id, context.call_id, role.role_id, task_digest,
            )
        except (AgentTaskCancelledError, AgentTaskIdempotencyConflictError) as error:
            raise DelegationDenied(str(error)) from error
        child = None
        child_grant: CapabilityGrant | None = None
        child_slot_acquired = False
        try:
            child = product.agent(
                record.child_agent_id,
                record.child_session_id,
                principal_id=parent_scope.principal_id,
                channel_id=parent_scope.channel_id,
                roles=parent_scope.roles,
                purpose="delegated-agent",
                request_id=parent_scope.request_id,
                correlation_id=parent_scope.correlation_id,
            )
            child_scope = RequestScope(
                path=child.path,
                principal_id=parent_scope.principal_id,
                channel_id=parent_scope.channel_id,
                roles=parent_scope.roles,
                purpose="delegated-agent",
                request_id=parent_scope.request_id,
                correlation_id=parent_scope.correlation_id,
                session_owner_id=parent_scope.effective_session_owner_id,
            )
            child_slot_acquired = await context.aggregate_budget.take_child_slot(
                policy.max_parallel
            )
            if not child_slot_acquired:
                await self._tasks.transition(
                    parent_scope, record.task_id, AgentTaskStatus.QUEUED,
                    AgentTaskStatus.TIMED_OUT, parent_run_id=context.run_id,
                    failure="delegated child deadline expired while waiting",
                )
                return {"task_id": record.task_id, "status": "TIMED_OUT", "output": None}
            child_grant = await self._authority.issue_child(
                context.grant_id,
                child_scope.path,
                tool_identities=tuple(identities),
                capabilities=frozenset(capabilities),
                principal_id=parent_scope.principal_id,
                lifetime=timedelta(seconds=remaining),
            )
            if (
                child_grant.tenant_id != parent_scope.tenant_id
                or child_grant.product_id != parent_scope.product_id
                or child_grant.principal_id != parent_scope.principal_id
                or child_grant.tool_identities != frozenset(identities)
                or child_grant.capabilities != frozenset(capabilities)
                or child_grant.expires_at > parent_grant.expires_at
            ):
                raise DelegationDenied("child authority returned a broader grant")
            running = await self._tasks.transition(
                parent_scope, record.task_id, AgentTaskStatus.QUEUED, AgentTaskStatus.RUNNING,
                parent_run_id=context.run_id,
            )
            if running is None:
                raise DelegationDenied("child task was cancelled before execution")
            await self._journal.append(AuditEvent(
                event_id=uuid4().hex,
                event_type=AuditEventType.AGENT_DELEGATED,
                occurred_at=datetime.now(UTC),
                tenant_id=parent_scope.tenant_id,
                product_id=parent_scope.product_id,
                principal_id=parent_scope.principal_id,
                request_id=parent_scope.request_id,
                correlation_id=parent_scope.correlation_id,
                purpose=parent_scope.purpose,
                run_id=context.run_id,
                channel_id=parent_scope.channel_id,
                call_id=context.call_id,
                tool_name="agent.delegate",
                tool_identity=context.tool_identity,
                data={
                    "task_id": record.task_id,
                    "child_run_id": record.child_run_id,
                    "role_id": role.role_id,
                },
            ))
            budget = ExecutionBudget(
                max_iterations=min(4, context.aggregate_budget.max_iterations),
                max_tool_calls=min(8, context.aggregate_budget.max_tool_calls),
                max_parallel_calls=min(policy.max_parallel, 2),
                timeout_seconds=remaining,
            )
            request = RunRequest(
                run_id=record.child_run_id,
                scope=child_scope,
                grant_id=child_grant.grant_id,
                input=task,
                read_only=context.read_only,
                budget=budget,
            )
            base = self._activation.strategies(parent_scope.product_id)
            workflow = base.workflow
            from suiteharness.agents.react import ProductRoutedReActWorkflow, ReActWorkflow

            if isinstance(workflow, ProductRoutedReActWorkflow | ReActWorkflow):
                workflow = ReActWorkflow(
                    gateway=workflow.gateway,
                    route_id=role.model_route,
                    model=workflow.model,
                    temperature=workflow.temperature,
                    max_output_tokens=workflow.max_output_tokens,
                    tool_choice=workflow.tool_choice,
                )
            elif isinstance(workflow, RoleRoutedWorkflow):
                workflow = workflow.with_role_route(role.role_id, role.model_route)
                if not isinstance(workflow, Workflow):
                    raise DelegationDenied("custom workflow returned an invalid role model route")
            else:
                raise DelegationDenied("product workflow cannot honor the role model route")
            strategies = ExecutionStrategies(
                workflow=workflow,
                prompt=_RolePromptStrategy(base.prompt, role.instruction),
                reflection=base.reflection,
            )
            result = await self._activation.run_trusted_child(
                request, strategies, context.aggregate_budget
            )
            current_parent = await self._authority.resolve(context.grant_id)
            now = datetime.now(UTC)
            if (
                current_parent != parent_grant
                or not parent_grant.issued_at <= now < parent_grant.expires_at
                or (context.cancel_signal is not None and context.cancel_signal.is_set())
            ):
                await self._tasks.cancel_children(parent_scope, context.run_id)
                return {
                    "task_id": record.task_id,
                    "status": AgentTaskStatus.CANCELLED.value,
                    "output": None,
                }
            return await self._finalize(parent_scope, record.task_id, context.run_id, result)
        except asyncio.CancelledError:
            await self._tasks.cancel_children(parent_scope, context.run_id)
            raise
        except Exception:
            updated = await self._tasks.transition(
                parent_scope, record.task_id, AgentTaskStatus.RUNNING,
                AgentTaskStatus.FAILED, parent_run_id=context.run_id,
                failure="delegated child infrastructure failed",
            )
            if updated is None:
                await self._tasks.transition(
                    parent_scope, record.task_id, AgentTaskStatus.QUEUED,
                    AgentTaskStatus.FAILED, parent_run_id=context.run_id,
                    failure="delegated child failed before execution",
                )
            raise
        finally:
            if child_grant is not None:
                await self._authority.revoke(child_grant.grant_id)
            if child_slot_acquired:
                await context.aggregate_budget.release_child_slot()
            if child is not None:
                await child.close()

    async def _finalize(
        self, parent_scope: RequestScope, task_id: str, parent_run_id: str, result: RunResult
    ) -> JsonValue:
        target = {
            RunStatus.SUCCEEDED: AgentTaskStatus.SUCCEEDED,
            RunStatus.CANCELLED: AgentTaskStatus.CANCELLED,
            RunStatus.TIMED_OUT: AgentTaskStatus.TIMED_OUT,
        }.get(result.status, AgentTaskStatus.FAILED)
        output: JsonValue | None = result.output if target is AgentTaskStatus.SUCCEEDED else None
        encoded = None
        if output is not None:
            encoded = json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > _MAX_RESULT_BYTES:
                target = AgentTaskStatus.FAILED
                output = None
                encoded = None
        updated = await self._tasks.transition(
            parent_scope, task_id, AgentTaskStatus.RUNNING, target,
            parent_run_id=parent_run_id,
            result=encoded,
            failure=(
                None if target is AgentTaskStatus.SUCCEEDED
                else "delegated child did not complete"
            ),
        )
        if updated is None:
            raise DelegationDenied("child task no longer owns a successful terminal transition")
        return {
            "task_id": task_id,
            "status": target.value,
            "output": output,
        }

    @staticmethod
    def _recorded_result(record: AgentTaskRecord) -> JsonValue:
        return {
            "task_id": record.task_id,
            "status": record.status.value,
            "output": (
                json.loads(record.result)
                if record.status is AgentTaskStatus.SUCCEEDED and record.result is not None
                else None
            ),
        }
