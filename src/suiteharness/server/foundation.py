"""Build non-product infrastructure from validated server configuration."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import cast

from pydantic import SecretStr

from suiteharness.agents import (
    DefaultPromptStrategy,
    NoOpReflectionStrategy,
    ProductRoutedReActWorkflow,
)
from suiteharness.channels import (
    CompanyChannelAuthorizationPolicy,
    InteractiveApprovalCoordinator,
    WorkspaceWriteRule,
)
from suiteharness.config import (
    BaiduQianfanSearchConfig,
    BrowserWorkerFetchConfig,
    DirectFetchConfig,
    DockerSandboxConfig,
    FoundrySearchConfig,
    LoadedSuiteHarnessConfig,
    ManagedProxyFetchConfig,
    SandboxLimitsConfig,
    validate_secret_references,
)
from suiteharness.execution import (
    PROMPT_STRATEGY,
    REFLECTION_STRATEGY,
    WORKFLOW,
    InMemoryApprovalStore,
    InMemoryCapabilityAuthority,
    InMemoryToolRegistry,
    SQLiteAuditJournal,
)
from suiteharness.models import (
    MODEL_GATEWAY,
    BedrockConverseAdapter,
    Boto3BedrockRuntimeClient,
    GoogleAuthTokenProvider,
    HttpxTransport,
    MappingSecretResolver,
    ModelGateway,
    ModelProfile,
    ProviderAuthKind,
    ProviderDescriptor,
    VertexGeminiAdapter,
    create_builtin_provider_registry,
)
from suiteharness.models import HttpTransport as ModelHttpTransport
from suiteharness.persistence import (
    SQLiteDatabase,
    SQLiteMcpHttpResumeStore,
    SQLiteProductStateStore,
    SQLiteSandboxQuarantineStore,
)
from suiteharness.runtime import HarnessKernel, RootContext, ServiceBindings
from suiteharness.sandbox import (
    AsyncioProcessTransport,
    DockerBackendConfig,
    DockerSandboxBackend,
    LocalDevelopmentConfig,
    LocalDevelopmentSandboxBackend,
    SandboxBackend,
    SandboxLimits,
    SandboxProcessTransport,
    SandboxUnavailable,
)
from suiteharness.sessions import SQLiteSessionStore
from suiteharness.tools import (
    BashToolConfig,
    BuiltinRegistrationSet,
    BuiltinToolInstaller,
    MappingWorkspaceBindingResolver,
    ProductEgressArgumentPolicy,
    WorkspaceToolBinding,
)
from suiteharness.web import (
    BaiduQianfanSearchProvider,
    BrowserWorkerClient,
    BrowserWorkerHttpTransport,
    DirectHttpTransport,
    FetchRoute,
    FetchTransportBinding,
    FetchTransportRegistry,
    FoundryGroundingClient,
    ManagedProxyConfig,
    ManagedProxyHttpTransport,
    MicrosoftFoundryGroundingProvider,
    SearchProviderRegistry,
    StaticAccessTokenProvider,
    StructuredJsonClient,
    WebFetchService,
    WebSearchProvider,
    WebSearchService,
)
from suiteharness.workspace import (
    WorkspaceAccessPolicy,
    WorkspaceLayout,
    WorkspaceSpace,
    WritableWorkspaceRoot,
    resolve_beneath,
)

from .extensions import (
    ActiveCustomerBundleView,
    ExtensionActivation,
    McpHostAdapters,
    PluginHostAdapters,
    ServerExtensionHost,
)


def _secret_value(value: SecretStr | None, label: str) -> SecretStr:
    if value is None or not value.get_secret_value().strip():
        raise ValueError(f"{label} is missing from the secrets file")
    return value


@dataclass(frozen=True, slots=True)
class ModelRuntime:
    gateway: ModelGateway
    transport: ModelHttpTransport
    default_route: str
    product_routes: Mapping[str, str]
    _owns_transport: bool = field(repr=False)

    def __post_init__(self) -> None:
        routes = dict(self.product_routes)
        if not self.default_route.strip() or any(
            not product_id.strip() or not route_id.strip()
            for product_id, route_id in routes.items()
        ):
            raise ValueError("model route ids must not be blank")
        object.__setattr__(self, "product_routes", MappingProxyType(routes))

    def route_for(self, product_id: str) -> str:
        return self.product_routes.get(product_id, self.default_route)

    async def close(self) -> None:
        if self._owns_transport:
            close = getattr(self.transport, "close", None)
            if close is not None:
                result = close()
                if result is not None:
                    await result


@dataclass(frozen=True, slots=True)
class WebToolRuntime:
    search: WebSearchService
    fetch: WebFetchService


@dataclass(frozen=True, slots=True)
class FoundationReadiness:
    """Public, non-secret readiness snapshot for core server dependencies."""

    checks: Mapping[str, bool]

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", MappingProxyType(dict(self.checks)))

    @property
    def ready(self) -> bool:
        return bool(self.checks) and all(self.checks.values())


class FoundationStartupError(RuntimeError):
    """A required server dependency failed its startup gate."""


def _build_model_runtime(
    loaded: LoadedSuiteHarnessConfig,
    transport: ModelHttpTransport | None,
) -> ModelRuntime:
    registry = create_builtin_provider_registry()
    secret_values: dict[str, SecretStr] = {}
    profiles: dict[str, ModelProfile] = {}
    for configured in loaded.config.models.profiles:
        profile = cast(ModelProfile, configured.to_runtime())
        profiles[profile.profile_id] = profile
        descriptor = registry.descriptor(profile.provider_id)
        if descriptor is None:
            raise ValueError(f"model profile references unknown provider {profile.provider_id!r}")
        credentials = (
            None
            if profile.credential_ref is None
            else loaded.secrets.model_providers.get(profile.credential_ref)
        )
        if descriptor.auth is ProviderAuthKind.API_KEY:
            if profile.credential_ref is None or credentials is None:
                raise ValueError(f"model profile {profile.profile_id!r} requires credentials_ref")
            secret_values[profile.credential_ref] = _secret_value(
                credentials.api_key, f"API key for model profile {profile.profile_id!r}"
            )
        elif descriptor.auth is ProviderAuthKind.INTERNAL_NONE and credentials is not None:
            candidate = credentials.api_key or credentials.endpoint_credential
            if candidate is not None and profile.credential_ref is not None:
                secret_values[profile.credential_ref] = candidate

    def bedrock_provider(
        descriptor: ProviderDescriptor,
        profile: ModelProfile,
        http: ModelHttpTransport,
        credential: SecretStr | None,
    ) -> BedrockConverseAdapter:
        del http, credential
        if profile.credential_ref is None:
            raise ValueError("Bedrock profiles require configured server credentials")
        configured = loaded.secrets.model_providers.get(profile.credential_ref)
        if configured is None:
            raise ValueError("Bedrock credentials_ref was not found")
        region = profile.provider_options.get("region")
        if not isinstance(region, str) or not region:
            raise ValueError("Bedrock profile provider_options.region is required")
        client = Boto3BedrockRuntimeClient(
            region_name=region,
            endpoint_url=profile.base_url,
            aws_access_key_id=configured.access_key_id,
            aws_secret_access_key=configured.secret_access_key,
            aws_session_token=configured.session_token,
        )
        return BedrockConverseAdapter(descriptor, profile, client)

    def vertex_provider(
        descriptor: ProviderDescriptor,
        profile: ModelProfile,
        http: ModelHttpTransport,
        credential: SecretStr | None,
    ) -> VertexGeminiAdapter:
        del credential
        if profile.credential_ref is None:
            raise ValueError("Vertex profiles require configured server credentials")
        configured = loaded.secrets.model_providers.get(profile.credential_ref)
        if configured is None:
            raise ValueError("Vertex credentials_ref was not found")
        info = _secret_value(
            configured.service_account_json,
            f"service_account_json for model profile {profile.profile_id!r}",
        )
        tokens = GoogleAuthTokenProvider(service_account_info=info.get_secret_value())
        return VertexGeminiAdapter(descriptor, profile, http, tokens)

    registry.bind_factory("bedrock", bedrock_provider)
    registry.bind_factory("vertex", vertex_provider)
    selected_transport = transport or HttpxTransport()
    routes = {
        route.route_id: route.to_runtime() for route in loaded.config.models.routes
    }
    gateway = ModelGateway(
        registry,
        profiles,
        routes,
        selected_transport,
        MappingSecretResolver(secret_values),
    )
    return ModelRuntime(
        gateway=gateway,
        transport=selected_transport,
        default_route=loaded.config.models.default_route,
        product_routes=dict(loaded.config.models.product_routes),
        _owns_transport=transport is None,
    )


def _build_fetch_registry(
    loaded: LoadedSuiteHarnessConfig,
    browser_workers: Mapping[str, BrowserWorkerClient],
) -> FetchTransportRegistry:
    bindings: dict[str, FetchTransportBinding] = {}
    for route in loaded.config.web_tools.fetch.routes:
        if isinstance(route, DirectFetchConfig):
            transport = DirectHttpTransport()
            kind = FetchRoute.DIRECT
        elif isinstance(route, ManagedProxyFetchConfig):
            headers: dict[str, str] = {}
            if route.credentials_ref is not None:
                credentials = loaded.secrets.services[route.credentials_ref]
                headers = {
                    name: value.get_secret_value() for name, value in credentials.headers.items()
                }
            transport = ManagedProxyHttpTransport(
                ManagedProxyConfig(endpoint=route.endpoint, headers=headers)
            )
            kind = FetchRoute.MANAGED_PROXY
        elif isinstance(route, BrowserWorkerFetchConfig):
            try:
                client = browser_workers[route.name]
            except KeyError as exc:
                raise ValueError(f"browser worker route {route.name!r} has no client adapter") from exc
            transport = BrowserWorkerHttpTransport(client)
            kind = FetchRoute.BROWSER_WORKER
        else:  # pragma: no cover - closed discriminated union
            raise TypeError("unsupported web fetch route")
        bindings[route.name] = FetchTransportBinding(route.name, kind, transport)
    default = bindings[loaded.config.web_tools.fetch.default_route]
    registry = FetchTransportRegistry(default)
    for name, binding in bindings.items():
        if name != default.name:
            registry.register(binding)
    return registry


def _build_web_tools(
    loaded: LoadedSuiteHarnessConfig,
    *,
    browser_workers: Mapping[str, BrowserWorkerClient],
    foundry_clients: Mapping[str, FoundryGroundingClient],
    search_provider_ids: frozenset[str] | None = None,
) -> WebToolRuntime:
    transports = _build_fetch_registry(loaded, browser_workers)
    providers: list[WebSearchProvider] = []
    for configured in loaded.config.web_tools.search.providers:
        if (
            search_provider_ids is not None
            and configured.provider_id not in search_provider_ids
        ):
            continue
        if isinstance(configured, BaiduQianfanSearchConfig):
            credentials = loaded.secrets.services[configured.credentials_ref]
            token = credentials.token or credentials.api_key
            provider = BaiduQianfanSearchProvider(
                StructuredJsonClient(
                    transports,
                    egress_profile=configured.egress_profile,
                ),
                StaticAccessTokenProvider(
                    _secret_value(token, f"search token for {configured.provider_id!r}")
                ),
                endpoint=configured.endpoint,
            )
        elif isinstance(configured, FoundrySearchConfig):
            try:
                client = foundry_clients[configured.provider_id]
            except KeyError as exc:
                raise ValueError(
                    f"Foundry search provider {configured.provider_id!r} has no client adapter"
                ) from exc
            provider = MicrosoftFoundryGroundingProvider(client)
        else:  # pragma: no cover - closed discriminated union
            raise TypeError("unsupported search provider")
        providers.append(provider)
    default_id = loaded.config.web_tools.search.default_provider
    default = next(
        (item for item in providers if item.provider_id == default_id),
        None,
    )
    search_registry = SearchProviderRegistry(default)
    registered_default_id = None if default is None else default.provider_id
    for provider in providers:
        if provider.provider_id != registered_default_id:
            search_registry.register(provider)
    return WebToolRuntime(
        search=WebSearchService(search_registry),
        fetch=WebFetchService(transports),
    )


def _limits(config: SandboxLimitsConfig) -> SandboxLimits:
    return SandboxLimits(
        cpu_count=config.cpu_count,
        memory_mb=config.memory_mb,
        pids=config.pids,
        timeout_seconds=config.timeout_seconds,
        output_bytes=config.output_bytes,
        tmpfs_mb=config.tmpfs_mb,
    )


class FoundationRuntime:
    """Owned infrastructure shared by every product in one company deployment."""

    def __init__(
        self,
        loaded: LoadedSuiteHarnessConfig,
        *,
        model_transport: ModelHttpTransport | None = None,
        sandbox_transport: SandboxProcessTransport | None = None,
        browser_workers: Mapping[str, BrowserWorkerClient] | None = None,
        foundry_clients: Mapping[str, FoundryGroundingClient] | None = None,
        search_provider_ids: frozenset[str] | None = None,
        interactive_approvals: InteractiveApprovalCoordinator | None = None,
        plugin_adapters: PluginHostAdapters | None = None,
        mcp_adapters: McpHostAdapters | None = None,
    ) -> None:
        self.config = loaded.config
        validate_secret_references(
            loaded.config,
            loaded.secrets,
            search_provider_ids=search_provider_ids,
        )
        self._selected_products = frozenset(
            item.product_id for item in self.config.customer_bundle.products
        )
        self._closed = False
        self._started = False
        self._close_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self.workspaces = WorkspaceLayout(self.config.workspace.root)
        self.config.workspace.root.mkdir(parents=True, exist_ok=True)
        self.config.storage.root.mkdir(parents=True, exist_ok=True)
        self.runtime_database = SQLiteDatabase(self.config.storage.runtime_path())
        self.product_state = SQLiteProductStateStore(self.runtime_database)
        self.mcp_resume = SQLiteMcpHttpResumeStore(self.product_state)
        self.sandbox_quarantine = SQLiteSandboxQuarantineStore(
            self.runtime_database,
            deployment_id=self.config.deployment.instance_id,
        )
        self.workspace_bindings = MappingWorkspaceBindingResolver()
        process_transport = sandbox_transport or AsyncioProcessTransport()
        sandbox_config = self.config.sandbox
        if isinstance(sandbox_config, DockerSandboxConfig):
            self.sandbox: SandboxBackend = DockerSandboxBackend(
                DockerBackendConfig(
                    image=sandbox_config.image,
                    allowed_host_roots=(self.config.workspace.root,),
                    binary=sandbox_config.binary,
                    context=sandbox_config.context,
                    require_rootless=sandbox_config.require_rootless,
                    production=self.config.deployment.environment == "production",
                    egress_networks=sandbox_config.network.egress_profiles,
                ),
                process_transport,
                quarantine_store=self.sandbox_quarantine,
            )
        else:
            self.sandbox = LocalDevelopmentSandboxBackend(
                LocalDevelopmentConfig(
                    environment=self.config.deployment.environment,
                    acknowledge_unsafe=sandbox_config.acknowledge_unsafe,
                ),
                process_transport,
            )
        self.models = _build_model_runtime(loaded, model_transport)
        self.web_tools = _build_web_tools(
            loaded,
            browser_workers=browser_workers or {},
            foundry_clients=foundry_clients or {},
            search_provider_ids=search_provider_ids,
        )
        self.sessions = SQLiteSessionStore(
            self.config.storage.sessions_path(),
            limits=self.config.storage.session_limits,
        )
        self.audit = SQLiteAuditJournal(self.config.storage.audit_path())
        self.tools = InMemoryToolRegistry()
        rules = tuple(
            WorkspaceWriteRule(root.space, root.path)
            for root in self.config.channels.feishu.writable_roots
        )
        self.capabilities = InMemoryCapabilityAuthority()
        self.approvals = InMemoryApprovalStore()
        root = RootContext(
            bindings=ServiceBindings.root(
                {
                    MODEL_GATEWAY: self.models.gateway,
                    WORKFLOW: ProductRoutedReActWorkflow(
                        gateway=self.models.gateway,
                        routes=self.models,
                    ),
                    PROMPT_STRATEGY: DefaultPromptStrategy(),
                    REFLECTION_STRATEGY: NoOpReflectionStrategy(),
                }
            )
        )
        self.kernel = HarnessKernel(
            root=root,
            tools=self.tools,
            capabilities=self.capabilities,
            approvals=self.approvals,
            journal=self.audit,
            tool_authorization_policy=CompanyChannelAuthorizationPolicy(
                feishu_writable_roots=rules
            ),
            interactive_approvals=interactive_approvals,
        )
        limits = _limits(sandbox_config.limits)
        self.egress_policy = ProductEgressArgumentPolicy(
            default_search_provider=self.config.web_tools.search.default_provider,
            default_fetch_route=self.config.web_tools.fetch.default_route,
            bash_profiles_by_product=sandbox_config.network.allowed_profiles_by_product,
            search_providers_by_product=self.config.web_tools.search_providers_by_product,
            fetch_routes_by_product=self.config.web_tools.fetch_routes_by_product,
        )
        builtin_installer = BuiltinToolInstaller(
            self.tools,
            workspaces=self.workspace_bindings,
            sandbox=self.sandbox,
            web_search=self.web_tools.search,
            web_fetch=self.web_tools.fetch,
            bash_config=BashToolConfig(
                production=self.config.deployment.environment == "production",
                limits=limits,
                allowed_egress_profiles=frozenset(sandbox_config.network.egress_profiles),
            ),
            egress_policy=self.egress_policy,
        )
        self.workspace_files_production_safe = (
            builtin_installer.workspace_files_production_safe
        )
        self.builtins: BuiltinRegistrationSet = builtin_installer.install()
        self._prepared_products: set[str] = set()
        self.extensions = ServerExtensionHost(
            loaded,
            sandbox=self.sandbox,
            tools=self.tools,
            product_workspace=self.prepare_product_workspace,
            plugin_adapters=plugin_adapters,
            mcp_adapters=mcp_adapters,
            mcp_resume_store=self.mcp_resume,
        )

    @classmethod
    async def create(
        cls,
        loaded: LoadedSuiteHarnessConfig,
        *,
        model_transport: ModelHttpTransport | None = None,
        sandbox_transport: SandboxProcessTransport | None = None,
        browser_workers: Mapping[str, BrowserWorkerClient] | None = None,
        foundry_clients: Mapping[str, FoundryGroundingClient] | None = None,
        search_provider_ids: frozenset[str] | None = None,
        interactive_approvals: InteractiveApprovalCoordinator | None = None,
        plugin_adapters: PluginHostAdapters | None = None,
        mcp_adapters: McpHostAdapters | None = None,
    ) -> FoundationRuntime:
        """Construct and probe the foundation, rolling back partial ownership."""

        runtime = cls.__new__(cls)
        try:
            cls.__init__(
                runtime,
                loaded,
                model_transport=model_transport,
                sandbox_transport=sandbox_transport,
                browser_workers=browser_workers,
                foundry_clients=foundry_clients,
                search_provider_ids=search_provider_ids,
                interactive_approvals=interactive_approvals,
                plugin_adapters=plugin_adapters,
                mcp_adapters=mcp_adapters,
            )
            await runtime.start()
            return runtime
        except BaseException as cause:
            cleanup_failures = await runtime._close_initialized_components()
            if cleanup_failures:
                raise BaseExceptionGroup(
                    "foundation startup and rollback failed",
                    [cause, *cleanup_failures],
                ) from cause
            raise

    @property
    def started(self) -> bool:
        return self._started and not self._closed

    async def start(self) -> None:
        """Probe every mandatory local dependency before traffic is accepted."""

        async with self._start_lock:
            if self._closed:
                raise RuntimeError("foundation runtime is closed")
            if self._started:
                return
            if (
                self.config.deployment.environment == "production"
                and not self.workspace_files_production_safe
            ):
                raise FoundationStartupError(
                    "production requires the Linux descriptor-anchored secure workspace backend"
                )
            try:
                sandbox, _runtime, _sessions, _audit = await asyncio.gather(
                    self.sandbox.availability(),
                    self.runtime_database.healthcheck(),
                    self.sessions.healthcheck(),
                    self.audit.healthcheck(),
                )
            except Exception as exc:
                raise FoundationStartupError(
                    "a required persistence dependency failed its startup check"
                ) from exc
            if not sandbox.available:
                raise SandboxUnavailable(
                    sandbox.detail or "required sandbox backend is unavailable"
                )
            self._started = True

    async def readiness(self) -> FoundationReadiness:
        """Re-check dependencies used to decide whether new traffic is safe."""

        if self._closed:
            return FoundationReadiness(
                {
                    "started": False,
                    "sandbox": False,
                    "runtime_database": False,
                    "sessions": False,
                    "audit": False,
                    "plugins": False,
                    "mcp": False,
                }
            )
        checks: dict[str, bool] = {"started": self._started}
        results = await asyncio.gather(
            self.sandbox.availability(),
            self.runtime_database.healthcheck(),
            self.sessions.healthcheck(),
            self.audit.healthcheck(),
            return_exceptions=True,
        )
        sandbox, runtime_database, sessions, audit = results
        checks["sandbox"] = not isinstance(sandbox, BaseException) and sandbox.available
        checks["runtime_database"] = not isinstance(runtime_database, BaseException)
        checks["sessions"] = not isinstance(sessions, BaseException)
        checks["audit"] = not isinstance(audit, BaseException)
        checks.update(self.extensions.readiness_checks())
        return FoundationReadiness(checks)

    def prepare_product_workspace(self, product_id: str) -> Path:
        """Create and bind channel-specific access for one selected product."""

        if self._closed:
            raise RuntimeError("foundation runtime is closed")
        if product_id not in self._selected_products:
            raise ValueError("product workspace is outside the configured customer bundle")
        if product_id in self._prepared_products:
            return self.workspaces.paths_for(
                self.config.deployment.tenant_id, product_id
            ).product_root
        tenant_id = self.config.deployment.tenant_id
        paths = self.workspaces.prepare_product(tenant_id, product_id)
        shared_access = self.config.workspace.shared_access_by_product.get(product_id)
        readable = [paths.product_root]
        writable = [paths.product_root]
        if shared_access is not None:
            readable.append(paths.shared_root)
        if shared_access == "read_write":
            writable.append(paths.shared_root)
        web_policy = WorkspaceAccessPolicy(
            readable_roots=readable,
            writable_roots=writable,
            allow_delete=True,
        )
        self.workspace_bindings.register(
            tenant_id,
            product_id,
            WorkspaceToolBinding(paths, web_policy),
            channel_id="web",
        )
        self.workspace_bindings.register(
            tenant_id,
            product_id,
            WorkspaceToolBinding(paths, web_policy),
            channel_id="internal",
        )
        if self.config.channels.feishu.enabled:
            writable_items: list[WritableWorkspaceRoot] = []
            for root in self.config.channels.feishu.writable_roots:
                if root.space == "shared" and shared_access != "read_write":
                    continue
                item = WritableWorkspaceRoot(WorkspaceSpace(root.space), root.path)
                selected = (
                    paths.product_root
                    if item.space is WorkspaceSpace.PRODUCT
                    else paths.shared_root
                )
                directory = resolve_beneath(selected, item.relative_path)
                directory.mkdir(parents=True, exist_ok=True)
                writable_items.append(item)
            writable = tuple(writable_items)
            feishu_policy = WorkspaceAccessPolicy.for_feishu(
                paths,
                writable,
                include_shared_read=shared_access is not None,
            )
            self.workspace_bindings.register(
                tenant_id,
                product_id,
                WorkspaceToolBinding(paths, feishu_policy),
                channel_id="feishu",
            )
        self._prepared_products.add(product_id)
        return paths.product_root

    async def start_extensions(
        self, activation: ActiveCustomerBundleView
    ) -> ExtensionActivation:
        """Activate plugins and attach configured MCP servers transactionally."""

        if self._closed:
            raise RuntimeError("foundation runtime is closed")
        await self.start()
        return await self.extensions.start(activation)

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._started = False
            errors: list[BaseException] = []
            # Stop extension traffic and product runs before removing root tools
            # or closing the durable/model services those handlers depend on.
            for close in (self.extensions.close, self.kernel.close):
                try:
                    await close()
                except BaseException as exc:
                    errors.append(exc)
            try:
                self.builtins.close()
            except BaseException as exc:
                errors.append(exc)
            for close in (self.sessions.close, self.audit.close, self.models.close):
                try:
                    await close()
                except BaseException as exc:
                    errors.append(exc)
            try:
                await self.runtime_database.close()
            except BaseException as exc:
                errors.append(exc)
            if errors:
                raise BaseExceptionGroup("foundation shutdown reported failures", errors)

    async def _close_initialized_components(self) -> list[BaseException]:
        """Best-effort rollback used when construction did not reach a full object."""

        self._closed = True
        self._started = False
        errors: list[BaseException] = []
        for name in ("extensions", "kernel"):
            component = getattr(self, name, None)
            close = getattr(component, "close", None)
            if close is None:
                continue
            try:
                await close()
            except BaseException as exc:
                errors.append(exc)
        builtins = getattr(self, "builtins", None)
        if builtins is not None:
            try:
                builtins.close()
            except BaseException as exc:
                errors.append(exc)
        for name in ("sessions", "audit", "models", "runtime_database"):
            component = getattr(self, name, None)
            close = getattr(component, "close", None)
            if close is None:
                continue
            try:
                await close()
            except BaseException as exc:
                errors.append(exc)
        return errors


__all__ = [
    "FoundationReadiness",
    "FoundationRuntime",
    "FoundationStartupError",
    "ModelRuntime",
    "WebToolRuntime",
]
