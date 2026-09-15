"""Ports for sandbox backends and the bounded host process supervisor."""

from __future__ import annotations

from abc import ABC, abstractmethod

from collections.abc import Mapping
from pathlib import Path


from .models import ProcessResult, SandboxAvailability, SandboxRequest, SandboxResult, SandboxUnavailable


class SandboxSession(ABC):
    """Persistent bounded byte session, owned and closed by its backend."""

    @abstractmethod
    async def send_line(self, payload: bytes) -> None: ...

    @abstractmethod
    async def receive_line(self) -> bytes | None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def wait_closed(self) -> None: ...


class SandboxProcessTransport(ABC):
    """Trusted process supervisor used by concrete sandbox adapters.

    ``environment`` is an overlay, not a complete replacement for the minimal
    host environment needed to locate the configured executable.
    """

    @abstractmethod
    async def probe(self, argv: tuple[str, ...], *, timeout_seconds: float) -> ProcessResult: ...

    async def open_session(self, argv: tuple[str, ...], *, environment: Mapping[str, str],
                           timeout_seconds: float, output_bytes: int) -> SandboxSession:
        raise SandboxUnavailable("process supervisor does not support persistent sessions")

    @abstractmethod
    async def execute(
        self,
        argv: tuple[str, ...],
        *,
        working_directory: Path | None,
        environment: Mapping[str, str],
        stdin: bytes | None,
        timeout_seconds: float,
        output_bytes: int,
    ) -> ProcessResult: ...


class SandboxBackend(ABC):
    async def open_session(self, request: SandboxRequest) -> SandboxSession:
        raise SandboxUnavailable("sandbox backend does not support persistent sessions")

    @property
    @abstractmethod
    def backend_id(self) -> str: ...

    @property
    @abstractmethod
    def production_safe(self) -> bool: ...

    @abstractmethod
    async def availability(self) -> SandboxAvailability: ...

    @abstractmethod
    async def run(self, request: SandboxRequest) -> SandboxResult: ...


class SandboxQuarantineStore(ABC):
    """Durable exact-name inventory for potentially orphaned sandboxes."""

    @abstractmethod
    async def load(self) -> Mapping[str, str]: ...

    @abstractmethod
    async def mark(self, container_name: str, reason: str) -> None: ...

    @abstractmethod
    async def claim(
        self,
        container_name: str,
        reason: str,
        *,
        owner_token: str,
    ) -> bool: ...

    @abstractmethod
    async def clear(
        self,
        container_name: str,
        *,
        owner_token: str | None = None,
    ) -> bool: ...


__all__ = ["SandboxBackend", "SandboxProcessTransport", "SandboxQuarantineStore"]
