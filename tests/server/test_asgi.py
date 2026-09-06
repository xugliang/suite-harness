from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from suiteharness.channels import AuthenticatedPrincipal
from suiteharness.config import SuiteHarnessConfigLoader
from suiteharness.execution import ToolCallContext
from suiteharness.memory import PROFILE_PROVIDER, InMemoryProfileProvider
from suiteharness.runtime import (
    PreparedProduct,
    ProductCatalog,
    ProductDescriptor,
    RequestScope,
    ScopePath,
)
from suiteharness.sandbox import ProcessResult, SandboxUnavailable
from suiteharness.server import (
    CompanyHttpAuthenticationRequest,
    CompanyServerApplication,
    CompanyServerBootstrap,
    CompanyServerStartupError,
    ToolAccessTemplate,
    create_company_asgi_app,
)
from suiteharness.web import SearchRequest, SearchResponse, WebPolicyDenied

TestClient = pytest.importorskip("starlette.testclient").TestClient


class EmptyProductConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelTransport:
    async def send(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError("model transport should not be called")

    def stream(self, request) -> AsyncIterator[bytes]:  # type: ignore[no-untyped-def]
        raise AssertionError("model transport should not be called")


class ProcessTransport:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available

    async def probe(self, argv, *, timeout_seconds):  # type: ignore[no-untyped-def]
        del argv, timeout_seconds
        return ProcessResult(0 if self.available else 1, b"", b"")

    async def execute(
        self,
        argv: tuple[str, ...],
        *,
        working_directory: Path | None,
        environment: Mapping[str, str],
        stdin: bytes | None,
        timeout_seconds: float,
        output_bytes: int,
    ) -> ProcessResult:
        del argv, working_directory, environment, stdin, timeout_seconds, output_bytes
        return ProcessResult(0, b"", b"")


class ProductActivator:
    def __init__(self, descriptor: ProductDescriptor) -> None:
        self.descriptor = descriptor

    async def prepare(self, product):  # type: ignore[no-untyped-def]
        return PreparedProduct(
            descriptor=self.descriptor,
            config=product.config,
            bindings={PROFILE_PROVIDER: InMemoryProfileProvider()},
        )


class CompanyAuthenticator:
    def __init__(self) -> None:
        self.requests: list[CompanyHttpAuthenticationRequest] = []

    async def authenticate(
        self,
        request: CompanyHttpAuthenticationRequest,
    ) -> AuthenticatedPrincipal | None:
        self.requests.append(request)
        if request.header("authorization") != "Bearer company-sso":
            return None
        return AuthenticatedPrincipal(
            tenant_id="acme",
            principal_id="alice",
            roles=frozenset({"employee"}),
        )


class HangingCompanyAuthenticator(CompanyAuthenticator):
    async def authenticate(
        self,
        request: CompanyHttpAuthenticationRequest,
    ) -> AuthenticatedPrincipal | None:
        self.requests.append(request)
        await asyncio.Event().wait()


class MutableWebSessionRevalidator:
    def __init__(self) -> None:
        self.current: AuthenticatedPrincipal | None = AuthenticatedPrincipal(
            tenant_id="acme",
            principal_id="alice",
            roles=frozenset({"employee"}),
        )
        self.calls = 0

    async def revalidate(
        self,
        handshake,  # type: ignore[no-untyped-def]
        established_principal: AuthenticatedPrincipal,
    ) -> AuthenticatedPrincipal | None:
        del handshake
        self.calls += 1
        assert established_principal.principal_id == "alice"
        return self.current


class FoundryClient:
    async def grounded_search(self, query: str, *, count: int):  # type: ignore[no-untyped-def]
        return [{"title": query, "url": "https://example.com", "snippet": str(count)}]


class SearchProvider:
    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id

    async def search(self, request: SearchRequest) -> SearchResponse:
        return SearchResponse(provider_id=self.provider_id, query=request.query, results=())


def _configuration(
    tmp_path: Path,
    *,
    authentication_timeout_seconds: float = 10.0,
    include_search_secret: bool = True,
    search_token: str = "token",
) -> tuple[Path, Path]:
    config = {
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
                {"product_id": "product-a", "version": "==1.0.0", "config": {}}
            ],
        },
        "workspace": {"root": str(tmp_path / "workspace")},
        "storage": {"root": str(tmp_path / "state")},
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
        "channels": {
            "web": {
                "enabled": True,
                "allowed_origins": ["https://portal.example.cn"],
                "authentication_timeout_seconds": authentication_timeout_seconds,
                "credentials_ref": "web/default",
            },
            "feishu": {"enabled": False},
            "agent_ids": {"product-a": "assistant"},
        },
    }
    secrets = {
        "config_version": 1,
        "web": {"web/default": {"session_signing_key": "w" * 32}},
    }
    if include_search_secret:
        secrets["services"] = {"search/baidu-qianfan": {"token": search_token}}
    config_path = tmp_path / "suiteharness.yaml"
    secrets_path = tmp_path / "suiteharness.secrets.yaml"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    secrets_path.write_text(json.dumps(secrets), encoding="utf-8")
    if os.name != "nt":
        secrets_path.chmod(0o600)
    return config_path, secrets_path


