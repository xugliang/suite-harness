"""Agent-facing model and live-event contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from suiteharness.agents.events import AgentEvent
from suiteharness.models import ModelRequest, ModelResponse
from suiteharness.sessions import SessionIdentity


class ModelGatewayLike(Protocol):
    """Narrow gateway surface used by workflows and easy to fake in tests."""

    async def complete(self, route_id: str, request: ModelRequest) -> ModelResponse: ...


class ProductModelRouteResolver(Protocol):
    """Resolve the host-owned model route for one selected product."""

    def route_for(self, product_id: str) -> str: ...


class AgentEventSink(Protocol):
    async def publish(self, event: AgentEvent) -> None: ...


class AgentEventStream(Protocol):
    """Source for WebSocket bridges; transport and authentication stay outside."""

    def stream(
        self,
        identity: SessionIdentity,
        *,
        after_sequence: int = 0,
    ) -> AsyncIterator[AgentEvent]: ...
