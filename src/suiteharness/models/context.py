"""Bounded, provider-neutral model context preparation.

UTF-8 bytes are a deliberately conservative text estimate, not a precise
tokenizer. Deployments may inject a model-specific estimator. Image/document
costs remain estimates and should be calibrated to the selected model.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass

from suiteharness.models.types import (
    DocumentContent, ImageContent, ModelMessage, ModelRequest, ModelRole, TextContent,
)


class ContextBudgetError(ValueError):
    pass


class ContextCapacityError(ContextBudgetError):
    """A valid request may fit another explicitly configured model profile."""


class TokenEstimator(ABC):
    @abstractmethod
    def text_tokens(self, text: str) -> int: ...


class Utf8TokenEstimator(TokenEstimator):
    def text_tokens(self, text: str) -> int:
        return len(text.encode('utf-8'))


@dataclass(frozen=True)
class PreparedContext:
    request: ModelRequest
    estimated_input_tokens: int
    removed_rounds: int = 0
    truncated_tools: int = 0


class ContextManager:
    def __init__(self, estimator: TokenEstimator | None = None, *, image_tokens: int = 8192):
        if isinstance(image_tokens, bool) or not isinstance(image_tokens, int) or image_tokens < 1:
            raise ValueError('image_tokens must be a positive integer')
        self.estimator = estimator or Utf8TokenEstimator()
        self.image_tokens = image_tokens

    def _json_cost(self, value) -> int:
        return self.estimator.text_tokens(json.dumps(value, ensure_ascii=False, separators=(',', ':')))

    def estimate(self, request: ModelRequest) -> int:
        total = 32  # Message framing and assistant prefix reserve.
        total += self._json_cost([tool.model_dump(mode='json') for tool in request.tools])
        if request.response_schema is not None:
            total += self._json_cost(request.response_schema)
        for message in request.messages:
            total += 16 + self.estimator.text_tokens(message.name or '')
            total += self.estimator.text_tokens(message.tool_call_id or '')
            for content in message.content:
                if isinstance(content, TextContent):
                    total += self.estimator.text_tokens(content.text)
                elif isinstance(content, ImageContent):
                    total += self.image_tokens
                elif isinstance(content, DocumentContent):
                    total += self.image_tokens + len(content.data_base64)
            if message.tool_calls:
                total += self._json_cost([call.model_dump(mode='json') for call in message.tool_calls])
        return total

    @staticmethod
    def _validate_pairs(messages: tuple[ModelMessage, ...]) -> None:
        pending: set[str] = set()
        used: set[str] = set()
        for message in messages:
            if message.role is ModelRole.SYSTEM:
                continue
            if message.role is ModelRole.TOOL:
                if message.tool_call_id not in pending:
                    raise ContextBudgetError('orphan or duplicate tool result')
                pending.remove(message.tool_call_id)
                continue
            if pending:
                raise ContextBudgetError('tool calls are missing their results')
            for call in message.tool_calls:
                if call.call_id in used:
                    raise ContextBudgetError('duplicate tool call id')
                used.add(call.call_id)
                pending.add(call.call_id)
        if pending:
            raise ContextBudgetError('tool calls are missing their results')

    def prepare(self, request: ModelRequest, *, window_tokens: int) -> PreparedContext:
        if isinstance(window_tokens, bool) or not isinstance(window_tokens, int) or window_tokens < 1:
            raise ValueError('window_tokens must be a positive integer')
        self._validate_pairs(request.messages)
        reserve = request.max_output_tokens or 4096
        budget = window_tokens - reserve
        if budget <= 0:
            raise ContextCapacityError('output reserve exhausts context budget')
        current = request.model_copy(deep=True)
        if self.estimate(current) <= budget:
            return PreparedContext(current, self.estimate(current))

        # Result excerpts remain valid JSON; tool call arguments are never edited.
        tool_count = sum(m.role is ModelRole.TOOL for m in current.messages)
        per_tool = max(64, min(2048, budget // max(4, tool_count * 2)))
        truncated = 0
        messages = []
        for message in current.messages:
            if message.role is ModelRole.TOOL:
                raw = '\n'.join(p.text for p in message.content if isinstance(p, TextContent))
                if self.estimator.text_tokens(raw) > per_tool:
                    excerpt = raw.encode('utf-8')[:max(0, per_tool - 64)].decode('utf-8', errors='ignore')
                    compact = {'truncated': True, 'excerpt': excerpt}
                    # Preserve the bounded opaque reader hint, never a filesystem
                    # path. Actual access is checked by the result tool and Runner.
                    try:
                        payload = json.loads(raw)
                        result = payload.get('result', {}) if isinstance(payload, dict) else {}
                        reference = result.get('result_reference') if isinstance(result, dict) else None
                        if isinstance(reference, dict) and len(json.dumps(reference)) <= 512:
                            compact['result_reference'] = reference
                    except (ValueError, TypeError):
                        pass
                    text = json.dumps(compact, ensure_ascii=False)
                    message = message.model_copy(update={'content': (TextContent(text=text),)})
                    truncated += 1
            messages.append(message)
        current = current.model_copy(update={'messages': tuple(messages)})

        # Indices rather than role regrouping keep system-message order intact.
        rounds: list[list[int]] = []
        answered = False
        for index, message in enumerate(messages):
            if message.role is ModelRole.SYSTEM:
                continue
            if not rounds or (message.role is ModelRole.USER and answered):
                rounds.append([])
                answered = False
            rounds[-1].append(index)
            if message.role is ModelRole.ASSISTANT and not message.tool_calls:
                answered = True
        removed: set[int] = set()
        removed_rounds = 0
        for old_round in rounds[:-1]:
            if self.estimate(current) <= budget:
                break
            removed.update(old_round)
            removed_rounds += 1
            current = current.model_copy(update={
                'messages': tuple(m for index, m in enumerate(messages) if index not in removed),
            })
        estimate = self.estimate(current)
        if estimate > budget:
            raise ContextCapacityError('required current input exceeds context budget')
        self._validate_pairs(current.messages)
        return PreparedContext(current, estimate, removed_rounds, truncated)