def _bootstrap(
    tmp_path: Path,
    process: ProcessTransport,
    *,
    authenticator: CompanyAuthenticator | None = None,
    session_revalidator: MutableWebSessionRevalidator | None = None,
    authentication_timeout_seconds: float = 10.0,
    search_access: str | None = None,
    include_search_secret: bool = True,
    search_token: str = "token",
) -> tuple[CompanyServerBootstrap, CompanyAuthenticator]:
    descriptor = ProductDescriptor(
        product_id="product-a",
        version="1.0.0",
        harness_api=">=0.1,<0.2",
        config_model=EmptyProductConfig,
    )
    authenticator = authenticator or CompanyAuthenticator()
    config_path, secrets_path = _configuration(
        tmp_path,
        authentication_timeout_seconds=authentication_timeout_seconds,
        include_search_secret=include_search_secret,
        search_token=search_token,
    )
    bootstrap = CompanyServerBootstrap.from_files(
        config_path,
        secrets_path,
        product_catalog=ProductCatalog([descriptor]),
        product_activators=(ProductActivator(descriptor),),
        grant_templates=(
            ToolAccessTemplate(
                channel_id="web",
                product_id="product-a",
                read_aliases=("suiteharness.web.search",) if search_access == "read" else (),
                write_aliases=("suiteharness.web.search",) if search_access == "write" else (),
                destructive_aliases=("suiteharness.web.search",)
                if search_access == "destructive"
                else (),
            ),
        ),
        web_authenticator=authenticator,
        web_session_revalidator=session_revalidator,
        model_transport=ModelTransport(),  # type: ignore[arg-type]
        sandbox_transport=process,
    )
    return bootstrap, authenticator


def _dual_product_search_bootstrap(
    tmp_path: Path,
    *,
    include_baidu_secret: bool = True,
    include_foundry_client: bool = True,
    foundry_client: object | None = None,
) -> CompanyServerBootstrap:
    config_path, secrets_path = _configuration(
        tmp_path,
        include_search_secret=include_baidu_secret,
    )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["customer_bundle"]["products"].append(
        {"product_id": "product-b", "version": "==1.0.0", "config": {}}
    )
    config["web_tools"]["search"]["providers"].append(
        {
            "kind": "microsoft_foundry",
        }
    )
    config["web_tools"]["search_providers_by_product"] = {
        "product-a": ["baidu-qianfan"],
        "product-b": ["microsoft-foundry-bing-grounding"],
    }
    config["channels"]["agent_ids"]["product-b"] = "assistant"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    descriptors = tuple(
        ProductDescriptor(
            product_id=product_id,
            version="1.0.0",
            harness_api=">=0.1,<0.2",
            config_model=EmptyProductConfig,
        )
        for product_id in ("product-a", "product-b")
    )
    return CompanyServerBootstrap.from_files(
        config_path,
        secrets_path,
        product_catalog=ProductCatalog(descriptors),
        product_activators=tuple(ProductActivator(item) for item in descriptors),
        grant_templates=tuple(
            ToolAccessTemplate(
                channel_id="web",
                product_id=product_id,
                read_aliases=("suiteharness.web.search",),
            )
            for product_id in ("product-a", "product-b")
        ),
        web_authenticator=CompanyAuthenticator(),
        model_transport=ModelTransport(),  # type: ignore[arg-type]
        sandbox_transport=ProcessTransport(),
        foundry_clients=(
            {}
            if not include_foundry_client
            else {
                "microsoft-foundry-bing-grounding": (
                    FoundryClient() if foundry_client is None else foundry_client
                )
            }
        ),  # type: ignore[arg-type]
    )


