"""Feishu Open Platform token cache and outbound message client."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from pydantic import SecretStr

from .models import FeishuApiError, FeishuHttpResponse, FeishuSendResult
from .processor import decode_json_object

_DEFAULT_ENDPOINT = "https://open.feishu.cn"


@dataclass(frozen=True, slots=True)
class FeishuHttpRequest:
    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    json_body: Mapping[str, Any] | None = field(default=None, repr=False)
    timeout_seconds: float = 15.0


class FeishuHttpTransport(Protocol):
    async def send(self, request: FeishuHttpRequest) -> FeishuHttpResponse: ...


class TenantAccessTokenProvider(Protocol):
    async def get_token(self) -> str: ...


def _secret(value: str | SecretStr) -> str:
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def _endpoint(value: str) -> str:
    if not value or value != value.strip() or any(char.isspace() for char in value):
        raise ValueError("Feishu API endpoint is invalid")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Feishu API endpoint must be an HTTPS origin")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Feishu API endpoint must not contain user information")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Feishu API endpoint must contain only scheme and authority")
    return value.rstrip("/")


def _api_payload(response: FeishuHttpResponse, operation: str) -> dict[str, Any]:
    if response.status_code < 200 or response.status_code >= 300:
        retryable = response.status_code == 429 or response.status_code >= 500
        raise FeishuApiError(
            f"Feishu {operation} failed with HTTP {response.status_code}", retryable=retryable
        )
    try:
        payload = decode_json_object(response.body)
    except Exception as exc:
        raise FeishuApiError(f"Feishu {operation} returned invalid JSON") from exc
    code = payload.get("code")
    if isinstance(code, bool) or not isinstance(code, int):
        raise FeishuApiError(f"Feishu {operation} returned no valid business code")
    if code != 0:
        # Provider messages may contain tenant data and are not propagated.
        raise FeishuApiError(
            f"Feishu {operation} failed with business code {code}",
            retryable=code in {99991400, 99991401, 99991402},
        )
    return payload


class CachedTenantAccessTokenProvider:
    """Fetch and cache a tenant token with single-flight refresh semantics."""

    def __init__(
        self,
        transport: FeishuHttpTransport,
        *,
        app_id: str,
        app_secret: str | SecretStr,
        endpoint: str = _DEFAULT_ENDPOINT,
        timeout_seconds: float = 15.0,
        refresh_skew_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not app_id or len(app_id) > 256 or "\x00" in app_id:
            raise ValueError("Feishu app_id is invalid")
        secret = _secret(app_secret)
        if not secret:
            raise ValueError("Feishu app_secret must not be blank")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if refresh_skew_seconds < 0:
            raise ValueError("refresh_skew_seconds must be non-negative")
        self._transport = transport
        self._app_id = app_id
        self._app_secret = SecretStr(secret)
        self._endpoint = _endpoint(endpoint)
        self._timeout_seconds = timeout_seconds
        self._refresh_skew = refresh_skew_seconds
        self._clock = clock
        self._token: SecretStr | None = None
        self._usable_until = 0.0
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        now = self._clock()
        if self._token is not None and now < self._usable_until:
            return self._token.get_secret_value()
        async with self._lock:
            now = self._clock()
            if self._token is not None and now < self._usable_until:
                return self._token.get_secret_value()
            response = await self._transport.send(
                FeishuHttpRequest(
                    method="POST",
                    url=f"{self._endpoint}/open-apis/auth/v3/tenant_access_token/internal",
                    headers={"Content-Type": "application/json; charset=utf-8"},
                    json_body={
                        "app_id": self._app_id,
                        "app_secret": self._app_secret.get_secret_value(),
                    },
                    timeout_seconds=self._timeout_seconds,
                )
            )
            payload = _api_payload(response, "tenant token request")
            token = payload.get("tenant_access_token")
            expires_in = payload.get("expire", payload.get("expires_in"))
            if not isinstance(token, str) or not token or "\x00" in token:
                raise FeishuApiError("Feishu tenant token response omitted the token")
            if (
                isinstance(expires_in, bool)
                or not isinstance(expires_in, int)
                or expires_in <= 0
            ):
                raise FeishuApiError("Feishu tenant token response has an invalid expiry")
            margin = min(self._refresh_skew, max(0.0, expires_in / 2))
            self._token = SecretStr(token)
            self._usable_until = self._clock() + expires_in - margin
            return token

    async def invalidate(self, token: str | None = None) -> None:
        """Invalidate only the expected token, avoiding a stale caller race."""

        async with self._lock:
            if token is None or (
                self._token is not None
                and hmac_compare(self._token.get_secret_value(), token)
            ):
                self._token = None
                self._usable_until = 0.0


def hmac_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


class FeishuMessageClient:
    """Send normalized text messages through a company-owned Feishu app."""

    _RECEIVE_ID_TYPES = frozenset({"chat_id", "open_id", "user_id", "union_id", "email"})

    def __init__(
        self,
        transport: FeishuHttpTransport,
        token_provider: TenantAccessTokenProvider,
        *,
        endpoint: str = _DEFAULT_ENDPOINT,
        timeout_seconds: float = 15.0,
        max_text_chars: int = 150_000,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_text_chars <= 0:
            raise ValueError("max_text_chars must be positive")
        self._transport = transport
        self._tokens = token_provider
        self._endpoint = _endpoint(endpoint)
        self._timeout_seconds = timeout_seconds
        self._max_text_chars = max_text_chars

    async def send_text(
        self,
        receive_id: str,
        text: str,
        *,
        receive_id_type: str = "chat_id",
    ) -> FeishuSendResult:
        if receive_id_type not in self._RECEIVE_ID_TYPES:
            raise ValueError("unsupported Feishu receive_id_type")
        if not receive_id or len(receive_id) > 1024 or "\x00" in receive_id:
            raise ValueError("invalid Feishu receive_id")
        if not text or len(text) > self._max_text_chars or "\x00" in text:
            raise ValueError("invalid Feishu message text")
        token = await self._tokens.get_token()
        query = urlencode({"receive_id_type": receive_id_type})
        response = await self._transport.send(
            FeishuHttpRequest(
                method="POST",
                url=f"{self._endpoint}/open-apis/im/v1/messages?{query}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json_body={
                    "receive_id": receive_id,
                    "msg_type": "text",
                    "content": json.dumps(
                        {"text": text}, ensure_ascii=False, separators=(",", ":")
                    ),
                },
                timeout_seconds=self._timeout_seconds,
            )
        )
        payload = _api_payload(response, "message send")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise FeishuApiError("Feishu message send response omitted data")
        message_id = data.get("message_id")
        if not isinstance(message_id, str):
            raise FeishuApiError("Feishu message send response omitted message_id")
        try:
            return FeishuSendResult(message_id=message_id)
        except ValueError as exc:
            raise FeishuApiError("Feishu message send returned an invalid message_id") from exc


class HttpxFeishuTransport:
    """Optional production HTTP transport; ``httpx`` is loaded only when used."""

    def __init__(
        self,
        *,
        proxy: str | None = None,
        verify: bool | str = True,
        max_connections: int = 100,
    ) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - deployment diagnostic
            raise RuntimeError(
                "HttpxFeishuTransport requires the optional 'httpx' dependency"
            ) from exc
        self._httpx = httpx
        self._client = httpx.AsyncClient(
            proxy=proxy,
            verify=verify,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=max_connections),
        )

    async def send(self, request: FeishuHttpRequest) -> FeishuHttpResponse:
        try:
            response = await self._client.request(
                request.method,
                request.url,
                headers=dict(request.headers),
                json=request.json_body,
                timeout=request.timeout_seconds,
            )
        except self._httpx.TimeoutException as exc:
            raise FeishuApiError("Feishu API request timed out", retryable=True) from exc
        except self._httpx.HTTPError as exc:
            raise FeishuApiError(
                f"Feishu API transport failed: {type(exc).__name__}", retryable=True
            ) from exc
        return FeishuHttpResponse(response.status_code, response.content)

    async def close(self) -> None:
        await self._client.aclose()


__all__ = [
    "CachedTenantAccessTokenProvider",
    "FeishuHttpRequest",
    "FeishuHttpTransport",
    "FeishuMessageClient",
    "HttpxFeishuTransport",
    "TenantAccessTokenProvider",
]
