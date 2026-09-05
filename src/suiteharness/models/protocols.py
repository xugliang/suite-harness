"""Extension contracts for model providers and server credential sources."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from pydantic import SecretStr

from suiteharness.models.types import ModelRequest, ModelResponse, ModelStreamEvent


@runtime_checkable
class SecretResolver(Protocol):
    """Resolve a server-managed secret reference.

    Implementations must never accept a credential supplied by a chat user or
    model output.  The file-backed resolver is the initial deployment option;
    this protocol leaves a clean migration path to a secret manager.
    """

    def resolve(self, reference: str) -> SecretStr:
        """Return the secret or raise when the reference is unavailable."""


@runtime_checkable
class ModelProvider(Protocol):
    provider_id: str

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Produce one normalized response."""

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        """Produce normalized streaming events."""


@runtime_checkable
class AccessTokenProvider(Protocol):
    async def access_token(self, audience: str) -> str:
        """Return a short-lived cloud service token for a server identity."""
