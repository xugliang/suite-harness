"""The non-replaceable execution path for every product-originated tool call."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import JsonValue, TypeAdapter, ValidationError

from suiteharness.execution.models import (
    ApprovalBinding,
    ApprovalTarget,
    AuditEvent,
    AuditEventType,
    CapabilityGrant,
    FailureCode,
    PromptEnvelope,
    RunFailure,
    RunRequest,
    RunResult,
    RunStatus,
    ToolAuthorization,
    ToolCallStatus,
    ToolEffect,
    ToolIdentity,
    ToolIntent,
    ToolObservation,
    ToolSpec,
    WorkflowDecision,
    WorkflowDecisionKind,
    WorkflowFrame,
    arguments_digest,
    canonical_json,
)
from suiteharness.execution.protocols import (
    ApprovalStore,
    AuditJournal,
    CapabilityAuthority,
    ExecutionStrategies,
    InteractiveApprovalBroker,
    ToolAuthorizationPolicy,
    ToolCallContext,
    ToolRegistry,
)
from suiteharness.runtime.scopes import RequestScope, ScopePath

_JSON = TypeAdapter(JsonValue)
_ABSOLUTE_BYTE_LIMITS = {
    "max_run_input_bytes": 8 * 1024 * 1024,
    "max_tool_argument_bytes": 8 * 1024 * 1024,
    "max_tool_result_bytes": 32 * 1024 * 1024,
    "max_final_output_bytes": 32 * 1024 * 1024,
    "max_workflow_state_bytes": 8 * 1024 * 1024,
}
_ABSOLUTE_BUDGET_LIMITS = {
    "max_iterations": 10_000,
    "max_tool_calls": 100_000,
    "max_parallel_calls": 1_024,
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """Root-owned policy.  Product strategy bindings cannot replace it."""

    approval_effects: frozenset[ToolEffect] = frozenset(
        {ToolEffect.WRITE, ToolEffect.DESTRUCTIVE, ToolEffect.EXTERNAL}
    )
    max_run_input_bytes: int = 256 * 1024
    max_tool_argument_bytes: int = 256 * 1024
    max_tool_result_bytes: int = 1024 * 1024
    max_final_output_bytes: int = 1024 * 1024
    max_workflow_state_bytes: int = 256 * 1024
    max_iterations: int = 16
    max_tool_calls: int = 32
    max_parallel_calls: int = 4
    max_timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        effects = frozenset(ToolEffect(effect) for effect in self.approval_effects)
        # Destructive approval is an invariant, not a deployment preference.
        object.__setattr__(self, "approval_effects", effects | {ToolEffect.DESTRUCTIVE})
        for name, maximum in _ABSOLUTE_BYTE_LIMITS.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 1 or value > maximum:
                raise ValueError(f"{name} must be between 1 and {maximum}")
        for name, maximum in _ABSOLUTE_BUDGET_LIMITS.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 1 or value > maximum:
                raise ValueError(f"{name} must be between 1 and {maximum}")
        timeout = self.max_timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not math.isfinite(timeout)
        ):
            raise TypeError("max_timeout_seconds must be a finite number")
        if timeout <= 0 or timeout > 86_400:
            raise ValueError("max_timeout_seconds must be between 0 and 86400")

    def requires_approval(self, spec: ToolSpec) -> bool:
        return bool(spec.effects & self.approval_effects)


@dataclass(slots=True)
class _RunState:
    started_at: datetime
    started_monotonic: float
    iterations_used: int = 0
    tool_calls_used: int = 0
    observations: list[ToolObservation] = field(default_factory=list)
    call_ids: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _ActiveRun:
    cancel: asyncio.Event
    state: _RunState


class ExecutionRunner:
    """Trusted executor that applies security policy after all replaceable strategies.

    The registry, grant authority, approval store and journal are constructor-owned
    infrastructure.  Only Workflow/Prompt/Reflection are product replaceable.
    """

    def __init__(
        self,
        *,
        tools: ToolRegistry,
        capabilities: CapabilityAuthority,
        approvals: ApprovalStore,
        journal: AuditJournal,
        policy: ExecutionPolicy | None = None,
        authorization_policy: ToolAuthorizationPolicy | None = None,
        interactive_approvals: InteractiveApprovalBroker | None = None,
    ) -> None:
        self.__tools = tools
        self.__capabilities = capabilities
        self.__approvals = approvals
        self.__journal = journal
        self.__policy = policy or ExecutionPolicy()
        self.__authorization_policy = authorization_policy
        self.__interactive_approvals = interactive_approvals
        self.__active: dict[tuple[ScopePath, str, str], _ActiveRun] = {}
        self.__active_lock = asyncio.Lock()

    async def cancel(self, scope: RequestScope, run_id: str) -> bool:
        """Cancel only a run owned by the same authenticated path and principal."""

        key = self._run_key(scope, run_id)
        async with self.__active_lock:
            active = self.__active.get(key)
            if active is None:
                return False
            active.cancel.set()
            return True

    async def run(self, request: RunRequest, strategies: ExecutionStrategies) -> RunResult:
        """Execute one run with an overall timeout and structured termination."""

        state = _RunState(started_at=_utcnow(), started_monotonic=time.monotonic())
        cancel_event = asyncio.Event()
        run_key = self._run_key(request.scope, request.run_id)
        async with self.__active_lock:
            if run_key in self.__active:
                return self._result(
                    request,
                    state,
                    RunStatus.FAILED,
                    failure=RunFailure(
                        code=FailureCode.RUN_CONFLICT,
                        message="a run with this run_id is already active",
                    ),
                )
            self.__active[run_key] = _ActiveRun(cancel=cancel_event, state=state)

        loop_task = asyncio.create_task(self._run_loop(request, strategies, state))
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {loop_task, cancel_task},
                timeout=request.budget.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if loop_task in done:
                cancel_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_task
                try:
                    return await loop_task
                except Exception:
                    result = self._result(
                        request,
                        state,
                        RunStatus.FAILED,
                        failure=RunFailure(
                            code=FailureCode.INTERNAL_ERROR,
                            message="trusted execution infrastructure failed",
                        ),
                    )
                    return result

            if cancel_task in done:
                return await self._terminate(
                    request,
                    state,
                    loop_task,
                    status=RunStatus.CANCELLED,
                    code=FailureCode.CANCELLED,
                    message="run was cancelled",
                )

            return await self._terminate(
                request,
                state,
                loop_task,
                status=RunStatus.TIMED_OUT,
                code=FailureCode.TIMED_OUT,
                message="run exceeded its time budget",
            )
        except asyncio.CancelledError:
            # API callers receive a structured result.  The strategy/handler task
            # is still cancelled, so product code cannot continue in the background.
            cancel_event.set()
            return await self._terminate(
                request,
                state,
                loop_task,
                status=RunStatus.CANCELLED,
                code=FailureCode.CANCELLED,
                message="caller cancelled the run",
            )
        finally:
            cancel_task.cancel()
            async with self.__active_lock:
                self.__active.pop(run_key, None)

    async def _run_loop(
        self,
        request: RunRequest,
        strategies: ExecutionStrategies,
        state: _RunState,
    ) -> RunResult:
        await self._emit(request, AuditEventType.RUN_STARTED)
        budget_failure = self._budget_policy_failure(request)
        if budget_failure is not None:
            return await self._finish(
                request,
                self._result(
                    request,
                    state,
                    RunStatus.BUDGET_EXCEEDED,
                    failure=budget_failure,
                ),
            )
        if len(canonical_json(request.input)) > self.__policy.max_run_input_bytes:
            return await self._finish(
                request,
                self._result(
                    request,
                    state,
                    RunStatus.FAILED,
                    failure=RunFailure(
                        code=FailureCode.RUN_INPUT_TOO_LARGE,
                        message="run input exceeds the configured byte limit",
                    ),
                ),
            )
        grant, failure = await self._grant(request)
        if failure is not None:
            return await self._finish(
                request,
                self._result(request, state, RunStatus.FAILED, failure=failure),
            )
        assert grant is not None

        workflow_state: JsonValue = None
        for iteration in range(1, request.budget.max_iterations + 1):
            state.iterations_used = iteration
            grant, failure = await self._grant(request)
            if failure is not None:
                return await self._finish(
                    request,
                    self._result(request, state, RunStatus.FAILED, failure=failure),
                )
            assert grant is not None
            available = self._visible_tools(request, grant)
            frame = WorkflowFrame(
                iteration=iteration,
                observations=tuple(item.model_copy(deep=True) for item in state.observations),
                available_tools=available,
                workflow_state=workflow_state,
                tool_calls_used=state.tool_calls_used,
                elapsed_seconds=max(0.0, time.monotonic() - state.started_monotonic),
            )

            prompt = None
            if strategies.prompt is not None:
                try:
                    prompt = await strategies.prompt.render(request, frame)
                except Exception:
                    return await self._strategy_failure(
                        request,
                        state,
                        FailureCode.PROMPT_ERROR,
                        "prompt strategy failed",
                        iteration,
                    )
                if not isinstance(prompt, PromptEnvelope):
                    return await self._strategy_failure(
                        request,
                        state,
                        FailureCode.PROMPT_ERROR,
                        "prompt strategy returned an invalid result",
                        iteration,
                    )
                prompt = prompt.model_copy(deep=True)

            try:
                decision = await strategies.workflow.next(request, frame, prompt)
            except Exception:
                return await self._strategy_failure(
                    request,
                    state,
                    FailureCode.WORKFLOW_ERROR,
                    "workflow strategy failed",
                    iteration,
                )
            if not isinstance(decision, WorkflowDecision):
                return await self._strategy_failure(
                    request,
                    state,
                    FailureCode.WORKFLOW_ERROR,
                    "workflow strategy returned an invalid decision",
                    iteration,
                )
            decision = decision.model_copy(deep=True)

            if strategies.reflection is not None:
                try:
                    decision = await strategies.reflection.review(request, frame, decision)
                except Exception:
                    return await self._strategy_failure(
                        request,
                        state,
                        FailureCode.REFLECTION_ERROR,
                        "reflection strategy failed",
                        iteration,
                    )
                if not isinstance(decision, WorkflowDecision):
                    return await self._strategy_failure(
                        request,
                        state,
                        FailureCode.REFLECTION_ERROR,
                        "reflection strategy returned an invalid decision",
                        iteration,
                    )
                decision = decision.model_copy(deep=True)

            await self._emit(
                request,
                AuditEventType.STRATEGY_DECISION,
                data={"kind": decision.kind.value, "intent_count": len(decision.intents)},
            )
            state_payload = canonical_json(decision.workflow_state)
            if len(state_payload) > self.__policy.max_workflow_state_bytes:
                return await self._strategy_failure(
                    request,
                    state,
                    FailureCode.WORKFLOW_STATE_TOO_LARGE,
                    "workflow state exceeds the configured byte limit",
                    iteration,
                )
            workflow_state = cast(JsonValue, json.loads(state_payload.decode("utf-8")))
            if decision.kind is WorkflowDecisionKind.FINAL:
                if len(canonical_json(decision.output)) > self.__policy.max_final_output_bytes:
                    return await self._strategy_failure(
                        request,
                        state,
                        FailureCode.FINAL_OUTPUT_TOO_LARGE,
                        "final output exceeds the configured byte limit",
                        iteration,
                    )
                return await self._finish(
                    request,
                    self._result(
                        request,
                        state,
                        RunStatus.SUCCEEDED,
                        output=decision.output,
                    ),
                )

            remaining = request.budget.max_tool_calls - state.tool_calls_used
            permitted_count = min(max(remaining, 0), len(decision.intents))
            permitted = decision.intents[:permitted_count]
            overflow = decision.intents[permitted_count:]
            state.tool_calls_used += len(permitted)

            duplicate_flags: list[bool] = []
            for intent in permitted:
                duplicate_flags.append(intent.call_id in state.call_ids)
                state.call_ids.add(intent.call_id)

            observations = await self._execute_batch(
                request,
                permitted,
                tuple(duplicate_flags),
                iteration,
                state,
            )
            state.observations.extend(observations)
            if overflow:
                for intent in overflow:
                    state.observations.append(
                        await self._budget_observation(request, intent, iteration)
                    )
                return await self._finish(
                    request,
                    self._result(
                        request,
                        state,
                        RunStatus.BUDGET_EXCEEDED,
                        failure=RunFailure(
                            code=FailureCode.TOOL_BUDGET_EXCEEDED,
                            message="workflow requested more tool calls than the remaining budget",
                            iteration=iteration,
                        ),
                    ),
                )

        return await self._finish(
            request,
            self._result(
                request,
                state,
                RunStatus.BUDGET_EXCEEDED,
                failure=RunFailure(
                    code=FailureCode.ITERATION_BUDGET_EXCEEDED,
                    message="workflow did not finish within the iteration budget",
                    iteration=request.budget.max_iterations,
                ),
            ),
        )

    async def _execute_batch(
        self,
        request: RunRequest,
        intents: tuple[ToolIntent, ...],
        duplicate_flags: tuple[bool, ...],
        iteration: int,
        state: _RunState,
    ) -> tuple[ToolObservation, ...]:
        if not intents:
            return ()
        semaphore = asyncio.Semaphore(request.budget.max_parallel_calls)

        async def execute(intent: ToolIntent, duplicate: bool) -> ToolObservation:
            async with semaphore:
                return await self._execute_intent(
                    request,
                    intent,
                    iteration,
                    state,
                    duplicate=duplicate,
                )

        tasks = [
            asyncio.create_task(execute(intent, duplicate))
            for intent, duplicate in zip(intents, duplicate_flags, strict=True)
        ]
        try:
            return tuple(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _execute_intent(
        self,
        request: RunRequest,
        intent: ToolIntent,
        iteration: int,
        state: _RunState,
        *,
        duplicate: bool,
    ) -> ToolObservation:
        started_at = _utcnow()
        safe_arguments = cast(
            dict[str, JsonValue],
            json.loads(canonical_json(intent.arguments).decode("utf-8")),
        )
        argument_payload = canonical_json(safe_arguments)
        digest = arguments_digest(safe_arguments)
        # Resolve the concrete registration before making any authorization
        # decision.  The short alias is not an authority-bearing identifier.
        registered = self.__tools.resolve(request.scope, intent.tool_name)
        tool_identity = None if registered is None else registered.identity
        await self._emit(
            request,
            AuditEventType.TOOL_INTENT,
            call_id=intent.call_id,
            tool_name=intent.tool_name,
            tool_identity=tool_identity,
            data={"arguments_digest": digest},
        )

        if len(argument_payload) > self.__policy.max_tool_argument_bytes:
            return await self._deny(
                request,
                intent,
                iteration,
                started_at,
                FailureCode.TOOL_ARGUMENTS_TOO_LARGE,
                "tool arguments exceed the configured byte limit",
                tool_identity=tool_identity,
            )

        if duplicate:
            return await self._deny(
                request,
                intent,
                iteration,
                started_at,
                FailureCode.DUPLICATE_CALL_ID,
                "call_id was already used in this run",
                tool_identity=tool_identity,
            )

        grant, grant_failure = await self._grant(request)
        if grant_failure is not None:
            return await self._deny(
                request,
                intent,
                iteration,
                started_at,
                grant_failure.code,
                grant_failure.message,
                tool_identity=tool_identity,
            )
        assert grant is not None
        if registered is None or registered.identity not in grant.tool_identities:
            return await self._deny(
                request,
                intent,
                iteration,
                started_at,
                FailureCode.TOOL_NOT_GRANTED,
                "tool is unavailable for this grant",
                tool_identity=tool_identity,
            )
        capability_gap = registered.spec.required_capabilities - grant.capabilities
        if capability_gap:
            return await self._deny(
                request,
                intent,
                iteration,
                started_at,
                FailureCode.CAPABILITY_DENIED,
                "tool requires capabilities not present in the product grant",
                tool_identity=registered.identity,
            )
        if request.read_only and not registered.spec.is_read:
            return await self._deny(
                request,
                intent,
                iteration,
                started_at,
                FailureCode.READ_ONLY_DENIED,
                "non-read tool is blocked for a read-only run",
                tool_identity=registered.identity,
            )

        try:
            registered.input_validator.validate(safe_arguments)
        except JsonSchemaValidationError:
            return await self._deny(
                request,
                intent,
                iteration,
                started_at,
                FailureCode.TOOL_INPUT_INVALID,
                "tool arguments do not match the declared input schema",
                tool_identity=registered.identity,
            )

        authorization = ToolAuthorization.DEFAULT
        if self.__authorization_policy is not None:
            try:
                authorization = self.__authorization_policy.evaluate(
                    request,
                    registered.identity,
                    registered.spec,
                    safe_arguments,
                )
            except Exception:
                authorization = ToolAuthorization.DENY
            if not isinstance(authorization, ToolAuthorization):
                authorization = ToolAuthorization.DENY
        if authorization is ToolAuthorization.DENY:
            return await self._deny(
                request,
                intent,
                iteration,
                started_at,
                FailureCode.POLICY_DENIED,
                "the trusted channel policy denied this tool call",
                tool_identity=registered.identity,
            )

        approved_by: str | None = None
        destructive = ToolEffect.DESTRUCTIVE in registered.spec.effects
        requires_approval = destructive or (
            authorization is ToolAuthorization.REQUIRE_APPROVAL
            or (
                authorization is ToolAuthorization.DEFAULT
                and self.__policy.requires_approval(registered.spec)
            )
        )
        if requires_approval:
            expected = ApprovalTarget(
                scope=request.scope.path,
                principal_id=request.scope.principal_id,
                run_id=request.run_id,
                call_id=intent.call_id,
                tool_identity=registered.identity,
                arguments_digest=digest,
            )
            try:
                if intent.approval_token is not None:
                    binding = await self.__approvals.consume_if_matches(
                        intent.approval_token.get_secret_value(), expected
                    )
                elif self.__interactive_approvals is not None:
                    binding = await self.__interactive_approvals.request_approval(
                        request,
                        expected,
                        registered.spec,
                        safe_arguments,
                    )
                else:
                    return await self._deny(
                        request,
                        intent,
                        iteration,
                        started_at,
                        FailureCode.APPROVAL_REQUIRED,
                        "this tool call requires a call-scoped approval",
                        tool_identity=registered.identity,
                    )
            except Exception:
                binding = None
            if binding is None or not self._approval_matches(expected, binding):
                return await self._deny(
                    request,
                    intent,
                    iteration,
                    started_at,
                    FailureCode.APPROVAL_INVALID,
                    "approval is expired, reused, or bound to a different call",
                    tool_identity=registered.identity,
                )
            approved_by = binding.approved_by

        started_data: dict[str, JsonValue] = {
            "effects": sorted(effect.value for effect in registered.spec.effects)
        }
        if approved_by is not None:
            started_data["approved_by"] = approved_by
        await self._emit(
            request,
            AuditEventType.TOOL_STARTED,
            call_id=intent.call_id,
            tool_name=intent.tool_name,
            tool_identity=registered.identity,
            data=started_data,
        )
        context = ToolCallContext(
            scope=request.scope,
            run_id=request.run_id,
            call_id=intent.call_id,
            tool_identity=registered.identity,
            tool=registered.spec,
            remaining_seconds=max(
                0.0,
                request.budget.timeout_seconds
                - (time.monotonic() - state.started_monotonic),
            ),
        )
        try:
            raw_result = await registered.handler(context, safe_arguments)
            validated = _JSON.validate_python(raw_result, strict=True)
            result_payload = canonical_json(validated)
        except asyncio.CancelledError:
            raise
        except ValidationError:
            return await self._tool_failure(
                request,
                intent,
                iteration,
                started_at,
                FailureCode.INVALID_TOOL_RESULT,
                "tool returned a non-JSON value",
                tool_identity=registered.identity,
            )
        except (TypeError, ValueError):
            return await self._tool_failure(
                request,
                intent,
                iteration,
                started_at,
                FailureCode.INVALID_TOOL_RESULT,
                "tool returned a non-JSON value",
                tool_identity=registered.identity,
            )
        except Exception:
            return await self._tool_failure(
                request,
                intent,
                iteration,
                started_at,
                FailureCode.TOOL_ERROR,
                "tool handler failed",
                tool_identity=registered.identity,
            )

        if len(result_payload) > self.__policy.max_tool_result_bytes:
            return await self._tool_failure(
                request,
                intent,
                iteration,
                started_at,
                FailureCode.TOOL_RESULT_TOO_LARGE,
                "tool result exceeds the configured byte limit",
                tool_identity=registered.identity,
            )
        result = cast(
            JsonValue,
            json.loads(result_payload.decode("utf-8")),
        )

        finished_at = _utcnow()
        await self._emit(
            request,
            AuditEventType.TOOL_FINISHED,
            call_id=intent.call_id,
            tool_name=intent.tool_name,
            tool_identity=registered.identity,
            data={"status": ToolCallStatus.SUCCEEDED.value},
        )
        return ToolObservation(
            call_id=intent.call_id,
            tool_name=intent.tool_name,
            status=ToolCallStatus.SUCCEEDED,
            result=result,
            started_at=started_at,
            finished_at=finished_at,
        )

    async def _grant(
        self,
        request: RunRequest,
    ) -> tuple[CapabilityGrant | None, RunFailure | None]:
        try:
            grant = await self.__capabilities.resolve(request.grant_id)
        except Exception:
            return None, RunFailure(
                code=FailureCode.INTERNAL_ERROR,
                message="capability authority failed",
            )
        if grant is None:
            return None, RunFailure(
                code=FailureCode.GRANT_NOT_FOUND,
                message="capability grant was not found or was revoked",
            )
        if (
            grant.tenant_id != request.scope.tenant_id
            or grant.product_id != request.scope.product_id
            or (
                grant.principal_id is not None
                and grant.principal_id != request.scope.principal_id
            )
        ):
            return None, RunFailure(
                code=FailureCode.GRANT_SCOPE_MISMATCH,
                message="capability grant does not match the authenticated request scope",
            )
        if grant.expires_at <= _utcnow():
            return None, RunFailure(
                code=FailureCode.GRANT_EXPIRED,
                message="capability grant has expired",
            )
        return grant, None

    def _visible_tools(
        self,
        request: RunRequest,
        grant: CapabilityGrant,
    ) -> tuple[ToolSpec, ...]:
        visible: list[ToolSpec] = []
        names = {identity.name for identity in grant.tool_identities}
        for name in sorted(names):
            item = self.__tools.resolve(request.scope, name)
            if item is None:
                continue
            if item.identity not in grant.tool_identities:
                continue
            if item.spec.required_capabilities - grant.capabilities:
                continue
            if request.read_only and not item.spec.is_read:
                continue
            visible.append(item.spec.model_copy(deep=True))
        return tuple(visible)

    def _budget_policy_failure(self, request: RunRequest) -> RunFailure | None:
        requested = request.budget
        ceilings = self.__policy
        violations: list[str] = []
        if requested.max_iterations > ceilings.max_iterations:
            violations.append("max_iterations")
        if requested.max_tool_calls > ceilings.max_tool_calls:
            violations.append("max_tool_calls")
        if requested.max_parallel_calls > ceilings.max_parallel_calls:
            violations.append("max_parallel_calls")
        if requested.timeout_seconds > ceilings.max_timeout_seconds:
            violations.append("timeout_seconds")
        if not violations:
            return None
        return RunFailure(
            code=FailureCode.BUDGET_POLICY_EXCEEDED,
            message=(
                "requested execution budget exceeds root policy ceiling: "
                + ", ".join(violations)
            ),
        )

    @staticmethod
    def _approval_matches(
        expected: ApprovalTarget,
        binding: ApprovalBinding,
    ) -> bool:
        now = _utcnow()
        return (
            binding.target == expected
            and binding.issued_at <= now
            and binding.expires_at > now
            and binding.single_use is True
        )

    @staticmethod
    def _run_key(scope: RequestScope, run_id: str) -> tuple[ScopePath, str, str]:
        if not isinstance(scope, RequestScope):
            raise TypeError("scope must be a RequestScope")
        return (scope.path, scope.principal_id, run_id)

    async def _deny(
        self,
        request: RunRequest,
        intent: ToolIntent,
        iteration: int,
        started_at: datetime,
        code: FailureCode,
        message: str,
        *,
        tool_identity: ToolIdentity | None = None,
    ) -> ToolObservation:
        failure = RunFailure(
            code=code,
            message=message,
            iteration=iteration,
            call_id=intent.call_id,
            tool_name=intent.tool_name,
        )
        await self._emit(
            request,
            AuditEventType.TOOL_DENIED,
            call_id=intent.call_id,
            tool_name=intent.tool_name,
            tool_identity=tool_identity,
            data={"code": code.value},
        )
        return ToolObservation(
            call_id=intent.call_id,
            tool_name=intent.tool_name,
            status=ToolCallStatus.DENIED,
            failure=failure,
            started_at=started_at,
            finished_at=_utcnow(),
        )

    async def _tool_failure(
        self,
        request: RunRequest,
        intent: ToolIntent,
        iteration: int,
        started_at: datetime,
        code: FailureCode,
        message: str,
        *,
        tool_identity: ToolIdentity,
    ) -> ToolObservation:
        failure = RunFailure(
            code=code,
            message=message,
            iteration=iteration,
            call_id=intent.call_id,
            tool_name=intent.tool_name,
        )
        await self._emit(
            request,
            AuditEventType.TOOL_FINISHED,
            call_id=intent.call_id,
            tool_name=intent.tool_name,
            tool_identity=tool_identity,
            data={"status": ToolCallStatus.FAILED.value, "code": code.value},
        )
        return ToolObservation(
            call_id=intent.call_id,
            tool_name=intent.tool_name,
            status=ToolCallStatus.FAILED,
            failure=failure,
            started_at=started_at,
            finished_at=_utcnow(),
        )

    async def _budget_observation(
        self,
        request: RunRequest,
        intent: ToolIntent,
        iteration: int,
    ) -> ToolObservation:
        started_at = _utcnow()
        registered = self.__tools.resolve(request.scope, intent.tool_name)
        tool_identity = None if registered is None else registered.identity
        failure = RunFailure(
            code=FailureCode.TOOL_BUDGET_EXCEEDED,
            message="tool call was not started because the budget was exhausted",
            iteration=iteration,
            call_id=intent.call_id,
            tool_name=intent.tool_name,
        )
        await self._emit(
            request,
            AuditEventType.TOOL_DENIED,
            call_id=intent.call_id,
            tool_name=intent.tool_name,
            tool_identity=tool_identity,
            data={"code": FailureCode.TOOL_BUDGET_EXCEEDED.value},
        )
        return ToolObservation(
            call_id=intent.call_id,
            tool_name=intent.tool_name,
            status=ToolCallStatus.BUDGET_EXCEEDED,
            failure=failure,
            started_at=started_at,
            finished_at=_utcnow(),
        )

    async def _strategy_failure(
        self,
        request: RunRequest,
        state: _RunState,
        code: FailureCode,
        message: str,
        iteration: int,
    ) -> RunResult:
        return await self._finish(
            request,
            self._result(
                request,
                state,
                RunStatus.FAILED,
                failure=RunFailure(code=code, message=message, iteration=iteration),
            ),
        )

    async def _terminate(
        self,
        request: RunRequest,
        state: _RunState,
        loop_task: asyncio.Task[RunResult],
        *,
        status: RunStatus,
        code: FailureCode,
        message: str,
    ) -> RunResult:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
        result = self._result(
            request,
            state,
            status,
            failure=RunFailure(code=code, message=message),
        )
        try:
            return await self._finish(request, result)
        except Exception:
            return self._result(
                request,
                state,
                RunStatus.FAILED,
                failure=RunFailure(
                    code=FailureCode.INTERNAL_ERROR,
                    message="trusted execution infrastructure failed",
                ),
            )

    def _result(
        self,
        request: RunRequest,
        state: _RunState,
        status: RunStatus,
        *,
        output: JsonValue | None = None,
        failure: RunFailure | None = None,
    ) -> RunResult:
        safe_output = (
            None
            if output is None
            else cast(JsonValue, json.loads(canonical_json(output).decode("utf-8")))
        )
        return RunResult(
            run_id=request.run_id,
            status=status,
            output=safe_output,
            failure=failure,
            observations=tuple(state.observations),
            iterations_used=state.iterations_used,
            tool_calls_used=state.tool_calls_used,
            started_at=state.started_at,
            finished_at=_utcnow(),
        )

    async def _finish(self, request: RunRequest, result: RunResult) -> RunResult:
        data: dict[str, JsonValue] = {"status": result.status.value}
        if result.failure is not None:
            data["code"] = result.failure.code.value
        await self._emit(request, AuditEventType.RUN_FINISHED, data=data)
        return result

    async def _emit(
        self,
        request: RunRequest,
        event_type: AuditEventType,
        *,
        call_id: str | None = None,
        tool_name: str | None = None,
        tool_identity: ToolIdentity | None = None,
        data: dict[str, JsonValue] | None = None,
    ) -> None:
        event = AuditEvent(
            event_id=uuid.uuid4().hex,
            event_type=event_type,
            occurred_at=_utcnow(),
            tenant_id=request.scope.tenant_id,
            product_id=request.scope.product_id,
            principal_id=request.scope.principal_id,
            request_id=request.scope.request_id,
            correlation_id=request.scope.correlation_id,
            purpose=request.scope.purpose,
            run_id=request.run_id,
            channel_id=request.scope.channel_id,
            call_id=call_id,
            tool_name=tool_name,
            tool_identity=tool_identity,
            data=data or {},
        )
        await self.__journal.append(event)
