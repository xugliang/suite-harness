"""Narrow host/plugin contracts without execution root authority."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

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


@runtime_checkable
class ContributionHandle(Protocol):
    """Identity-specific revocation; implementations must be idempotent."""

    async def revoke(self) -> None: ...


@runtime_checkable
class ContributionRegistry(Protocol):
    """Only host capability exposed for publishing plugin contributions.

    Implementations must publish the complete batch atomically.  They must not
    expose the tool Runner, grant issuer, approval authority or root registry.
    """

    async def publish(
        self,
        registrations: tuple[ContributionRegistration, ...],
        *,
        replaces: frozenset[str],
    ) -> tuple[ContributionHandle, ...]: ...


class PluginPrepareContextProtocol(Protocol):
    manifest: PluginManifest
    config: Mapping[str, Any]
    permissions: PermissionSet

    def inject(self, service: str) -> object | None: ...


class PluginActivationContextProtocol(Protocol):
    manifest: PluginManifest
    permissions: PermissionSet

    def contribute(
        self,
        kind: ContributionKind,
        contribution_id: str,
        value: object,
    ) -> ContributionHandle: ...


@runtime_checkable
class PreparedPlugin(Protocol):
    @property
    def exports(self) -> Mapping[str, object]: ...

    async def activate(self, context: PluginActivationContextProtocol) -> None: ...

    async def close(self) -> None: ...


@runtime_checkable
class PluginRuntime(Protocol):
    async def prepare(self, context: PluginPrepareContextProtocol) -> PreparedPlugin: ...

    async def close(self) -> None: ...


@runtime_checkable
class TrustedPluginLoader(Protocol):
    async def load(self, source: VerifiedPluginSource) -> PluginRuntime: ...


@runtime_checkable
class PluginLauncher(Protocol):
    """Trusted host adapter that returns a proxy to an isolated process/MCP peer."""

    @property
    def trust_mode(self) -> TrustMode: ...

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
