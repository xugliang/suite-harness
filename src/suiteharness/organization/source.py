"""Provider-neutral source boundary and safe synchronization errors."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import DirectorySnapshot


class SourceError(Exception):
    """A safe operational error; messages must never contain provider payloads."""

    def __init__(
        self,
        message: str = "Organization source failed",
        *,
        code: str = "source_error",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class OrganizationSource(ABC):
    @abstractmethod
    async def fetch(self) -> DirectorySnapshot:
        """Return a complete visible snapshot or raise SourceError."""


__all__ = ["OrganizationSource", "SourceError"]
