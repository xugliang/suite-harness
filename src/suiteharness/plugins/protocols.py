"""Narrow host/plugin contracts without execution root authority."""

from __future__ import annotations

from abc import ABC, abstractmethod

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from suiteharness.plugins.discovery import VerifiedPluginSource
from suiteharness.plugins.models import (
    ContributionDeclaration,
    ContributionKind,
    PermissionSet,
    PluginManifest,
    TrustMode,
)


@dataclass(frozen=True, slots=True)
class PluginActivationIdentity:
    plugin_id: str
    version: str
    artifact_digest: str
    activation_id: str


@dataclass(frozen=True, slots=True)
class PluginLaunchDescriptor:
    """Data passed to a trusted worker/MCP launcher without importing source."""

    source_path: str
    entrypoint: str
    artifact_digest: str
    manifest: PluginManifest
    config: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ProviderBinding:
    service: str
    version: str
    provider_plugin_id: str
    value: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ContributionRegistration:
    owner: PluginActivationIdentity
    kind: ContributionKind
    declaration: ContributionDeclaration
    value: object = field(repr=False, compare=False)

    @property
    def key(self) -> tuple[ContributionKind, str]:
        return self.kind, self.declaration.contribution_id


class ContributionHandle(ABC):
    """Identity-specific revocation; implementations must be idempotent."""

    @abstractmethod
    async def revoke(self) -> None: ...


class ContributionRegistry(ABC):
    """Only host capability exposed for publishing plugin contributions.

    Implementations must publish the complete batch atomically.  They must not
    expose the tool Runner, grant issuer, approval authority or root registry.
    """

    @abstractmethod
    async def publish(
        self,
        registrations: tuple[ContributionRegistration, ...],
        *,
        replaces: frozenset[str],
    ) -> tuple[ContributionHandle, ...]: ...

    async def snapshot(self) -> tuple:
        """Host composition requires a consistent visible contribution snapshot."""
        raise NotImplementedError("registry must support snapshot for server composition")


class PluginPrepareContextProtocol(ABC):
    manifest: PluginManifest
    config: Mapping[str, Any]
    permissions: PermissionSet

    @abstractmethod
    def inject(self, service: str) -> object | None: ...


class PluginActivationContextProtocol(ABC):
    manifest: PluginManifest
    permissions: PermissionSet

    @abstractmethod
    def contribute(
        self,
        kind: ContributionKind,
        contribution_id: str,
        value: object,
    ) -> ContributionHandle: ...


class PreparedPlugin(ABC):
    @property
    @abstractmethod
    def exports(self) -> Mapping[str, object]: ...

    @abstractmethod
    async def activate(self, context: PluginActivationContextProtocol) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...


class PluginRuntime(ABC):
    @abstractmethod
    async def prepare(self, context: PluginPrepareContextProtocol) -> PreparedPlugin: ...

    @abstractmethod
    async def close(self) -> None: ...


class TrustedPluginLoader(ABC):
    @abstractmethod
    async def load(self, source: VerifiedPluginSource) -> PluginRuntime: ...


class PluginLauncher(ABC):
    """Trusted host adapter that returns a proxy to an isolated process/MCP peer."""

    @property
    @abstractmethod
    def trust_mode(self) -> TrustMode: ...

    @abstractmethod
    async def launch(
        self,
        descriptor: PluginLaunchDescriptor,
        identity: PluginActivationIdentity,
    ) -> PluginRuntime: ...


__all__ = [
    "ContributionHandle",
    "ContributionRegistration",
    "ContributionRegistry",
    "PluginActivationContextProtocol",
    "PluginActivationIdentity",
    "PluginLauncher",
    "PluginLaunchDescriptor",
    "PluginPrepareContextProtocol",
    "PluginRuntime",
    "PreparedPlugin",
    "ProviderBinding",
    "TrustedPluginLoader",
]
