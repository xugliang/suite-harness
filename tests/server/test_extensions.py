from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from suiteharness.config import LoadedSuiteHarnessConfig, SuiteHarnessConfig, SuiteHarnessSecrets
from suiteharness.execution import InMemoryToolRegistry, ToolEffect
from suiteharness.mcp.protocols import HttpRequest, HttpResponse, ServerSentEvent
from suiteharness.persistence import (
    SQLiteDatabase,
    SQLiteMcpHttpResumeStore,
    SQLiteProductStateStore,
)
from suiteharness.plugins import (
    PluginRuntime,
    PreparedPlugin,
    deterministic_directory_digest,
)
from suiteharness.runtime import ProductContext, RootContext
from suiteharness.sandbox import SandboxAvailability
from suiteharness.server.extensions import (
    ExtensionConfigurationError,
    McpHostAdapters,
    PluginHostAdapters,
    ServerExtensionHost,
)


class FakeSandbox:
    backend_id = "test-sandbox"
    production_safe = True

    async def availability(self) -> SandboxAvailability:
        return SandboxAvailability(available=True, detail="ready")

    async def run(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError("the HTTP MCP test must not invoke the process sandbox")


@dataclass
class FakeActivation:
    tenant_id: str
    products: Mapping[str, ProductContext]
    root: RootContext

    @property
    def customer_bundle_id(self) -> str:
        return "acme-products"

    @property
    def closed(self) -> bool:
        return self.root.effects.closed


def _activation(*product_ids: str) -> FakeActivation:
    root = RootContext()
    tenant = root.tenant("acme")
    return FakeActivation(
        tenant_id="acme",
        products={product_id: tenant.product(product_id) for product_id in product_ids},
        root=root,
    )


def _loaded(
    tmp_path: Path,
    *,
    products: tuple[str, ...] = ("sales",),
    mcp: dict[str, object] | None = None,
    plugins: dict[str, object] | None = None,
) -> LoadedSuiteHarnessConfig:
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
                    {"product_id": product_id, "version": "==1.0.0", "config": {}}
                    for product_id in products
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
            "plugins": plugins or {},
        }
    )
    secrets = SuiteHarnessSecrets.model_validate(
        {
            "config_version": 1,
            "services": {
                "search/baidu-qianfan": {"token": "search-token"},
                "mcp/acme": {"token": "server-token"},
            },
        }
    )
    return LoadedSuiteHarnessConfig(
        config=config,
        secrets=secrets,
        config_path=tmp_path / "suiteharness.yaml",
        secrets_path=tmp_path / "suiteharness.secrets.yaml",
    )


class FakeEgress:
    async def authorize(self, endpoint: str, *, tenant_id: str, product_id: str) -> str:
        assert tenant_id == "acme"
        assert product_id == "sales"
        return endpoint


class FakeTokenProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def authorization_header(self, credential_ref: str, *, endpoint: str) -> str:
        self.calls.append((credential_ref, endpoint))
        return "Bearer company-server-token"


class FakeMcpExchange:
    def __init__(self, *, fail_tools: bool = False) -> None:
        self.fail_tools = fail_tools
        self.methods: list[str] = []
        self.request_headers: list[Mapping[str, str]] = []
        self.closed_sessions = 0

    async def send(self, request: HttpRequest) -> HttpResponse:
        if request.method == "DELETE":
            self.closed_sessions += 1
            return HttpResponse(status=204, headers={}, body=b"")
        assert request.body is not None
        self.request_headers.append(request.headers)
        message = json.loads(request.body)
        method = message.get("method", "response")
        self.methods.append(method)
        if method == "notifications/initialized":
            return HttpResponse(status=202, headers={}, body=b"")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1"},
            }
        elif method == "tools/list":
            if self.fail_tools:
                raise RuntimeError("remote tool discovery failed")
            result = {
                "tools": [
                    {
                        "name": "crm.lookup",
                        "description": "Read a CRM record",
                        "inputSchema": {"type": "object", "additionalProperties": False},
                    }
                ]
            }
        else:
            raise AssertionError(f"unexpected MCP request: {method}")
        body = json.dumps(
            {"jsonrpc": "2.0", "id": message["id"], "result": result}
        ).encode()
        return HttpResponse(
            status=200,
            headers={"content-type": "application/json", "mcp-session-id": "session-1"},
            body=body,
        )

    async def open_events(
        self, request: HttpRequest
    ) -> AsyncIterator[ServerSentEvent]:
        del request
        if False:  # pragma: no cover - makes this an async generator
            yield ServerSentEvent(data="")


def _http_mcp_config(*, resume_sessions: bool = False) -> dict[str, object]:
    return {
        "servers_by_product": {
            "sales": [
                {
                    "server_id": "crm",
                    "transport": "streamable_http",
                    "endpoint": "https://mcp.example.test/rpc",
                    "credential_ref": "mcp/acme",
                    "resume_sessions": resume_sessions,
                }
            ]
        },
        "tool_security_overrides": {
            "sales": {
                "crm": {
                    "crm.lookup": {
                        "effect": "read",
                        "rationale": "administrator audited this read-only endpoint",
                    }
                }
            }
        },
    }


