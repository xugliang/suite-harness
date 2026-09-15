"""Result references through the real Runner, ReAct and protected builtin tool.

Only the external model is scripted. Removing offload, weakening owner binding,
granting references without a reader, or re-offloading read pages breaks these
tests at the observations actually consumed by ReAct.
"""

import asyncio
import json
from dataclasses import dataclass

import pytest

from suiteharness.agents.react import ReActWorkflow
from suiteharness.execution import RunRequest, RunStatus, ToolEffect, ToolSpec
from suiteharness.execution.in_memory import (
    InMemoryApprovalStore,
    InMemoryAuditJournal,
    InMemoryCapabilityAuthority,
    InMemoryToolRegistry,
)
from suiteharness.execution.models import FailureCode, ToolCallStatus
from suiteharness.execution.protocols import ExecutionStrategies
from suiteharness.execution.results import SQLiteToolResultStore
from suiteharness.execution.runner import ExecutionRunner
from suiteharness.models import (
    FinishReason,
    ModelRole,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelToolCall,
)
from suiteharness.runtime import RequestScope, ScopePath
from suiteharness.tools.builtins import BuiltinToolInstaller

READER = "suiteharness.result.read"
LARGE_TOOL = "large.result"


def owner(
    *, seller="seller-a", session="chat-a", tenant="company", product="sales", agent="assistant"
):
    return RequestScope(
        path=ScopePath.agent(tenant, product, agent, session),
        principal_id=seller,
    )


class UnusedExternalDependency:
    """Other builtins may be registered but must never perform I/O in this fixture."""

    def __getattr__(self, name):
        raise AssertionError(f"unexpected external dependency access: {name}")


class ScriptedModel:
    def __init__(self, choose):
        self.choose = choose
        self.requests = []
        self.tool_messages = []

    async def stream(self, route_id, request):
        self.requests.append(request)
        self.tool_messages = [
            json.loads("".join(part.text for part in message.content))
            for message in request.messages
            if message.role is ModelRole.TOOL
        ]
        decision = self.choose(self.tool_messages)
        if decision is not None:
            name, arguments = decision
            yield ModelStreamEvent(
                type=ModelStreamEventType.TOOL_CALL_DELTA,
                tool_call=ModelToolCall(
                    call_id=f"call-{len(self.requests)}",
                    name=name,
                    arguments=arguments,
                ),
            )
            yield ModelStreamEvent(
                type=ModelStreamEventType.MESSAGE_END, finish_reason=FinishReason.TOOL_CALLS
            )
        else:
            yield ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta="complete")
            yield ModelStreamEvent(
                type=ModelStreamEventType.MESSAGE_END, finish_reason=FinishReason.STOP
            )


@dataclass
class Runtime:
    registry: InMemoryToolRegistry
    authority: InMemoryCapabilityAuthority
    store: SQLiteToolResultStore
    runner: ExecutionRunner
    registrations: object

    async def run(self, model, *, scope=None, run_id="run-a", read_granted=True):
        scope = scope or owner()
        names = [LARGE_TOOL] + ([READER] if read_granted else [])
        identities = tuple(
            registered.identity
            for name in names
            if (registered := self.registry.resolve(scope, name)) is not None
        )
        grant = await self.authority.issue(
            scope.path,
            principal_id=scope.principal_id,
            tool_identities=identities,
        )
        request = RunRequest(
            run_id=run_id,
            scope=scope,
            grant_id=grant.grant_id,
            input="Read the complete result",
        )
        return await self.runner.run(
            request,
            ExecutionStrategies(workflow=ReActWorkflow(model, "test-model")),
        )


def runtime(path, payload):
    registry = InMemoryToolRegistry()
    store = SQLiteToolResultStore(path)
    unused = UnusedExternalDependency()
    registrations = BuiltinToolInstaller(
        registry,
        workspaces=unused,
        sandbox=unused,
        web_search=unused,
        web_fetch=unused,
        tool_results=store,
    ).install()

    async def large_result(context, arguments):
        return payload

    registry.register(
        ScopePath.product("company", "sales"),
        ToolSpec(
            name=LARGE_TOOL,
            description="Return a large private result",
            input_schema={"type": "object", "additionalProperties": False},
            effects=frozenset({ToolEffect.READ}),
        ),
        large_result,
    )
    authority = InMemoryCapabilityAuthority()
    runner = ExecutionRunner(
        tools=registry,
        capabilities=authority,
        approvals=InMemoryApprovalStore(),
        journal=InMemoryAuditJournal(),
        tool_results=store,
    )
    return Runtime(registry, authority, store, runner, registrations)


def produce_once(messages):
    return (LARGE_TOOL, {}) if not messages else None


