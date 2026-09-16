"""Channel dispatcher contracts shared without importing channel adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .models import AuthenticatedPrincipal, InboundMessage, OutboundEvent

if TYPE_CHECKING:
    from suiteharness.sessions.models import RunStopOutcome, TranscriptEvent


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """Ascending visible events and an exclusive cursor for older records."""

    events: tuple[TranscriptEvent, ...]
    next_cursor: int | None = None
    pagination_supported: bool = True


class FeishuDispatcher(ABC):
    """Dispatch an inbound envelope through authentication and routing."""

    @abstractmethod
    def dispatch(self, message: InboundMessage) -> AsyncIterator[OutboundEvent]: ...

    async def stop(
        self,
        message: InboundMessage,
        target_message_id: str | None = None,
    ) -> RunStopOutcome:
        """Optionally stop a run after authenticating a Feishu envelope."""

        raise NotImplementedError("this dispatcher does not provide run stopping")


class WebSocketDispatcher(ABC):
    """Dispatch an envelope with an already established principal."""

    @abstractmethod
    def dispatch_authenticated(
        self,
        message: InboundMessage,
        principal: AuthenticatedPrincipal,
    ) -> AsyncIterator[OutboundEvent]: ...

    async def stop_authenticated(
        self,
        message: InboundMessage,
        principal: AuthenticatedPrincipal,
        target_message_id: str,
    ) -> RunStopOutcome:
        """Optionally request a durable stop for one authenticated Web message."""

        raise NotImplementedError("this dispatcher does not provide run stopping")

    async def history_authenticated(
        self,
        message: InboundMessage,
        principal: AuthenticatedPrincipal,
        limit: int,
    ) -> tuple[TranscriptEvent, ...]:
        """Optionally return authenticated transcript events for display."""

        raise NotImplementedError("this dispatcher does not provide conversation history")

    async def history_page_authenticated(
        self, message: InboundMessage, principal: AuthenticatedPrincipal, limit: int,
        *, before_sequence: int | None = None,
    ) -> HistoryPage:
        """Old adapters retain initial history without claiming cursor support."""
        if before_sequence is not None:
            raise NotImplementedError("this dispatcher does not provide history pagination")
        return HistoryPage(
            await self.history_authenticated(message, principal, limit),
            pagination_supported=False,
        )

    def continue_authenticated(
        self,
        message: InboundMessage,
        principal: AuthenticatedPrincipal,
        target_message_id: str,
    ) -> AsyncIterator[OutboundEvent]:
        """Optionally stream an authenticated continuation of a cancelled run."""

        raise NotImplementedError("this dispatcher does not provide run continuation")
