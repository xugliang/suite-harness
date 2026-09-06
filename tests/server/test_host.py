from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from suiteharness.channels import (
    AuthenticatedPrincipal,
    ChannelAdmissionError,
    ChannelKind,
    CompanyIdentity,
    InboundMessage,
    OutboundEventKind,
)
from suiteharness.channels.feishu import FeishuEventOutcome
from suiteharness.config import LoadedSuiteHarnessConfig, SuiteHarnessConfig, SuiteHarnessSecrets
from suiteharness.execution import (
    InMemoryCapabilityAuthority,
    InMemoryToolRegistry,
    RunResult,
    RunStatus,
    ToolEffect,
    ToolIdentity,
    ToolSpec,
)
from suiteharness.persistence import SQLiteDatabase
from suiteharness.runtime import RootContext, ScopePath
from suiteharness.server import (
    ChannelHostConfigurationError,
    CompanyChannelRuntime,
    FeishuHostAdapters,
    McpGrantMaterial,
    ToolAccessTemplate,
    WebApprovalRuntime,
)
from suiteharness.server.access import AdditionalGrantTool
from suiteharness.sessions import InMemorySessionStore


def _loaded(
    tmp_path: Path,
    *,
    web: bool,
    feishu: bool,
    feishu_transport: str = "long_connection",
    feishu_processing_timeout_seconds: float = 600.0,
    feishu_processing_lease_seconds: float = 660.0,
    products: tuple[str, ...] = ("sales",),
    routes: tuple[dict[str, str], ...] = (),
    writable: bool = False,
    share_sessions: bool = False,
    mcp: dict[str, object] | None = None,
) -> LoadedSuiteHarnessConfig:
    channels: dict[str, object] = {
        "web": {
            "enabled": web,
            "credentials_ref": "web/default",
            "allowed_origins": ["https://portal.example.cn"] if web else [],
        },
        "feishu": {
            "enabled": feishu,
            "app_id": "cli-company" if feishu else None,
            "credentials_ref": "feishu/default",
            "transport": feishu_transport,
            "event_processing_timeout_seconds": feishu_processing_timeout_seconds,
            "event_processing_lease_seconds": feishu_processing_lease_seconds,
            "writable_roots": (
                [{"space": "product", "path": "exports"}] if writable else []
            ),
        },
        "routes": list(routes),
        "agent_ids": {product: f"{product}-agent" for product in products},
        "share_conversation_sessions": share_sessions,
    }
    config = SuiteHarnessConfig.model_validate(
        {
            "config_version": 1,
            "deployment": {
                "mode": "server",
                "environment": "development",
                "instance_id": "acme-main",
                "tenant_id": "acme",
            },
            "customer_bundle": {
                "customer_bundle_id": "acme-products",
                "version": "1.0.0",
                "harness_api": ">=0.1,<0.2",
                "products": [
                    {"product_id": product, "version": "==1.0.0", "config": {}}
                    for product in products
                ],
            },
            "workspace": {"root": tmp_path / "workspace"},
            "storage": {"root": tmp_path / "state"},
            "sandbox": {
                "backend": "docker",
                "required": True,
                "image": "registry.example/suiteharness-sandbox:0.1.0",
            },
            "models": {
                "profiles": [
                    {
                        "profile_id": "local-qwen",
                        "provider_id": "ollama",
                        "model": "qwen3",
                        "base_url": "http://ollama:11434/v1",
                        "allow_plain_http": True,
                    }
                ],
                "routes": [{"route_id": "default", "primary_profile": "local-qwen"}],
                "default_route": "default",
            },
            "web_tools": {
                "search": {
                    "default_provider": "baidu-qianfan",
                    "providers": [
                        {
                            "kind": "baidu_qianfan",
                            "credentials_ref": "search/baidu-qianfan",
                        }
                    ],
                },
                "fetch": {
                    "default_route": "direct",
                    "routes": [{"kind": "direct", "name": "direct"}],
                },
            },
            "mcp": mcp or {},
            "channels": channels,
        }
    )
    feishu_secret: dict[str, str] = {"app_secret": "app-secret"}
    if feishu_transport == "webhook":
        feishu_secret.update(
            {"verification_token": "verify-token", "encrypt_key": "encrypt-key"}
        )
    secrets = SuiteHarnessSecrets.model_validate(
        {
            "config_version": 1,
            "web": {"web/default": {"session_signing_key": "w" * 32}},
            "feishu": {"feishu/default": feishu_secret},
            "services": {"search/baidu-qianfan": {"token": "token"}},
        }
    )
    return LoadedSuiteHarnessConfig(
        config=config,
        secrets=secrets,
        config_path=tmp_path / "suiteharness.yaml",
        secrets_path=tmp_path / "suiteharness.secrets.yaml",
    )


