"""Derived conversation memory; the host's SessionStore owns all chat facts."""

from __future__ import annotations

import copy
from abc import ABC
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field

from suiteharness.execution.models import ConversationContext, ConversationMessage
from suiteharness.sessions.models import (
    SessionIdentity,
    TranscriptEvent,
    TranscriptEventType,
    canonical_json,
)

SummaryCallback = Callable[[str, tuple[ConversationMessage, ...]], Awaitable[str]]
CompletedPair = tuple[TranscriptEvent, ConversationMessage, ConversationMessage]


class MemoryState(BaseModel):
    """Checkpoint projection owned by the exact identity supplied by the host.

    ``covered_revision`` is the assistant event revision of the newest complete
    pair removed from short-term context and processed into ``summary``. It is
    not a SessionStore checkpoint revision and never advances for a pending or
    failed turn. Hosts must reset this state when source data is invalidated;
    the state cannot infer product deletions from a partial transcript window.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    covered_revision: int = Field(default=0, ge=0)
    summary: str = Field(default="", max_length=12_000)
    degraded: bool = False


class MemoryPreparation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    context: ConversationContext
    state: MemoryState


def _bounded_int(value: int, name: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return value


class SessionConversationMemory(ABC):
    """Usable default policy, optionally overridden by a product binding.

    Summary callbacks receive only visible user/assistant messages, never tool
    execution internals or hidden reasoning. Both generated summaries and the
    explicit fallback excerpts remain untrusted context, not system rules.
    This object stores configuration only: pass durable transcript and previous
    checkpoint state into every call, including after process restart.
    """

    def __init__(
        self,
        *,
        short_term_pairs: int = 12,
        max_short_term_bytes: int = 128 * 1024,
        max_summary_chars: int = 12_000,
        summary_callback: SummaryCallback | None = None,
        max_summary_input_bytes: int = 128 * 1024,
    ) -> None:
        self.short_term_pairs = _bounded_int(short_term_pairs, "short_term_pairs", 10_000)
        self.max_short_term_bytes = _bounded_int(
            max_short_term_bytes, "max_short_term_bytes", 4 * 1024 * 1024
        )
        self.max_summary_chars = _bounded_int(max_summary_chars, "max_summary_chars", 12_000)
        self.max_summary_input_bytes = _bounded_int(
            max_summary_input_bytes, "max_summary_input_bytes", 4 * 1024 * 1024
        )
        self.summary_callback = summary_callback

    @staticmethod
    def extract_completed_pairs(
        identity: SessionIdentity, events: tuple[TranscriptEvent, ...]
    ) -> tuple[CompletedPair, ...]:
        """Return (assistant event, user message, assistant message) triples.

        The input is a chronological transcript, possibly a bounded suffix.
        Every event is checked before use, including non-message events. A new
        user event, malformed message, error or cancellation cuts an open pair.
        """

        pairs: list[CompletedPair] = []
        pending: ConversationMessage | None = None
        last_sequence = last_revision = 0
        for event in events:
            if event.identity != identity:
                raise ValueError("transcript event identity does not match memory identity")
            if event.sequence <= last_sequence or event.revision <= last_revision:
                raise ValueError("transcript events must have increasing sequence and revision")
            last_sequence, last_revision = event.sequence, event.revision
            if event.type in {TranscriptEventType.ERROR, TranscriptEventType.CANCEL}:
                pending = None
            elif event.type is TranscriptEventType.USER_MESSAGE:
                pending = None
                payload = event.payload
                if (not isinstance(payload, dict)
                    or payload.get("schema") != "suiteharness.channel-user-message.v1"
                    or not isinstance(payload.get("authenticated_principal_id"), str)
                    or not isinstance(payload.get("message"), dict)):
                    continue
                content = payload["message"].get("text")
                if isinstance(content, str):
                    pending = ConversationMessage(
                        role="user", content=content,
                        principal_id=payload["authenticated_principal_id"],
                        occurred_at=event.occurred_at,
                    )
            elif event.type is TranscriptEventType.ASSISTANT_MESSAGE:
                payload = event.payload
                if pending is not None and isinstance(payload, dict) and "output" in payload:
                    assistant = ConversationMessage(
                        role="assistant", content=copy.deepcopy(payload["output"]),
                        occurred_at=event.occurred_at,
                    )
                    pairs.append((event, pending, assistant))
                pending = None
        return tuple(pairs)

    @staticmethod
    def _pair_size(pair: CompletedPair) -> int:
        return sum(len(message.model_dump_json().encode("utf-8")) for message in pair[1:])

    def _excerpt(self, previous: str, messages: tuple[ConversationMessage, ...]) -> str:
        marker = "[Degraded structured excerpt; not a semantic summary]\n"
        # Cap each piece before joining so even one huge visible response cannot
        # expand the fallback beyond the configured projection budget.
        pieces = [previous[-self.max_summary_chars:]]
        for message in messages:
            content = message.content
            visible = content if isinstance(content, str) else canonical_json(content)
            pieces.append(f"{message.role}: {visible[:self.max_summary_chars]}")
        room = max(0, self.max_summary_chars - len(marker))
        body = "\n".join(pieces)
        return (marker + (body[-room:] if room else ""))[:self.max_summary_chars]

    async def prepare(
        self,
        identity: SessionIdentity,
        events: tuple[TranscriptEvent, ...],
        previous: MemoryState,
    ) -> MemoryPreparation:
        pairs = self.extract_completed_pairs(identity, events)
        used_bytes = 0
        start = len(pairs)
        for pair in reversed(pairs):
            size = self._pair_size(pair)
            if len(pairs) - start >= self.short_term_pairs or used_bytes + size > self.max_short_term_bytes:
                break
            start -= 1
            used_bytes += size

        summary = previous.summary[:self.max_summary_chars]
        degraded = previous.degraded or len(previous.summary) > self.max_summary_chars
        covered = previous.covered_revision
        batch: list[ConversationMessage] = []
        batch_bytes = 0

        async def summarize(messages: tuple[ConversationMessage, ...], oversized: bool = False) -> None:
            nonlocal summary, degraded
            if self.summary_callback is not None and not oversized:
                try:
                    result = await self.summary_callback(summary, messages)
                    if not isinstance(result, str) or not result.strip():
                        raise ValueError("summary callback returned no usable text")
                    summary = result[:self.max_summary_chars]
                    degraded = degraded or len(result) > self.max_summary_chars
                    return
                except Exception:
                    # Cancellation inherits BaseException and must propagate.
                    pass
            summary = self._excerpt(summary, messages)
            degraded = True

        for pair in pairs[:start]:
            if pair[0].revision <= previous.covered_revision:
                continue
            size = self._pair_size(pair)
            if batch and batch_bytes + size > self.max_summary_input_bytes:
                await summarize(tuple(batch))
                batch, batch_bytes = [], 0
            if size > self.max_summary_input_bytes:
                await summarize(pair[1:], oversized=True)
            else:
                batch.extend(pair[1:])
                batch_bytes += size
            covered = pair[0].revision
        if batch:
            await summarize(tuple(batch))
        state = MemoryState(covered_revision=covered, summary=summary, degraded=degraded)
        context = ConversationContext(
            messages=tuple(message for pair in pairs[start:] for message in pair[1:]),
            truncated=start > 0 or bool(summary),
            working_summary=state.summary,
            memory_degraded=state.degraded,
        )
        return MemoryPreparation(context=context, state=state)
