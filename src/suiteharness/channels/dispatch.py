"""Channel dispatcher contracts shared without importing channel adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from .models import AuthenticatedPrincipal, InboundMessage, OutboundEvent


class FeishuDispatcher(ABC):
    """Dispatch an inbound envelope through authentication and routing."""

    @abstractmethod
    def dispatch(self, message: InboundMessage) -> AsyncIterator[OutboundEvent]: ...


class WebSocketDispatcher(ABC):
    """Dispatch an envelope with an already established principal."""

    @abstractmethod
    def dispatch_authenticated(
        self,
        message: InboundMessage,
        principal: AuthenticatedPrincipal,
    ) -> AsyncIterator[OutboundEvent]: ...