def test_dual_product_bootstrap_loads_authorized_search_union_and_isolates_egress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: list[str] = []

    def provider(provider_id: str) -> SearchProvider:
        constructed.append(provider_id)
        return SearchProvider(provider_id)

    monkeypatch.setattr(
        "suiteharness.server.foundation.BaiduQianfanSearchProvider",
        lambda *_args, **_kwargs: provider("baidu-qianfan"),
    )
    monkeypatch.setattr(
        "suiteharness.server.foundation.MicrosoftFoundryGroundingProvider",
        lambda *_args, **_kwargs: provider("microsoft-foundry-bing-grounding"),
    )
    bootstrap = _dual_product_search_bootstrap(tmp_path)

    async def exercise() -> None:
        runtime = await bootstrap.build()
        try:
            for provider_id in (
                "baidu-qianfan",
                "microsoft-foundry-bing-grounding",
            ):
                response = await runtime.foundation.web_tools.search.search(
                    SearchRequest(provider_id),
                    provider_id=provider_id,
                )
                assert response.provider_id == provider_id

            def context(product_id: str) -> ToolCallContext:
                scope = RequestScope(
                    ScopePath.agent("acme", product_id, "assistant", "session-1"),
                    "user-1",
                    channel_id="web",
                )
                registration = runtime.foundation.tools.resolve(
                    scope,
                    "suiteharness.web.search",
                )
                assert registration is not None
                return ToolCallContext(
                    scope=scope,
                    run_id="run-1",
                    call_id=f"call-{product_id}",
                    tool_identity=registration.identity,
                    tool=registration.spec,
                    remaining_seconds=10,
                )

            product_a = context("product-a")
            product_b = context("product-b")
            policy = runtime.foundation.egress_policy
            assert policy.authorize_search(product_a, "baidu-qianfan") == "baidu-qianfan"
            assert (
                policy.authorize_search(product_b, "microsoft-foundry-bing-grounding")
                == "microsoft-foundry-bing-grounding"
            )
            with pytest.raises(PermissionError, match="not allowed"):
                policy.authorize_search(product_a, "microsoft-foundry-bing-grounding")
            with pytest.raises(PermissionError, match="not allowed"):
                policy.authorize_search(product_b, "baidu-qianfan")
        finally:
            await runtime.close()

    asyncio.run(exercise())
    assert set(constructed) == {
        "baidu-qianfan",
        "microsoft-foundry-bing-grounding",
    }


def test_dual_product_bootstrap_fails_when_one_authorized_search_secret_is_missing(
    tmp_path: Path,
) -> None:
    bootstrap = _dual_product_search_bootstrap(
        tmp_path,
        include_baidu_secret=False,
    )

    async def exercise() -> None:
        with pytest.raises(ValueError, match="service credentials_ref"):
            await bootstrap.build()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("include_foundry_client", "foundry_client", "expected_error", "message"),
    [
        (False, None, ValueError, "has no client adapter"),
        (True, object(), TypeError, "must implement callable grounded_search"),
    ],
)
def test_authorized_foundry_requires_valid_prebound_client(
    tmp_path: Path,
    include_foundry_client: bool,
    foundry_client: object | None,
    expected_error: type[Exception],
    message: str,
) -> None:
    bootstrap = _dual_product_search_bootstrap(
        tmp_path,
        include_foundry_client=include_foundry_client,
        foundry_client=foundry_client,
    )

    async def exercise() -> None:
        with pytest.raises(expected_error, match=message):
            await bootstrap.build()

    asyncio.run(exercise())


