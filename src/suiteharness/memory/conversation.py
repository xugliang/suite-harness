"""Replaceable layered conversation memory for product runtimes.

The host resolves one provider per product.  A product may bind its own
implementation; otherwise :class:`DefaultMemoryResolver` supplies the shared
provider.  Long-term retention is deliberately opt-in and Markdown is the
portable archive format.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path


from suiteharness.runtime.scopes import RequestScope


class MemoryLayer(StrEnum):
    SHORT_TERM = "short_term"
    WORKING = "working"
    LONG_TERM = "long_term"


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    """Retention policy; long-term storage is off unless explicitly enabled."""

    short_term_turns: int = 12
    working_memory_chars: int = 12_000
    long_term_enabled: bool = False
    archive_format: str = "md"
    archive_root: Path | None = None

    def __post_init__(self) -> None:
        if self.short_term_turns < 1 or self.short_term_turns > 100:
            raise ValueError("short_term_turns must be between 1 and 100")
        if self.working_memory_chars < 100:
            raise ValueError("working_memory_chars must be at least 100")
        if self.archive_format != "md":
            raise ValueError("only Markdown (md) archives are supported")
        if self.long_term_enabled and self.archive_root is None:
            raise ValueError("archive_root is required when long-term memory is enabled")


@dataclass(frozen=True, slots=True)
class MemoryTurn:
    role: str
    content: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    short_term: tuple[MemoryTurn, ...] = ()
    working: str = ""
    long_term_ref: str | None = None


class ConversationMemory(ABC):
    name: str

    @abstractmethod
    def append(self, scope: RequestScope, session_id: str, turn: MemoryTurn) -> None: ...

    @abstractmethod
    def snapshot(self, scope: RequestScope, session_id: str) -> MemorySnapshot: ...

    @abstractmethod
    def clear(self, scope: RequestScope, session_id: str) -> None: ...


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._:-]+", "_", value)[:128] or "unknown"


class InMemoryConversationMemory(ConversationMemory):
    """Default provider for products that do not supply a custom memory module."""

    name = "generic-in-memory"

    def __init__(self, config: MemoryConfig | None = None) -> None:
        self.config = config or MemoryConfig()
        self._turns: dict[tuple[str, str, str, str], list[MemoryTurn]] = defaultdict(list)

    def _key(self, scope: RequestScope, session_id: str) -> tuple[str, str, str, str]:
        return (scope.tenant_id, scope.product_id, scope.effective_session_owner_id, _safe(session_id))

    def append(self, scope: RequestScope, session_id: str, turn: MemoryTurn) -> None:
        if turn.role not in {"user", "assistant", "system"} or not turn.content.strip():
            raise ValueError("memory turn must have a supported role and non-empty content")
        turns = self._turns[self._key(scope, session_id)]
        turns.append(turn)
        del turns[:-self.config.short_term_turns]

    def snapshot(self, scope: RequestScope, session_id: str) -> MemorySnapshot:
        turns = tuple(self._turns.get(self._key(scope, session_id), ()))
        working = "\n".join(f"{turn.role}: {turn.content}" for turn in turns)
        return MemorySnapshot(short_term=turns, working=working[-self.config.working_memory_chars:])

    def clear(self, scope: RequestScope, session_id: str) -> None:
        self._turns.pop(self._key(scope, session_id), None)


class MarkdownConversationMemory(InMemoryConversationMemory):
    """Generic provider with optional append-only Markdown long-term archives."""

    name = "generic-markdown"

    def append(self, scope: RequestScope, session_id: str, turn: MemoryTurn) -> None:
        super().append(scope, session_id, turn)
        if not self.config.long_term_enabled:
            return
        assert self.config.archive_root is not None
        path = (
            self.config.archive_root
            / _safe(scope.tenant_id)
            / _safe(scope.product_id)
            / _safe(scope.effective_session_owner_id)
            / f"{_safe(session_id)}.md"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(f"# 会话记忆 {session_id}\n\n", encoding="utf-8")
        stamp = turn.created_at.astimezone(timezone.utc).isoformat()
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"## {stamp} · {turn.role}\n\n{turn.content.strip()}\n\n")


class DefaultMemoryResolver:
    """Resolve custom product providers with a safe generic fallback."""

    def __init__(self, default: ConversationMemory | None = None) -> None:
        self.default = default or InMemoryConversationMemory()
        self._custom: dict[tuple[str, str], ConversationMemory] = {}

    def register(self, tenant_id: str, product_id: str, provider: ConversationMemory) -> None:
        self._custom[(tenant_id, product_id)] = provider

    def resolve(self, scope: RequestScope) -> ConversationMemory:
        return self._custom.get((scope.tenant_id, scope.product_id), self.default)


__all__ = [
    "ConversationMemory",
    "DefaultMemoryResolver",
    "InMemoryConversationMemory",
    "MarkdownConversationMemory",
    "MemoryConfig",
    "MemoryLayer",
    "MemorySnapshot",
    "MemoryTurn",
]
