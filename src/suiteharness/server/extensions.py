"""Transactional server wiring for source plugins and product-scoped MCP.

This module is intentionally a composition root rather than another protocol
implementation.  Production transports, signature verification and plugin
execution adapters are supplied explicitly by the server application.  SuiteHarness
never scans plugin directories, launches an unsandboxed stdio process, or
falls back to an unauthenticated HTTP client here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from suiteharness import __version__
from suiteharness.config import LoadedSuiteHarnessConfig
from suiteharness.execution import InMemoryToolRegistry, ToolIdentity
from suiteharness.mcp import (
    ClientCapabilities,
    LegacySseAdapter,
    LegacySseCompatibility,
    McpChangeSink,
    McpClientManager,
    McpClientTaskHandler,
    McpConnectionState,
    McpEgressPolicy,
    McpElicitationHandler,
    McpHttpExchange,
    McpHttpResumeStore,
    McpLogSink,
    McpSampler,
    McpServerConfig,
    McpTokenProvider,
    McpToolBridge,
    McpToolBridgeHandle,
    McpTransportKind,
    ProductMcpConfig,
    SandboxedStdioSessionFactory,
    SecureMcpTransportFactory,
)
from suiteharness.plugins import (
    ContributionRegistry,
    InMemoryContributionRegistry,
    PluginDiscovery,
    PluginLauncher,
    PluginManager,
    PluginManifest,
    PluginPlan,
    SignatureVerifier,
    TrustedPluginLoader,
    TrustMode,
)
from suiteharness.runtime import ProductContext, RequestScope, ScopePath
from suiteharness.sandbox import SandboxBackend

from .access import AdditionalGrantMaterial, AdditionalGrantTool


class ActiveCustomerBundleView(Protocol):
    """Narrow view required after the kernel has activated a customer bundle."""

    @property
    def tenant_id(self) -> str: ...

    @property
    def customer_bundle_id(self) -> str: ...

    @property
    def products(self) -> Mapping[str, ProductContext]: ...

    @property
    def closed(self) -> bool: ...


ProductWorkspaceResolver = Callable[[str], Path]


@dataclass(frozen=True, slots=True)
class PluginHostAdapters:
    """Trusted server adapters; none are inferred from plugin source code."""

    trusted_loader: TrustedPluginLoader
    signature_verifier: SignatureVerifier | None
    isolated_launchers: tuple[PluginLauncher, ...] = ()
    contribution_registry: ContributionRegistry | None = None


@dataclass(frozen=True, slots=True)
class McpHostAdapters:
    """Privileged MCP host ports implemented by the company server.

    ``stdio_sessions`` must create the persistent process through the supplied
    :class:`SandboxBackend`.  ``http_exchange`` is paired with an egress policy;
    credential-bearing HTTP declarations additionally require ``token_provider``.
    """

    stdio_sessions: SandboxedStdioSessionFactory | None = None
    egress_policy: McpEgressPolicy | None = None
    http_exchange: McpHttpExchange | None = None
    token_provider: McpTokenProvider | None = None
    legacy_sse_adapter: LegacySseAdapter | None = None
    client_capabilities: ClientCapabilities = field(default_factory=ClientCapabilities)
    sampler: McpSampler | None = None
    elicitation: McpElicitationHandler | None = None
    logs: McpLogSink | None = None
    changes: McpChangeSink | None = None
    tasks: McpClientTaskHandler | None = None


@dataclass(frozen=True, slots=True)
class McpGrantMaterial:
    """Structured remote-tool inventory published to the channel host."""

    tools: tuple[AdditionalGrantTool, ...] = ()

    def __post_init__(self) -> None:
        validated = AdditionalGrantMaterial(self.tools)
        object.__setattr__(self, "tools", validated.tools)

    @property
    def tool_identities(self) -> frozenset[ToolIdentity]:
        return frozenset(tool.identity for tool in self.tools)

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(
            capability
            for tool in self.tools
            for capability in tool.capabilities
        )


@dataclass(frozen=True, slots=True)
class ExtensionActivation:
    """Immutable result published only after every configured extension starts."""

    plugin_ids: tuple[str, ...]
    mcp_by_product: Mapping[str, McpGrantMaterial]

    def __post_init__(self) -> None:
        object.__setattr__(self, "mcp_by_product", MappingProxyType(dict(self.mcp_by_product)))

    def grant_material(self, product_id: str) -> McpGrantMaterial:
        return self.mcp_by_product.get(product_id, McpGrantMaterial())


class ExtensionConfigurationError(ValueError):
    """Configured extension authority has no explicit trusted host adapter."""


class ExtensionStartupError(RuntimeError):
    """Startup failed and rollback itself also reported one or more failures."""

    def __init__(
        self,
        cause: BaseException,
        rollback_failures: tuple[BaseException, ...],
    ) -> None:
        self.startup_cause = cause
        self.rollback_failures = rollback_failures
        super().__init__("extension startup failed and rollback was incomplete")


@dataclass(frozen=True, slots=True)
class _ConfiguredPluginTrustPolicy:
    """Bind trust to exact configured paths and digests, never directory scans."""

    sources: Mapping[Path, str]
    trusted_in_process_plugins: frozenset[str]
    require_signatures: bool

    def allows(
        self,
        *,
        manifest: PluginManifest,
        source_path: Path,
        digest: str,
        signature_verified: bool,
    ) -> bool:
        configured = self.sources.get(source_path.resolve(strict=True))
        if configured != digest:
            return False
        if self.require_signatures and not signature_verified:
            return False
        return not (
            manifest.trust_mode is TrustMode.TRUSTED_IN_PROCESS
            and manifest.plugin_id not in self.trusted_in_process_plugins
        )


class ServerExtensionHost:
    """Own plugin and MCP lifecycles for one server deployment.

    Construction is side-effect free. :meth:`start_plugins` may run before
    product resolution when plugin contributions are needed to build a catalog.
    :meth:`attach_customer_bundle` runs only after the kernel has activated the
    exact configured customer bundle. :meth:`start` performs both phases as one
    transaction for hosts that already have their product activators.
    """

    def __init__(
        self,
        loaded: LoadedSuiteHarnessConfig,
        *,
        sandbox: SandboxBackend,
        tools: InMemoryToolRegistry,
        product_workspace: ProductWorkspaceResolver,
        plugin_adapters: PluginHostAdapters | None = None,
        mcp_adapters: McpHostAdapters | None = None,
        mcp_resume_store: McpHttpResumeStore | None = None,
        harness_version: str = __version__,
    ) -> None:
        self._loaded = loaded
        self._sandbox = sandbox
        self._tools = tools
        self._product_workspace = product_workspace
        self._plugin_adapters = plugin_adapters
        self._mcp_adapters = mcp_adapters
        self._mcp_resume_store = mcp_resume_store
        self._harness_version = harness_version
        self._plugin_manager: PluginManager | None = None
        self._plugin_registry: ContributionRegistry | None = None
        self._mcp_manager: McpClientManager | None = None
        self._mcp_handles: list[McpToolBridgeHandle] = []
        self._mcp_material: dict[str, McpGrantMaterial] = {}
        self._attached_bundle: ActiveCustomerBundleView | None = None
        self._closed = False
        self._lock = asyncio.Lock()

    @property
    def plugin_manager(self) -> PluginManager | None:
        return self._plugin_manager

    @property
    def plugin_contributions(self) -> ContributionRegistry | None:
        return self._plugin_registry

    @property
    def mcp_manager(self) -> McpClientManager | None:
        return self._mcp_manager

    @property
    def has_configured_extensions(self) -> bool:
        return bool(self._loaded.config.plugins.sources or self._enabled_mcp_servers())

    @property
    def closed(self) -> bool:
        return self._closed

    def readiness_checks(self) -> Mapping[str, bool]:
        """Return non-secret extension readiness for the server health gate."""

        plugins_ready = not self._loaded.config.plugins.sources or (
            self._plugin_manager is not None
        )
        enabled = self._enabled_mcp_servers()
        mcp_ready = not enabled
        if enabled and self._mcp_manager is not None and self._attached_bundle is not None:
            mcp_ready = not self._attached_bundle.closed
            tenant_id = self._loaded.config.deployment.tenant_id
            for product_id, servers in self._loaded.config.mcp.servers_by_product.items():
                health = self._mcp_manager.health(tenant_id, product_id)
                for server in servers:
                    if not server.enabled:
                        continue
                    item = health.get(server.server_id)
                    if item is None or item.state is not McpConnectionState.READY:
                        mcp_ready = False
        return MappingProxyType(
            {
                "plugins": not self._closed and plugins_ready,
                "mcp": not self._closed and mcp_ready,
            }
        )

    @property
    def mcp_tool_identities(self) -> Mapping[str, frozenset[ToolIdentity]]:
        if self._closed or (
            self._attached_bundle is not None and self._attached_bundle.closed
        ):
            return MappingProxyType({})
        return MappingProxyType(
            {
                product_id: material.tool_identities
                for product_id, material in self._mcp_material.items()
            }
        )

    def grant_material(self, product_id: str) -> McpGrantMaterial:
        if self._closed or (
            self._attached_bundle is not None and self._attached_bundle.closed
        ):
            return McpGrantMaterial()
        return self._mcp_material.get(product_id, McpGrantMaterial())

    async def start_plugins(self) -> tuple[str, ...]:
        async with self._lock:
            self._require_open()
            return await self._start_plugins_locked()

    async def attach_customer_bundle(
        self, activation: ActiveCustomerBundleView
    ) -> ExtensionActivation:
        async with self._lock:
            self._require_open()
            mcp_was_attached = self._attached_bundle is not None
            try:
                await self._attach_mcp_locked(activation)
            except BaseException as exc:
                rollback = (
                    [] if mcp_was_attached else await self._rollback_mcp_locked()
                )
                if rollback:
                    raise ExtensionStartupError(exc, tuple(rollback)) from exc
                raise
            return self._snapshot()

    async def start(self, activation: ActiveCustomerBundleView) -> ExtensionActivation:
        """Start both phases and roll plugins back if MCP attachment fails."""

        async with self._lock:
            self._require_open()
            plugins_were_active = self._plugin_manager is not None
            mcp_was_attached = self._attached_bundle is not None
            try:
                await self._start_plugins_locked()
                await self._attach_mcp_locked(activation)
            except BaseException as exc:
                rollback: list[BaseException] = []
                if not mcp_was_attached:
                    rollback.extend(await self._rollback_mcp_locked())
                if not plugins_were_active:
                    rollback.extend(await self._rollback_plugins_locked())
                if rollback:
                    raise ExtensionStartupError(exc, tuple(rollback)) from exc
                raise
            return self._snapshot()

    async def close(self) -> None:
        """Close bridges, connections and plugins in strict reverse startup order."""

        async with self._lock:
            if self._closed:
                return
            self._closed = True
            failures = await self._rollback_mcp_locked()
            failures.extend(await self._rollback_plugins_locked())
            if failures:
                raise BaseExceptionGroup("extension shutdown reported failures", failures)

    async def _start_plugins_locked(self) -> tuple[str, ...]:
        declarations = self._loaded.config.plugins.sources
        if not declarations:
            return ()
        if self._plugin_manager is not None:
            return self._plugin_manager.active_plugin_ids
        adapters = self._plugin_adapters
        if adapters is None:
            raise ExtensionConfigurationError(
                "configured plugins require explicit PluginHostAdapters"
            )
        plugin_config = self._loaded.config.plugins
        if plugin_config.require_signatures and adapters.signature_verifier is None:
            raise ExtensionConfigurationError(
                "require_signatures=true requires an explicit SignatureVerifier"
            )
        source_digests = {
            declaration.path.resolve(strict=False): declaration.expected_digest
            for declaration in declarations
        }
        trust = _ConfiguredPluginTrustPolicy(
            sources=MappingProxyType(source_digests),
            trusted_in_process_plugins=plugin_config.trusted_in_process_plugins,
            require_signatures=plugin_config.require_signatures,
        )
        discovery = PluginDiscovery(
            allowed_roots=plugin_config.allowed_roots,
            trust_policy=trust,
            signature_verifier=adapters.signature_verifier,
        )
        registry = (
            adapters.contribution_registry
            if adapters.contribution_registry is not None
            else InMemoryContributionRegistry()
        )
        manager = PluginManager(
            harness_version=self._harness_version,
            discovery=discovery,
            registry=registry,
            trusted_loader=adapters.trusted_loader,
            launchers=adapters.isolated_launchers,
        )
        plan = manager.plan(declarations)
        self._validate_plugin_launchers(plan, adapters)
        await manager.activate(plan)
        self._plugin_registry = registry
        self._plugin_manager = manager
        return manager.active_plugin_ids

    async def _attach_mcp_locked(self, activation: ActiveCustomerBundleView) -> None:
        enabled = self._enabled_mcp_servers()
        if not enabled:
            return
        if self._attached_bundle is not None:
            if self._attached_bundle is activation:
                return
            raise RuntimeError("MCP is already attached to another customer activation")
        self._validate_activation(activation)
        adapters = self._mcp_adapters
        if adapters is None:
            raise ExtensionConfigurationError(
                "configured MCP servers require explicit McpHostAdapters"
            )
        self._validate_mcp_adapters(enabled, adapters)
        resumable_http = tuple(
            server
            for server in enabled
            if server.transport is McpTransportKind.STREAMABLE_HTTP
            and server.resume_sessions
        )
        if resumable_http:
            if self._mcp_resume_store is None:
                raise ExtensionConfigurationError(
                    "resume_sessions=true requires the server runtime resume store"
                )
            if any(
                server.resume_sessions
                and server.legacy_sse is not LegacySseCompatibility.DISABLED
                for server in resumable_http
            ):
                raise ExtensionConfigurationError(
                    "durable resume is unsupported for legacy SSE adapters"
                )
        configured_secret_refs = {
            server.credential_ref
            for server in enabled
            if server.transport is McpTransportKind.STREAMABLE_HTTP
            and server.credential_ref is not None
        }
        missing_secret_refs = configured_secret_refs - set(self._loaded.secrets.services)
        if missing_secret_refs:
            raise ExtensionConfigurationError(
                "MCP credential references must resolve through server secrets: "
                f"{sorted(missing_secret_refs)!r}"
            )
        transport_factory = SecureMcpTransportFactory(
            sandbox=self._sandbox,
            stdio_sessions=adapters.stdio_sessions,
            egress_policy=adapters.egress_policy,
            http_exchange=adapters.http_exchange,
            token_provider=adapters.token_provider,
            legacy_sse_adapter=adapters.legacy_sse_adapter,
        )
        manager = McpClientManager(
            transport_factory,
            client_capabilities=adapters.client_capabilities,
            sampler=adapters.sampler,
            elicitation=adapters.elicitation,
            logs=adapters.logs,
            changes=adapters.changes,
            tasks=adapters.tasks,
            resume_store=self._mcp_resume_store,
        )
        self._mcp_manager = manager
        bridge = McpToolBridge(manager, self._tools)
        tenant_id = self._loaded.config.deployment.tenant_id
        for product_id in sorted(self._loaded.config.mcp.servers_by_product):
            servers = self._loaded.config.mcp.servers_by_product[product_id]
            if not any(server.enabled for server in servers):
                continue
            product = activation.products[product_id]
            workspace = self._product_workspace(product_id)
            manager.configure(
                ProductMcpConfig(
                    tenant_id=tenant_id,
                    product_id=product_id,
                    product_workspace=workspace,
                    servers=servers,
                )
            )
            clients = await manager.connect_product(tenant_id, product_id)
            try:
                product.effects.callback(
                    f"mcp-connections:{product_id}",
                    lambda tenant_id=tenant_id, product_id=product_id: manager.close_product(
                        tenant_id, product_id
                    ),
                )
            except BaseException:
                await manager.close_product(tenant_id, product_id)
                raise
            request_scope = RequestScope(
                path=ScopePath.product(tenant_id, product_id),
                principal_id="suiteharness-mcp-host",
                channel_id="internal",
                purpose="mcp-tool-discovery",
                request_id=f"mcp-bootstrap-{product_id}",
            )
            grant_tools: list[AdditionalGrantTool] = []
            for client in clients:
                handle = await bridge.install(
                    request_scope,
                    client.config.server_id,
                    registration_scope=product.path,
                    overrides=(
                        self._loaded.config.mcp.tool_security_overrides
                        .get(product_id, {})
                        .get(client.config.server_id)
                    ),
                    effects=product.effects,
                )
                self._mcp_handles.append(handle)
                for tool in handle.tools:
                    grant_tools.append(
                        AdditionalGrantTool(
                            server_id=tool.server_id,
                            remote_name=tool.remote_name,
                            identity=tool.identity,
                            capabilities=tool.spec.required_capabilities,
                        )
                    )
            self._mcp_material[product_id] = McpGrantMaterial(
                tools=tuple(grant_tools),
            )
        self._attached_bundle = activation

    def _snapshot(self) -> ExtensionActivation:
        plugin_ids = (
            () if self._plugin_manager is None else self._plugin_manager.active_plugin_ids
        )
        return ExtensionActivation(plugin_ids, dict(self._mcp_material))

    def _validate_activation(self, activation: ActiveCustomerBundleView) -> None:
        if activation.closed:
            raise RuntimeError("cannot attach MCP to a closed customer activation")
        expected_tenant = self._loaded.config.deployment.tenant_id
        if activation.tenant_id != expected_tenant:
            raise ValueError("customer activation tenant does not match server configuration")
        expected_bundle = self._loaded.config.customer_bundle.customer_bundle_id
        if activation.customer_bundle_id != expected_bundle:
            raise ValueError("active customer bundle does not match server configuration")
        configured_products = {
            selection.product_id
            for selection in self._loaded.config.customer_bundle.products
        }
        if set(activation.products) != configured_products:
            raise ValueError(
                "active products must exactly match the configured customer bundle"
            )
        for product_id, product in activation.products.items():
            if product.path != ScopePath.product(expected_tenant, product_id):
                raise ValueError("active product context does not match its configured scope")

    @staticmethod
    def _validate_plugin_launchers(
        plan: PluginPlan,
        adapters: PluginHostAdapters,
    ) -> None:
        configured_modes = {launcher.trust_mode for launcher in adapters.isolated_launchers}
        missing = sorted(
            {
                source.manifest.trust_mode.value
                for source in plan.sources
                if source.manifest.trust_mode is not TrustMode.TRUSTED_IN_PROCESS
                and source.manifest.trust_mode not in configured_modes
            }
        )
        if missing:
            raise ExtensionConfigurationError(
                f"plugin trust modes have no explicit launcher: {missing!r}"
            )

    @staticmethod
    def _validate_mcp_adapters(
        enabled_servers: tuple[McpServerConfig, ...],
        adapters: McpHostAdapters,
    ) -> None:
        stdio = [
            server
            for server in enabled_servers
            if server.transport is McpTransportKind.STDIO
        ]
        http = [
            server
            for server in enabled_servers
            if server.transport is McpTransportKind.STREAMABLE_HTTP
        ]
        if stdio and adapters.stdio_sessions is None:
            raise ExtensionConfigurationError(
                "stdio MCP requires an explicit sandboxed session factory"
            )
        if http and (adapters.egress_policy is None or adapters.http_exchange is None):
            raise ExtensionConfigurationError(
                "HTTP MCP requires explicit egress policy and HTTP exchange adapters"
            )
        if any(server.credential_ref is not None for server in http):
            if adapters.token_provider is None:
                raise ExtensionConfigurationError(
                    "credential-bearing HTTP MCP requires a server token provider"
                )

    def _enabled_mcp_servers(self) -> tuple[McpServerConfig, ...]:
        return tuple(
            server
            for servers in self._loaded.config.mcp.servers_by_product.values()
            for server in servers
            if server.enabled
        )

    async def _rollback_mcp_locked(self) -> list[BaseException]:
        failures: list[BaseException] = []
        for handle in reversed(self._mcp_handles):
            try:
                handle.close()
            except BaseException as exc:
                failures.append(exc)
        self._mcp_handles.clear()
        manager = self._mcp_manager
        self._mcp_manager = None
        if manager is not None:
            try:
                await manager.close()
            except BaseException as exc:
                failures.append(exc)
        self._mcp_material.clear()
        self._attached_bundle = None
        return failures

    async def _rollback_plugins_locked(self) -> list[BaseException]:
        manager = self._plugin_manager
        self._plugin_manager = None
        self._plugin_registry = None
        if manager is None:
            return []
        try:
            await manager.deactivate()
        except BaseException as exc:
            return [exc]
        return []

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("extension host is closed")


__all__ = [
    "ActiveCustomerBundleView",
    "ExtensionActivation",
    "ExtensionConfigurationError",
    "ExtensionStartupError",
    "McpGrantMaterial",
    "McpHostAdapters",
    "PluginHostAdapters",
    "ProductWorkspaceResolver",
    "ServerExtensionHost",
]
