"""Agent-facing model and live-event contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod

from collections.abc import AsyncIterator
from typing import Protocol

from suiteharness.agents.events import AgentEvent
from suiteharness.models import ModelRequest, ModelResponse, ModelStreamEvent
from suiteharness.sessions import SessionIdentity


class ModelGatewayLike(Protocol):
    """Structural consumer view of the model gateway.

    The owned provider extension lives in models.ModelProvider; this view avoids
    making the model layer depend on the higher-level agents package.
    """

    async def complete(self, route_id: str, request: ModelRequest) -> ModelResponse: ...

    def stream(self, route_id: str, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]: ...


class ProductModelRouteResolver(ABC):
    """Resolve the host-owned model route for one selected product."""

    @abstractmethod
    def route_for(self, product_id: str) -> str: ...


class AgentEventSink(ABC):
    @abstractmethod
    async def publish(self, event: AgentEvent) -> None: ...


class AgentEventStream(ABC):
    """Source for WebSocket bridges; transport and authentication stay outside."""

    @abstractmethod
    def stream(
        self,
        identity: SessionIdentity,
        *,
        after_sequence: int = 0,
    ) -> AsyncIterator[AgentEvent]: ...
