"""Ports for sandbox backends and the bounded host process supervisor."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from .models import ProcessResult, SandboxAvailability, SandboxRequest, SandboxResult


class SandboxProcessTransport(Protocol):
    """Trusted process supervisor used by concrete sandbox adapters.

    ``environment`` is an overlay, not a complete replacement for the minimal
    host environment needed to locate the configured executable.
    """

    async def probe(self, argv: tuple[str, ...], *, timeout_seconds: float) -> ProcessResult: ...

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


class SandboxBackend(Protocol):
    @property
    def backend_id(self) -> str: ...

    @property
    def production_safe(self) -> bool: ...

    async def availability(self) -> SandboxAvailability: ...

    async def run(self, request: SandboxRequest) -> SandboxResult: ...


class SandboxQuarantineStore(Protocol):
    """Durable exact-name inventory for potentially orphaned sandboxes."""

    async def load(self) -> Mapping[str, str]: ...

    async def mark(self, container_name: str, reason: str) -> None: ...

    async def claim(
        self,
        container_name: str,
        reason: str,
        *,
        owner_token: str,
    ) -> bool: ...

    async def clear(
        self,
        container_name: str,
        *,
        owner_token: str | None = None,
    ) -> bool: ...


__all__ = ["SandboxBackend", "SandboxProcessTransport", "SandboxQuarantineStore"]
