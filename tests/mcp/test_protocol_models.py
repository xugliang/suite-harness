from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from suiteharness.mcp.models import (
    ElicitationParams,
    JsonRpcErrorObject,
    JsonRpcRequest,
    JsonRpcResponse,
    LegacySseCompatibility,
    McpFeatureFlags,
    McpServerConfig,
    McpTransportKind,
    ProductMcpConfig,
    ServerCapabilities,
)
from suiteharness.mcp.transports import decode_message, encode_message, parse_sse


def test_json_rpc_preserves_successful_null_and_rejects_batch() -> None:
    encoded = encode_message(JsonRpcResponse(id="r1", result=None))
    assert encoded == b'{"jsonrpc":"2.0","id":"r1","result":null}'
    decoded = decode_message(encoded)
    assert isinstance(decoded, JsonRpcResponse)
    assert decoded.result is None
    assert decoded.error is None

    with pytest.raises(ValueError):
        JsonRpcResponse(id="r1")
    with pytest.raises(ValueError):
        JsonRpcResponse(
            id="r1",
            result={},
            error=JsonRpcErrorObject(code=-32603, message="bad"),
        )
    with pytest.raises(Exception, match="batch"):
        decode_message(b"[]")


def test_json_rpc_request_rejects_boolean_id_and_bad_method() -> None:
    with pytest.raises(ValidationError):
        JsonRpcRequest(id=True, method="ping")
    with pytest.raises(ValidationError):
        JsonRpcRequest(id=1, method="bad method")


def test_official_capability_names_round_trip() -> None:
    capabilities = ServerCapabilities.model_validate(
        {
            "tools": {"listChanged": True},
            "resources": {"subscribe": True, "listChanged": True},
            "prompts": {"listChanged": True},
            "logging": {},
            "completions": {},
            "tasks": {"list": True, "cancel": True, "requests": {"tools/call": {}}},
        }
    )
    wire = capabilities.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert wire["tools"] == {"listChanged": True}
    assert wire["resources"] == {"listChanged": True, "subscribe": True}


def test_server_config_closes_transport_ambiguity() -> None:
    stdio = McpServerConfig(
        server_id="local-files",
        transport=McpTransportKind.STDIO,
        command="mcp-server",
    )
    assert stdio.require_production_sandbox is True

    with pytest.raises(ValidationError, match="forbids endpoint"):
        McpServerConfig(
            server_id="bad",
            transport=McpTransportKind.STDIO,
            command="server",
            endpoint="https://example.com/mcp",
        )
    with pytest.raises(ValidationError, match="absolute HTTPS"):
        McpServerConfig(
            server_id="remote",
            transport=McpTransportKind.STREAMABLE_HTTP,
            endpoint="http://127.0.0.1/mcp",
        )
    with pytest.raises(ValidationError, match="2025-11-25"):
        McpServerConfig(
            server_id="legacy",
            transport=McpTransportKind.STREAMABLE_HTTP,
            endpoint="https://example.com/mcp",
            legacy_sse=LegacySseCompatibility.ADAPTER_REQUIRED,
            protocol_versions=("2024-11-05",),
        )


def test_server_config_strictly_bounds_transport_inputs() -> None:
    config = McpServerConfig(
        server_id="remote",
        transport=McpTransportKind.STREAMABLE_HTTP,
        endpoint="https://example.com/mcp",
        max_message_bytes=2_048,
        max_stream_events=4,
        max_list_pages=3,
        max_list_items=20,
    )
    assert config.max_message_bytes == 2_048
    assert config.max_stream_events == 4
    assert config.max_list_pages == 3
    assert config.max_list_items == 20

    for field, value in (
        ("max_message_bytes", 1_023),
        ("max_message_bytes", True),
        ("max_stream_events", 0),
        ("max_stream_events", "4"),
        ("max_list_pages", 0),
        ("max_list_pages", True),
        ("max_list_pages", 1_001),
        ("max_list_items", 100_001),
    ):
        with pytest.raises(ValidationError):
            McpServerConfig(
                server_id="remote",
                transport=McpTransportKind.STREAMABLE_HTTP,
                endpoint="https://example.com/mcp",
                **{field: value},
            )


def test_product_config_requires_real_isolated_workspace(tmp_path: Path) -> None:
    product = tmp_path / "product"
    product.mkdir()
    config = ProductMcpConfig(
        tenant_id="tenant-a",
        product_id="product-a",
        product_workspace=product,
        servers=(
            McpServerConfig(
                server_id="remote",
                transport=McpTransportKind.STREAMABLE_HTTP,
                endpoint="https://example.com/mcp",
                features=McpFeatureFlags(experimental_tasks=True),
            ),
        ),
    )
    assert config.product_workspace == product.resolve()

    with pytest.raises(ValidationError, match="unique"):
        ProductMcpConfig(
            tenant_id="tenant-a",
            product_id="product-a",
            product_workspace=product,
            servers=(config.servers[0], config.servers[0]),
        )


def test_sse_parser_tracks_event_id_and_multiline_data() -> None:
    events = parse_sse(
        "id: 41\nevent: message\ndata: {\"jsonrpc\":\"2.0\",\ndata: \"id\":1}\n\n"
    )
    assert len(events) == 1
    assert events[0].event_id == "41"
    assert events[0].data == '{"jsonrpc":"2.0",\n"id":1}'


def test_elicitation_form_and_url_are_mutually_exclusive() -> None:
    ElicitationParams(message="name", requestedSchema={"type": "object", "properties": {}})
    ElicitationParams(
        mode="url",
        message="login",
        url="https://id.example.com/start",
        elicitationId="login-1",
    )
    with pytest.raises(ValidationError):
        ElicitationParams(
            message="bad",
            requestedSchema={"type": "object", "properties": {}},
            url="https://x",
        )
