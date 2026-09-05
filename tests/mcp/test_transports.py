from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from suiteharness.mcp.models import (
    HttpResumeState,
    JsonRpcResponse,
    McpProtocolError,
    McpServerConfig,
    McpTransportError,
    McpTransportKind,
)
from suiteharness.mcp.protocols import HttpResponse, ServerSentEvent
from suiteharness.mcp.transports import (
    SandboxedStdioTransport,
    SecureMcpTransportFactory,
    StreamableHttpTransport,
    decode_message,
    encode_message,
    parse_sse,
)
from suiteharness.sandbox import SandboxAvailability


async def _request_handler(request, context):  # type: ignore[no-untyped-def]
    return JsonRpcResponse(id=request.id, result={})


async def _notification_handler(notification, context):  # type: ignore[no-untyped-def]
    return None


class FakeSandbox:
    backend_id = "fake"

    def __init__(self, *, production_safe: bool, available: bool = True) -> None:
        self.production_safe = production_safe
        self._available = available

    async def availability(self):  # type: ignore[no-untyped-def]
        return SandboxAvailability(available=self._available, detail="test")

    async def run(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError("persistent stdio must use the sandbox session factory")


class FakeSession:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def send_line(self, payload):  # type: ignore[no-untyped-def]
        self.sent.append(payload)
        message = decode_message(payload)
        if hasattr(message, "id") and hasattr(message, "method"):
            await self.incoming.put(
                encode_message(JsonRpcResponse(id=message.id, result={"ok": True}))
            )

    async def receive_line(self):  # type: ignore[no-untyped-def]
        return await self.incoming.get()

    async def close(self):  # type: ignore[no-untyped-def]
        self.closed = True
        await self.incoming.put(None)


class FakeSessionFactory:
    def __init__(self, session: FakeSession | None = None) -> None:
        self.session = session or FakeSession()
        self.calls = []

    async def open(self, config, *, sandbox, product_workspace):  # type: ignore[no-untyped-def]
        self.calls.append((config, sandbox, product_workspace))
        return self.session


def test_stdio_requires_available_production_sandbox(tmp_path: Path) -> None:
    async def exercise() -> None:
        config = McpServerConfig(
            server_id="local",
            transport=McpTransportKind.STDIO,
            command="server",
        )
        factory = FakeSessionFactory()
        transport = SandboxedStdioTransport(
            config,
            sandbox=FakeSandbox(production_safe=False),
            session_factory=factory,
            product_workspace=tmp_path,
        )
        with pytest.raises(McpTransportError, match="production-safe"):
            await transport.start(_request_handler, _notification_handler)
        assert factory.calls == []

    asyncio.run(exercise())


def test_stdio_correlates_request_without_host_subprocess(tmp_path: Path) -> None:
    async def exercise() -> None:
        config = McpServerConfig(
            server_id="local",
            transport=McpTransportKind.STDIO,
            command="server",
        )
        factory = FakeSessionFactory()
        sandbox = FakeSandbox(production_safe=True)
        transport = SandboxedStdioTransport(
            config,
            sandbox=sandbox,
            session_factory=factory,
            product_workspace=tmp_path,
        )
        await transport.start(_request_handler, _notification_handler)
        assert await transport.request("ping", {}, context=None, timeout_seconds=1) == {
            "ok": True
        }
        assert factory.calls[0][1] is sandbox
        assert factory.calls[0][2] == tmp_path.resolve()
        await transport.close()
        assert factory.session.closed

    asyncio.run(exercise())


def test_stdio_revalidates_received_line_size(tmp_path: Path) -> None:
    class OversizedResponseSession(FakeSession):
        async def send_line(self, payload):  # type: ignore[no-untyped-def]
            self.sent.append(payload)
            await self.incoming.put(b"x" * 1_025)

    async def exercise() -> None:
        config = McpServerConfig(
            server_id="local",
            transport=McpTransportKind.STDIO,
            command="server",
            max_message_bytes=1_024,
        )
        factory = FakeSessionFactory(OversizedResponseSession())
        transport = SandboxedStdioTransport(
            config,
            sandbox=FakeSandbox(production_safe=True),
            session_factory=factory,
            product_workspace=tmp_path,
        )
        await transport.start(_request_handler, _notification_handler)
        with pytest.raises(McpProtocolError, match="exceeds max_message_bytes"):
            await transport.request("ping", {}, context=None, timeout_seconds=1)
        with pytest.raises(McpTransportError, match="reader previously failed"):
            await transport.request("ping", {}, context=None, timeout_seconds=1)
        await transport.close()

    asyncio.run(exercise())


class FakeEgress:
    def __init__(self) -> None:
        self.calls = []

    async def authorize(self, endpoint, *, tenant_id, product_id):  # type: ignore[no-untyped-def]
        self.calls.append((endpoint, tenant_id, product_id))
        return endpoint


class FakeHttp:
    def __init__(self, *, accepted: bool = False) -> None:
        self.requests = []
        self.accepted = accepted

    async def send(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        if request.method == "DELETE":
            return HttpResponse(status=204, headers={}, body=b"")
        message = decode_message(request.body or b"")
        if self.accepted:
            return HttpResponse(
                status=202,
                headers={"Mcp-Session-Id": "session-1"},
                body=b"",
            )
        return HttpResponse(
            status=200,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Mcp-Session-Id": "session-1",
            },
            body=encode_message(JsonRpcResponse(id=message.id, result={"pong": True})),
        )

    async def _events(self, request):  # type: ignore[no-untyped-def]
        yield ServerSentEvent(
            event_id="evt-9",
            data='{"jsonrpc":"2.0","id":"suiteharness-1","result":{"pong":true}}',
        )

    def open_events(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return self._events(request)


class StaticHttp:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.requests = []

    async def send(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return self.response

    def open_events(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError(f"unexpected event stream: {request}")


class StreamingHttp:
    def __init__(self, events: tuple[ServerSentEvent, ...]) -> None:
        self.events = events
        self.requests = []

    async def send(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return HttpResponse(status=202, headers={}, body=b"")

    async def _events(self, request):  # type: ignore[no-untyped-def]
        del request
        for event in self.events:
            yield event

    def open_events(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return self._events(request)


def test_streamable_http_requires_egress_and_tracks_resume_state() -> None:
    async def exercise() -> None:
        config = McpServerConfig(
            server_id="remote",
            transport=McpTransportKind.STREAMABLE_HTTP,
            endpoint="https://mcp.example.com/rpc",
        )
        egress = FakeEgress()
        exchange = FakeHttp(accepted=True)
        transport = StreamableHttpTransport(
            config,
            tenant_id="tenant-a",
            product_id="product-a",
            egress_policy=egress,
            exchange=exchange,
        )
        await transport.start(_request_handler, _notification_handler)
        assert await transport.request("ping", {}, context=None, timeout_seconds=1) == {
            "pong": True
        }
        assert egress.calls == [
            ("https://mcp.example.com/rpc", "tenant-a", "product-a")
        ]
        assert transport.resume_state.session_id == "session-1"
        assert transport.resume_state.last_event_id == "evt-9"
        event_get = exchange.requests[1]
        assert event_get.method == "GET"
        await transport.close()

    asyncio.run(exercise())


def test_streamable_http_rejects_endpoint_rewrite_before_resume_headers_leave() -> None:
    class RewritingEgress:
        async def authorize(  # type: ignore[no-untyped-def]
            self, endpoint, *, tenant_id, product_id
        ):
            del endpoint, tenant_id, product_id
            return "https://other.example.com/rpc"

    async def exercise() -> None:
        config = McpServerConfig(
            server_id="remote",
            transport=McpTransportKind.STREAMABLE_HTTP,
            endpoint="https://mcp.example.com/rpc",
        )
        exchange = FakeHttp()
        transport = StreamableHttpTransport(
            config,
            tenant_id="tenant-a",
            product_id="product-a",
            egress_policy=RewritingEgress(),
            exchange=exchange,
            resume=HttpResumeState(session_id="endpoint-bound-session"),
        )
        with pytest.raises(McpTransportError, match="must not rewrite"):
            await transport.start(_request_handler, _notification_handler)
        assert exchange.requests == []

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (
            HttpResponse(
                status=200,
                headers={"Content-Type": "application/json"},
                body=b"x" * 1_025,
            ),
            "body exceeds max_message_bytes",
        ),
        (
            HttpResponse(
                status=200,
                headers={"Content-Type": "application/json", "X-Large": "x" * 65_537},
                body=b"{}",
            ),
            "headers exceed the byte limit",
        ),
        (
            HttpResponse(
                status=200,
                headers={"Content-Type": "application/json", "content-type": "application/json"},
                body=b"{}",
            ),
            "duplicate headers",
        ),
    ],
)
def test_streamable_http_revalidates_response_bounds(
    response: HttpResponse,
    error: str,
) -> None:
    async def exercise() -> None:
        config = McpServerConfig(
            server_id="remote",
            transport=McpTransportKind.STREAMABLE_HTTP,
            endpoint="https://mcp.example.com/rpc",
            max_message_bytes=1_024,
        )
        transport = StreamableHttpTransport(
            config,
            tenant_id="tenant-a",
            product_id="product-a",
            egress_policy=FakeEgress(),
            exchange=StaticHttp(response),
        )
        await transport.start(_request_handler, _notification_handler)
        with pytest.raises(McpProtocolError, match=error):
            await transport.request("ping", {}, context=None, timeout_seconds=1)
        await transport.close()

    asyncio.run(exercise())


def test_sse_parser_enforces_payload_and_event_bounds() -> None:
    with pytest.raises(McpProtocolError, match="payload exceeds max_message_bytes"):
        parse_sse(b"x" * 1_025, max_message_bytes=1_024)

    payload = "data: {\"jsonrpc\":\"2.0\",\"method\":\"one\"}\n\n" * 2
    with pytest.raises(McpProtocolError, match="exceeds max_stream_events"):
        parse_sse(payload, max_message_bytes=1_024, max_events=1)


def test_streamable_http_limits_live_sse_event_count() -> None:
    async def exercise() -> None:
        event = ServerSentEvent(data='{"jsonrpc":"2.0","method":"progress"}')
        exchange = StreamingHttp((event, event))
        config = McpServerConfig(
            server_id="remote",
            transport=McpTransportKind.STREAMABLE_HTTP,
            endpoint="https://mcp.example.com/rpc",
            max_stream_events=1,
        )
        transport = StreamableHttpTransport(
            config,
            tenant_id="tenant-a",
            product_id="product-a",
            egress_policy=FakeEgress(),
            exchange=exchange,
        )
        await transport.start(_request_handler, _notification_handler)
        with pytest.raises(McpProtocolError, match="exceeds max_stream_events"):
            await transport.request("ping", {}, context=None, timeout_seconds=1)
        await transport.close()

    asyncio.run(exercise())


def test_streamable_http_revalidates_live_sse_event_data_size() -> None:
    async def exercise() -> None:
        exchange = StreamingHttp((ServerSentEvent(data="x" * 1_025),))
        config = McpServerConfig(
            server_id="remote",
            transport=McpTransportKind.STREAMABLE_HTTP,
            endpoint="https://mcp.example.com/rpc",
            max_message_bytes=1_024,
        )
        transport = StreamableHttpTransport(
            config,
            tenant_id="tenant-a",
            product_id="product-a",
            egress_policy=FakeEgress(),
            exchange=exchange,
        )
        await transport.start(_request_handler, _notification_handler)
        with pytest.raises(McpProtocolError, match="exceeds max_message_bytes"):
            await transport.request("ping", {}, context=None, timeout_seconds=1)
        await transport.close()

    asyncio.run(exercise())


def test_secure_factory_fails_closed_when_dependencies_are_missing(tmp_path: Path) -> None:
    stdio = McpServerConfig(
        server_id="local",
        transport=McpTransportKind.STDIO,
        command="server",
    )
    remote = McpServerConfig(
        server_id="remote",
        transport=McpTransportKind.STREAMABLE_HTTP,
        endpoint="https://mcp.example.com/rpc",
    )
    factory = SecureMcpTransportFactory()
    with pytest.raises(McpTransportError, match="SandboxBackend"):
        factory.create(
            stdio,
            tenant_id="tenant-a",
            product_id="product-a",
            product_workspace=tmp_path,
        )
    with pytest.raises(McpTransportError, match="EgressPolicy"):
        factory.create(
            remote,
            tenant_id="tenant-a",
            product_id="product-a",
            product_workspace=tmp_path,
        )
