"""Built-in reflection policies; none of them can execute a tool."""

from __future__ import annotations

import json
from dataclasses import dataclass

from suiteharness.agents.protocols import ModelGatewayLike
from suiteharness.execution import RunRequest, WorkflowDecision, WorkflowDecisionKind, WorkflowFrame
from suiteharness.models import ModelMessage, ModelRequest, ModelRole, TextContent


@dataclass(frozen=True, slots=True)
class NoOpReflectionStrategy:
    """Explicitly disable reflection while retaining the replaceable contract."""

    async def review(
        self,
        request: RunRequest,
        frame: WorkflowFrame,
        decision: WorkflowDecision,
    ) -> WorkflowDecision:
        del request, frame
        return decision


@dataclass(frozen=True, slots=True)
class SinglePassModelReflectionStrategy:
    """Ask a separate route to accept or revise a final answer exactly once.

    The reviewer receives no tools.  It can only return ``accept=true`` or a
    replacement JSON value, so reflection cannot bypass ``ExecutionRunner``.
    """

    gateway: ModelGatewayLike
    route_id: str
    system_prompt: str = (
        "Review the proposed enterprise-agent answer for correctness, safety and "
        'clarity. Return JSON only: {"accept": true} or '
        '{"accept": false, "revised_output": <JSON value>}.'
    )
    model: str | None = None
    temperature: float | None = 0.0
    max_output_tokens: int = 2048

    def __post_init__(self) -> None:
        if not self.route_id.strip():
            raise ValueError("route_id must not be blank")
        if not self.system_prompt.strip():
            raise ValueError("system_prompt must not be blank")

    async def review(
        self,
        request: RunRequest,
        frame: WorkflowFrame,
        decision: WorkflowDecision,
    ) -> WorkflowDecision:
        if decision.kind is not WorkflowDecisionKind.FINAL:
            return decision
        candidate = json.dumps(
            {
                "input": request.input,
                "candidate_output": decision.output,
                "iteration": frame.iteration,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        response = await self.gateway.complete(
            self.route_id,
            ModelRequest(
                messages=(
                    ModelMessage.text(ModelRole.SYSTEM, self.system_prompt),
                    ModelMessage.text(ModelRole.USER, candidate),
                ),
                model=self.model,
                tools=(),
                tool_choice="none",
                temperature=self.temperature,
                max_output_tokens=self.max_output_tokens,
                metadata={"run_id": request.run_id, "purpose": "single_pass_reflection"},
            ),
        )
        text = "".join(
            part.text for part in response.content if isinstance(part, TextContent)
        ).strip()
        if not text:
            raise ValueError("reflection model returned no JSON text")
        try:
            review = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("reflection model returned invalid JSON") from exc
        if not isinstance(review, dict) or not isinstance(review.get("accept"), bool):
            raise ValueError("reflection JSON requires a boolean accept field")
        if review["accept"] is True:
            return decision
        if "revised_output" not in review:
            raise ValueError("rejected reflection requires revised_output")
        return WorkflowDecision.final(
            review["revised_output"],
            state=decision.workflow_state,
        )
