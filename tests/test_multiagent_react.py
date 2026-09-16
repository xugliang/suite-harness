"""Delegation goes through the ordinary granted ReAct tool-observation cycle."""

import asyncio

from suiteharness.agents.react import ReActWorkflow
from suiteharness.execution import RunRequest, RunStatus, ToolCallStatus
from suiteharness.execution.in_memory import (
    InMemoryApprovalStore,
    InMemoryAuditJournal,
    InMemoryCapabilityAuthority,
    InMemoryToolRegistry,
)
from suiteharness.execution.protocols import ExecutionStrategies
from suiteharness.execution.runner import ExecutionRunner
from suiteharness.models import (
    FinishReason,
    ModelRole,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelToolCall,
)
from suiteharness.multiagent.service import DelegationService
from suiteharness.multiagent.tool import install_delegate_tool
from suiteharness.runtime import RequestScope, ScopePath
from suiteharness.server.access import ConfiguredGrantIssuer, ToolAccessTemplate


class _VisibleDelegation(DelegationService):
    async def delegate(self, context, role_id, task):
        return {
            "task_id": "child-task",
            "status": "SUCCEEDED",
            "output": {
                "parent_principal": context.scope.principal_id,
                "parent_product": context.scope.product_id,
                "role": role_id,
                "task": task,
            },
        }


class _TwoStepModel:
    def __init__(self):
        self.requests = []

    async def stream(self, route_id, request):
        self.requests.append(request)
        if len(self.requests) == 1 and request.tools:
            yield ModelStreamEvent(
                type=ModelStreamEventType.TOOL_CALL_DELTA,
                tool_call=ModelToolCall(
                    call_id="delegate-call", name="agent.delegate",
                    arguments={"role_id": "reader", "task": "Summarize"},
                ),
            )
            yield ModelStreamEvent(
                type=ModelStreamEventType.MESSAGE_END,
                finish_reason=FinishReason.TOOL_CALLS,
            )
        else:
            yield ModelStreamEvent(
                type=ModelStreamEventType.TEXT_DELTA, text_delta="已完成",
            )
            yield ModelStreamEvent(
                type=ModelStreamEventType.MESSAGE_END,
                finish_reason=FinishReason.STOP,
            )


def _scope():
    return RequestScope(
        path=ScopePath.agent("tenant", "product", "parent", "session"),
        principal_id="seller-one", channel_id="web",
    )


def test_exact_grant_exposes_delegate_and_returns_standard_tool_message():
    async def check():
        registry = InMemoryToolRegistry()
        handle = install_delegate_tool(registry, _VisibleDelegation())
        authority = InMemoryCapabilityAuthority()
        issuer = ConfiguredGrantIssuer(
            registry=registry, authority=authority,
            templates=(ToolAccessTemplate(
                channel_id="web", product_id="product",
                read_aliases=("agent.delegate",),
            ),),
        )
        model = _TwoStepModel()
        runner = ExecutionRunner(
            tools=registry, capabilities=authority,
            approvals=InMemoryApprovalStore(), journal=InMemoryAuditJournal(),
        )
        try:
            async with issuer.lease(_scope()) as issued:
                result = await runner.run(
                    RunRequest(
                        run_id="parent-run", scope=_scope(),
                        grant_id=issued.grant_id,
                        input="Investigate this customer",
                        read_only=issued.read_only,
                    ),
                    ExecutionStrategies(
                        workflow=ReActWorkflow(model, "chat"),
                    ),
                )
            assert result.status is RunStatus.SUCCEEDED
            assert result.output == "已完成"
            assert len(model.requests) == 2
            assert {tool.name for tool in model.requests[0].tools} == {"agent.delegate"}
            assert result.observations[0].status is ToolCallStatus.SUCCEEDED
            tool_messages = [
                message for message in model.requests[1].messages
                if message.role is ModelRole.TOOL
            ]
            assert len(tool_messages) == 1
            assert tool_messages[0].tool_call_id == "delegate-call"
            assert tool_messages[0].name == "agent.delegate"
            assert "seller-one" in tool_messages[0].content[0].text
        finally:
            handle.close()

    asyncio.run(check())


def test_registration_without_route_grant_is_not_model_visible():
    async def check():
        registry = InMemoryToolRegistry()
        handle = install_delegate_tool(registry, _VisibleDelegation())
        authority = InMemoryCapabilityAuthority()
        issuer = ConfiguredGrantIssuer(
            registry=registry, authority=authority,
            templates=(ToolAccessTemplate(
                channel_id="web", product_id="product",
            ),),
        )
        model = _TwoStepModel()
        runner = ExecutionRunner(
            tools=registry, capabilities=authority,
            approvals=InMemoryApprovalStore(), journal=InMemoryAuditJournal(),
        )
        try:
            async with issuer.lease(_scope()) as issued:
                result = await runner.run(
                    RunRequest(
                        run_id="parent-run", scope=_scope(),
                        grant_id=issued.grant_id,
                        input="Investigate this customer",
                    ),
                    ExecutionStrategies(workflow=ReActWorkflow(model, "chat")),
                )
            assert result.status is RunStatus.SUCCEEDED
            assert model.requests[0].tools == ()
            assert result.observations == ()
        finally:
            handle.close()

    asyncio.run(check())
