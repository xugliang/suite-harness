"""Runnable company-server composition and Starlette/ASGI lifecycle.

The framework owns process-local infrastructure and channel lifecycle while
the deployment supplies company trust adapters: product activators, SSO,
Feishu directory/message adapters, plugin verification, and MCP transports.
No personal-login or desktop execution fallback exists here.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response

from suiteharness import __version__
from suiteharness.channels import (
    AuthenticatedPrincipal,
    ConversationAuthorizer,
    ProductAccessAuthorizer,
)
from suiteharness.channels.websocket import OriginPolicy
from suiteharness.config import LoadedSuiteHarnessConfig, SuiteHarnessConfigLoader
from suiteharness.models import HttpTransport as ModelHttpTransport
from suiteharness.runtime import (
    ActivatedCustomerBundle,
    CustomerBundlePlan,
    ProductActivator,
    ProductCatalog,
    resolve_customer_bundle,
)
from suiteharness.sandbox import SandboxProcessTransport
from suiteharness.web import BrowserWorkerClient, FoundryGroundingClient

from .access import ToolAccessTemplate
from .application import RunInputBuilder, default_run_input
from .extensions import ExtensionActivation, McpHostAdapters, PluginHostAdapters
from .foundation import FoundationReadiness, FoundationRuntime
from .host import CompanyChannelRuntime, FeishuHostAdapters, WebApprovalRuntime

_LOGGER = logging.getLogger(__name__)
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_SECURITY_SINGLETON_HEADERS = frozenset(
    {"authorization", "content-length", "cookie", "origin", "transfer-encoding"}
)


def _require_starlette() -> tuple[type[Any], type[Any], type[Any], type[Any]]:
    try:
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse, Response
        from starlette.routing import Route
    except ImportError as exc:  # pragma: no cover - deployment diagnostic
        raise RuntimeError(
            "the company ASGI server requires the declared 'starlette' dependency"
        ) from exc
    return Starlette, Route, JSONResponse, Response


class CompanyAuthenticationUnavailable(RuntimeError):
    """The company SSO/session verifier is temporarily unavailable."""


class CompanyServerStartupError(RuntimeError):
    """The composed company server failed before it could accept traffic."""


@dataclass(frozen=True, slots=True)
class CompanyHttpAuthenticationRequest:
    """Bounded HTTP metadata passed to a company-owned SSO adapter.

    Authentication bodies are intentionally unsupported.  A deployment should
    authenticate an existing secure company cookie or bearer credential and
    exchange it for SuiteHarness's short-lived WebSocket ticket.
    """

    method: str
    path: str
    origin: str
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    client_host: str | None = None

    def __post_init__(self) -> None:
        if self.method != "POST":
            raise ValueError("company authentication exchange must use POST")
        if not self.path.startswith("/") or len(self.path) > 2048 or "\x00" in self.path:
            raise ValueError("invalid company authentication request path")
        if not self.origin or len(self.origin) > 2048 or "\x00" in self.origin:
            raise ValueError("invalid company authentication request origin")
        if len(self.headers) > 128:
            raise ValueError("too many company authentication request headers")
        total = 0
        counts: dict[str, int] = {}
        normalized: list[tuple[str, str]] = []
        for name, value in self.headers:
            lowered = name.casefold()
            if not _HEADER_NAME.fullmatch(name) or "\r" in value or "\n" in value:
                raise ValueError("invalid company authentication request header")
            if len(value) > 16_384:
                raise ValueError("company authentication request header is too large")
            total += len(name) + len(value)
            counts[lowered] = counts.get(lowered, 0) + 1
            normalized.append((lowered, value))
        if total > 65_536:
            raise ValueError("company authentication request headers are too large")
        if any(counts.get(name, 0) > 1 for name in _SECURITY_SINGLETON_HEADERS):
            raise ValueError("duplicate security-sensitive request header")
        object.__setattr__(self, "headers", tuple(normalized))

    def header(self, name: str) -> str | None:
        """Return one header value, or ``None`` when it was not supplied."""

        lowered = name.casefold()
        values = [value for candidate, value in self.headers if candidate == lowered]
        if len(values) > 1:
            raise ValueError("requested header has multiple values")
        return None if not values else values[0]


class CompanyHttpAuthenticator(Protocol):
    """Verify an existing company SSO credential at the HTTP trust boundary."""

    async def authenticate(
        self,
        request: CompanyHttpAuthenticationRequest,
    ) -> AuthenticatedPrincipal | None: ...


@dataclass(frozen=True, slots=True)
class CompanyServerReadiness:
    """Aggregated readiness without endpoint, path, or credential detail."""

    checks: Mapping[str, bool]

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", MappingProxyType(dict(self.checks)))

    @property
    def ready(self) -> bool:
        return bool(self.checks) and all(self.checks.values())


class CompanyServerRuntime:
    """One fully activated company deployment and all of its owned resources."""

    def __init__(
        self,
        *,
        foundation: FoundationRuntime,
        channels: CompanyChannelRuntime,
        activation: ActivatedCustomerBundle,
        extensions: ExtensionActivation,
    ) -> None:
        self.foundation = foundation
        self.channels = channels
        self.activation = activation
        self.extensions = extensions
        self._long_connection_task: asyncio.Task[None] | None = None
        self._closed = False
        self._close_lock = asyncio.Lock()

    @property
    def closed(self) -> bool:
        return self._closed

    async def start_background_channels(self) -> None:
        """Start the configured Feishu long connection exactly once."""

        if self._closed:
            raise RuntimeError("company server runtime is closed")
        if self.channels.feishu_long_connection is None:
            return
        if self._long_connection_task is not None:
            raise RuntimeError("Feishu long connection is already started")
        task = asyncio.create_task(
            self.channels.run_feishu_long_connection(),
            name="suiteharness-feishu-long-connection",
        )
        self._long_connection_task = task
        task.add_done_callback(self._log_background_exit)
        await asyncio.sleep(0)
        if task.done():
            try:
                task.result()
            except asyncio.CancelledError as exc:
                raise CompanyServerStartupError(
                    "Feishu long connection stopped during startup"
                ) from exc
            except BaseException as exc:
                raise CompanyServerStartupError(
                    "Feishu long connection failed during startup"
                ) from exc
            raise CompanyServerStartupError(
                "Feishu long connection returned during startup"
            )

    async def readiness(self) -> CompanyServerReadiness:
        foundation: FoundationReadiness = await self.foundation.readiness()
        checks = dict(foundation.checks)
        checks["channels"] = not self._closed and not self.channels.closed
        checks["activation"] = not self._closed and not self.activation.closed
        if self.channels.feishu_long_connection is not None:
            task = self._long_connection_task
            checks["feishu_long_connection"] = task is not None and not task.done()
        return CompanyServerReadiness(checks)

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            failures: list[BaseException] = []
            try:
                await self.channels.close()
            except BaseException as exc:
                failures.append(exc)
            task = self._long_connection_task
            if task is not None:
                if not task.done():
                    task.cancel()
                results = await asyncio.gather(task, return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException) and not isinstance(
                        result, asyncio.CancelledError
                    ):
                        failures.append(result)
            try:
                await self.foundation.close()
            except BaseException as exc:
                failures.append(exc)
            if failures:
                raise BaseExceptionGroup("company server shutdown reported failures", failures)

    def _log_background_exit(self, task: asyncio.Task[None]) -> None:
        if self._closed or task.cancelled():
            return
        if task.exception() is None:
            _LOGGER.critical("Feishu long connection exited unexpectedly")
        else:
            _LOGGER.critical("Feishu long connection failed; readiness is now false")


class CompanyServerBootstrap:
    """Canonical YAML-to-runtime composition for a source-deployed server."""

    def __init__(
        self,
        loaded: LoadedSuiteHarnessConfig,
        *,
        product_catalog: ProductCatalog,
        product_activators: Iterable[ProductActivator],
        grant_templates: Iterable[ToolAccessTemplate],
        web_authenticator: CompanyHttpAuthenticator | None = None,
        web_conversation_authorizer: ConversationAuthorizer | None = None,
        product_access_authorizer: ProductAccessAuthorizer | None = None,
        feishu_adapters: FeishuHostAdapters | None = None,
        model_transport: ModelHttpTransport | None = None,
        sandbox_transport: SandboxProcessTransport | None = None,
        browser_workers: Mapping[str, BrowserWorkerClient] | None = None,
        foundry_clients: Mapping[str, FoundryGroundingClient] | None = None,
        plugin_adapters: PluginHostAdapters | None = None,
        mcp_adapters: McpHostAdapters | None = None,
        input_builder: RunInputBuilder = default_run_input,
        approval_timeout_seconds: float = 60.0,
    ) -> None:
        if not isinstance(loaded, LoadedSuiteHarnessConfig):
            raise TypeError("loaded must be LoadedSuiteHarnessConfig")
        if not isinstance(product_catalog, ProductCatalog):
            raise TypeError("product_catalog must be ProductCatalog")
        if not callable(input_builder):
            raise TypeError("input_builder must be callable")
        if approval_timeout_seconds <= 0 or approval_timeout_seconds > 3600:
            raise ValueError("approval timeout must be in (0, 3600]")
        if loaded.config.channels.web.enabled and web_authenticator is None:
            raise ValueError("enabled Web channel requires a company HTTP authenticator")
        if not loaded.config.channels.web.enabled and web_authenticator is not None:
            raise ValueError("company HTTP authenticator requires the Web channel")
        self.loaded = loaded
        self.product_catalog = product_catalog
        self.product_activators = tuple(product_activators)
        self.grant_templates = tuple(grant_templates)
        self.web_authenticator = web_authenticator
        self.web_conversation_authorizer = web_conversation_authorizer
        self.product_access_authorizer = product_access_authorizer
        self.feishu_adapters = feishu_adapters
        self.model_transport = model_transport
        self.sandbox_transport = sandbox_transport
        self.browser_workers = MappingProxyType(dict(browser_workers or {}))
        self.foundry_clients = MappingProxyType(dict(foundry_clients or {}))
        self.plugin_adapters = plugin_adapters
        self.mcp_adapters = mcp_adapters
        self.input_builder = input_builder
        self.approval_timeout_seconds = approval_timeout_seconds

    @classmethod
    def from_files(
        cls,
        config_path: str | Path,
        secrets_path: str | Path,
        **trusted_adapters: Any,
    ) -> CompanyServerBootstrap:
        """Load the two explicit YAML files, then create a trusted bootstrap."""

        loaded = SuiteHarnessConfigLoader().load(config_path, secrets_path)
        return cls(loaded, **trusted_adapters)

    def resolve_plan(self) -> CustomerBundlePlan:
        return resolve_customer_bundle(
            self.loaded.config.customer_bundle,
            self.product_catalog,
            harness_version=__version__,
        )

    async def build(self) -> CompanyServerRuntime:
        """Activate products, extensions and channels as one rollback-safe unit."""

        plan = self.resolve_plan()
        approvals = (
            WebApprovalRuntime(timeout_seconds=self.approval_timeout_seconds)
            if self.loaded.config.channels.web.enabled
            else None
        )
        foundation: FoundationRuntime | None = None
        channels: CompanyChannelRuntime | None = None
        runtime: CompanyServerRuntime | None = None
        try:
            foundation = await FoundationRuntime.create(
                self.loaded,
                model_transport=self.model_transport,
                sandbox_transport=self.sandbox_transport,
                browser_workers=self.browser_workers,
                foundry_clients=self.foundry_clients,
                interactive_approvals=(
                    approvals.coordinator if approvals is not None else None
                ),
                plugin_adapters=self.plugin_adapters,
                mcp_adapters=self.mcp_adapters,
            )
            for product in plan.products:
                foundation.prepare_product_workspace(product.product_id)
            activation = await foundation.kernel.activate_customer_bundle(
                self.loaded.config.deployment.tenant_id,
                plan,
                self.product_activators,
            )
            extensions = await foundation.start_extensions(activation)
            channels = await CompanyChannelRuntime.create(
                self.loaded,
                activation=activation,
                sessions=foundation.sessions,
                tools=foundation.tools,
                capabilities=foundation.capabilities,
                grant_templates=self.grant_templates,
                web_approvals=approvals,
                web_conversation_authorizer=self.web_conversation_authorizer,
                product_access_authorizer=self.product_access_authorizer,
                feishu_adapters=self.feishu_adapters,
                mcp_grant_material=extensions.mcp_by_product,
                runtime_database=foundation.runtime_database,
                input_builder=self.input_builder,
            )
            runtime = CompanyServerRuntime(
                foundation=foundation,
                channels=channels,
                activation=activation,
                extensions=extensions,
            )
            await runtime.start_background_channels()
            status = await runtime.readiness()
            if not status.ready:
                raise CompanyServerStartupError(
                    "company server dependencies did not become ready"
                )
            return runtime
        except BaseException as cause:
            failures: list[BaseException] = []
            if runtime is not None:
                try:
                    await runtime.close()
                except BaseException as exc:
                    failures.append(exc)
                channels = None
                foundation = None
            elif channels is not None:
                try:
                    await channels.close()
                except BaseException as exc:
                    failures.append(exc)
            elif approvals is not None:
                try:
                    await approvals.close()
                except BaseException as exc:
                    failures.append(exc)
            if foundation is not None:
                try:
                    await foundation.close()
                except BaseException as exc:
                    failures.append(exc)
            if failures:
                raise BaseExceptionGroup(
                    "company server startup and rollback failed",
                    [cause, *failures],
                ) from cause
            raise


class CompanyServerApplication:
    """ASGI application with health gates, SSO exchange, and owned lifespan."""

    def __init__(self, bootstrap: CompanyServerBootstrap) -> None:
        if not isinstance(bootstrap, CompanyServerBootstrap):
            raise TypeError("bootstrap must be CompanyServerBootstrap")
        self.bootstrap = bootstrap
        self._runtime: CompanyServerRuntime | None = None
        self._accepting = False
        starlette_type, route_type, _json_type, _response_type = _require_starlette()
        routes: list[Any] = [
            route_type("/health/live", self._liveness, methods=["GET"]),
            route_type("/health/ready", self._readiness, methods=["GET"]),
        ]
        if bootstrap.loaded.config.channels.web.enabled:
            routes.append(
                route_type(
                    bootstrap.loaded.config.channels.web.session_path,
                    self._issue_web_session,
                    methods=["POST", "OPTIONS"],
                )
            )
        self.app = starlette_type(routes=routes, lifespan=self._lifespan)

    @property
    def runtime(self) -> CompanyServerRuntime | None:
        return self._runtime

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        await self.app(scope, receive, send)

    @asynccontextmanager
    async def _lifespan(self, app: Starlette):
        if self._runtime is not None:
            raise RuntimeError("company server ASGI lifespan is already active")
        lifecycle = self.bootstrap.loaded.config.server
        try:
            async with asyncio.timeout(lifecycle.startup_timeout_seconds):
                runtime = await self.bootstrap.build()
        except TimeoutError as exc:
            raise CompanyServerStartupError(
                "company server exceeded its configured startup timeout"
            ) from exc
        channel_routes = runtime.channels.starlette_routes()
        app.router.routes.extend(channel_routes)
        self._runtime = runtime
        self._accepting = True
        try:
            yield
        finally:
            self._accepting = False
            try:
                try:
                    async with asyncio.timeout(lifecycle.shutdown_timeout_seconds):
                        await runtime.close()
                except TimeoutError as exc:
                    raise RuntimeError(
                        "company server exceeded its configured shutdown timeout"
                    ) from exc
            finally:
                for route in channel_routes:
                    try:
                        app.router.routes.remove(route)
                    except ValueError:
                        pass
                self._runtime = None

    async def _liveness(self, _request: Request) -> JSONResponse:
        return self._json({"status": "live"}, status_code=200)

    async def _readiness(self, _request: Request) -> JSONResponse:
        runtime = self._runtime
        if runtime is None or not self._accepting:
            return self._json({"status": "not_ready", "checks": {}}, status_code=503)
        status = await runtime.readiness()
        code = 200 if status.ready else 503
        public = {name: ("ok" if ready else "failed") for name, ready in status.checks.items()}
        return self._json(
            {"status": "ready" if status.ready else "not_ready", "checks": public},
            status_code=code,
        )

    async def _issue_web_session(self, request: Request) -> Response:
        config = self.bootstrap.loaded.config.channels.web
        origin = self._single_header(request, "origin")
        origin_policy = OriginPolicy(config.allowed_origins)
        if origin is None or not origin_policy.permits(origin):
            return self._json({"error": "origin_not_allowed"}, status_code=403)
        cors = self._cors_headers(origin)
        if request.method == "OPTIONS":
            requested = self._single_header(request, "access-control-request-method")
            if requested != "POST":
                return self._json(
                    {"error": "invalid_preflight"}, status_code=400, headers=cors
                )
            _app_type, _route_type, _json_type, response_type = _require_starlette()
            return response_type(status_code=204, headers=cors)
        if not self._empty_body_headers(request) or not await self._body_is_empty(request):
            return self._json(
                {"error": "request_body_not_supported"}, status_code=413, headers=cors
            )
        runtime = self._runtime
        authenticator = self.bootstrap.web_authenticator
        if runtime is None or not self._accepting or authenticator is None:
            return self._json({"error": "service_unavailable"}, status_code=503, headers=cors)
        try:
            auth_request = self._authentication_request(request, origin)
        except ValueError:
            return self._json({"error": "invalid_request"}, status_code=400, headers=cors)
        try:
            async with asyncio.timeout(config.authentication_timeout_seconds):
                principal = await authenticator.authenticate(auth_request)
        except (TimeoutError, CompanyAuthenticationUnavailable):
            return self._json({"error": "service_unavailable"}, status_code=503, headers=cors)
        except Exception:
            _LOGGER.error("company HTTP authenticator failed")
            return self._json({"error": "service_unavailable"}, status_code=503, headers=cors)
        if not isinstance(principal, AuthenticatedPrincipal) or (
            principal.tenant_id != self.bootstrap.loaded.config.deployment.tenant_id
        ):
            return self._json({"error": "unauthorized"}, status_code=401, headers=cors)
        codec = runtime.channels.web_session_tokens
        if codec is None:
            return self._json({"error": "service_unavailable"}, status_code=503, headers=cors)
        token = codec.issue(principal, lifetime_seconds=config.session_lifetime_seconds)
        return self._json(
            {
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": config.session_lifetime_seconds,
            },
            status_code=200,
            headers=cors,
        )

    @staticmethod
    def _single_header(request: Request, name: str) -> str | None:
        target = name.casefold().encode("ascii")
        values = [
            value.decode("latin-1")
            for key, value in request.scope.get("headers", ())
            if key.lower() == target
        ]
        if len(values) != 1:
            return None
        return values[0]

    @staticmethod
    def _empty_body_headers(request: Request) -> bool:
        raw_headers = request.scope.get("headers", ())
        transfer_encodings = [
            value for name, value in raw_headers if name.lower() == b"transfer-encoding"
        ]
        if transfer_encodings:
            return False
        content_lengths = [
            value.decode("latin-1")
            for name, value in raw_headers
            if name.lower() == b"content-length"
        ]
        if not content_lengths:
            return True
        if len(content_lengths) != 1:
            return False
        content_length = content_lengths[0]
        try:
            return int(content_length) == 0 and content_length.strip() == "0"
        except ValueError:
            return False

    @staticmethod
    async def _body_is_empty(request: Request) -> bool:
        """Inspect the ASGI body event so HTTP/2 cannot bypass header checks.

        A request without ``Content-Length`` or ``Transfer-Encoding`` can still
        carry DATA frames on HTTP/2.  Reading only the first event is sufficient
        here: any bytes or a continuation flag violates this bodyless endpoint,
        while avoiding an unbounded ``request.body()`` allocation.
        """

        try:
            async with asyncio.timeout(5.0):
                message = await request.receive()
        except TimeoutError:
            return False
        if message.get("type") != "http.request":
            return False
        body = message.get("body", b"")
        return isinstance(body, bytes) and not body and message.get("more_body") is not True

    @staticmethod
    def _authentication_request(
        request: Request,
        origin: str,
    ) -> CompanyHttpAuthenticationRequest:
        headers = tuple(
            (name.decode("ascii"), value.decode("latin-1"))
            for name, value in request.scope.get("headers", ())
        )
        client = request.client
        return CompanyHttpAuthenticationRequest(
            method=request.method,
            path=request.url.path,
            origin=origin,
            headers=headers,
            client_host=None if client is None else client.host,
        )

    @staticmethod
    def _cors_headers(origin: str) -> dict[str, str]:
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Headers": "Authorization, Content-Type, X-CSRF-Token",
            "Access-Control-Allow-Methods": "POST",
            "Vary": "Origin",
        }

    @staticmethod
    def _json(
        content: dict[str, object],
        *,
        status_code: int,
        headers: Mapping[str, str] | None = None,
    ) -> JSONResponse:
        safe_headers = {
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            **dict(headers or {}),
        }
        _app_type, _route_type, json_type, _response_type = _require_starlette()
        return json_type(content, status_code=status_code, headers=safe_headers)

    async def serve(self) -> None:
        """Run Uvicorn with config-file listener values and safe proxy defaults."""

        import uvicorn

        server_config = self.bootstrap.loaded.config.server
        server = uvicorn.Server(
            uvicorn.Config(
                self,
                host=server_config.host,
                port=server_config.port,
                lifespan="on",
                proxy_headers=False,
                limit_concurrency=server_config.max_concurrent_connections,
                backlog=server_config.backlog,
                ws_max_size=self.bootstrap.loaded.config.channels.web.max_frame_bytes,
                timeout_graceful_shutdown=server_config.shutdown_timeout_seconds,
            )
        )
        await server.serve()

    def run(self) -> None:
        """Blocking Uvicorn entry point for a source-checkout deployment."""

        import uvicorn

        server_config = self.bootstrap.loaded.config.server
        uvicorn.run(
            self,
            host=server_config.host,
            port=server_config.port,
            lifespan="on",
            proxy_headers=False,
            limit_concurrency=server_config.max_concurrent_connections,
            backlog=server_config.backlog,
            ws_max_size=self.bootstrap.loaded.config.channels.web.max_frame_bytes,
            timeout_graceful_shutdown=server_config.shutdown_timeout_seconds,
        )


def create_company_asgi_app(bootstrap: CompanyServerBootstrap) -> CompanyServerApplication:
    """Create the canonical company-only ASGI application."""

    return CompanyServerApplication(bootstrap)


__all__ = [
    "CompanyAuthenticationUnavailable",
    "CompanyHttpAuthenticationRequest",
    "CompanyHttpAuthenticator",
    "CompanyServerApplication",
    "CompanyServerBootstrap",
    "CompanyServerReadiness",
    "CompanyServerRuntime",
    "CompanyServerStartupError",
    "create_company_asgi_app",
]
