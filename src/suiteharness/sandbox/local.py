"""Explicitly unsafe local command adapter for source-tree development only."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .models import SandboxAvailability, SandboxRequest, SandboxResult, SandboxUnavailable
from .protocols import SandboxProcessTransport


@dataclass(frozen=True)
class LocalDevelopmentConfig:
    environment: str
    acknowledge_unsafe: bool

    def __post_init__(self) -> None:
        if self.environment != "development" or not self.acknowledge_unsafe:
            raise ValueError(
                "the unsafe local backend requires development environment and explicit acknowledgement"
            )


class LocalDevelopmentSandboxBackend:
    """Not a security boundary; never valid for a production deployment."""

    def __init__(
        self,
        config: LocalDevelopmentConfig,
        transport: SandboxProcessTransport,
    ) -> None:
        self._config = config
        self._transport = transport

    @property
    def backend_id(self) -> str:
        return "local-unsafe"

    @property
    def production_safe(self) -> bool:
        return False

    async def availability(self) -> SandboxAvailability:
        # Availability means a trusted process supervisor was explicitly wired;
        # it does not imply isolation.
        return SandboxAvailability(True, "unsafe local development backend")

    @staticmethod
    def _host_working_directory(request: SandboxRequest) -> Path:
        working = PurePosixPath(request.working_directory)
        candidates = sorted(
            request.mounts,
            key=lambda item: len(PurePosixPath(item.container_path).parts),
            reverse=True,
        )
        for mount in candidates:
            target = PurePosixPath(mount.container_path)
            if working == target or target in working.parents:
                relative = working.relative_to(target)
                host = mount.host_path.joinpath(*relative.parts).resolve(strict=True)
                source = mount.host_path.resolve(strict=True)
                if host == source or host.is_relative_to(source):
                    return host
        raise SandboxUnavailable("local working directory is outside mounted workspace roots")

    async def run(self, request: SandboxRequest) -> SandboxResult:
        result = await self._transport.execute(
            request.argv,
            working_directory=self._host_working_directory(request),
            environment=request.environment,
            stdin=request.stdin,
            timeout_seconds=request.limits.timeout_seconds,
            output_bytes=request.limits.output_bytes,
        )
        return SandboxResult(
            request_id=request.request_id,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
            output_truncated=result.output_truncated,
        )


__all__ = ["LocalDevelopmentConfig", "LocalDevelopmentSandboxBackend"]
