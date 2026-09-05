"""Provider-neutral requests, limits and results for command isolation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType

_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENVIRONMENT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SandboxError(RuntimeError):
    """Base error for sandbox validation and execution failures."""


class SandboxUnavailable(SandboxError):
    """The required isolation backend is unavailable; execution failed closed."""


class SandboxCleanupError(SandboxUnavailable):
    """A known sandbox process may still exist, so the backend is quarantined."""


class SandboxConfigurationError(SandboxError):
    """A sandbox request or backend setting is unsafe or malformed."""


class SandboxNetworkMode(str, Enum):
    NONE = "none"
    EGRESS_PROFILE = "egress_profile"


@dataclass(frozen=True)
class SandboxNetworkPolicy:
    mode: SandboxNetworkMode = SandboxNetworkMode.NONE
    egress_profile: str | None = None

    def __post_init__(self) -> None:
        if self.mode is SandboxNetworkMode.NONE and self.egress_profile is not None:
            raise SandboxConfigurationError("network-disabled requests cannot name an egress profile")
        if self.mode is SandboxNetworkMode.EGRESS_PROFILE and not self.egress_profile:
            raise SandboxConfigurationError("network-enabled requests require an egress profile")


@dataclass(frozen=True)
class SandboxLimits:
    cpu_count: float = 1.0
    memory_mb: int = 512
    pids: int = 128
    timeout_seconds: float = 120.0
    output_bytes: int = 1_048_576
    tmpfs_mb: int = 128

    def __post_init__(self) -> None:
        if not 0 < self.cpu_count <= 64:
            raise SandboxConfigurationError("cpu_count must be in (0, 64]")
        if not 64 <= self.memory_mb <= 262_144:
            raise SandboxConfigurationError("memory_mb must be in [64, 262144]")
        if not 16 <= self.pids <= 32_768:
            raise SandboxConfigurationError("pids must be in [16, 32768]")
        if not 0 < self.timeout_seconds <= 86_400:
            raise SandboxConfigurationError("timeout_seconds must be in (0, 86400]")
        if not 1_024 <= self.output_bytes <= 1_073_741_824:
            raise SandboxConfigurationError("output_bytes must be in [1024, 1073741824]")
        if not 16 <= self.tmpfs_mb <= 65_536:
            raise SandboxConfigurationError("tmpfs_mb must be in [16, 65536]")


def _container_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise SandboxConfigurationError("container mount paths must be normalized absolute paths")
    workspace = PurePosixPath("/workspace")
    if path != workspace and workspace not in path.parents:
        raise SandboxConfigurationError("container mounts are restricted to /workspace")
    return path


@dataclass(frozen=True)
class SandboxMount:
    host_path: Path
    container_path: str
    read_only: bool = True

    def __post_init__(self) -> None:
        host = Path(self.host_path)
        if not host.is_absolute():
            raise SandboxConfigurationError("sandbox mount host paths must be absolute")
        if not host.exists() or not host.is_dir():
            raise SandboxConfigurationError("sandbox mount host paths must be existing directories")
        if "," in str(host) or "\x00" in str(host):
            raise SandboxConfigurationError("sandbox mount host paths contain an unsupported character")
        _container_path(self.container_path)
        object.__setattr__(self, "host_path", host)


@dataclass(frozen=True)
class SandboxRequest:
    request_id: str
    argv: tuple[str, ...]
    mounts: tuple[SandboxMount, ...]
    working_directory: str = "/workspace"
    environment: Mapping[str, str] = field(default_factory=dict)
    stdin: bytes | None = None
    limits: SandboxLimits = field(default_factory=SandboxLimits)
    network: SandboxNetworkPolicy = field(default_factory=SandboxNetworkPolicy)

    def __post_init__(self) -> None:
        if not _REQUEST_ID.fullmatch(self.request_id):
            raise SandboxConfigurationError("invalid sandbox request_id")
        if not self.argv or any(not value or "\x00" in value for value in self.argv):
            raise SandboxConfigurationError("sandbox argv must contain non-empty NUL-free values")
        if not self.mounts:
            raise SandboxConfigurationError("sandbox requests require at least one workspace mount")
        targets = [str(_container_path(item.container_path)) for item in self.mounts]
        if len(targets) != len(set(targets)):
            raise SandboxConfigurationError("sandbox mount targets must be unique")
        working = _container_path(self.working_directory)
        if not any(
            working == PurePosixPath(target) or PurePosixPath(target) in working.parents
            for target in targets
        ):
            raise SandboxConfigurationError("working_directory must be inside a mounted path")
        clean_environment: dict[str, str] = {}
        for key, value in self.environment.items():
            if not _ENVIRONMENT_KEY.fullmatch(key):
                raise SandboxConfigurationError(f"invalid environment variable name: {key!r}")
            if "\x00" in value:
                raise SandboxConfigurationError("environment values must not contain NUL")
            clean_environment[key] = value
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(self, "mounts", tuple(self.mounts))
        object.__setattr__(self, "environment", MappingProxyType(clean_environment))


@dataclass(frozen=True)
class SandboxResult:
    request_id: str
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_truncated: bool = False

    def __post_init__(self) -> None:
        if not _REQUEST_ID.fullmatch(self.request_id):
            raise SandboxConfigurationError("invalid sandbox result request_id")
        if self.timed_out and self.exit_code is not None:
            raise SandboxConfigurationError("timed-out results must not report a normal exit code")


@dataclass(frozen=True)
class SandboxAvailability:
    available: bool
    detail: str = ""


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_truncated: bool = False


__all__ = [
    "ProcessResult",
    "SandboxAvailability",
    "SandboxCleanupError",
    "SandboxConfigurationError",
    "SandboxError",
    "SandboxLimits",
    "SandboxMount",
    "SandboxNetworkMode",
    "SandboxNetworkPolicy",
    "SandboxRequest",
    "SandboxResult",
    "SandboxUnavailable",
]
