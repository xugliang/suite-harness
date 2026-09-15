import asyncio
from datetime import UTC, datetime, timedelta

from suiteharness.execution import PromptEnvelope, PromptMessage, RunRequest, WorkflowDecision
from suiteharness.execution.in_memory import (
    InMemoryApprovalStore, InMemoryAuditJournal, InMemoryCapabilityAuthority,
    InMemoryToolRegistry,
)
from suiteharness.execution.protocols import ExecutionStrategies, PromptStrategy, Workflow
from suiteharness.execution.runner import ExecutionRunner
from suiteharness.runtime import RequestScope, ScopePath


class CustomPrompt(PromptStrategy):
    async def render(self, request, frame):
        return PromptEnvelope(messages=(
            PromptMessage(role="system", content="产品自己的规则"),
            PromptMessage(role="user", content="今天日期？"),
        ))


class EchoSystem(Workflow):
    async def next(self, request, frame, prompt):
        return WorkflowDecision.final("\n".join(
            message.content for message in prompt.messages if message.role == "system"
        ))


def test_custom_product_inherits_beijing_date_across_utc_midnight(monkeypatch):
    import suiteharness.execution.runner as runner_module
    instant = datetime(2026, 9, 9, 16, 5, 6, tzinfo=UTC)
    monkeypatch.setattr(runner_module, "_utcnow", lambda: instant)

    async def run():
        authority = InMemoryCapabilityAuthority()
        scope = RequestScope(path=ScopePath.product("company", "product"), principal_id="seller")
        grant = await authority.issue(scope.path, tool_identities=(), now=instant,
                                      lifetime=timedelta(days=1))
        executor = ExecutionRunner(tools=InMemoryToolRegistry(), capabilities=authority,
                                   approvals=InMemoryApprovalStore(), journal=InMemoryAuditJournal())
        request = RunRequest(run_id="clock-test", scope=scope, grant_id=grant.grant_id, input="今天日期？")
        return await executor.run(request, ExecutionStrategies(workflow=EchoSystem(), prompt=CustomPrompt()))

    result = asyncio.run(run())
    assert result.status.value == "succeeded", result.failure
    assert "产品自己的规则" in result.output
    assert "2026-09-10" in result.output
    assert "00:05:06" in result.output
    assert "星期四" in result.output
    assert "Asia/Shanghai" in result.output