def test_react_offloads_then_reads_every_page_through_the_standard_tool(tmp_path):
    async def scenario():
        payload = {"private_text": "中文记录" * 2200 + "unique-final-marker"}
        app = runtime(tmp_path / "results.sqlite3", payload)
        reference = None

        def choose(messages):
            nonlocal reference
            if not messages:
                return LARGE_TOOL, {}
            result = messages[-1].get("result", {})
            if len(messages) == 1:
                reference = result.get("result_reference")
                if reference is None:
                    return None  # Outer assertions expose a missing offload.
                return READER, {"result_id": reference["result_id"], "limit": 2048}
            if result.get("next_offset") is not None:
                return READER, {
                    "result_id": reference["result_id"],
                    "offset": result["next_offset"],
                    "limit": 2048,
                }
            return None

        model = ScriptedModel(choose)
        try:
            result = await app.run(model)
            assert result.status is RunStatus.SUCCEEDED, result.failure
            assert reference is not None
            assert reference["read_tool"] == READER
            first = result.observations[0]
            assert first.result["truncated"] is True
            assert "unique-final-marker" not in first.result["preview"]
            pages = result.observations[1:]
            assert len(pages) >= 3
            assert all(page.tool_name == READER for page in pages)
            assert all(page.status is ToolCallStatus.SUCCEEDED for page in pages)
            assert pages[0].result["offset"] == 0
            assert pages[-1].result["next_offset"] is None
            for earlier, later in zip(pages, pages[1:], strict=False):
                assert earlier.result["next_offset"] == later.result["offset"]
            serialized = "".join(page.result["content"] for page in pages)
            assert json.loads(serialized) == payload
            # These are the tool messages fed back into the actual ReAct model request.
            assert (
                json.loads("".join(m["result"]["content"] for m in model.tool_messages[1:]))
                == payload
            )
            assert result.output == "complete"
            # A fresh store instance must observe the committed record, not process memory.
            reopened = SQLiteToolResultStore(tmp_path / "results.sqlite3")
            last = await reopened.read(
                owner(), "run-a", reference["result_id"], offset=len(serialized) - 64, limit=64
            )
            assert last["content"] == serialized[-64:]
        finally:
            app.registrations.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes,run_id",
    [
        ({"seller": "seller-b"}, "run-a"),
        ({"session": "chat-b"}, "run-a"),
        ({}, "run-b"),
        ({"tenant": "other-company"}, "run-a"),
        ({"product": "other-product"}, "run-a"),
        ({"agent": "other-agent"}, "run-a"),
    ],
)
def test_result_reader_rejects_other_authenticated_owners(tmp_path, changes, run_id):
    async def scenario():
        app = runtime(tmp_path / "results.sqlite3", {"secret": "private-sales-record" * 1600})
        try:
            producer = await app.run(ScriptedModel(produce_once))
            assert producer.status is RunStatus.SUCCEEDED
            reference = producer.observations[0].result["result_reference"]
            attacker = ScriptedModel(
                lambda messages: None
                if messages
                else (
                    READER,
                    {"result_id": reference["result_id"]},
                )
            )
            # The reader itself is legitimately granted: denial must come from result ownership.
            denied = await app.run(attacker, scope=owner(**changes), run_id=run_id)
            assert READER in {tool.name for tool in attacker.requests[0].tools}
            assert len(denied.observations) == 1
            observation = denied.observations[0]
            assert observation.status is ToolCallStatus.FAILED
            assert observation.failure.code is FailureCode.TOOL_ERROR
            assert observation.result is None
            assert "private-sales-record" not in json.dumps(attacker.tool_messages)
        finally:
            app.registrations.close()

    asyncio.run(scenario())


def test_ungranted_reader_does_not_create_an_unreadable_reference(tmp_path):
    async def scenario():
        payload = {"content": "private-sales-record" * 1600}
        app = runtime(tmp_path / "results.sqlite3", payload)
        model = ScriptedModel(produce_once)
        try:
            result = await app.run(model, read_granted=False)
            assert result.status is RunStatus.SUCCEEDED, result.failure
            assert READER not in {tool.name for tool in model.requests[0].tools}
            assert result.observations[0].result == payload
            assert model.tool_messages[0]["result"] == payload
            assert "result_reference" not in result.observations[0].result
        finally:
            app.registrations.close()

    asyncio.run(scenario())


def test_four_byte_character_page_is_not_offloaded_again(tmp_path):
    async def scenario():
        app = runtime(tmp_path / "results.sqlite3", {})
        text = "\U0001f600" * 4096
        try:
            reference = await app.store.put(owner(), "run-a", text + "tail")
            model = ScriptedModel(
                lambda messages: None
                if messages
                else (
                    READER,
                    {"result_id": reference["result_id"], "limit": 4096},
                )
            )
            result = await app.run(model)
            assert result.status is RunStatus.SUCCEEDED, result.failure
            assert len(result.observations) == 1
            page = result.observations[0]
            assert page.status is ToolCallStatus.SUCCEEDED
            assert page.result == {"content": text, "offset": 0, "next_offset": 4096}
            assert model.tool_messages[0]["result"] == page.result
            assert "result_reference" not in page.result
        finally:
            app.registrations.close()

    asyncio.run(scenario())
