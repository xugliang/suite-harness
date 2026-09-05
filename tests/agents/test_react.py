from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from suiteharness.agents import (
    DefaultPromptStrategy,
    NoOpReflectionStrategy,
    ProductRoutedReActWorkflow,
    ReActStateError,
    ReActWorkflow,
    SinglePassModelReflectionStrategy,
)
from suiteharness.execution import (
    ConversationContext,
    ConversationMessage,
    PromptEnvelope,
    PromptMessage,
    RunRequest,
    ToolCallStatus,
    ToolEffect,
    ToolObservation,
    ToolSpec,
    WorkflowDecision,
    WorkflowFrame,
)
from suiteharness.models import (
    FinishReason,
    ModelResponse,
    ModelRole,
    ModelToolCall,
    TextContent,
)
from suiteharness.runtime.scopes import RequestScope, ScopePath


class FakeGateway:
    def __init__(self, *responses: ModelResponse) -> None:
        self.responses = list(responses)
        self.requests = []

    async def complete(self, route_id, request):  # type: ignore[no-untyped-def]
        self.requests.append((route_id, request))
        return self.responses.pop(0)


def _request() -> RunRequest:
    scope = RequestScope(
        path=ScopePath.agent("tenant-a", "product-a", "agent-a", "session-a"),
        principal_id="user-a",
    )
    return RunRequest(run_id="run-a", scope=scope, grant_id="grant-a", input={"message": "查库存"})


def _product_request(product_id: str, *, run_id: str) -> RunRequest:
    scope = RequestScope(
        path=ScopePath.agent("tenant-a", product_id, "agent-a", f"session-{product_id}"),
        principal_id="user-a",
    )
    return RunRequest(run_id=run_id, scope=scope, grant_id="grant-a", input="hello")


def _frame(*, state=None, observations=()):  # type: ignore[no-untyped-def]
    return WorkflowFrame(
        iteration=1 if state is None else 2,
        observations=observations,
        available_tools=(
            ToolSpec(
                name="inventory.read",
                description="Read inventory",
                effects=frozenset({ToolEffect.READ}),
                input_schema={
                    "type": "object",
                    "properties": {"sku": {"type": "string"}},
                    "required": ["sku"],
                },
            ),
        ),
        workflow_state=state,
        tool_calls_used=len(observations),
        elapsed_seconds=0.1,
    )


def _response(*, calls=(), text: str | None = None, response_id="response-a"):  # type: ignore[no-untyped-def]
    return ModelResponse(
        response_id=response_id,
        provider_id="provider-a",
        model="model-a",
        content=() if text is None else (TextContent(text=text),),
        tool_calls=calls,
        finish_reason=FinishReason.TOOL_CALLS if calls else FinishReason.STOP,
    )


def test_react_returns_intent_then_consumes_observation_and_finishes() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        gateway = FakeGateway(
            _response(
                calls=(
                    ModelToolCall(
                        call_id="call-a",
                        name="inventory.read",
                        arguments={"sku": "SKU-1"},
                    ),
                )
            ),
            _response(text="库存为 8", response_id="response-b"),
        )
        workflow = ReActWorkflow(gateway=gateway, route_id="primary")
        request = _request()
        prompt = PromptEnvelope(
            messages=(
                PromptMessage(role="system", content="You are a stock assistant."),
                PromptMessage(role="user", content="查 SKU-1"),
            )
        )
        first = await workflow.next(request, _frame(), prompt)
        now = datetime.now(UTC)
        observation = ToolObservation(
            call_id="call-a",
            tool_name="inventory.read",
            status=ToolCallStatus.SUCCEEDED,
            result={"stock": 8},
            started_at=now,
            finished_at=now,
        )
        second = await workflow.next(
            request,
            _frame(state=first.workflow_state, observations=(observation,)),
            prompt,
        )
        return gateway, first, second

    gateway, first, second = asyncio.run(exercise())
    assert first.intents[0].tool_name == "inventory.read"
    assert first.intents[0].call_id == "call-a"
    assert first.intents[0].arguments == {"sku": "SKU-1"}
    assert second.output == "库存为 8"
    second_messages = gateway.requests[1][1].messages
    assert second_messages[-1].role is ModelRole.TOOL
    assert second_messages[-1].tool_call_id == "call-a"
    assert gateway.requests[0][1].tools[0].name == "inventory.read"


