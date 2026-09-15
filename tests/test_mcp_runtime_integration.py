"""Runtime regressions against controlled wire peers, without network or Docker."""

import asyncio
import json

import httpx
import pytest

from suiteharness.execution import InMemoryToolRegistry
from suiteharness.execution.protocols import ToolCallContext
from suiteharness.mcp.bridge import McpToolBridge
from suiteharness.mcp.callbacks import McpClientRequestDispatcher
from suiteharness.mcp.client import McpClient
from suiteharness.mcp.defaults import ConfiguredMcpEgressPolicy, GuardedMcpHttpExchange
from suiteharness.mcp.models import ClientCapabilities, JsonRpcResponse, McpServerConfig
from suiteharness.mcp.protocols import HttpRequest
from suiteharness.mcp.transports import StreamableHttpTransport
from suiteharness.runtime.scopes import RequestScope, ScopePath


class DNS:
    def __init__(self, *answers):
        self.answers = list(answers or ({"8.8.8.8"},))
        self.calls = 0

    async def resolve(self, hostname, port):
        answer = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        return frozenset(answer)


def config(**changes):
    return McpServerConfig(
        server_id="fixture",
        transport="streamable_http",
        endpoint="https://mcp.example/rpc",
        resume_sessions=False,
        **changes,
    )


def scope(product="product"):
    return RequestScope(
        path=ScopePath.product("tenant", product),
        principal_id="alice",
        channel_id="web",
        purpose="test",
        request_id="req",
    )


def exchange(cfg, handler, dns=None, products=None):
    policy = ConfiguredMcpEgressPolicy(
        "tenant", products or {"product": (cfg,)}, resolver=dns or DNS()
    )
    transport = GuardedMcpHttpExchange(
        policy,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ),
    )
    return policy, transport.bind(tenant_id="tenant", product_id="product", config=cfg)


def test_rebinding_cannot_replace_checked_address_and_unbound_exchange_denies():
    async def check():
        cfg = config()
        dns = DNS({"8.8.8.8"}, {"127.0.0.1"})
        seen = []

        async def handle(request):
            seen.append(str(request.url))
            return httpx.Response(200, json={"ok": True})

        policy, http = exchange(cfg, handle, dns)
        reply = await http.send(HttpRequest("POST", cfg.endpoint, {}, b"{}", 3))
        assert reply.status == 200
        assert seen == ["https://8.8.8.8/rpc"]
        assert dns.calls == 1
        unbound = GuardedMcpHttpExchange(policy, client_factory=lambda: httpx.AsyncClient())
        with pytest.raises(PermissionError):
            await unbound.send(HttpRequest("POST", cfg.endpoint, {}, b"{}", 3))

    asyncio.run(check())


def test_same_endpoint_does_not_inherit_another_products_private_exception():
    async def check():
        cfg = config()
        private = cfg.model_copy(update={"allowed_private_networks": ("127.0.0.0/8",)})
        _, http = exchange(
            cfg,
            lambda _: httpx.Response(200),
            DNS({"127.0.0.1"}),
            {"other": (private,), "product": (cfg,)},
        )
        with pytest.raises(PermissionError):
            await http.send(HttpRequest("POST", cfg.endpoint, {}, b"{}", 3))

    asyncio.run(check())


def test_same_product_shared_url_still_authorizes_the_exact_server():
    async def check():
        cfg = config()
        private = cfg.model_copy(
            update={"server_id": "private", "allowed_private_networks": ("127.0.0.0/8",)}
        )
        policy, http = exchange(
            private,
            lambda _: httpx.Response(200),
            DNS({"127.0.0.1"}),
            {"product": (cfg, private)},
        )
        transport = StreamableHttpTransport(
            private,
            tenant_id="tenant",
            product_id="product",
            egress_policy=policy,
            exchange=http,
        )
        await transport.start(lambda _: None, lambda _: None)
        await transport.close()
        denied = StreamableHttpTransport(
            cfg,
            tenant_id="tenant",
            product_id="product",
            egress_policy=policy,
            exchange=http,
        )
        with pytest.raises(PermissionError):
            await denied.start(lambda _: None, lambda _: None)

    asyncio.run(check())


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
        self.read_count = 0

    async def __aiter__(self):
        for item in self.chunks:
            self.read_count += 1
            yield item

    async def aclose(self):
        self.closed = True


