"""Controlled HTML and structured web-search providers."""

from __future__ import annotations

import asyncio
import codecs
import html
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit

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


class AccessTokenProvider(ABC):
    @abstractmethod
    async def access_token(self) -> SecretStr: ...


class StaticAccessTokenProvider(AccessTokenProvider):
    """Server-configured token provider; the token is never rendered in results."""

    def __init__(self, token: SecretStr) -> None:
        self._token = token

    async def access_token(self) -> SecretStr:
        return self._token


class WebSearchProvider(ABC):
    @property
    @abstractmethod
    def provider_id(self) -> str: ...

    @abstractmethod
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
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        request_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
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
            response = await asyncio.wait_for(
                binding.transport.send(request), self._timeout
            )
        except TimeoutError as exc:
            raise WebAccessError("structured search request timed out") from exc
        self._policy.verify_connected_peer(target, response.connected_target_ip)
        if len(response.body) > self._max_response:
            raise WebResponseTooLarge("structured search response exceeded its limit")
        if not 200 <= response.status <= 299:
            raise WebAccessError(
                f"structured search API returned HTTP {response.status}"
            )
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


_HTML_CHARSET = re.compile(r"charset\s*=\s*['\"]?([^\s;'\"]+)", re.IGNORECASE)
_SEARCH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
_VOID_HTML_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


def _decode_html(content: bytes, content_type: str) -> str:
    match = _HTML_CHARSET.search(content_type)
    candidates = [match.group(1) if match else "", "utf-8", "gb18030", "gbk"]
    attempted: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        try:
            encoding = codecs.lookup(candidate).name
        except LookupError:
            continue
        if encoding in attempted:
            continue
        attempted.add(encoding)
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise WebAccessError("HTML search response could not be decoded")


class HtmlSearchClient:
    """Bounded GET client for administrator-configured public search pages."""

    def __init__(
        self,
        transports: FetchTransportRegistry,
        *,
        network_policy: PublicNetworkPolicy | None = None,
        egress_profile: str | None = None,
        timeout_seconds: float = 20.0,
        max_response_bytes: int = 4_194_304,
        user_agent: str = _SEARCH_USER_AGENT,
    ) -> None:
        self._transports = transports
        self._policy = network_policy or PublicNetworkPolicy()
        self._egress_profile = egress_profile
        self._timeout = timeout_seconds
        self._max_response = max_response_bytes
        self._user_agent = user_agent

    async def get(self, url: str) -> str:
        target = await self._policy.validate(url)
        binding = self._transports.resolve(self._egress_profile)
        request = HttpRequest(
            method="GET",
            url=target.url,
            allowed_target_ips=target.addresses,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
                "User-Agent": self._user_agent,
            },
            timeout_seconds=self._timeout,
            max_response_bytes=self._max_response,
        )
        try:
            response = await asyncio.wait_for(
                binding.transport.send(request), self._timeout
            )
        except TimeoutError as exc:
            raise WebAccessError("HTML search request timed out") from exc
        except WebPolicyDenied as exc:
            # Direct transports historically classify socket failures as policy
            # errors. The URL was already approved above, so this is a provider
            # availability failure and may use the configured fallback chain.
            raise WebAccessError("HTML search request failed") from exc
        self._policy.verify_connected_peer(target, response.connected_target_ip)
        if len(response.body) > self._max_response:
            raise WebResponseTooLarge("HTML search response exceeded its limit")
        if not 200 <= response.status <= 299:
            raise WebAccessError(
                f"HTML search endpoint returned HTTP {response.status}"
            )
        content_type = response.headers.get("content-type", "text/html").lower()
        if (
            "text/html" not in content_type
            and "application/xhtml+xml" not in content_type
        ):
            raise WebAccessError("HTML search endpoint did not return HTML")
        return _decode_html(response.body, content_type)


def _classes(attrs: list[tuple[str, str | None]]) -> frozenset[str]:
    for name, value in attrs:
        if name.lower() == "class" and value:
            return frozenset(value.lower().split())
    return frozenset()


def _attribute(attrs: list[tuple[str, str | None]], name: str) -> str:
    for key, value in attrs:
        if key.lower() == name and value:
            return value.strip()
    return ""


