"""Transactional two-phase plugin activation and replacement."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from suiteharness.plugins.discovery import PluginDiscovery, VerifiedPluginSource
from suiteharness.plugins.errors import (
    PluginErrorCode,
    PluginLifecycleError,
)
from suiteharness.plugins.graph import PluginPlan, PluginPlanner
from suiteharness.plugins.lifecycle import Disposer, PluginCleanupError, PluginFiber
from suiteharness.plugins.models import (
    ContributionKind,
    PermissionSet,
    PluginManifest,
    PluginSourceDeclaration,
    TrustMode,
)
from suiteharness.plugins.protocols import (
    ContributionHandle,
    ContributionRegistration,
    ContributionRegistry,
    PluginActivationIdentity,
    PluginLaunchDescriptor,
    PluginLauncher,
    PluginRuntime,
    PreparedPlugin,
    ProviderBinding,
    TrustedPluginLoader,
)

_MISSING = object()


class PluginPrepareContext:
    """Dependency injection view; permissions here are metadata, not authority."""

    def __init__(
        self,
        *,
        manifest: PluginManifest,
        config: Mapping[str, Any],
        bindings: Mapping[str, ProviderBinding | None],
        fiber: PluginFiber,
    ) -> None:
        self.manifest = manifest
        self.config = config
        self.permissions = manifest.permissions
        self._bindings = MappingProxyType(dict(bindings))
        self._fiber = fiber
        self._declared = frozenset(item.service for item in manifest.requires)

    def inject(self, service: str) -> object | None:
        if service not in self._declared:
            raise PluginLifecycleError(
                PluginErrorCode.INVALID_EXPORTS,
                "plugin attempted to inject an undeclared service",
                plugin_id=self.manifest.plugin_id,
                service=service,
            )
        binding = self._bindings.get(service)
        return None if binding is None else binding.value

    def provider(self, service: str) -> ProviderBinding | None:
        if service not in self._declared:
            raise PluginLifecycleError(
                PluginErrorCode.INVALID_EXPORTS,
                "plugin attempted to inspect an undeclared service",
                plugin_id=self.manifest.plugin_id,
                service=service,
            )
        return self._bindings.get(service)

    def own(self, label: str, disposer: Disposer) -> None:
        self._fiber.own(f"prepare:{label}", disposer)


class _PendingContributionHandle:
    def __init__(self, context: PluginActivationContext, key: tuple[ContributionKind, str]) -> None:
        self._context = context
        self._key = key
        self._lock = asyncio.Lock()
        self._revoked = False

    async def revoke(self) -> None:
        async with self._lock:
            if self._revoked:
                return
            self._revoked = True
        self._context._revoke(self._key)


class PluginActivationContext:
    """Collect contributions privately; publication happens once, atomically."""

    def __init__(
        self,
        *,
        identity: PluginActivationIdentity,
        manifest: PluginManifest,
        fiber: PluginFiber,
    ) -> None:
        self.identity = identity
        self.manifest = manifest
        self.permissions: PermissionSet = manifest.permissions
        self._fiber = fiber
        self._expected = manifest.contribution_map()
        self._values: dict[tuple[ContributionKind, str], object] = {}
        self._lock = threading.RLock()

    def contribute(
        self,
        kind: ContributionKind,
        contribution_id: str,
        value: object,
    ) -> ContributionHandle:
        key = kind, contribution_id
        declaration = self._expected.get(key)
        if declaration is None:
            raise PluginLifecycleError(
                PluginErrorCode.INVALID_CONTRIBUTION,
                "plugin attempted to publish an undeclared contribution",
                plugin_id=self.manifest.plugin_id,
                kind=kind.value,
                contribution_id=contribution_id,
            )
        if not self.permissions.contains(declaration.permissions):
            raise PluginLifecycleError(
                PluginErrorCode.INVALID_CONTRIBUTION,
                "contribution exceeds the plugin permission ceiling",
                plugin_id=self.manifest.plugin_id,
                kind=kind.value,
                contribution_id=contribution_id,
            )
        with self._lock:
            if key in self._values:
                raise PluginLifecycleError(
                    PluginErrorCode.INVALID_CONTRIBUTION,
                    "plugin published a contribution more than once",
                    plugin_id=self.manifest.plugin_id,
                    kind=kind.value,
                    contribution_id=contribution_id,
                )
            self._values[key] = value
        return _PendingContributionHandle(self, key)

    def own(self, label: str, disposer: Disposer) -> None:
        self._fiber.own(f"activate:{label}", disposer)

    def _revoke(self, key: tuple[ContributionKind, str]) -> None:
        with self._lock:
            self._values.pop(key, None)

    def registrations(self) -> tuple[ContributionRegistration, ...]:
        with self._lock:
            missing = [
                f"{kind.value}:{contribution_id}"
                for kind, contribution_id in self._expected
                if (kind, contribution_id) not in self._values
            ]
            if missing:
                raise PluginLifecycleError(
                    PluginErrorCode.INVALID_CONTRIBUTION,
                    "plugin did not publish every declared contribution",
                    plugin_id=self.manifest.plugin_id,
                    missing=tuple(missing),
                )
            return tuple(
                ContributionRegistration(
                    owner=self.identity,
                    kind=kind,
                    declaration=declaration,
                    value=self._values[(kind, declaration.contribution_id)],
                )
                for kind, declaration in self.manifest.contributions.items()
            )


@dataclass(slots=True)
class _PreparedState:
    source: VerifiedPluginSource
    identity: PluginActivationIdentity
    fiber: PluginFiber
    runtime: PluginRuntime
    prepared: PreparedPlugin
    activation_context: PluginActivationContext


@dataclass(slots=True)
class _ActiveSet:
    plan: PluginPlan
    fiber: PluginFiber
    plugins: tuple[_PreparedState, ...]

    @property
    def activation_ids(self) -> frozenset[str]:
        return frozenset(item.identity.activation_id for item in self.plugins)


class PluginManager:
    """Serialize lifecycle transitions and atomically publish complete plans."""

    def __init__(
        self,
        *,
        harness_version: str,
        discovery: PluginDiscovery,
        registry: ContributionRegistry,
        trusted_loader: TrustedPluginLoader,
        launchers: Iterable[PluginLauncher] = (),
    ) -> None:
        self._discovery = discovery
        self._planner = PluginPlanner(
            harness_version=harness_version,
            attestation=discovery.attestation,
        )
        self._registry = registry
        self._trusted_loader = trusted_loader
        self._launchers: dict[TrustMode, PluginLauncher] = {}
        for launcher in launchers:
            if launcher.trust_mode is TrustMode.TRUSTED_IN_PROCESS:
                raise ValueError("trusted in-process plugins use TrustedPluginLoader")
            if launcher.trust_mode in self._launchers:
                raise ValueError(f"duplicate launcher for {launcher.trust_mode.value}")
            self._launchers[launcher.trust_mode] = launcher
        self._lock = asyncio.Lock()
        self._active: _ActiveSet | None = None

    def plan(self, declarations: Iterable[PluginSourceDeclaration]) -> PluginPlan:
        """Read and verify manifests, then resolve the graph without loading code."""

        return self._planner.plan(self._discovery.discover_all(declarations))

    @property
    def active_plugin_ids(self) -> tuple[str, ...]:
        active = self._active
        return () if active is None else active.plan.plugin_ids

    async def activate(self, plan: PluginPlan) -> None:
        async with self._lock:
            if self._active is not None:
                raise PluginLifecycleError(
                    PluginErrorCode.ALREADY_ACTIVE,
                    "a plugin plan is already active; use reload",
                )
            self._active = await self._build(plan, replaces=frozenset())

    async def reload(self, plan: PluginPlan) -> None:
        """Prepare replacement first, publish it, then retire the old fibers."""

        async with self._lock:
            previous = self._active
            replaces = frozenset() if previous is None else previous.activation_ids
            replacement = await self._build(plan, replaces=replaces)
            self._active = replacement
            if previous is not None:
                await previous.fiber.close()

    async def deactivate(self) -> None:
        async with self._lock:
            active = self._active
            if active is None:
                return
            self._active = None
            await active.fiber.close()

    async def _build(self, plan: PluginPlan, *, replaces: frozenset[str]) -> _ActiveSet:
        if not plan.is_attested_by(self._discovery.attestation):
            raise PluginLifecycleError(
                PluginErrorCode.TRUST_REJECTED,
                "plugin plan was not attested by this manager's discovery",
            )

        activation_fiber = PluginFiber(f"plugin-plan:{uuid4().hex}")
        prepared_states: list[_PreparedState] = []
        exported: dict[str, tuple[VerifiedPluginSource, object]] = {}
        try:
            for source in plan.sources:
                self._discovery.assert_current(source)
                identity = PluginActivationIdentity(
                    plugin_id=source.manifest.plugin_id,
                    version=source.manifest.version,
                    artifact_digest=source.digest,
                    activation_id=uuid4().hex,
                )
                plugin_fiber = PluginFiber(
                    f"plugin:{source.manifest.plugin_id}:{identity.activation_id}"
                )
                activation_fiber.own(
                    f"plugin:{source.manifest.plugin_id}", plugin_fiber.close
                )
                runtime = await self._load_runtime(source, identity)
                if not isinstance(runtime, PluginRuntime):
                    raise PluginLifecycleError(
                        PluginErrorCode.ACTIVATION_FAILED,
                        "plugin entrypoint did not return a PluginRuntime",
                        plugin_id=source.manifest.plugin_id,
                    )
                plugin_fiber.own("runtime", runtime.close)

                bindings: dict[str, ProviderBinding | None] = {}
                for resolved in plan.bindings[source.manifest.plugin_id]:
                    service = resolved.requirement.service
                    if resolved.provider_plugin_id is None:
                        bindings[service] = None
                        continue
                    provider_source, value = exported[service]
                    provided = next(
                        item
                        for item in provider_source.manifest.provides
                        if item.service == service
                    )
                    bindings[service] = ProviderBinding(
                        service=service,
                        version=provided.version,
                        provider_plugin_id=provider_source.manifest.plugin_id,
                        value=value,
                    )
                prepare_context = PluginPrepareContext(
                    manifest=source.manifest,
                    config=source.config,
                    bindings=bindings,
                    fiber=plugin_fiber,
                )
                prepared = await runtime.prepare(prepare_context)
                if not isinstance(prepared, PreparedPlugin):
                    raise PluginLifecycleError(
                        PluginErrorCode.ACTIVATION_FAILED,
                        "plugin prepare did not return a PreparedPlugin",
                        plugin_id=source.manifest.plugin_id,
                    )
                plugin_fiber.own("prepared", prepared.close)
                self._validate_exports(source, prepared.exports)
                for service, value in prepared.exports.items():
                    exported[service] = source, value

                activation_context = PluginActivationContext(
                    identity=identity,
                    manifest=source.manifest,
                    fiber=plugin_fiber,
                )
                prepared_states.append(
                    _PreparedState(
                        source=source,
                        identity=identity,
                        fiber=plugin_fiber,
                        runtime=runtime,
                        prepared=prepared,
                        activation_context=activation_context,
                    )
                )

            pending: list[tuple[_PreparedState, ContributionRegistration]] = []
            for state in prepared_states:
                await state.prepared.activate(state.activation_context)
                pending.extend(
                    (state, registration)
                    for registration in state.activation_context.registrations()
                )

            handles = await self._registry.publish(
                tuple(registration for _, registration in pending),
                replaces=replaces,
            )
            if len(handles) != len(pending):
                for handle in reversed(handles):
                    await handle.revoke()
                raise PluginLifecycleError(
                    PluginErrorCode.ACTIVATION_FAILED,
                    "contribution registry returned an invalid handle count",
                    expected=len(pending),
                    actual=len(handles),
                )
            for (state, registration), handle in zip(pending, handles, strict=True):
                state.fiber.own(
                    f"contribution:{registration.kind.value}:"
                    f"{registration.declaration.contribution_id}",
                    handle.revoke,
                )
            return _ActiveSet(plan, activation_fiber, tuple(prepared_states))
        except (KeyboardInterrupt, SystemExit):
            try:
                await activation_fiber.close()
            finally:
                raise
        except BaseException as exc:
            try:
                await activation_fiber.close()
            except PluginCleanupError as cleanup:
                raise PluginLifecycleError(
                    PluginErrorCode.ACTIVATION_FAILED,
                    "plugin activation failed and rollback was incomplete",
                    activation_error=repr(exc),
                    cleanup_error=repr(cleanup),
                ) from exc
            if isinstance(exc, PluginLifecycleError):
                raise
            raise PluginLifecycleError(
                PluginErrorCode.ACTIVATION_FAILED,
                "plugin activation failed and was rolled back",
                cause=repr(exc),
            ) from exc

    async def _load_runtime(
        self,
        source: VerifiedPluginSource,
        identity: PluginActivationIdentity,
    ) -> PluginRuntime:
        mode = source.manifest.trust_mode
        if mode is TrustMode.TRUSTED_IN_PROCESS:
            return await self._trusted_loader.load(source)
        launcher = self._launchers.get(mode)
        if launcher is None:
            raise PluginLifecycleError(
                PluginErrorCode.UNSUPPORTED_TRUST_MODE,
                "no isolated launcher is configured for this trust mode",
                plugin_id=source.manifest.plugin_id,
                trust_mode=mode.value,
            )
        # The launcher is trusted host code.  It must return an RPC proxy; the
        # manager never imports isolated-worker or MCP plugin source modules.
        descriptor = PluginLaunchDescriptor(
            source_path=str(source.root),
            entrypoint=source.manifest.entrypoint,
            artifact_digest=source.digest,
            manifest=source.manifest,
            config=source.config,
        )
        return await launcher.launch(descriptor, identity)

    @staticmethod
    def _validate_exports(
        source: VerifiedPluginSource,
        exports: Mapping[str, object],
    ) -> None:
        if not isinstance(exports, Mapping):
            raise PluginLifecycleError(
                PluginErrorCode.INVALID_EXPORTS,
                "prepared plugin exports must be a mapping",
                plugin_id=source.manifest.plugin_id,
            )
        declared = frozenset(item.service for item in source.manifest.provides)
        actual = frozenset(exports)
        if declared != actual:
            raise PluginLifecycleError(
                PluginErrorCode.INVALID_EXPORTS,
                "prepared plugin exports must exactly match manifest provides",
                plugin_id=source.manifest.plugin_id,
                missing=tuple(sorted(declared - actual)),
                undeclared=tuple(sorted(actual - declared)),
            )


__all__ = ["PluginActivationContext", "PluginManager", "PluginPrepareContext"]