def test_http_body_limit_stops_before_reading_remaining_chunks():
    async def check():
        cfg = config(max_message_bytes=1024)
        body = Chunks([b"a" * 600] * 10)
        _, http = exchange(cfg, lambda _: httpx.Response(200, stream=body))
        with pytest.raises(Exception, match="exceeds|limit"):
            await http.send(HttpRequest("POST", cfg.endpoint, {}, b"{}", 3))
        assert body.read_count <= 2
        assert body.closed

    asyncio.run(check())


def test_post_sse_callback_is_answered_before_server_finishes_stream():
    async def check():
        cfg = config()
        answered = asyncio.Event()
        observed = []

        class CallbackStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"jsonrpc":"2.0","id":"cb","method":"ping"}\n\n'
                await asyncio.wait_for(answered.wait(), 1)
                yield b'data: {"jsonrpc":"2.0","id":"suiteharness-1","result":{"ok":true}}\n\n'

        async def handle(request):
            payload = json.loads(request.content)
            if payload.get("id") == "cb":
                assert payload["result"] == {}
                answered.set()
                return httpx.Response(202)
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=CallbackStream()
            )

        policy, http = exchange(cfg, handle)
        transport = StreamableHttpTransport(
            cfg, tenant_id="tenant", product_id="product", egress_policy=policy, exchange=http
        )

        async def callback(request, context):
            observed.append(context)
            return JsonRpcResponse(id=request.id, result={})

        async def notification(*args):
            pass

        await transport.start(callback, notification)
        try:
            assert await transport.request(
                "tools/call", {}, context=scope(), timeout_seconds=2
            ) == {"ok": True}
            assert observed == [scope()]
        finally:
            await transport.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    "capabilities,operation,arguments,wanted",
    [
        ({"resources": {}}, "resources/read", {"uri": "fixture://readme"}, "resource text"),
        ({"prompts": {}}, "prompts/get", {"name": "help"}, "prompt text"),
    ],
)
def test_resource_and_prompt_only_peers_install_scoped_grant_tools(
    capabilities, operation, arguments, wanted
):
    async def check():
        cfg = config()
        methods = []

        async def handle(request):
            payload = json.loads(request.content)
            method = payload.get("method")
            methods.append(method)
            if "id" not in payload:
                return httpx.Response(202)
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-11-25",
                    "capabilities": capabilities,
                    "serverInfo": {"name": "fixture", "version": "1"},
                }
            elif method == "resources/read":
                result = {"contents": [{"uri": arguments["uri"], "text": "resource text"}]}
            elif method == "prompts/get":
                result = {
                    "messages": [
                        {"role": "user", "content": {"type": "text", "text": "prompt text"}}
                    ]
                }
            else:
                raise AssertionError(f"unadvertised operation: {method}")
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result}
            )

        policy, http = exchange(cfg, handle)
        transport = StreamableHttpTransport(
            cfg, tenant_id="tenant", product_id="product", egress_policy=policy, exchange=http
        )
        client = McpClient(
            cfg,
            tenant_id="tenant",
            product_id="product",
            transport=transport,
            dispatcher=McpClientRequestDispatcher(
                server_id="fixture",
                tenant_id="tenant",
                product_id="product",
                capabilities=ClientCapabilities(),
                features=cfg.features,
            ),
            capabilities=ClientCapabilities(),
        )
        await client.connect()

        class Manager:
            def client(self, request_scope, server_id):
                assert request_scope.product_id == "product" and server_id == "fixture"
                return client

        registry = InMemoryToolRegistry()
        try:
            installed = await McpToolBridge(Manager(), registry).install(
                scope(), "fixture", registration_scope=scope().path
            )
            item = next(tool for tool in installed.tools if tool.remote_name == operation)
            registered = registry.resolve(scope(), item.alias)
            assert registered.spec.required_capabilities == frozenset(
                {"mcp.fixture.call", f"mcp.fixture.{operation.split('/')[0]}"}
            )
            assert registry.resolve(scope("other"), item.alias) is None
            context = ToolCallContext(scope(), "run", "call", item.identity, item.spec, 3)
            result = await registered.handler(context, arguments)
            assert wanted in json.dumps(result)
            assert "tools/list" not in methods
            installed.close()
            assert registry.resolve(scope(), item.alias) is None
        finally:
            await client.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    "post_endpoint", ["/messages?session=abc", "https://evil.example/messages"]
)
def test_legacy_sse_roundtrip_and_cross_origin_endpoint_rejection(post_endpoint):
    async def check():
        from suiteharness.mcp import defaults

        assert hasattr(defaults, "DefaultLegacySseAdapter"), "legacy SSE runtime adapter missing"
        cfg = config(legacy_sse="adapter_required")
        events = asyncio.Queue()
        closed = asyncio.Event()
        posts = []

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield f"event: endpoint\ndata: {post_endpoint}\n\n".encode()
                while True:
                    yield await events.get()

            async def aclose(self):
                closed.set()

        async def handle(request):
            if request.method == "GET":
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=Stream()
                )
            posts.append(request)
            value = json.loads(request.content)
            if "id" in value:
                await events.put(
                    (
                        "event: message\ndata: "
                        + json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": value["id"],
                                "result": {"echo": value["params"]},
                            }
                        )
                        + "\n\n"
                    ).encode()
                )
            return httpx.Response(202)

        policy, http = exchange(cfg, handle)
        adapter = defaults.DefaultLegacySseAdapter(policy, http)
        transport = adapter.create_transport(
            cfg, tenant_id="tenant", product_id="product", resume=None
        )

        async def callback(request, context):
            return JsonRpcResponse(id=request.id, result={})

        async def notification(*args):
            pass

        if post_endpoint.startswith("https://evil"):
            with pytest.raises(PermissionError):
                await transport.start(callback, notification)
            assert not posts
        else:
            await transport.start(callback, notification)
            assert await transport.request(
                "ping", {"n": 1}, context=scope(), timeout_seconds=2
            ) == {"echo": {"n": 1}}
            assert await transport.request(
                "ping", {"n": 2}, context=scope(), timeout_seconds=2
            ) == {"echo": {"n": 2}}
            assert all(str(p.url) == "https://8.8.8.8/messages?session=abc" for p in posts)
        await transport.close()
        assert closed.is_set()

    asyncio.run(check())