def _clean_parts(parts: Sequence[str]) -> str:
    return " ".join(" ".join(parts).split())


class _OrganicResultParser(HTMLParser):
    """Extract the stable organic-result subset used by Bing and Baidu."""

    def __init__(self, engine: str, endpoint: str, limit: int) -> None:
        super().__init__(convert_charrefs=True)
        self._engine = engine
        self._endpoint = endpoint
        self._limit = limit
        self._result_depth = 0
        self._ignored_depth = 0
        self._heading_depth = 0
        self._anchor_depth = 0
        self._snippet_depth = 0
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []
        self._body_parts: list[str] = []
        self._url = ""
        self.results: list[SearchResult] = []

    def _starts_result(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        classes = _classes(attrs)
        if self._engine == "bing":
            return tag == "li" and "b_algo" in classes
        return tag == "div" and bool(classes & {"result", "result-op", "c-container"})

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._ignored_depth:
            if tag in {"script", "style", "noscript", "template", "svg"}:
                self._ignored_depth += 1
            return
        if tag in {"script", "style", "noscript", "template", "svg"}:
            self._ignored_depth = 1
            return
        if not self._result_depth:
            if self._starts_result(tag, attrs):
                self._result_depth = 1
            return
        if tag in _VOID_HTML_TAGS:
            return
        self._result_depth += 1
        if tag in {"h2", "h3"}:
            self._heading_depth = self._result_depth
        elif tag == "a" and self._heading_depth and not self._url:
            href = _attribute(attrs, "href")
            if href:
                self._url = href
                self._anchor_depth = self._result_depth
        if self._engine == "bing" and tag == "p" and not self._snippet_depth:
            self._snippet_depth = self._result_depth
        elif (
            self._engine == "baidu" and tag == "div" and "c-abstract" in _classes(attrs)
        ):
            self._snippet_depth = self._result_depth

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag, attrs

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._ignored_depth:
            if tag in {"script", "style", "noscript", "template", "svg"}:
                self._ignored_depth -= 1
            return
        if not self._result_depth:
            return
        if self._anchor_depth == self._result_depth and tag == "a":
            self._anchor_depth = 0
        if self._heading_depth == self._result_depth and tag in {"h2", "h3"}:
            self._heading_depth = 0
        if self._snippet_depth == self._result_depth:
            self._snippet_depth = 0
        self._result_depth -= 1
        if self._result_depth == 0:
            self._finish_result()

    def handle_data(self, data: str) -> None:
        if not self._result_depth or self._ignored_depth:
            return
        if self._anchor_depth:
            self._title_parts.append(data)
        elif not self._heading_depth:
            self._body_parts.append(data)
        if self._snippet_depth:
            self._snippet_parts.append(data)

    def _finish_result(self) -> None:
        title = _clean_parts(self._title_parts)[:1_000]
        snippet = _clean_parts(self._snippet_parts or self._body_parts)[:10_000]
        raw_url = html.unescape(self._url)
        if self._engine == "bing" and (
            raw_url.startswith("/url?") or "/url?" in raw_url
        ):
            target = parse_qs(urlsplit(raw_url).query).get("url", [""])[0]
            if target:
                raw_url = unquote(target)
        raw_url = urljoin(self._endpoint, raw_url)
        if title and raw_url and len(self.results) < self._limit:
            try:
                safe_url = _safe_result_url(raw_url)
            except WebAccessError:
                pass
            else:
                self.results.append(
                    SearchResult(title=title, url=safe_url, snippet=snippet)
                )
        self._title_parts.clear()
        self._snippet_parts.clear()
        self._body_parts.clear()
        self._url = ""
        self._heading_depth = 0
        self._anchor_depth = 0
        self._snippet_depth = 0


class HtmlSearchProvider(WebSearchProvider):
    """Read-only Bing or ordinary Baidu HTML search provider."""

    def __init__(
        self,
        client: HtmlSearchClient,
        *,
        provider_id: str,
        engine: str,
        endpoint: str,
    ) -> None:
        if engine not in {"bing", "baidu"}:
            raise ValueError("unsupported HTML search engine")
        parsed = urlsplit(endpoint)
        host = (parsed.hostname or "").lower()
        expected_domain = "bing.com" if engine == "bing" else "baidu.com"
        if (
            parsed.scheme != "https"
            or (host != expected_domain and not host.endswith(f".{expected_domain}"))
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("HTML search endpoint does not match its engine")
        self._client = client
        self._provider_id = provider_id
        self._engine = engine
        self._endpoint = endpoint

    @property
    def provider_id(self) -> str:
        return self._provider_id

    async def search(self, request: SearchRequest) -> SearchResponse:
        parameter = "q" if self._engine == "bing" else "wd"
        url = f"{self._endpoint}?{urlencode({parameter: request.query})}"
        body = await self._client.get(url)
        lowered = body.lower()
        if self._engine == "baidu" and (
            "百度安全验证" in body
            or "请输入验证码" in body
            or "wappass.baidu.com/static/captcha" in lowered
        ):
            raise WebAccessError("HTML search endpoint returned a challenge page")
        parser = _OrganicResultParser(self._engine, self._endpoint, request.count)
        parser.feed(body)
        parser.close()
        return SearchResponse(
            provider_id=self.provider_id,
            query=request.query,
            results=tuple(parser.results),
        )


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
                snippet=_string(raw, "content", "snippet", "summary", "description")[
                    :10_000
                ],
                published_at=_string(raw, "date", "published_at", "publish_time")
                or None,
            )
        )
        if len(output) >= count:
            break
    return tuple(output)


