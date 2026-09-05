"""Compose an activated customer bundle with company Web and Feishu channels."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from suiteharness.channels import (
    AuthenticatedPrincipal,
    ChannelKind,
    ChannelRoute,
    CompanyIdentityDirectory,
    ConversationAuthorizer,
    EnterpriseChannelGateway,
    FeishuCompanyAuthenticator,
    InboundMessage,
    InteractiveApprovalCoordinator,
    OutboundEvent,
    ProductAccessAuthorizer,
    ProductRouter,
)
from suiteharness.channels.feishu import (
    FeishuEventProcessor,
    FeishuHeaderSignatureVerifier,
    FeishuLongConnectionRunner,
    FeishuLongConnectionSdk,
    FeishuWebhookHandler,
    OutboundSink,
    RawEventDecryptor,
    create_starlette_feishu_route,
)
from suiteharness.channels.websocket import (
    EnterpriseWebSocketServer,
    HmacSessionTokenCodec,
    OriginPolicy,
    ServerSessionWebSocketAuthenticator,
    WebSocketApprovalHub,
    create_starlette_websocket_route,
)
from suiteharness.config import LoadedSuiteHarnessConfig
from suiteharness.execution import ToolRegistry
from suiteharness.persistence import (
    ChannelEventScope,
    SQLiteChannelEventDeduplicator,
    SQLiteDatabase,
)
from suiteharness.runtime import ProductContext, ScopePath
from suiteharness.sessions import SessionStore

from .access import (
    AdditionalGrantMaterial,
    ConfiguredGrantIssuer,
    GrantAuthority,
    McpChannelAccessRule,
    ToolAccessTemplate,
)
from .application import (
    ChannelApplication,
    CustomerBundleRunner,
    RunInputBuilder,
    default_run_input,
)
from .extensions import McpGrantMaterial

StarletteRouteFactory = Callable[[str, Any], Any]


class ActivatedCustomerBundleView(CustomerBundleRunner, Protocol):
    """Activated bundle information required for fail-closed host validation."""

    @property
    def customer_bundle_id(self) -> str: ...

    @property
    def products(self) -> Mapping[str, ProductContext]: ...

    @property
    def closed(self) -> bool: ...


class ChannelHostConfigurationError(ValueError):
    """Safe configuration failure before any channel begins accepting traffic."""


class _RejectingAuthenticator:
    async def authenticate(self, _message: InboundMessage) -> AuthenticatedPrincipal | None:
        return None


class _HostedEnterpriseChannelGateway(EnterpriseChannelGateway):
    """Reject new channel traffic as soon as its owning host begins shutdown."""

    def __init__(self, *args: Any, host_closed: Callable[[], bool], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._host_closed = host_closed

    def _require_open(self) -> None:
        if self._host_closed():
            raise RuntimeError("company channel runtime is closed")

    async def dispatch(self, message: InboundMessage) -> AsyncIterator[OutboundEvent]:
        self._require_open()
        async for event in super().dispatch(message):
            yield event

    async def dispatch_authenticated(
        self,
        message: InboundMessage,
        principal: AuthenticatedPrincipal,
    ) -> AsyncIterator[OutboundEvent]:
        self._require_open()
        async for event in super().dispatch_authenticated(message, principal):
            yield event


class WebApprovalRuntime:
    """Approval hub/coordinator pair created before the execution foundation.

    Pass ``coordinator`` to ``FoundationRuntime(interactive_approvals=...)`` and
    then pass this same object to :meth:`CompanyChannelRuntime.create`.  This
    ensures the runner publishes approval challenges to the WebSocket hub that
    receives decisions from the authenticated company frontend.
    """

    def __init__(self, *, timeout_seconds: float = 60.0) -> None:
        self.hub = WebSocketApprovalHub()
        self.coordinator = InteractiveApprovalCoordinator(
            self.hub.publish,
            timeout_seconds=timeout_seconds,
        )
        self._closed = False
        self._close_lock = asyncio.Lock()

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            await self.coordinator.close()


@dataclass(frozen=True, slots=True)
class FeishuHostAdapters:
    """Company-owned Feishu integrations that SuiteHarness must not infer.

    ``directory`` maps provider identities to company principals. ``outbound_sink``
    normally wraps ``FeishuMessageClient``.  The official long-connection SDK is
    required only for that transport; webhook decryption is optional for signed
    plaintext callbacks and mandatory when the deployment enables encryption.
    """

    directory: CompanyIdentityDirectory
    outbound_sink: OutboundSink
    bot_open_id: str | None = None
    webhook_decryptor: RawEventDecryptor | None = None
    long_connection_sdk: FeishuLongConnectionSdk | None = None

    def __post_init__(self) -> None:
        if not callable(getattr(self.directory, "resolve_feishu_identity", None)):
            raise TypeError("Feishu directory must implement resolve_feishu_identity")
        if not callable(self.outbound_sink):
            raise TypeError("Feishu outbound_sink must be callable")
        if self.webhook_decryptor is not None and not callable(
            getattr(self.webhook_decryptor, "decrypt", None)
        ):
            raise TypeError("Feishu webhook_decryptor must implement decrypt")
        if self.long_connection_sdk is not None and any(
            not callable(getattr(self.long_connection_sdk, name, None))
            for name in ("run", "close")
        ):
            raise TypeError("Feishu long_connection_sdk must implement run and close")


class CompanyChannelRuntime:
    """Owned company-channel adapters for one already activated customer bundle.

    The activation, session store, tool registry, and grant authority are
    borrowed from the server foundation.  This runtime owns its Web approval
    coordinator and long-connection SDK lifecycle.  It owns the runtime SQLite
    database only when one was not injected; an injected database is shared and
    remains caller-owned. Construction is asynchronous so a partially opened
    database can be closed before a fail-closed startup error is returned.
    """

    def __init__(self) -> None:
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._web_route_factory: StarletteRouteFactory = create_starlette_websocket_route
        self._feishu_route_factory: StarletteRouteFactory = create_starlette_feishu_route

    @classmethod
    async def create(
        cls,
        loaded: LoadedSuiteHarnessConfig,
        *,
        activation: ActivatedCustomerBundleView,
        sessions: SessionStore,
        tools: ToolRegistry,
        capabilities: GrantAuthority,
        grant_templates: tuple[ToolAccessTemplate, ...],
        web_approvals: WebApprovalRuntime | None = None,
        web_conversation_authorizer: ConversationAuthorizer | None = None,
        product_access_authorizer: ProductAccessAuthorizer | None = None,
        feishu_adapters: FeishuHostAdapters | None = None,
        mcp_grant_material: Mapping[str, McpGrantMaterial] | None = None,
        runtime_database: SQLiteDatabase | None = None,
        input_builder: RunInputBuilder = default_run_input,
        web_route_factory: StarletteRouteFactory = create_starlette_websocket_route,
        feishu_route_factory: StarletteRouteFactory = create_starlette_feishu_route,
    ) -> CompanyChannelRuntime:
        if not isinstance(loaded, LoadedSuiteHarnessConfig):
            raise TypeError("loaded must be LoadedSuiteHarnessConfig")
        if not callable(web_route_factory) or not callable(feishu_route_factory):
            raise TypeError("Starlette route factories must be callable")
        runtime = cls()
        runtime._web_route_factory = web_route_factory
        runtime._feishu_route_factory = feishu_route_factory
        database: SQLiteDatabase | None = runtime_database
        owns_database = False
        try:
            runtime._build_common(
                loaded,
                activation=activation,
                sessions=sessions,
                tools=tools,
                capabilities=capabilities,
                grant_templates=grant_templates,
                mcp_grant_material=mcp_grant_material or {},
                input_builder=input_builder,
                product_access_authorizer=product_access_authorizer,
            )
            runtime._build_web(
                loaded,
                web_approvals,
                web_conversation_authorizer,
            )
            if loaded.config.channels.feishu.enabled:
                if database is None:
                    database = SQLiteDatabase(loaded.config.storage.runtime_path())
                    owns_database = True
                runtime._build_feishu(loaded, database, feishu_adapters)
            else:
                runtime.feishu_gateway = None
                runtime.feishu_deduplicator = None
                runtime.feishu_processor = None
                runtime.feishu_webhook = None
                runtime.feishu_long_connection = None
            runtime._database = database
            runtime.runtime_database = database
            runtime._owns_database = owns_database
            return runtime
        except BaseException as cause:
            cleanup_failures: list[BaseException] = []
            if database is not None and owns_database:
                try:
                    await database.close()
                except BaseException as exc:
                    cleanup_failures.append(exc)
            if web_approvals is not None:
                try:
                    await web_approvals.close()
                except BaseException as exc:
                    cleanup_failures.append(exc)
            if cleanup_failures:
                raise BaseExceptionGroup(
                    "company channel construction and rollback failed",
                    [cause, *cleanup_failures],
                ) from cause
            raise

    def _build_common(
        self,
        loaded: LoadedSuiteHarnessConfig,
        *,
        activation: ActivatedCustomerBundleView,
        sessions: SessionStore,
        tools: ToolRegistry,
        capabilities: GrantAuthority,
        grant_templates: tuple[ToolAccessTemplate, ...],
        mcp_grant_material: Mapping[str, McpGrantMaterial],
        input_builder: RunInputBuilder,
        product_access_authorizer: ProductAccessAuthorizer | None,
    ) -> None:
        config = loaded.config
        required_session_methods = (
            "append",
            "claim_run",
            "create",
            "finalize_run",
            "get",
            "recent_transcript",
            "resume",
            "save_checkpoint",
            "set_status",
        )
        if any(not callable(getattr(sessions, name, None)) for name in required_session_methods):
            raise TypeError("sessions does not implement the SessionStore contract")
        if not callable(getattr(tools, "resolve", None)):
            raise TypeError("tools does not implement the ToolRegistry contract")
        if any(
            not callable(getattr(capabilities, name, None))
            for name in ("issue", "revoke")
        ):
            raise TypeError("capabilities does not implement the grant authority contract")
        if not callable(getattr(activation, "run", None)):
            raise TypeError("activation does not implement the customer bundle run contract")
        if product_access_authorizer is not None and not callable(
            getattr(product_access_authorizer, "authorize", None)
        ):
            raise TypeError(
                "product_access_authorizer must implement async authorize"
            )
        self._validate_activation(loaded, activation)
        product_ids = tuple(item.product_id for item in config.customer_bundle.products)
        enabled_channels: set[str] = set()
        if config.channels.web.enabled:
            enabled_channels.add(ChannelKind.WEB.value)
        if config.channels.feishu.enabled:
            enabled_channels.add(ChannelKind.FEISHU.value)
        expected_templates = {
            (channel, product_id)
            for channel in enabled_channels
            for product_id in product_ids
        }
        actual_templates = {
            (template.channel_id, template.product_id) for template in grant_templates
        }
        if actual_templates != expected_templates or len(actual_templates) != len(grant_templates):
            missing = sorted(expected_templates - actual_templates)
            unexpected = sorted(actual_templates - expected_templates)
            raise ChannelHostConfigurationError(
                "grant templates must exactly cover enabled channel/product routes; "
                f"missing={missing!r}, unexpected={unexpected!r}"
            )
        feishu_template_writes = any(
            template.channel_id == ChannelKind.FEISHU.value and template.write_aliases
            for template in grant_templates
        )
        if feishu_template_writes and not config.channels.feishu.writable_roots:
            raise ChannelHostConfigurationError(
                "Feishu write/edit grants require at least one configured writable_root"
            )

        unknown_material = set(mcp_grant_material) - set(product_ids)
        if unknown_material:
            raise ChannelHostConfigurationError(
                f"MCP grant material references inactive products: {sorted(unknown_material)!r}"
            )
        material: dict[str, AdditionalGrantMaterial] = {}
        for product_id, item in mcp_grant_material.items():
            if not isinstance(item, McpGrantMaterial):
                raise TypeError("mcp_grant_material must contain McpGrantMaterial values")
            expected_origin = (
                f"scope:product:{config.deployment.tenant_id}:{product_id}"
            )
            if any(
                identity.namespace != "product" or identity.origin != expected_origin
                for identity in item.tool_identities
            ):
                raise ChannelHostConfigurationError(
                    "MCP grant material must contain exact identities from its product scope"
                )
            configured_servers = {
                server.server_id
                for server in config.mcp.servers_by_product.get(product_id, ())
                if server.enabled
            }
            if unknown_servers := {
                tool.server_id for tool in item.tools
            } - configured_servers:
                raise ChannelHostConfigurationError(
                    "MCP grant material references unconfigured servers: "
                    f"{sorted(unknown_servers)!r}"
                )
            material[product_id] = AdditionalGrantMaterial(
                tools=item.tools,
            )

        mcp_access: dict[tuple[str, str], McpChannelAccessRule] = {}
        for channel_id, product_rules in (
            (ChannelKind.WEB.value, config.mcp.channel_access.web),
            (ChannelKind.FEISHU.value, config.mcp.channel_access.feishu),
        ):
            for product_id, configured in product_rules.items():
                rule = McpChannelAccessRule(
                    server_ids=configured.allow_servers,
                    tools_by_server=configured.allow_tools,
                )
                if rule.server_ids or rule.tools_by_server:
                    product_material = material.get(product_id)
                    if product_material is None:
                        raise ChannelHostConfigurationError(
                            "configured MCP channel access has no connected product inventory"
                        )
                    discovered = {
                        (tool.server_id, tool.remote_name)
                        for tool in product_material.tools
                    }
                    configured_tools = {
                        (server_id, remote_name)
                        for server_id, names in rule.tools_by_server.items()
                        for remote_name in names
                    }
                    if missing := configured_tools - discovered:
                        raise ChannelHostConfigurationError(
                            "configured MCP channel tools were not discovered: "
                            f"{sorted(missing)!r}"
                        )
                mcp_access[(channel_id, product_id)] = rule

        routes = tuple(
            ChannelRoute(
                channel=ChannelKind(item.channel),
                conversation_id=item.conversation_id,
                product_id=item.product_id,
            )
            for item in config.channels.routes
        )
        self.router = ProductRouter(product_ids, routes)
        self.grants = ConfiguredGrantIssuer(
            registry=tools,
            authority=capabilities,
            templates=grant_templates,
            additional_material_by_product=material,
            mcp_access_by_route=mcp_access,
        )
        self.application = ChannelApplication(
            activation=activation,
            sessions=sessions,
            grants=self.grants,
            input_builder=input_builder,
        )
        self._loaded = loaded
        self._activation = activation
        self._web_approvals: WebApprovalRuntime | None = None
        self._product_access_authorizer = product_access_authorizer

    def _build_web(
        self,
        loaded: LoadedSuiteHarnessConfig,
        approvals: WebApprovalRuntime | None,
        conversation_authorizer: ConversationAuthorizer | None,
    ) -> None:
        config = loaded.config
        if not config.channels.web.enabled:
            if approvals is not None:
                raise ChannelHostConfigurationError(
                    "web_approvals must be omitted when the Web channel is disabled"
                )
            if conversation_authorizer is not None:
                raise ChannelHostConfigurationError(
                    "web_conversation_authorizer must be omitted when the Web channel is disabled"
                )
            self.web_session_tokens = None
            self.web_gateway = None
            self.web_server = None
            return
        if not isinstance(approvals, WebApprovalRuntime):
            raise ChannelHostConfigurationError(
                "enabled Web channel requires a pre-wired WebApprovalRuntime"
            )
        if approvals.closed:
            raise ChannelHostConfigurationError("WebApprovalRuntime is already closed")
        if conversation_authorizer is not None and not callable(
            getattr(conversation_authorizer, "authorize", None)
        ):
            raise TypeError(
                "web_conversation_authorizer must implement async authorize"
            )
        if (
            config.channels.share_conversation_sessions
            and conversation_authorizer is None
        ):
            raise ChannelHostConfigurationError(
                "shared Web conversations require a trusted conversation authorizer"
            )
        credentials = loaded.secrets.web.get(config.channels.web.credentials_ref)
        if credentials is None:
            raise ChannelHostConfigurationError("configured Web credentials are unavailable")
        if not config.channels.web.allowed_origins:
            raise ChannelHostConfigurationError(
                "enabled Web channel requires an explicit origin allowlist"
            )

        tenant_id = config.deployment.tenant_id
        codec = HmacSessionTokenCodec(
            credentials.session_signing_key.get_secret_value().encode("utf-8"),
            issuer=f"suiteharness-server:{config.deployment.instance_id}",
            audience="suiteharness-company-websocket",
            tenant_id=tenant_id,
        )
        authenticator = ServerSessionWebSocketAuthenticator(codec)
        gateway = _HostedEnterpriseChannelGateway(
            tenant_id=tenant_id,
            authenticator=_RejectingAuthenticator(),
            router=self.router,
            application=self.application,
            agent_ids=config.channels.agent_ids,
            share_conversation_sessions=config.channels.share_conversation_sessions,
            conversation_authorizer=conversation_authorizer,
            product_access_authorizer=self._product_access_authorizer,
            authentication_timeout_seconds=config.channels.web.authentication_timeout_seconds,
            authorization_timeout_seconds=config.channels.authorization_timeout_seconds,
            host_closed=lambda: self._closed,
        )
        self.web_session_tokens = codec
        self.web_gateway = gateway
        self.web_server = EnterpriseWebSocketServer(
            tenant_id=tenant_id,
            authenticator=authenticator,
            origin_policy=OriginPolicy(config.channels.web.allowed_origins),
            dispatcher=gateway,
            approvals=approvals.coordinator,
            approval_hub=approvals.hub,
            max_frame_bytes=config.channels.web.max_frame_bytes,
        )
        self._web_approvals = approvals

    def _build_feishu(
        self,
        loaded: LoadedSuiteHarnessConfig,
        database: SQLiteDatabase,
        adapters: FeishuHostAdapters | None,
    ) -> None:
        config = loaded.config
        if not isinstance(adapters, FeishuHostAdapters):
            raise ChannelHostConfigurationError(
                "enabled Feishu channel requires explicit company host adapters"
            )
        credentials = loaded.secrets.feishu.get(config.channels.feishu.credentials_ref)
        if credentials is None:
            raise ChannelHostConfigurationError("configured Feishu credentials are unavailable")
        tenant_id = config.deployment.tenant_id
        authenticator = FeishuCompanyAuthenticator(
            tenant_id=tenant_id,
            directory=adapters.directory,
        )
        gateway = _HostedEnterpriseChannelGateway(
            tenant_id=tenant_id,
            authenticator=authenticator,
            router=self.router,
            application=self.application,
            agent_ids=config.channels.agent_ids,
            share_conversation_sessions=config.channels.share_conversation_sessions,
            product_access_authorizer=self._product_access_authorizer,
            authentication_timeout_seconds=(
                config.channels.feishu.authentication_timeout_seconds
            ),
            authorization_timeout_seconds=config.channels.authorization_timeout_seconds,
            host_closed=lambda: self._closed,
        )
        deduplicator = SQLiteChannelEventDeduplicator(
            database,
            scope=ChannelEventScope.deployment(
                config.deployment.instance_id,
                ChannelKind.FEISHU.value,
            ),
        )
        processor = FeishuEventProcessor(
            gateway,
            bot_open_id=adapters.bot_open_id,
            deduplicator=deduplicator,
            outbound_sink=adapters.outbound_sink,
        )
        self.feishu_gateway = gateway
        self.feishu_deduplicator = deduplicator
        self.feishu_processor = processor
        self.feishu_webhook = None
        self.feishu_long_connection = None

        if config.channels.feishu.transport == "webhook":
            if credentials.verification_token is None or credentials.encrypt_key is None:
                raise ChannelHostConfigurationError(
                    "Feishu webhook credentials require verification_token and encrypt_key"
                )
            self.feishu_webhook = FeishuWebhookHandler(
                processor,
                verifier=FeishuHeaderSignatureVerifier(
                    credentials.encrypt_key.get_secret_value()
                ),
                verification_token=credentials.verification_token.get_secret_value(),
                decryptor=adapters.webhook_decryptor,
            )
            if adapters.long_connection_sdk is not None:
                raise ChannelHostConfigurationError(
                    "long_connection_sdk must be omitted for Feishu webhook transport"
                )
        else:
            if adapters.webhook_decryptor is not None:
                raise ChannelHostConfigurationError(
                    "webhook_decryptor must be omitted for Feishu long_connection transport"
                )
            if adapters.long_connection_sdk is None:
                raise ChannelHostConfigurationError(
                    "Feishu long_connection transport requires the official SDK adapter"
                )
            self.feishu_long_connection = FeishuLongConnectionRunner(
                adapters.long_connection_sdk,
                processor,
            )

    def starlette_routes(self) -> tuple[Any, ...]:
        """Return only routes enabled by configuration.

        Starlette remains an optional host dependency. Calling this method with
        a real enabled HTTP route raises the adapter's explicit diagnostic when
        Starlette is not installed.
        """

        if self._closed:
            raise RuntimeError("company channel runtime is closed")
        routes: list[Any] = []
        if self.web_server is not None:
            routes.append(
                self._web_route_factory(
                    self._loaded.config.channels.web.websocket_path,
                    self.web_server,
                )
            )
        if self.feishu_webhook is not None:
            routes.append(
                self._feishu_route_factory(
                    self._loaded.config.channels.feishu.webhook_path,
                    self.feishu_webhook,
                )
            )
        return tuple(routes)

    @property
    def closed(self) -> bool:
        return self._closed

    async def run_feishu_long_connection(self) -> None:
        """Run the official-SDK adapter when long-connection transport is selected."""

        if self._closed:
            raise RuntimeError("company channel runtime is closed")
        if self.feishu_long_connection is None:
            raise ChannelHostConfigurationError(
                "Feishu long_connection transport is not enabled"
            )
        await self.feishu_long_connection.run()

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            failures: list[BaseException] = []
            if self.feishu_long_connection is not None:
                try:
                    await self.feishu_long_connection.close()
                except BaseException as exc:
                    failures.append(exc)
            if self._web_approvals is not None:
                try:
                    await self._web_approvals.close()
                except BaseException as exc:
                    failures.append(exc)
            if self._database is not None and self._owns_database:
                try:
                    await self._database.close()
                except BaseException as exc:
                    failures.append(exc)
            if failures:
                raise BaseExceptionGroup("company channel shutdown reported failures", failures)

    @staticmethod
    def _validate_activation(
        loaded: LoadedSuiteHarnessConfig,
        activation: ActivatedCustomerBundleView,
    ) -> None:
        config = loaded.config
        if activation.closed:
            raise ChannelHostConfigurationError("cannot host a closed customer activation")
        if activation.tenant_id != config.deployment.tenant_id:
            raise ChannelHostConfigurationError(
                "customer activation tenant does not match deployment"
            )
        if activation.customer_bundle_id != config.customer_bundle.customer_bundle_id:
            raise ChannelHostConfigurationError(
                "customer activation bundle does not match configuration"
            )
        selected = {item.product_id for item in config.customer_bundle.products}
        if set(activation.products) != selected:
            raise ChannelHostConfigurationError(
                "active products do not exactly match the configured customer bundle"
            )
        for product_id, product in activation.products.items():
            if product.path != ScopePath.product(config.deployment.tenant_id, product_id):
                raise ChannelHostConfigurationError(
                    "active product context does not match its configured scope"
                )


__all__ = [
    "ActivatedCustomerBundleView",
    "ChannelHostConfigurationError",
    "CompanyChannelRuntime",
    "FeishuHostAdapters",
    "StarletteRouteFactory",
    "WebApprovalRuntime",
]