def test_default_stdio_factory_uses_injected_sandbox_and_readonly_product(tmp_path):
    async def check():
        from suiteharness.mcp import defaults
        from suiteharness.sandbox import SandboxLimits

        assert hasattr(defaults, "DefaultStdioSessionFactory"), "default stdio factory missing"
        seen = []
        session = object()

        class Backend:
            async def open_session(self, request):
                seen.append(request)
                return session

        cfg = McpServerConfig(
            server_id="stdio",
            transport="stdio",
            command="python",
            arguments=("/workspace/peer.py",),
        )
        result = await defaults.DefaultStdioSessionFactory(SandboxLimits()).open(
            cfg, sandbox=Backend(), product_workspace=tmp_path
        )
        assert result is session
        request = seen[0]
        assert request.argv == ("python", "/workspace/peer.py")
        assert len(request.mounts) == 1 and request.mounts[0].host_path == tmp_path
        assert request.mounts[0].container_path == "/workspace" and request.mounts[0].read_only
        assert request.network.mode.value == "none" and not request.environment
        assert request.limits.timeout_seconds >= 3600

    asyncio.run(check())


def test_default_factory_extension_host_and_channel_grants_for_resource_only_stdio(tmp_path):
    async def check():
        from types import SimpleNamespace as NS

        from suiteharness.config.models import (
            McpConfig,
            PluginsConfig,
            SandboxLimitsConfig,
            SuiteHarnessSecrets,
        )
        from suiteharness.execution import InMemoryCapabilityAuthority
        from suiteharness.mcp import defaults
        from suiteharness.runtime.effects import EffectScope
        from suiteharness.sandbox import SandboxAvailability
        from suiteharness.server.access import (
            AdditionalGrantMaterial,
            ConfiguredGrantIssuer,
            McpChannelAccessRule,
            ToolAccessTemplate,
        )
        from suiteharness.server.extensions import ServerExtensionHost

        assert hasattr(defaults, "build_default_mcp_adapters"), "default host factory missing"
        cfg = McpServerConfig(server_id="fixture", transport="stdio", command="peer")
        loaded = NS(
            config=NS(
                mcp=McpConfig(servers_by_product={"product": (cfg,)}),
                plugins=PluginsConfig(),
                sandbox=NS(limits=SandboxLimitsConfig()),
                deployment=NS(tenant_id="tenant"),
                customer_bundle=NS(
                    customer_bundle_id="bundle", products=(NS(product_id="product"),)
                ),
            ),
            secrets=SuiteHarnessSecrets(),
        )
        finished = asyncio.Event()

        class Session:
            def __init__(self):
                self.queue = asyncio.Queue()

            async def send_line(self, payload):
                message = json.loads(payload)
                if "id" not in message:
                    return
                assert message["method"] == "initialize"
                result = {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"resources": {}},
                    "serverInfo": {"name": "peer", "version": "1"},
                }
                await self.queue.put(
                    json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
                )

            async def receive_line(self):
                return await self.queue.get()

            async def close(self):
                finished.set()

        class Sandbox:
            production_safe = True

            async def availability(self):
                return SandboxAvailability(True)

            async def open_session(self, request):
                assert request.mounts[0].host_path == tmp_path
                return Session()

        registry = InMemoryToolRegistry()
        effects = EffectScope("product")
        active = NS(
            closed=False,
            tenant_id="tenant",
            customer_bundle_id="bundle",
            products={"product": NS(path=scope().path, effects=effects)},
        )
        host = ServerExtensionHost(
            loaded,
            sandbox=Sandbox(),
            tools=registry,
            product_workspace=lambda _: tmp_path,
            mcp_adapters=defaults.build_default_mcp_adapters(loaded),
        )
        try:
            activation = await host.start(active)
            material = AdditionalGrantMaterial(activation.mcp_by_product["product"].tools)
            assert {t.remote_name for t in material.tools} == {
                "resources/list",
                "resources/templates/list",
                "resources/read",
            }
            authority = InMemoryCapabilityAuthority()
            template = ToolAccessTemplate(channel_id="web", product_id="product")
            denied = ConfiguredGrantIssuer(
                registry=registry,
                authority=authority,
                templates=(template,),
                additional_material_by_product={"product": material},
            )
            async with denied.lease(scope()) as lease:
                assert not lease.grant.tool_identities
            allowed = ConfiguredGrantIssuer(
                registry=registry,
                authority=authority,
                templates=(template,),
                additional_material_by_product={"product": material},
                mcp_access_by_route={
                    ("web", "product"): McpChannelAccessRule(
                        tools_by_server={"fixture": frozenset({"resources/read"})}
                    )
                },
            )
            async with allowed.lease(scope()) as lease:
                assert len(lease.grant.tool_identities) == 1
                assert lease.grant.capabilities == frozenset(
                    {"mcp.fixture.call", "mcp.fixture.resources"}
                )
        finally:
            await host.close()
            await effects.close()
        assert finished.is_set()

    asyncio.run(check())