class BaiduQianfanSearchProvider(WebSearchProvider):
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

    async def grounded_search(
        self, query: str, *, count: int
    ) -> Sequence[Mapping[str, Any]]: ...


class MicrosoftFoundryGroundingProvider(WebSearchProvider):
    """Foundry Grounding with Bing Search adapter, not the retired Bing REST API."""

    def __init__(self, client: FoundryGroundingClient) -> None:
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
    def __init__(self, default: WebSearchProvider) -> None:
        self._default = default.provider_id
        self._items: dict[str, WebSearchProvider] = {default.provider_id: default}

    def register(self, provider: WebSearchProvider) -> None:
        if provider.provider_id in self._items:
            raise ValueError(f"duplicate search provider: {provider.provider_id}")
        self._items[provider.provider_id] = provider

    def resolve(self, provider_id: str | None = None) -> WebSearchProvider:
        selected = provider_id or self._default
        try:
            return self._items[selected]
        except KeyError as exc:
            raise WebPolicyDenied(
                "requested search provider is not configured"
            ) from exc


class WebSearchService:
    def __init__(
        self,
        providers: SearchProviderRegistry,
        *,
        fallback_provider_ids: Sequence[str] = (),
    ) -> None:
        self._providers = providers
        if len(fallback_provider_ids) != len(set(fallback_provider_ids)):
            raise ValueError("fallback search providers must be unique")
        self._fallback_provider_ids = tuple(fallback_provider_ids)
        for provider_id in self._fallback_provider_ids:
            self._providers.resolve(provider_id)

    async def search(
        self,
        request: SearchRequest,
        *,
        provider_id: str | None = None,
    ) -> SearchResponse:
        if provider_id is not None:
            return await self._providers.resolve(provider_id).search(request)

        candidates = (
            self._providers.resolve(),
            *(self._providers.resolve(item) for item in self._fallback_provider_ids),
        )
        last_empty: SearchResponse | None = None
        failures = 0
        for provider in candidates:
            try:
                response = await provider.search(request)
            except WebPolicyDenied:
                raise
            except WebAccessError:
                failures += 1
                continue
            if response.results:
                return response
            last_empty = response
        if last_empty is not None:
            return last_empty
        if failures:
            raise WebAccessError("all configured search providers failed")
        raise WebAccessError("no search provider was attempted")


__all__ = [
    "AccessTokenProvider",
    "BaiduQianfanSearchProvider",
    "FoundryGroundingClient",
    "HtmlSearchClient",
    "HtmlSearchProvider",
    "MicrosoftFoundryGroundingProvider",
    "SearchProviderRegistry",
    "StaticAccessTokenProvider",
    "StructuredJsonClient",
    "WebSearchProvider",
    "WebSearchService",
]