def test_react_rejects_unavailable_tool_and_reused_call_id() -> None:
    async def unavailable():  # type: ignore[no-untyped-def]
        gateway = FakeGateway(
            _response(calls=(ModelToolCall(call_id="call-a", name="admin.delete"),))
        )
        workflow = ReActWorkflow(gateway=gateway, route_id="primary")
        await workflow.next(_request(), _frame(), None)

    with pytest.raises(ReActStateError, match="unavailable"):
        asyncio.run(unavailable())

    async def duplicated():  # type: ignore[no-untyped-def]
        gateway = FakeGateway(
            _response(
                calls=(
                    ModelToolCall(call_id="call-a", name="inventory.read"),
                    ModelToolCall(call_id="call-a", name="inventory.read"),
                )
            )
        )
        workflow = ReActWorkflow(gateway=gateway, route_id="primary")
        await workflow.next(_request(), _frame(), None)

    with pytest.raises(ReActStateError, match="reused"):
        asyncio.run(duplicated())


def test_default_prompt_and_reflection_strategies() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        request = _request().model_copy(
            update={
                "conversation": ConversationContext(
                    messages=(
                        ConversationMessage(
                            role="user",
                            content="上一问",
                            principal_id="other-user",
                            occurred_at=datetime.now(UTC),
                        ),
                        ConversationMessage(
                            role="assistant",
                            content={"answer": "上一答"},
                            occurred_at=datetime.now(UTC),
                        ),
                    ),
                    truncated=True,
                )
            }
        )
        frame = _frame()
        prompt = await DefaultPromptStrategy(system_prompt="Company policy").render(request, frame)
        candidate = WorkflowDecision.final({"answer": "draft"}, state={"step": 1})
        assert await NoOpReflectionStrategy().review(request, frame, candidate) == candidate
        gateway = FakeGateway(_response(text='{"accept":false,"revised_output":{"answer":"ok"}}'))
        reflected = await SinglePassModelReflectionStrategy(
            gateway=gateway,
            route_id="reviewer",
        ).review(request, frame, candidate)
        return prompt, reflected, gateway

    prompt, reflected, gateway = asyncio.run(exercise())
    assert [message.role for message in prompt.messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert "authenticated participant-1" in prompt.messages[1].content
    assert "other-user" not in prompt.messages[1].content
    assert "上一答" in prompt.messages[2].content
    assert "查库存" in prompt.messages[3].content
    assert prompt.metadata["conversation_truncated"] is True
    assert reflected.output == {"answer": "ok"}
    assert reflected.workflow_state == {"step": 1}
    assert gateway.requests[0][1].tools == ()
    assert gateway.requests[0][1].tool_choice == "none"


def test_product_routed_react_uses_distinct_configured_routes() -> None:
    class Routes:
        def route_for(self, product_id: str) -> str:
            return {"product-a": "route-a", "product-b": "route-b"}[product_id]

    async def exercise():  # type: ignore[no-untyped-def]
        gateway = FakeGateway(_response(text="A"), _response(text="B"))
        workflow = ProductRoutedReActWorkflow(gateway=gateway, routes=Routes())
        first = await workflow.next(
            _product_request("product-a", run_id="run-a"), _frame(), None
        )
        second = await workflow.next(
            _product_request("product-b", run_id="run-b"), _frame(), None
        )
        return gateway, first, second

    gateway, first, second = asyncio.run(exercise())
    assert [route for route, _ in gateway.requests] == ["route-a", "route-b"]
    assert first.output == "A"
    assert second.output == "B"


def test_react_checkpoint_cannot_cross_product_or_model_route() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        gateway = FakeGateway(
            _response(
                calls=(
                    ModelToolCall(
                        call_id="call-a",
                        name="inventory.read",
                        arguments={"sku": "SKU-1"},
                    ),
                )
            )
        )
        request = _product_request("product-a", run_id="run-a")
        first = await ReActWorkflow(gateway=gateway, route_id="route-a").next(
            request, _frame(), None
        )
        return first.workflow_state

    state = asyncio.run(exercise())
    cross_product = _product_request("product-b", run_id="run-a")
    gateway = FakeGateway(_response(text="must-not-run"))
    with pytest.raises(ReActStateError, match="request scope"):
        asyncio.run(
            ReActWorkflow(gateway=gateway, route_id="route-a").next(
                cross_product, _frame(state=state), None
            )
        )
    with pytest.raises(ReActStateError, match="model route"):
        asyncio.run(
            ReActWorkflow(gateway=gateway, route_id="route-b").next(
                _product_request("product-a", run_id="run-a"),
                _frame(state=state),
                None,
            )
        )
    assert gateway.requests == []
