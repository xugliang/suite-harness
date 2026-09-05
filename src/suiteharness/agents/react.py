"""Default provider-neutral ReAct workflow.

The workflow may describe tool calls, but it never resolves or invokes a tool
handler.  Every returned :class:`ToolIntent` therefore still crosses the trusted
``ExecutionRunner`` authorization, approval, validation and audit boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from pydantic import JsonValue

from suiteharness.agents.protocols import ModelGatewayLike, ProductModelRouteResolver
from suiteharness.execution import (
    PromptEnvelope,
    RunRequest,
    ToolIntent,
    ToolObservation,
    WorkflowDecision,
    WorkflowFrame,
)
from suiteharness.models import (
    FinishReason,
    ModelMessage,
    ModelRequest,
    ModelRole,
    ModelTool,
    ModelToolCall,
    TextContent,
)
from suiteharness.sessions.models import canonical_json

_STATE_SCHEMA = "suiteharness.react.v2"


class ReActStateError(ValueError):
    """A checkpoint or model response cannot safely continue a ReAct run."""


@dataclass(frozen=True, slots=True)
class ReActWorkflow:
    gateway: ModelGatewayLike
    route_id: str
    model: str | None = None
    temperature: float | None = None
    max_output_tokens: int = 4096
    tool_choice: str = "auto"

    def __post_init__(self) -> None:
        if not self.route_id.strip():
            raise ValueError("route_id must not be blank")
        if self.tool_choice not in {"auto", "none", "required"}:
            raise ValueError("tool_choice must be auto, none, or required")

    async def next(
        self,
        request: RunRequest,
        frame: WorkflowFrame,
        prompt: PromptEnvelope | None,
    ) -> WorkflowDecision:
        messages, consumed, seen_call_ids = self._restore_state(request, frame, prompt)
        new_observations = frame.observations[consumed:]
        for observation in new_observations:
            messages.append(self._observation_message(observation))
            seen_call_ids.add(observation.call_id)

        available_names = {tool.name for tool in frame.available_tools}
        model_tools = tuple(
            ModelTool(
                name=tool.name,
                description=tool.description,
                input_schema=tool.input_schema,
            )
            for tool in frame.available_tools
        )
        effective_choice: Literal["auto", "none", "required"] = (
            "none"
            if not model_tools
            else cast(Literal["auto", "none", "required"], self.tool_choice)
        )
        metadata: dict[str, JsonValue] = dict(prompt.metadata) if prompt is not None else {}
        metadata.update(
            {
                "run_id": request.run_id,
                "iteration": frame.iteration,
                "tenant_id": request.scope.tenant_id,
                "product_id": request.scope.product_id,
            }
        )
        response = await self.gateway.complete(
            self.route_id,
            ModelRequest(
                messages=tuple(messages),
                model=self.model,
                tools=model_tools,
                tool_choice=effective_choice,
                temperature=self.temperature,
                max_output_tokens=self.max_output_tokens,
                metadata=metadata,
            ),
        )

        if response.finish_reason is FinishReason.TOOL_CALLS and not response.tool_calls:
            raise ReActStateError("model declared tool_calls but returned none")

        assistant = ModelMessage(
            role=ModelRole.ASSISTANT,
            content=response.content,
            tool_calls=response.tool_calls,
        )
        messages.append(assistant)
        state = self._dump_state(request, messages, len(frame.observations), seen_call_ids)

        if response.tool_calls:
            intents = self._tool_intents(
                response.tool_calls,
                available_names=available_names,
                seen_call_ids=seen_call_ids,
            )
            state = self._dump_state(
                request,
                messages,
                len(frame.observations),
                seen_call_ids | {intent.call_id for intent in intents},
            )
            return WorkflowDecision.tools(*intents, state=state)

        if not response.content:
            raise ReActStateError("model returned neither content nor tool calls")
        text_parts = [part.text for part in response.content if isinstance(part, TextContent)]
        if len(text_parts) == len(response.content):
            output: JsonValue = "".join(text_parts)
        else:
            output = cast(
                JsonValue,
                [part.model_dump(mode="json") for part in response.content],
            )
        return WorkflowDecision.final(output, state=state)

    def _restore_state(
        self,
        request: RunRequest,
        frame: WorkflowFrame,
        prompt: PromptEnvelope | None,
    ) -> tuple[list[ModelMessage], int, set[str]]:
        if frame.workflow_state is None:
            messages = self._initial_messages(request, prompt)
            return messages, 0, {item.call_id for item in frame.observations}
        state = frame.workflow_state
        if not isinstance(state, dict) or state.get("schema") != _STATE_SCHEMA:
            raise ReActStateError("workflow_state is not a suiteharness.react.v2 checkpoint")
        if state.get("route_id") != self.route_id:
            raise ReActStateError("workflow_state belongs to a different model route")
        if state.get("request_identity") != self._request_identity(request):
            raise ReActStateError("workflow_state belongs to a different request scope")
        raw_messages = state.get("messages")
        consumed = state.get("consumed_observations")
        raw_seen = state.get("seen_call_ids")
        if (
            not isinstance(raw_messages, list)
            or isinstance(consumed, bool)
            or not isinstance(consumed, int)
            or consumed < 0
            or consumed > len(frame.observations)
            or not isinstance(raw_seen, list)
            or not all(isinstance(item, str) for item in raw_seen)
        ):
            raise ReActStateError("workflow_state has an invalid shape")
        try:
            messages = [ModelMessage.model_validate(item, strict=False) for item in raw_messages]
        except Exception as exc:
            raise ReActStateError("workflow_state contains invalid model messages") from exc
        return messages, consumed, set(raw_seen)

    @staticmethod
    def _initial_messages(
        request: RunRequest,
        prompt: PromptEnvelope | None,
    ) -> list[ModelMessage]:
        if prompt is None or not prompt.messages:
            content = (
                request.input if isinstance(request.input, str) else canonical_json(request.input)
            )
            return [ModelMessage.text(ModelRole.USER, content)]
        role_map = {
            "system": ModelRole.SYSTEM,
            "user": ModelRole.USER,
            "assistant": ModelRole.ASSISTANT,
        }
        messages: list[ModelMessage] = []
        for message in prompt.messages:
            # PromptEnvelope cannot bind a tool message to a tool_call_id.  Preserve
            # its text as untrusted user context instead of forging a tool result.
            role = role_map.get(message.role, ModelRole.USER)
            content = message.content if message.role != "tool" else f"[tool] {message.content}"
            messages.append(ModelMessage.text(role, content))
        return messages

    @staticmethod
    def _observation_message(observation: ToolObservation) -> ModelMessage:
        payload: dict[str, JsonValue] = {"status": observation.status.value}
        if observation.result is not None:
            payload["result"] = observation.result
        if observation.failure is not None:
            payload["failure"] = cast(
                JsonValue,
                observation.failure.model_dump(mode="json"),
            )
        return ModelMessage.text(
            ModelRole.TOOL,
            canonical_json(payload),
            tool_call_id=observation.call_id,
            name=observation.tool_name,
        )

    @staticmethod
    def _tool_intents(
        calls: tuple[ModelToolCall, ...],
        *,
        available_names: set[str],
        seen_call_ids: set[str],
    ) -> tuple[ToolIntent, ...]:
        response_ids: set[str] = set()
        intents: list[ToolIntent] = []
        for call in calls:
            if call.name not in available_names:
                raise ReActStateError(f"model requested unavailable tool {call.name!r}")
            if call.call_id in response_ids or call.call_id in seen_call_ids:
                raise ReActStateError(f"model reused tool call id {call.call_id!r}")
            response_ids.add(call.call_id)
            intents.append(
                ToolIntent(
                    call_id=call.call_id,
                    tool_name=call.name,
                    arguments=call.arguments,
                )
            )
        return tuple(intents)

    def _dump_state(
        self,
        request: RunRequest,
        messages: list[ModelMessage],
        consumed_observations: int,
        seen_call_ids: set[str],
    ) -> JsonValue:
        return cast(
            JsonValue,
            {
                "schema": _STATE_SCHEMA,
                "route_id": self.route_id,
                "request_identity": self._request_identity(request),
                "messages": [item.model_dump(mode="json") for item in messages],
                "consumed_observations": consumed_observations,
                "seen_call_ids": sorted(seen_call_ids),
            },
        )

    @staticmethod
    def _request_identity(request: RunRequest) -> dict[str, JsonValue]:
        path = request.scope.path
        return {
            "run_id": request.run_id,
            "tenant_id": request.scope.tenant_id,
            "product_id": request.scope.product_id,
            "agent_id": path.agent_id,
            "session_id": path.session_id,
            "principal_id": request.scope.principal_id,
            "session_owner_id": request.scope.effective_session_owner_id,
            "channel_id": request.scope.channel_id,
        }


@dataclass(frozen=True, slots=True)
class ProductRoutedReActWorkflow:
    """Default ReAct workflow whose model route is selected per product.

    The resolver is owned by the server configuration.  Product extensions may
    still override the complete workflow at product scope, but a product cannot
    silently redirect another product's default model traffic.
    """

    gateway: ModelGatewayLike
    routes: ProductModelRouteResolver
    model: str | None = None
    temperature: float | None = None
    max_output_tokens: int = 4096
    tool_choice: str = "auto"

    async def next(
        self,
        request: RunRequest,
        frame: WorkflowFrame,
        prompt: PromptEnvelope | None,
    ) -> WorkflowDecision:
        route_id = self.routes.route_for(request.scope.product_id)
        if not isinstance(route_id, str) or not route_id.strip():
            raise ReActStateError(
                f"no valid model route is configured for product "
                f"{request.scope.product_id!r}"
            )
        workflow = ReActWorkflow(
            gateway=self.gateway,
            route_id=route_id,
            model=self.model,
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
            tool_choice=self.tool_choice,
        )
        return await workflow.next(request, frame, prompt)