def test_no_extension_config_allocates_no_managers_or_requires_adapters(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        loaded = _loaded(tmp_path)
        activation = _activation("sales")
        host = ServerExtensionHost(
            loaded,
            sandbox=FakeSandbox(),
            tools=InMemoryToolRegistry(),
            product_workspace=lambda product_id: tmp_path / product_id,
        )
        result = await host.start(activation)
        assert not host.has_configured_extensions
        assert host.plugin_manager is None
        assert host.mcp_manager is None
        assert result.plugin_ids == ()
        assert dict(result.mcp_by_product) == {}
        await host.close()
        await activation.root.close()

    asyncio.run(exercise())


def test_mcp_is_product_scoped_effect_owned_and_exposes_grant_material(
    tmp_path: Path,
) -> None:
    async def exercise() -> tuple[FakeMcpExchange, FakeTokenProvider]:
        workspace = tmp_path / "sales"
        workspace.mkdir()
        loaded = _loaded(tmp_path, mcp=_http_mcp_config())
        activation = _activation("sales")
        exchange = FakeMcpExchange()
        tokens = FakeTokenProvider()
        registry = InMemoryToolRegistry()
        host = ServerExtensionHost(
            loaded,
            sandbox=FakeSandbox(),
            tools=registry,
            product_workspace=lambda product_id: tmp_path / product_id,
            mcp_adapters=McpHostAdapters(
                egress_policy=FakeEgress(),
                http_exchange=exchange,
                token_provider=tokens,
            ),
        )
        result = await host.attach_customer_bundle(activation)
        material = result.grant_material("sales")
        assert len(material.tool_identities) == 1
        assert material.capabilities == frozenset({"mcp.crm.call"})
        identity = next(iter(material.tool_identities))
        assert identity in host.mcp_tool_identities["sales"]
        registered = registry.resolve(_request_scope("sales"), identity.name)
        assert registered is not None
        assert registered.spec.effects == frozenset({ToolEffect.READ, ToolEffect.EXTERNAL})

        # Product shutdown owns tool removal before connection shutdown.
        await activation.root.close()
        assert registry.resolve(_request_scope("sales"), identity.name) is None
        assert dict(host.mcp_tool_identities) == {}
        await host.close()
        return exchange, tokens

    exchange, tokens = asyncio.run(exercise())
    assert exchange.methods == ["initialize", "notifications/initialized", "tools/list"]
    assert tokens.calls
    assert exchange.closed_sessions == 1


def test_extension_host_reuses_durable_resume_from_injected_runtime_database(
    tmp_path: Path,
) -> None:
    async def exercise() -> tuple[FakeMcpExchange, FakeMcpExchange]:
        workspace = tmp_path / "sales"
        workspace.mkdir()
        loaded = _loaded(tmp_path, mcp=_http_mcp_config(resume_sessions=True))
        database = SQLiteDatabase((tmp_path / "state" / "runtime.sqlite3").resolve())
        resume = SQLiteMcpHttpResumeStore(SQLiteProductStateStore(database))

        first_exchange = FakeMcpExchange()
        first_activation = _activation("sales")
        first = ServerExtensionHost(
            loaded,
            sandbox=FakeSandbox(),
            tools=InMemoryToolRegistry(),
            product_workspace=lambda product_id: tmp_path / product_id,
            mcp_adapters=McpHostAdapters(
                egress_policy=FakeEgress(),
                http_exchange=first_exchange,
                token_provider=FakeTokenProvider(),
            ),
            mcp_resume_store=resume,
        )
        await first.attach_customer_bundle(first_activation)
        await first_activation.root.close()
        await first.close()
        checkpoint = await resume.load(
            tenant_id="acme",
            product_id="sales",
            server_id="crm",
            endpoint="https://mcp.example.test/rpc",
        )
        assert checkpoint is not None
        assert checkpoint.state.session_id == "session-1"

        second_exchange = FakeMcpExchange()
        second_activation = _activation("sales")
        second = ServerExtensionHost(
            loaded,
            sandbox=FakeSandbox(),
            tools=InMemoryToolRegistry(),
            product_workspace=lambda product_id: tmp_path / product_id,
            mcp_adapters=McpHostAdapters(
                egress_policy=FakeEgress(),
                http_exchange=second_exchange,
                token_provider=FakeTokenProvider(),
            ),
            mcp_resume_store=resume,
        )
        await second.attach_customer_bundle(second_activation)
        await second_activation.root.close()
        await second.close()
        await database.close()
        return first_exchange, second_exchange

    first_exchange, second_exchange = asyncio.run(exercise())
    assert first_exchange.closed_sessions == 0
    assert second_exchange.closed_sessions == 0
    assert second_exchange.request_headers[0]["Mcp-Session-Id"] == "session-1"


def _request_scope(product_id: str):  # type: ignore[no-untyped-def]
    from suiteharness.runtime import RequestScope, ScopePath

    return RequestScope(
        ScopePath.product("acme", product_id),
        "user-1",
        channel_id="web",
    )


class FakePrepared(PreparedPlugin):
    exports: Mapping[str, object] = {}

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def activate(self, context):  # type: ignore[no-untyped-def]
        del context
        self.events.append("plugin-activate")

    async def close(self) -> None:
        self.events.append("plugin-prepared-close")


class FakePluginRuntime(PluginRuntime):
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def prepare(self, context):  # type: ignore[no-untyped-def]
        del context
        self.events.append("plugin-prepare")
        return FakePrepared(self.events)

    async def close(self) -> None:
        self.events.append("plugin-runtime-close")


class FakePluginLoader:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def load(self, source):  # type: ignore[no-untyped-def]
        del source
        return FakePluginRuntime(self.events)


def _plugin_source(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "plugins" / "memory"
    root.mkdir(parents=True)
    (root / "plugin_impl.py").write_text("VALUE = 1\n", encoding="utf-8")
    digest = deterministic_directory_digest(root)
    manifest = {
        "schema_version": "1",
        "plugin_id": "memory.enterprise",
        "version": "1.0.0",
        "harness_api": ">=0.1,<0.2",
        "trust_mode": "trusted_in_process",
        "entrypoint": "plugin_impl:create",
        "artifact": {"digest": digest},
    }
    (root / "suiteharness-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root, digest


def test_combined_start_rolls_plugin_back_when_mcp_tool_discovery_fails(
    tmp_path: Path,
) -> None:
    async def exercise() -> list[str]:
        workspace = tmp_path / "sales"
        workspace.mkdir()
        plugin_root, digest = _plugin_source(tmp_path)
        loaded = _loaded(
            tmp_path,
            mcp=_http_mcp_config(),
            plugins={
                "allowed_roots": [plugin_root.parent],
                "sources": [{"path": plugin_root, "expected_digest": digest}],
                "trusted_in_process_plugins": ["memory.enterprise"],
                "require_signatures": False,
            },
        )
        activation = _activation("sales")
        events: list[str] = []
        host = ServerExtensionHost(
            loaded,
            sandbox=FakeSandbox(),
            tools=InMemoryToolRegistry(),
            product_workspace=lambda product_id: tmp_path / product_id,
            plugin_adapters=PluginHostAdapters(
                trusted_loader=FakePluginLoader(events),
                signature_verifier=None,
            ),
            mcp_adapters=McpHostAdapters(
                egress_policy=FakeEgress(),
                http_exchange=FakeMcpExchange(fail_tools=True),
                token_provider=FakeTokenProvider(),
            ),
        )
        with pytest.raises(RuntimeError, match="remote tool discovery failed"):
            await host.start(activation)
        assert host.plugin_manager is None
        assert host.mcp_manager is None
        assert events == [
            "plugin-prepare",
            "plugin-activate",
            "plugin-prepared-close",
            "plugin-runtime-close",
        ]
        await host.close()
        await activation.root.close()
        return events

    asyncio.run(exercise())


def test_missing_explicit_mcp_or_signature_adapter_fails_closed(tmp_path: Path) -> None:
    async def exercise() -> None:
        workspace = tmp_path / "sales"
        workspace.mkdir()
        activation = _activation("sales")
        mcp_host = ServerExtensionHost(
            _loaded(tmp_path, mcp=_http_mcp_config()),
            sandbox=FakeSandbox(),
            tools=InMemoryToolRegistry(),
            product_workspace=lambda product_id: tmp_path / product_id,
        )
        with pytest.raises(ExtensionConfigurationError, match="McpHostAdapters"):
            await mcp_host.attach_customer_bundle(activation)

        failing_mcp_host = ServerExtensionHost(
            _loaded(tmp_path, mcp=_http_mcp_config()),
            sandbox=FakeSandbox(),
            tools=InMemoryToolRegistry(),
            product_workspace=lambda product_id: tmp_path / product_id,
            mcp_adapters=McpHostAdapters(
                egress_policy=FakeEgress(),
                http_exchange=FakeMcpExchange(fail_tools=True),
                token_provider=FakeTokenProvider(),
            ),
        )
        with pytest.raises(RuntimeError, match="remote tool discovery failed"):
            await failing_mcp_host.attach_customer_bundle(activation)
        assert failing_mcp_host.mcp_manager is None

        plugin_root, digest = _plugin_source(tmp_path)
        plugin_host = ServerExtensionHost(
            _loaded(
                tmp_path,
                plugins={
                    "allowed_roots": [plugin_root.parent],
                    "sources": [{"path": plugin_root, "expected_digest": digest}],
                    "trusted_in_process_plugins": ["memory.enterprise"],
                    "require_signatures": True,
                },
            ),
            sandbox=FakeSandbox(),
            tools=InMemoryToolRegistry(),
            product_workspace=lambda product_id: tmp_path / product_id,
            plugin_adapters=PluginHostAdapters(
                trusted_loader=FakePluginLoader([]),
                signature_verifier=None,
            ),
        )
        with pytest.raises(ExtensionConfigurationError, match="SignatureVerifier"):
            await plugin_host.start_plugins()
        await mcp_host.close()
        await failing_mcp_host.close()
        await plugin_host.close()
        await activation.root.close()

    asyncio.run(exercise())