def test_ungranted_search_provider_is_not_initialized_without_its_secret(
    tmp_path: Path,
) -> None:
    bootstrap, _authenticator = _bootstrap(
        tmp_path,
        ProcessTransport(),
        include_search_secret=False,
    )

    async def exercise() -> None:
        runtime = await bootstrap.build()
        try:
            with pytest.raises(WebPolicyDenied, match="not enabled"):
                await runtime.foundation.web_tools.search.search(SearchRequest("query"))
        finally:
            await runtime.close()

    asyncio.run(exercise())


def test_granted_search_provider_still_requires_its_secret_at_startup(
    tmp_path: Path,
) -> None:
    bootstrap, _authenticator = _bootstrap(
        tmp_path,
        ProcessTransport(),
        search_access="read",
        include_search_secret=False,
    )

    async def exercise() -> None:
        with pytest.raises(ValueError, match="service credentials_ref"):
            await bootstrap.build()

    asyncio.run(exercise())


@pytest.mark.parametrize("search_token", ["", " \t "])
def test_granted_search_provider_rejects_blank_token_at_startup(
    tmp_path: Path,
    search_token: str,
) -> None:
    bootstrap, _authenticator = _bootstrap(
        tmp_path,
        ProcessTransport(),
        search_access="read",
        search_token=search_token,
    )

    async def exercise() -> None:
        with pytest.raises(ValueError, match="search token.*missing from the secrets file"):
            await bootstrap.build()

    asyncio.run(exercise())


@pytest.mark.parametrize("search_token", ["", " \t "])
def test_ungranted_search_provider_ignores_blank_token(
    tmp_path: Path,
    search_token: str,
) -> None:
    bootstrap, _authenticator = _bootstrap(
        tmp_path,
        ProcessTransport(),
        search_token=search_token,
    )

    async def exercise() -> None:
        runtime = await bootstrap.build()
        await runtime.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("search_access", ["write", "destructive"])
def test_search_provider_rejects_nonread_grant_categories_at_startup(
    tmp_path: Path,
    search_access: str,
) -> None:
    bootstrap, _authenticator = _bootstrap(
        tmp_path,
        ProcessTransport(),
        search_access=search_access,
    )

    async def exercise() -> None:
        with pytest.raises(CompanyServerStartupError, match="only in read_aliases"):
            await bootstrap.build()

    asyncio.run(exercise())