def test_http_202_resumes_with_get_and_closes_stream_at_matching_response():
    async def check():
        cfg = config()
        stream = Chunks(
            [
                b"id: cursor-1\r",
                b'\ndata: {"jsonrpc":"2.0","id":"suiteharness-1","result":{"ok":true}}\r\n\r',
                b"\n",
            ]
        )
        seen = []

        async def handle(request):
            seen.append(request)
            if request.method == "POST":
                return httpx.Response(202, headers={"Mcp-Session-Id": "session1"})
            if request.method == "DELETE":
                return httpx.Response(204)
            assert request.headers["Mcp-Session-Id"] == "session1"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

        policy, http = exchange(cfg, handle)
        transport = StreamableHttpTransport(
            cfg, tenant_id="tenant", product_id="product", egress_policy=policy, exchange=http
        )

        async def notification(*args):
            pass

        await transport.start(None, notification)
        assert await transport.request("ping", {}, context=scope(), timeout_seconds=2) == {
            "ok": True
        }
        assert transport.resume_state.last_event_id == "cursor-1"
        assert stream.closed
        await transport.close()
        assert [r.method for r in seen] == ["POST", "GET", "DELETE"]

    asyncio.run(check())


def test_sse_chunk_boundaries_multibyte_and_event_count_are_bounded():
    async def check():
        from suiteharness.mcp.models import McpProtocolError
        from suiteharness.mcp.streaming import iter_sse

        async def chunks():
            for value in "data: 中\r\n\r\ndata: two\n\n".encode():
                yield bytes([value])

        parsed = [event async for event in iter_sse(chunks(), max_message_bytes=1024, max_events=2)]
        assert [item.data for item in parsed] == ["中", "two"]
        with pytest.raises(McpProtocolError, match="max_stream_events"):
            [event async for event in iter_sse(chunks(), max_message_bytes=1024, max_events=1)]

        async def oversized():
            yield b"data: " + b"x" * 2048

        with pytest.raises(McpProtocolError, match="max_message_bytes"):
            [event async for event in iter_sse(oversized(), max_message_bytes=1024, max_events=2)]

    asyncio.run(check())


