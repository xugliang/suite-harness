"""Default company-agent prompt strategy."""

from __future__ import annotations

import json
from dataclasses import dataclass

from suiteharness.execution import (
    ConversationMessage,
    PromptEnvelope,
    PromptMessage,
    RunRequest,
    WorkflowFrame,
)


@dataclass(frozen=True, slots=True)
class DefaultPromptStrategy:
    """Render server-owned instructions and the untrusted user input separately."""

    system_prompt: str = (
        "You are an enterprise AI agent. Follow company policy, use only the tools "
        "provided to you, and never claim that a tool ran before receiving its result."
    )

    def __post_init__(self) -> None:
        if not self.system_prompt.strip():
            raise ValueError("system_prompt must not be blank")

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
        return PromptEnvelope(
            messages=(
                PromptMessage(role="system", content=self.system_prompt),
                *history,
                PromptMessage(role="user", content=self._text(request.input)),
            ),
            metadata={
                "run_id": request.run_id,
                "tenant_id": request.scope.tenant_id,
                "product_id": request.scope.product_id,
                "conversation_truncated": request.conversation.truncated,
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
        return PromptMessage(role=message.role, content=content)

    @staticmethod
    def _text(value: object) -> str:
        return (
            value
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
