"""Extension contracts for model providers and server credential sources."""

from __future__ import annotations

from abc import ABC, abstractmethod

from collections.abc import AsyncIterator


from pydantic import SecretStr

from suiteharness.models.types import ModelRequest, ModelResponse, ModelStreamEvent


class SecretResolver(ABC):
    """Resolve a server-managed secret reference.

    Implementations must never accept a credential supplied by a chat user or
    model output.  The file-backed resolver is the initial deployment option;
    this protocol leaves a clean migration path to a secret manager.
    """

    @abstractmethod
    def resolve(self, reference: str) -> SecretStr:
        """Return the secret or raise when the reference is unavailable."""


class ModelProvider(ABC):
    provider_id: str

    @abstractmethod
    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Produce one normalized response."""

    @abstractmethod
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        """Produce normalized streaming events."""


class AccessTokenProvider(ABC):
    @abstractmethod
    async def access_token(self, audience: str) -> str:
        """Return a short-lived cloud service token for a server identity."""