def test_default_credentials_remain_bound_to_configured_endpoint():
    async def check():
        from types import SimpleNamespace as NS

        from suiteharness.config.models import McpConfig, SuiteHarnessSecrets
        from suiteharness.mcp.defaults import ConfiguredMcpTokenProvider

        cfg = config(credential_ref="mcp-token")
        provider = ConfiguredMcpTokenProvider(
            NS(
                config=NS(mcp=McpConfig(servers_by_product={"product": (cfg,)})),
                secrets=SuiteHarnessSecrets(services={"mcp-token": {"token": "fixture-value"}}),
            )
        )
        assert (
            await provider.authorization_header("mcp-token", endpoint=cfg.endpoint)
            == "Bearer fixture-value"
        )
        with pytest.raises(PermissionError):
            await provider.authorization_header("mcp-token", endpoint="https://other.example/rpc")

    asyncio.run(check())


def test_stdio_protocol_failure_marks_transport_unavailable_and_closes_session(tmp_path):
    async def check():
        from suiteharness.mcp.transports import SandboxedStdioTransport
        from suiteharness.sandbox import SandboxAvailability

        finished = asyncio.Event()

        class Session:
            async def receive_line(self):
                return b"not json"

            async def close(self):
                finished.set()

            async def send_line(self, payload):
                pass

        class Factory:
            async def open(self, *args, **kwargs):
                return Session()

        class Sandbox:
            production_safe = True

            async def availability(self):
                return SandboxAvailability(True)

        transport = SandboxedStdioTransport(
            McpServerConfig(server_id="peer", transport="stdio", command="peer"),
            sandbox=Sandbox(),
            session_factory=Factory(),
            product_workspace=tmp_path,
        )
        await transport.start(None, None)
        await asyncio.sleep(0)
        try:
            assert getattr(transport, "operational", True) is False
            await asyncio.wait_for(finished.wait(), 1)
        finally:
            await transport.close()

    asyncio.run(check())