class Activation:
    tenant_id = "acme"
    customer_bundle_id = "acme-products"
    closed = False

    def __init__(
        self,
        products: tuple[str, ...],
        *,
        authority: InMemoryCapabilityAuthority | None = None,
    ) -> None:
        root = RootContext()
        tenant = root.tenant("acme")
        self.products = {product: tenant.product(product) for product in products}
        self.requests = []
        self.grants = []
        self._authority = authority

    async def run(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        if self._authority is not None:
            self.grants.append(await self._authority.resolve(request.grant_id))
        now = datetime.now(UTC)
        return RunResult(
            run_id=request.run_id,
            status=RunStatus.SUCCEEDED,
            output={"product": request.scope.product_id, "text": request.input["message"]},
            iterations_used=1,
            tool_calls_used=0,
            started_at=now,
            finished_at=now,
        )


class Directory:
    async def resolve_feishu_identity(self, *, tenant_id, sender_external_id):  # type: ignore[no-untyped-def]
        if tenant_id == "acme" and sender_external_id == "ou-alice":
            return CompanyIdentity("alice", frozenset({"employee"}))
        return None


class ConversationAcl:
    def __init__(self, members: dict[tuple[str, str], frozenset[str]]) -> None:
        self._members = members

    async def authorize(
        self,
        principal: AuthenticatedPrincipal,
        *,
        channel: ChannelKind,
        conversation_id: str,
        product_id: str,
    ) -> bool:
        return (
            channel is ChannelKind.WEB
            and principal.principal_id
            in self._members.get((conversation_id, product_id), frozenset())
        )


class LongConnectionSdk:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.outcomes = []
        self.close_calls = 0

    async def run(self, callback):  # type: ignore[no-untyped-def]
        self.outcomes.append(await callback(self.body))
        self.outcomes.append(await callback(self.body))

    async def close(self) -> None:
        self.close_calls += 1


def _feishu_body() -> bytes:
    return json.dumps(
        {
            "header": {
                "event_type": "im.message.receive_v1",
                "event_id": "feishu-event-a",
                "create_time": "1788537600000",
            },
            "event": {
                "sender": {
                    "sender_type": "user",
                    "sender_id": {"open_id": "ou-alice"},
                },
                "message": {
                    "message_type": "text",
                    "message_id": "feishu-message-a",
                    "chat_id": "chat-a",
                    "chat_type": "p2p",
                    "content": json.dumps({"text": "查询订单"}, ensure_ascii=False),
                },
            },
        },
        ensure_ascii=False,
    ).encode()


async def _noop_tool(context, arguments):  # type: ignore[no-untyped-def]
    return {"ok": True}


def test_web_runtime_builds_routes_auth_router_and_shared_session_policy(tmp_path: Path) -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        loaded = _loaded(
            tmp_path,
            web=True,
            feishu=False,
            products=("sales", "support"),
            routes=(
                {"channel": "web", "conversation_id": "support-room", "product_id": "support"},
            ),
            share_sessions=True,
        )
        activation = Activation(("sales", "support"))
        approvals = WebApprovalRuntime()
        route_calls = []

        def route_factory(path, server):  # type: ignore[no-untyped-def]
            route_calls.append((path, server))
            return (path, "web-route")

        runtime = await CompanyChannelRuntime.create(
            loaded,
            activation=activation,
            sessions=InMemorySessionStore(),
            tools=InMemoryToolRegistry(),
            capabilities=InMemoryCapabilityAuthority(),
            grant_templates=(
                ToolAccessTemplate(channel_id="web", product_id="sales"),
                ToolAccessTemplate(channel_id="web", product_id="support"),
            ),
            web_approvals=approvals,
            web_conversation_authorizer=ConversationAcl(
                {("support-room", "support"): frozenset({"alice", "bob"})}
            ),
            web_route_factory=route_factory,
        )
        principal = AuthenticatedPrincipal(
            tenant_id="acme",
            principal_id="alice",
            roles=frozenset({"employee"}),
        )
        token = runtime.web_session_tokens.issue(principal)
        assert runtime.web_session_tokens.verify(token).principal() == principal
        message = InboundMessage(
            channel=ChannelKind.WEB,
            event_id="connection-a:message-a",
            message_id="message-a",
            conversation_id="support-room",
            sender_external_id="alice",
            text="help",
            received_at=datetime.now(UTC),
        )
        events = [
            event
            async for event in runtime.web_gateway.dispatch_authenticated(message, principal)
        ]
        conflicting = message.model_copy(update={"product_id": "sales"})
        with pytest.raises(ChannelAdmissionError, match="conflicts"):
            _ = [
                event
                async for event in runtime.web_gateway.dispatch_authenticated(
                    conflicting, principal
                )
            ]
        bob = principal.model_copy(update={"principal_id": "bob"})
        bob_message = message.model_copy(
            update={
                "event_id": "connection-b:message-a",
                "sender_external_id": "bob",
            }
        )
        _ = [
            event
            async for event in runtime.web_gateway.dispatch_authenticated(bob_message, bob)
        ]
        mallory = principal.model_copy(update={"principal_id": "mallory"})
        mallory_message = message.model_copy(
            update={
                "event_id": "connection-mallory:message-a",
                "sender_external_id": "mallory",
            }
        )
        with pytest.raises(ChannelAdmissionError, match="not a member"):
            _ = [
                event
                async for event in runtime.web_gateway.dispatch_authenticated(
                    mallory_message, mallory
                )
            ]
        routes = runtime.starlette_routes()
        await runtime.close()
        with pytest.raises(RuntimeError, match="closed"):
            _ = [
                event
                async for event in runtime.web_gateway.dispatch_authenticated(
                    message, principal
                )
            ]
        await runtime.close()
        return events, activation, routes, route_calls

    events, activation, routes, route_calls = asyncio.run(exercise())
    assert [event.kind for event in events] == [
        OutboundEventKind.STARTED,
        OutboundEventKind.COMPLETED,
    ]
    assert activation.requests[0].scope.product_id == "support"
    assert activation.requests[0].scope.path.agent_id == "support-agent"
    assert activation.requests[0].scope.roles == frozenset({"employee"})
    assert activation.requests[0].scope.path.session_id == activation.requests[1].scope.path.session_id
    assert routes == (("/ws", "web-route"),)
    assert route_calls[0][1] is not None


def test_feishu_long_connection_uses_durable_deployment_dedupe(tmp_path: Path) -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        loaded = _loaded(
            tmp_path,
            web=False,
            feishu=True,
            feishu_processing_timeout_seconds=700,
            feishu_processing_lease_seconds=701,
        )
        activation = Activation(("sales",))
        sdk = LongConnectionSdk(_feishu_body())
        shared_database = SQLiteDatabase(loaded.config.storage.runtime_path())
        outbound = []

        async def sink(message, event):  # type: ignore[no-untyped-def]
            outbound.append((message, event))

        runtime = await CompanyChannelRuntime.create(
            loaded,
            activation=activation,
            sessions=InMemorySessionStore(),
            tools=InMemoryToolRegistry(),
            capabilities=InMemoryCapabilityAuthority(),
            grant_templates=(
                ToolAccessTemplate(channel_id="feishu", product_id="sales"),
            ),
            feishu_adapters=FeishuHostAdapters(
                directory=Directory(),
                outbound_sink=sink,
                long_connection_sdk=sdk,
            ),
            runtime_database=shared_database,
        )
        assert runtime.feishu_processor.processing_timeout_seconds == 700
        assert runtime.feishu_deduplicator.processing_lease_seconds == 701
        assert runtime.starlette_routes() == ()
        await runtime.run_feishu_long_connection()
        row = runtime._database.connection.execute(  # noqa: SLF001
            "SELECT deployment_id, channel_id FROM suiteharness_channel_event_claims"
        ).fetchone()
        await runtime.close()
        assert shared_database.connection.execute("SELECT 1").fetchone()[0] == 1
        await shared_database.close()
        await runtime.close()
        return activation, sdk, outbound, tuple(row)

    activation, sdk, outbound, scope = asyncio.run(exercise())
    assert sdk.outcomes == [
        FeishuEventOutcome.DISPATCHED,
        FeishuEventOutcome.DUPLICATE,
    ]
    assert sdk.close_calls == 1
    assert [event.kind for _message, event in outbound] == [
        OutboundEventKind.STARTED,
        OutboundEventKind.COMPLETED,
    ]
    assert activation.requests[0].read_only is True
    assert activation.requests[0].scope.channel_id == "feishu"
    assert activation.requests[0].scope.path.agent_id == "sales-agent"
    assert scope == ("acme-main", "feishu")


def test_feishu_default_product_routes_new_ab_private_chat(tmp_path: Path) -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        loaded = _loaded(
            tmp_path,
            web=False,
            feishu=True,
            feishu_transport="webhook",
            products=("product-a", "product-b"),
            # This satisfies the static config rule but deliberately does not
            # match the new private chat in _feishu_body().
            routes=(
                {
                    "channel": "feishu",
                    "conversation_id": "previously-known-chat",
                    "product_id": "product-a",
                },
            ),
        )
        activation = Activation(("product-a", "product-b"))
        runtime = await CompanyChannelRuntime.create(
            loaded,
            activation=activation,
            sessions=InMemorySessionStore(),
            tools=InMemoryToolRegistry(),
            capabilities=InMemoryCapabilityAuthority(),
            grant_templates=(
                ToolAccessTemplate(
                    channel_id="feishu", product_id="product-a"
                ),
                ToolAccessTemplate(channel_id="feishu", product_id="product-b"),
            ),
            feishu_adapters=FeishuHostAdapters(
                directory=Directory(),
                outbound_sink=lambda _message, _event: asyncio.sleep(0),
                default_product_id="product-a",
            ),
        )
        outcome = await runtime.feishu_processor.accept_verified_json(_feishu_body())
        await runtime.close()
        return activation, outcome

    activation, outcome = asyncio.run(exercise())
    assert outcome is FeishuEventOutcome.DISPATCHED
    assert activation.requests[0].scope.product_id == "product-a"


def test_feishu_unknown_default_product_fails_startup(tmp_path: Path) -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        loaded = _loaded(tmp_path, web=False, feishu=True)
        with pytest.raises(
            ChannelHostConfigurationError,
            match="default_product_id.*active product",
        ):
            await CompanyChannelRuntime.create(
                loaded,
                activation=Activation(("sales",)),
                sessions=InMemorySessionStore(),
                tools=InMemoryToolRegistry(),
                capabilities=InMemoryCapabilityAuthority(),
                grant_templates=(
                    ToolAccessTemplate(channel_id="feishu", product_id="sales"),
                ),
                feishu_adapters=FeishuHostAdapters(
                    directory=Directory(),
                    outbound_sink=lambda _message, _event: asyncio.sleep(0),
                    default_product_id="not-selected",
                    long_connection_sdk=LongConnectionSdk(_feishu_body()),
                ),
            )

    asyncio.run(exercise())


def test_feishu_whitelisted_write_template_makes_run_not_read_only(tmp_path: Path) -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        loaded = _loaded(
            tmp_path,
            web=False,
            feishu=True,
            feishu_transport="webhook",
            writable=True,
        )
        activation = Activation(("sales",))
        registry = InMemoryToolRegistry()
        identity = ToolIdentity(
            namespace="suiteharness",
            name="suiteharness.fs.write",
            origin="suiteharness.builtin.fs",
            version="1",
        )
        registry.register_protected(
            ToolSpec(
                name="suiteharness.fs.write",
                effects=frozenset({ToolEffect.WRITE}),
                required_capabilities=frozenset({"workspace.write"}),
                input_schema={"type": "object"},
            ),
            _noop_tool,
            identity=identity,
        )
        outbound = []

        async def sink(message, event):  # type: ignore[no-untyped-def]
            outbound.append(event)

        def route_factory(path, handler):  # type: ignore[no-untyped-def]
            return (path, handler)

        runtime = await CompanyChannelRuntime.create(
            loaded,
            activation=activation,
            sessions=InMemorySessionStore(),
            tools=registry,
            capabilities=InMemoryCapabilityAuthority(),
            grant_templates=(
                ToolAccessTemplate(
                    channel_id="feishu",
                    product_id="sales",
                    write_aliases=("suiteharness.fs.write",),
                ),
            ),
            feishu_adapters=FeishuHostAdapters(
                directory=Directory(),
                outbound_sink=sink,
            ),
            feishu_route_factory=route_factory,
        )
        outcome = await runtime.feishu_processor.accept_verified_json(_feishu_body())
        routes = runtime.starlette_routes()
        await runtime.close()
        return activation, outcome, routes, outbound

    activation, outcome, routes, outbound = asyncio.run(exercise())
    assert outcome is FeishuEventOutcome.DISPATCHED
    assert activation.requests[0].read_only is False
    assert routes[0][0] == "/channels/feishu/events"
    assert len(routes) == 1
    assert outbound[-1].kind is OutboundEventKind.COMPLETED


def test_explicit_mcp_material_can_merge_into_a_web_grant(tmp_path: Path) -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        loaded = _loaded(
            tmp_path,
            web=True,
            feishu=False,
            mcp={
                "servers_by_product": {
                    "sales": [
                        {
                            "server_id": "inventory",
                            "transport": "streamable_http",
                            "endpoint": "https://inventory.example.test/rpc",
                            "resume_sessions": False,
                        }
                    ]
                },
                "channel_access": {
                    "web": {
                        "sales": {
                            "allow_tools": {
                                "inventory": ["inventory.lookup"]
                            }
                        }
                    }
                },
            },
        )
        authority = InMemoryCapabilityAuthority()
        activation = Activation(("sales",), authority=authority)
        registry = InMemoryToolRegistry()
        handle = registry.register(
            ScopePath.product("acme", "sales"),
            ToolSpec(
                name="mcp.inventory.inventory-lookup-a1",
                effects=frozenset({ToolEffect.READ, ToolEffect.EXTERNAL}),
                required_capabilities=frozenset({"mcp.inventory.call"}),
                input_schema={"type": "object"},
            ),
            _noop_tool,
        )
        runtime = await CompanyChannelRuntime.create(
            loaded,
            activation=activation,
            sessions=InMemorySessionStore(),
            tools=registry,
            capabilities=authority,
            grant_templates=(
                ToolAccessTemplate(channel_id="web", product_id="sales"),
            ),
            web_approvals=WebApprovalRuntime(),
            mcp_grant_material={
                "sales": McpGrantMaterial(
                    tools=(
                        AdditionalGrantTool(
                            server_id="inventory",
                            remote_name="inventory.lookup",
                            identity=handle.identity,
                            capabilities=frozenset({"mcp.inventory.call"}),
                        ),
                    ),
                )
            },
        )
        principal = AuthenticatedPrincipal(tenant_id="acme", principal_id="alice")
        message = InboundMessage(
            channel=ChannelKind.WEB,
            event_id="event-a",
            message_id="message-a",
            conversation_id="conversation-a",
            sender_external_id="alice",
            text="inventory",
            product_id="sales",
            received_at=datetime.now(UTC),
        )
        _ = [
            event
            async for event in runtime.web_gateway.dispatch_authenticated(message, principal)
        ]
        await runtime.close()
        return activation, handle.identity

    activation, identity = asyncio.run(exercise())
    assert activation.requests[0].read_only is True
    assert activation.grants[0].tool_identities == frozenset({identity})
    assert activation.grants[0].capabilities == frozenset({"mcp.inventory.call"})


def test_connected_mcp_material_is_denied_without_yaml_channel_allowlist(
    tmp_path: Path,
) -> None:
    async def exercise() -> Activation:
        loaded = _loaded(
            tmp_path,
            web=True,
            feishu=False,
            mcp={
                "servers_by_product": {
                    "sales": [
                        {
                            "server_id": "inventory",
                            "transport": "streamable_http",
                            "endpoint": "https://inventory.example.test/rpc",
                            "resume_sessions": False,
                        }
                    ]
                }
            },
        )
        authority = InMemoryCapabilityAuthority()
        activation = Activation(("sales",), authority=authority)
        registry = InMemoryToolRegistry()
        handle = registry.register(
            ScopePath.product("acme", "sales"),
            ToolSpec(
                name="mcp.inventory.inventory-lookup-a1",
                effects=frozenset({ToolEffect.READ, ToolEffect.EXTERNAL}),
                required_capabilities=frozenset({"mcp.inventory.call"}),
                input_schema={"type": "object"},
            ),
            _noop_tool,
        )
        runtime = await CompanyChannelRuntime.create(
            loaded,
            activation=activation,
            sessions=InMemorySessionStore(),
            tools=registry,
            capabilities=authority,
            grant_templates=(ToolAccessTemplate(channel_id="web", product_id="sales"),),
            web_approvals=WebApprovalRuntime(),
            mcp_grant_material={
                "sales": McpGrantMaterial(
                    tools=(
                        AdditionalGrantTool(
                            server_id="inventory",
                            remote_name="inventory.lookup",
                            identity=handle.identity,
                            capabilities=frozenset({"mcp.inventory.call"}),
                        ),
                    )
                )
            },
        )
        principal = AuthenticatedPrincipal(tenant_id="acme", principal_id="alice")
        message = InboundMessage(
            channel=ChannelKind.WEB,
            event_id="event-denied",
            message_id="message-denied",
            conversation_id="conversation-denied",
            sender_external_id="alice",
            text="inventory",
            product_id="sales",
            received_at=datetime.now(UTC),
        )
        _ = [
            event
            async for event in runtime.web_gateway.dispatch_authenticated(message, principal)
        ]
        await runtime.close()
        return activation

    activation = asyncio.run(exercise())
    assert activation.grants[0].tool_identities == frozenset()
    assert activation.grants[0].capabilities == frozenset()


def test_host_rejects_yaml_tool_allowlist_not_present_in_discovered_inventory(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        loaded = _loaded(
            tmp_path,
            web=True,
            feishu=False,
            mcp={
                "servers_by_product": {
                    "sales": [
                        {
                            "server_id": "inventory",
                            "transport": "streamable_http",
                            "endpoint": "https://inventory.example.test/rpc",
                            "resume_sessions": False,
                        }
                    ]
                },
                "channel_access": {
                    "web": {
                        "sales": {
                            "allow_tools": {"inventory": ["inventory.missing"]}
                        }
                    }
                },
            },
        )
        registry = InMemoryToolRegistry()
        handle = registry.register(
            ScopePath.product("acme", "sales"),
            ToolSpec(
                name="mcp.inventory.inventory-lookup-a1",
                effects=frozenset({ToolEffect.READ, ToolEffect.EXTERNAL}),
                required_capabilities=frozenset({"mcp.inventory.call"}),
                input_schema={"type": "object"},
            ),
            _noop_tool,
        )
        approvals = WebApprovalRuntime()
        with pytest.raises(ChannelHostConfigurationError, match="not discovered"):
            await CompanyChannelRuntime.create(
                loaded,
                activation=Activation(("sales",)),
                sessions=InMemorySessionStore(),
                tools=registry,
                capabilities=InMemoryCapabilityAuthority(),
                grant_templates=(
                    ToolAccessTemplate(channel_id="web", product_id="sales"),
                ),
                web_approvals=approvals,
                mcp_grant_material={
                    "sales": McpGrantMaterial(
                        tools=(
                            AdditionalGrantTool(
                                server_id="inventory",
                                remote_name="inventory.lookup",
                                identity=handle.identity,
                                capabilities=frozenset({"mcp.inventory.call"}),
                            ),
                        )
                    )
                },
            )
        assert approvals.closed

    asyncio.run(exercise())


def test_host_fails_closed_for_missing_authority_or_channel_adapters(tmp_path: Path) -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        web_loaded = _loaded(tmp_path / "web", web=True, feishu=False)

        class LegacySessionStore:
            async def append(self): ...

            async def create(self): ...

            async def get(self): ...

            async def resume(self): ...

            async def save_checkpoint(self): ...

            async def set_status(self): ...

        with pytest.raises(TypeError, match="SessionStore contract"):
            await CompanyChannelRuntime.create(
                web_loaded,
                activation=Activation(("sales",)),
                sessions=LegacySessionStore(),  # type: ignore[arg-type]
                tools=InMemoryToolRegistry(),
                capabilities=InMemoryCapabilityAuthority(),
                grant_templates=(
                    ToolAccessTemplate(channel_id="web", product_id="sales"),
                ),
                web_approvals=WebApprovalRuntime(),
            )
        with pytest.raises(ChannelHostConfigurationError, match="exactly cover"):
            await CompanyChannelRuntime.create(
                web_loaded,
                activation=Activation(("sales",)),
                sessions=InMemorySessionStore(),
                tools=InMemoryToolRegistry(),
                capabilities=InMemoryCapabilityAuthority(),
                grant_templates=(),
                web_approvals=WebApprovalRuntime(),
            )
        with pytest.raises(ChannelHostConfigurationError, match="WebApprovalRuntime"):
            await CompanyChannelRuntime.create(
                web_loaded,
                activation=Activation(("sales",)),
                sessions=InMemorySessionStore(),
                tools=InMemoryToolRegistry(),
                capabilities=InMemoryCapabilityAuthority(),
                grant_templates=(
                    ToolAccessTemplate(channel_id="web", product_id="sales"),
                ),
            )

        invalid_product_acl_approvals = WebApprovalRuntime()
        with pytest.raises(TypeError, match="product_access_authorizer"):
            await CompanyChannelRuntime.create(
                web_loaded,
                activation=Activation(("sales",)),
                sessions=InMemorySessionStore(),
                tools=InMemoryToolRegistry(),
                capabilities=InMemoryCapabilityAuthority(),
                grant_templates=(
                    ToolAccessTemplate(channel_id="web", product_id="sales"),
                ),
                web_approvals=invalid_product_acl_approvals,
                product_access_authorizer=object(),  # type: ignore[arg-type]
            )
        assert invalid_product_acl_approvals.closed

        shared_web_loaded = _loaded(
            tmp_path / "shared-web",
            web=True,
            feishu=False,
            share_sessions=True,
        )
        approvals = WebApprovalRuntime()
        with pytest.raises(
            ChannelHostConfigurationError,
            match="trusted conversation authorizer",
        ):
            await CompanyChannelRuntime.create(
                shared_web_loaded,
                activation=Activation(("sales",)),
                sessions=InMemorySessionStore(),
                tools=InMemoryToolRegistry(),
                capabilities=InMemoryCapabilityAuthority(),
                grant_templates=(
                    ToolAccessTemplate(channel_id="web", product_id="sales"),
                ),
                web_approvals=approvals,
            )
        assert approvals.closed

        feishu_loaded = _loaded(tmp_path / "feishu", web=False, feishu=True)
        with pytest.raises(ChannelHostConfigurationError, match="host adapters"):
            await CompanyChannelRuntime.create(
                feishu_loaded,
                activation=Activation(("sales",)),
                sessions=InMemorySessionStore(),
                tools=InMemoryToolRegistry(),
                capabilities=InMemoryCapabilityAuthority(),
                grant_templates=(
                    ToolAccessTemplate(channel_id="feishu", product_id="sales"),
                ),
            )
        with pytest.raises(ChannelHostConfigurationError, match="writable_root"):
            await CompanyChannelRuntime.create(
                feishu_loaded,
                activation=Activation(("sales",)),
                sessions=InMemorySessionStore(),
                tools=InMemoryToolRegistry(),
                capabilities=InMemoryCapabilityAuthority(),
                grant_templates=(
                    ToolAccessTemplate(
                        channel_id="feishu",
                        product_id="sales",
                        write_aliases=("suiteharness.fs.write",),
                    ),
                ),
                feishu_adapters=FeishuHostAdapters(
                    directory=Directory(),
                    outbound_sink=lambda _message, _event: asyncio.sleep(0),
                    long_connection_sdk=LongConnectionSdk(_feishu_body()),
                ),
            )

    asyncio.run(exercise())