def test_company_asgi_lifespan_sso_health_and_websocket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe_clock = [100.0]
    monkeypatch.setattr(
        "suiteharness.sandbox.docker.monotonic", lambda: probe_clock[0]
    )
    process = ProcessTransport()
    bootstrap, authenticator = _bootstrap(tmp_path, process)
    application = create_company_asgi_app(bootstrap)

    with TestClient(application) as client:
        assert client.get("/health/live").json() == {"status": "live"}
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"

        assert client.post("/auth/session").status_code == 403
        preflight = client.options(
            "/auth/session",
            headers={
                "Origin": "https://portal.example.cn",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert preflight.status_code == 204
        assert preflight.headers["access-control-allow-origin"] == (
            "https://portal.example.cn"
        )
        oversized = client.post(
            "/auth/session",
            content=b"credentials-do-not-belong-here",
            headers={
                "Origin": "https://portal.example.cn",
                "Authorization": "Bearer company-sso",
            },
        )
        assert oversized.status_code == 413
        denied = client.post(
            "/auth/session",
            headers={
                "Origin": "https://portal.example.cn",
                "Authorization": "Bearer wrong",
            },
        )
        assert denied.status_code == 401
        issued = client.post(
            "/auth/session",
            headers={
                "Origin": "https://portal.example.cn",
                "Authorization": "Bearer company-sso",
            },
        )
        assert issued.status_code == 200
        assert issued.headers["cache-control"] == "no-store"
        token = issued.json()["access_token"]
        assert authenticator.requests[-1].client_host == "testclient"

        with client.websocket_connect(
            "/ws",
            headers={
                "Origin": "https://portal.example.cn",
                "Authorization": f"Bearer {token}",
            },
            subprotocols=["suiteharness.v1"],
        ) as socket:
            socket.send_json({"type": "ping", "nonce": "n-1"})
            assert socket.receive_json() == {"type": "pong", "nonce": "n-1"}

        process.available = False
        probe_clock[0] += 3.0
        degraded = client.get("/health/ready")
        assert degraded.status_code == 503
        assert degraded.json()["checks"]["sandbox"] == "failed"
        owned_runtime = application.runtime
        assert owned_runtime is not None

    assert application.runtime is None
    assert owned_runtime.closed


def test_browser_websocket_uses_https_cookie_without_authorization_header(tmp_path: Path) -> None:
    from starlette.websockets import WebSocketDisconnect

    bootstrap, _authenticator = _bootstrap(tmp_path, ProcessTransport())
    application = create_company_asgi_app(bootstrap)
    with TestClient(application, base_url="https://portal.example.cn") as client:
        issued = client.post("/auth/session", headers={
            "Origin": "https://portal.example.cn", "Authorization": "Bearer company-sso",
        })
        assert issued.status_code == 200
        cookie = issued.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie
        assert "SameSite=strict" in cookie and "Max-Age=300" in cookie
        assert "Path=/ws" in cookie and "Domain=" not in cookie
        # Browser WebSocket constructors cannot attach an Authorization header.
        # The client must automatically select the host/path/secure cookie instead.
        with client.websocket_connect(
            "wss://portal.example.cn/ws", headers={"Origin": "https://portal.example.cn"},
            subprotocols=["suiteharness.v1"],
        ) as socket:
            socket.send_json({"type": "ping", "nonce": "browser-cookie"})
            assert socket.receive_json() == {"type": "pong", "nonce": "browser-cookie"}
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect(
                "wss://portal.example.cn/ws", headers={"Origin": "https://foreign.invalid"},
            ):
                pytest.fail("cross-origin cookie connection was accepted")
        assert denied.value.code == 4403


def test_bootstrap_injected_session_revalidator_revokes_an_open_socket(
    tmp_path: Path,
) -> None:
    from starlette.websockets import WebSocketDisconnect

    revalidator = MutableWebSessionRevalidator()
    bootstrap, _authenticator = _bootstrap(
        tmp_path,
        ProcessTransport(),
        session_revalidator=revalidator,
    )
    application = create_company_asgi_app(bootstrap)
    with TestClient(application, base_url="https://portal.example.cn") as client:
        issued = client.post(
            "/auth/session",
            headers={
                "Origin": "https://portal.example.cn",
                "Authorization": "Bearer company-sso",
            },
        )
        token = issued.json()["access_token"]
        with client.websocket_connect(
            "/ws",
            headers={
                "Origin": "https://portal.example.cn",
                "Authorization": f"Bearer {token}",
            },
            subprotocols=["suiteharness.v1"],
        ) as socket:
            socket.send_json({"type": "ping", "nonce": "active"})
            assert socket.receive_json() == {"type": "pong", "nonce": "active"}
            revalidator.current = None
            socket.send_json({"type": "ping", "nonce": "revoked"})
            with pytest.raises(WebSocketDisconnect) as denied:
                socket.receive_json()
            assert denied.value.code == 4401
    assert revalidator.calls == 2


def test_browser_cookie_is_not_sent_over_plain_websocket_or_as_query_token(tmp_path: Path) -> None:
    from starlette.websockets import WebSocketDisconnect

    bootstrap, _authenticator = _bootstrap(tmp_path, ProcessTransport())
    application = create_company_asgi_app(bootstrap)
    with TestClient(application, base_url="https://portal.example.cn") as client:
        issued = client.post("/auth/session", headers={
            "Origin": "https://portal.example.cn", "Authorization": "Bearer company-sso",
        })
        token = issued.json()["access_token"]
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect(
                f"ws://portal.example.cn/ws?token={token}",
                headers={"Origin": "https://portal.example.cn"},
            ):
                pytest.fail("an insecure URL token was accepted")
        assert denied.value.code == 4401
def test_company_asgi_startup_fails_closed_when_sandbox_is_unavailable(
    tmp_path: Path,
) -> None:
    bootstrap, _authenticator = _bootstrap(tmp_path, ProcessTransport(available=False))
    application = create_company_asgi_app(bootstrap)

    with pytest.raises(SandboxUnavailable):
        with TestClient(application):
            pass
    assert application.runtime is None


def test_channel_paths_cannot_shadow_health_or_each_other(tmp_path: Path) -> None:
    config_path, secrets_path = _configuration(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["channels"]["web"]["session_path"] = "/health/ready"
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="health endpoints"):
        SuiteHarnessConfigLoader().load(config_path, secrets_path)


def test_company_http_authentication_request_rejects_duplicate_credentials() -> None:
    with pytest.raises(ValueError, match="duplicate security-sensitive"):
        CompanyHttpAuthenticationRequest(
            method="POST",
            path="/auth/session",
            origin="https://portal.example.cn",
            headers=(
                ("Authorization", "Bearer first"),
                ("authorization", "Bearer second"),
            ),
        )


def test_bodyless_authentication_checks_the_actual_asgi_body_event() -> None:
    class Request:
        def __init__(self, message: dict[str, object]) -> None:
            self._message = message

        async def receive(self) -> dict[str, object]:
            return self._message

    assert asyncio.run(
        CompanyServerApplication._body_is_empty(  # type: ignore[arg-type]
            Request({"type": "http.request", "body": b"", "more_body": False})
        )
    )
    assert not asyncio.run(
        CompanyServerApplication._body_is_empty(  # type: ignore[arg-type]
            Request({"type": "http.request", "body": b"hidden-http2-body"})
        )
    )
    assert not asyncio.run(
        CompanyServerApplication._body_is_empty(  # type: ignore[arg-type]
            Request({"type": "http.request", "body": b"", "more_body": True})
        )
    )


def test_company_http_authenticator_has_a_total_timeout(tmp_path: Path) -> None:
    authenticator = HangingCompanyAuthenticator()
    bootstrap, _ = _bootstrap(
        tmp_path,
        ProcessTransport(),
        authenticator=authenticator,
        authentication_timeout_seconds=0.01,
    )
    application = create_company_asgi_app(bootstrap)

    with TestClient(application) as client:
        response = client.post(
            "/auth/session",
            headers={
                "Origin": "https://portal.example.cn",
                "Authorization": "Bearer company-sso",
            },
        )
    assert response.status_code == 503
    assert response.json() == {"error": "service_unavailable"}


def test_uvicorn_entry_points_apply_the_protocol_frame_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap, _ = _bootstrap(tmp_path, ProcessTransport())
    application = create_company_asgi_app(bootstrap)
    captured_run: dict[str, object] = {}

    def fake_run(app: object, **kwargs: object) -> None:
        captured_run.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)
    application.run()
    assert captured_run["ws_max_size"] == 1_048_576

    captured_serve: dict[str, object] = {}

    def fake_config(app: object, **kwargs: object) -> dict[str, object]:
        captured_serve.update(kwargs)
        return captured_serve

    class FakeServer:
        def __init__(self, config: object) -> None:
            self.config = config

        async def serve(self) -> None:
            return None

    monkeypatch.setattr("uvicorn.Config", fake_config)
    monkeypatch.setattr("uvicorn.Server", FakeServer)
    asyncio.run(application.serve())
    assert captured_serve["ws_max_size"] == 1_048_576
