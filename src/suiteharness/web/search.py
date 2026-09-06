"""Structured search providers for Baidu Qianfan and Microsoft Foundry."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol
from urllib.parse import urlsplit

from pydantic import SecretStr

from .fetch import FetchTransportRegistry
from .models import (
    HttpRequest,
    SearchRequest,
    SearchResponse,
    SearchResult,
    WebAccessError,
    WebPolicyDenied,
    WebResponseTooLarge,
)
from .network import PublicNetworkPolicy


class AccessTokenProvider(Protocol):
    async def access_token(self) -> SecretStr: ...


class StaticAccessTokenProvider:
    """Server-configured token provider; the token is never rendered in results."""

    def __init__(self, token: SecretStr) -> None:
        self._token = token

    async def access_token(self) -> SecretStr:
        return self._token


class WebSearchProvider(Protocol):
    @property
    def provider_id(self) -> str: ...

    async def search(self, request: SearchRequest) -> SearchResponse: ...


class StructuredJsonClient:
    """Small secure POST client shared by structured search APIs."""

    def __init__(
        self,
        transports: FetchTransportRegistry,
        *,
        network_policy: PublicNetworkPolicy | None = None,
        egress_profile: str | None = None,
        timeout_seconds: float = 20.0,
        max_response_bytes: int = 4_194_304,
    ) -> None:
        self._transports = transports
        self._policy = network_policy or PublicNetworkPolicy()
        self._egress_profile = egress_profile
        self._timeout = timeout_seconds
        self._max_response = max_response_bytes

    async def post(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        target = await self._policy.validate(url)
        binding = self._transports.resolve(self._egress_profile)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request_headers = {"Accept": "application/json", "Content-Type": "application/json"}
        request_headers.update(headers or {})
        request = HttpRequest(
            method="POST",
            url=target.url,
            allowed_target_ips=target.addresses,
            headers=request_headers,
            body=body,
            timeout_seconds=self._timeout,
            max_response_bytes=self._max_response,
        )
        try:
            response = await asyncio.wait_for(binding.transport.send(request), self._timeout)
        except TimeoutError as exc:
            raise WebAccessError("structured search request timed out") from exc
        self._policy.verify_connected_peer(target, response.connected_target_ip)
        if len(response.body) > self._max_response:
            raise WebResponseTooLarge("structured search response exceeded its limit")
        if not 200 <= response.status <= 299:
            raise WebAccessError(f"structured search API returned HTTP {response.status}")
        content_type = response.headers.get("content-type", "application/json").lower()
        if "json" not in content_type:
            raise WebAccessError("structured search API did not return JSON")
        try:
            value = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebAccessError("structured search API returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise WebAccessError("structured search API returned a non-object payload")
        return value


def _string(item: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = item.get(name)
        if isinstance(value, str):
            return value.strip()
    return ""


def _safe_result_url(value: str) -> str:
    if not value or len(value) > 8_192 or any(char in value for char in "\r\n\x00"):
        raise WebAccessError("search provider returned an invalid result URL")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise WebAccessError("search provider returned a non-HTTP result URL")
    if parsed.username is not None or parsed.password is not None:
        raise WebAccessError("search result URLs cannot contain user information")
    return value


def _parse_items(value: object, count: int) -> tuple[SearchResult, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return ()
    output: list[SearchResult] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        url = _string(raw, "url", "link")
        if not url:
            continue
        output.append(
            SearchResult(
                title=_string(raw, "title", "name")[:1_000],
                url=_safe_result_url(url),
                snippet=_string(raw, "content", "snippet", "summary", "description")[:10_000],
                published_at=_string(raw, "date", "published_at", "publish_time") or None,
            )
        )
        if len(output) >= count:
            break
    return tuple(output)


class BaiduQianfanSearchProvider:
    """Baidu Qianfan structured Web Search API; never scrapes result HTML."""

    DEFAULT_ENDPOINT = "https://qianfan.baidubce.com/v2/ai_search/web_search"

    def __init__(
        self,
        client: StructuredJsonClient,
        token_provider: AccessTokenProvider,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
    ) -> None:
        self._client = client
        self._tokens = token_provider
        self._endpoint = endpoint

    @property
    def provider_id(self) -> str:
        return "baidu-qianfan"

    async def search(self, request: SearchRequest) -> SearchResponse:
        token = await self._tokens.access_token()
        payload: dict[str, Any] = {"query": request.query, "count": request.count}
        if request.freshness:
            payload["freshness"] = request.freshness
        response = await self._client.post(
            self._endpoint,
            payload,
            headers={"Authorization": f"Bearer {token.get_secret_value()}"},
        )
        items: object = response.get("references", response.get("results", ()))
        if isinstance(response.get("webPages"), Mapping):
            items = response["webPages"].get("value", items)
        return SearchResponse(
            provider_id=self.provider_id,
            query=request.query,
            results=_parse_items(items, request.count),
        )


class FoundryGroundingClient(Protocol):
    """Adapter seam for an authenticated Microsoft Foundry Agent client."""

    async def grounded_search(self, query: str, *, count: int) -> Sequence[Mapping[str, Any]]: ...


class MicrosoftFoundryGroundingProvider:
    """Foundry Grounding with Bing Search adapter, not the retired Bing REST API."""

    def __init__(self, client: FoundryGroundingClient) -> None:
        if not callable(getattr(client, "grounded_search", None)):
            raise TypeError(
                "Foundry grounding client must implement callable grounded_search"
            )
        self._client = client

    @property
    def provider_id(self) -> str:
        return "microsoft-foundry-bing-grounding"

    async def search(self, request: SearchRequest) -> SearchResponse:
        items = await self._client.grounded_search(request.query, count=request.count)
        return SearchResponse(
            provider_id=self.provider_id,
            query=request.query,
            results=_parse_items(items, request.count),
        )


class SearchProviderRegistry:
    def __init__(self, default: WebSearchProvider | None = None) -> None:
        self._default = None if default is None else default.provider_id
        self._items: dict[str, WebSearchProvider] = (
            {} if default is None else {default.provider_id: default}
        )

    def register(self, provider: WebSearchProvider) -> None:
        if provider.provider_id in self._items:
            raise ValueError(f"duplicate search provider: {provider.provider_id}")
        self._items[provider.provider_id] = provider

    def resolve(self, provider_id: str | None = None) -> WebSearchProvider:
        selected = provider_id or self._default
        if selected is None:
            raise WebPolicyDenied("Web search is not enabled for this deployment")
        try:
            return self._items[selected]
        except KeyError as exc:
            raise WebPolicyDenied("requested search provider is not configured") from exc


class WebSearchService:
    def __init__(self, providers: SearchProviderRegistry) -> None:
        self._providers = providers

    async def search(
        self,
        request: SearchRequest,
        *,
        provider_id: str | None = None,
    ) -> SearchResponse:
        return await self._providers.resolve(provider_id).search(request)


__all__ = [
    "AccessTokenProvider",
    "BaiduQianfanSearchProvider",
    "FoundryGroundingClient",
    "MicrosoftFoundryGroundingProvider",
    "SearchProviderRegistry",
    "StaticAccessTokenProvider",
    "StructuredJsonClient",
    "WebSearchProvider",
    "WebSearchService",
]
