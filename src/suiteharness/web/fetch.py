"""Domestic-network-friendly fetch service with strict egress controls."""

from __future__ import annotations

import asyncio
import codecs
import json
import re
import time
import zlib
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urljoin

from .models import (
    FetchDocument,
    FetchLimits,
    FetchRoute,
    HttpRequest,
    UnsupportedWebContent,
    WebAccessError,
    WebPolicyDenied,
    WebResponseTooLarge,
)
from .network import HttpTransport, PublicNetworkPolicy

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_CHARSET = re.compile(r"charset\s*=\s*['\"]?([^\s;'\"]+)", re.IGNORECASE)


class PdfTextExtractor(Protocol):
    """Optional seam for a sandboxed/maintained PDF parser."""

    async def extract(self, content: bytes, *, source_url: str) -> str: ...


@dataclass(frozen=True, slots=True)
class FetchTransportBinding:
    name: str
    route: FetchRoute
    transport: HttpTransport

    def __post_init__(self) -> None:
        if not self.name or any(char.isspace() for char in self.name):
            raise ValueError("invalid fetch transport profile name")


class FetchTransportRegistry:
    """Explicit route registry; it never reads proxy settings from the environment."""

    def __init__(self, default: FetchTransportBinding) -> None:
        self._default = default.name
        self._items = {default.name: default}

    def register(self, binding: FetchTransportBinding) -> None:
        if binding.name in self._items:
            raise ValueError(f"duplicate fetch transport profile: {binding.name}")
        self._items[binding.name] = binding

    def resolve(self, profile: str | None) -> FetchTransportBinding:
        name = profile or self._default
        try:
            return self._items[name]
        except KeyError as exc:
            raise WebPolicyDenied("requested web egress profile is not configured") from exc


class _HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._in_title = False
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript", "template", "svg"}:
            self._ignored_depth += 1
        elif lowered == "title" and self._ignored_depth == 0:
            self._in_title = True
        elif lowered in {"p", "div", "section", "article", "li", "br", "h1", "h2", "h3"}:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript", "template", "svg"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
        elif lowered == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        self.text_parts.append(data)


def _bounded_decompress(data: bytes, encoding: str, limit: int) -> bytes:
    if not encoding or encoding == "identity":
        if len(data) > limit:
            raise WebResponseTooLarge("web content exceeded the decompressed limit")
        return data
    if encoding == "gzip":
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding == "deflate":
        decoder = zlib.decompressobj()
    else:
        raise UnsupportedWebContent("unsupported HTTP content encoding")
    try:
        output = decoder.decompress(data, limit + 1)
        if len(output) <= limit:
            output += decoder.flush(limit + 1 - len(output))
    except zlib.error as exc:
        raise WebAccessError("invalid compressed HTTP response") from exc
    if len(output) > limit or decoder.unconsumed_tail:
        raise WebResponseTooLarge("web content exceeded the decompressed limit")
    return output


def _media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _decode(content: bytes, content_type: str) -> str:
    match = _CHARSET.search(content_type)
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
    raise UnsupportedWebContent("web text could not be decoded as UTF-8/GB18030/GBK")


def _clean_text(value: str) -> str:
    lines = (" ".join(line.split()) for line in value.replace("\r", "\n").split("\n"))
    return "\n".join(line for line in lines if line)


class WebFetchService:
    """Fetch one document through a validated and administrator-selected route."""

    def __init__(
        self,
        transports: FetchTransportRegistry,
        *,
        network_policy: PublicNetworkPolicy | None = None,
        limits: FetchLimits | None = None,
        pdf_extractor: PdfTextExtractor | None = None,
        user_agent: str = "SuiteHarness/1.0",
    ) -> None:
        self._transports = transports
        self._policy = network_policy or PublicNetworkPolicy()
        self._limits = limits or FetchLimits()
        self._pdf_extractor = pdf_extractor
        self._user_agent = user_agent

    async def fetch(self, url: str, *, egress_profile: str | None = None) -> FetchDocument:
        binding = self._transports.resolve(egress_profile)
        requested_url = url
        current_url = url
        started = time.monotonic()
        response = None
        for redirect_count in range(self._limits.max_redirects + 1):
            remaining = self._limits.timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise WebAccessError("web fetch timed out")
            target = await self._policy.validate(current_url)
            request = HttpRequest(
                method="GET",
                url=target.url,
                allowed_target_ips=target.addresses,
                headers={
                    "Accept": "text/html,text/plain,application/json,application/pdf",
                    "Accept-Encoding": "gzip, deflate",
                    "User-Agent": self._user_agent,
                },
                timeout_seconds=remaining,
                max_response_bytes=self._limits.max_compressed_bytes,
            )
            try:
                response = await asyncio.wait_for(binding.transport.send(request), timeout=remaining)
            except TimeoutError as exc:
                raise WebAccessError("web fetch timed out") from exc
            self._policy.verify_connected_peer(target, response.connected_target_ip)
            if len(response.body) > self._limits.max_compressed_bytes:
                raise WebResponseTooLarge("web response exceeded the compressed limit")
            content_length = response.headers.get("content-length")
            if content_length is not None:
                try:
                    if int(content_length) > self._limits.max_compressed_bytes:
                        raise WebResponseTooLarge("declared web response is too large")
                except ValueError as exc:
                    raise WebAccessError("invalid HTTP Content-Length") from exc
            if response.status not in _REDIRECTS:
                break
            if redirect_count >= self._limits.max_redirects:
                raise WebPolicyDenied("web fetch exceeded the redirect limit")
            location = response.headers.get("location")
            if not location:
                raise WebAccessError("redirect response omitted Location")
            current_url = urljoin(target.url, location)
        if response is None:
            raise WebAccessError("web fetch produced no response")
        if not 200 <= response.status <= 299:
            raise WebAccessError(f"web server returned HTTP {response.status}")
        content = _bounded_decompress(
            response.body,
            response.headers.get("content-encoding", "").lower().strip(),
            self._limits.max_content_bytes,
        )
        declared_type = response.headers.get("content-type", "text/plain")
        media_type = _media_type(declared_type)
        title: str | None = None
        if media_type in {"text/html", "application/xhtml+xml"}:
            parser = _HtmlTextExtractor()
            parser.feed(_decode(content, declared_type))
            title = _clean_text(" ".join(parser.title_parts)) or None
            text = _clean_text(" ".join(parser.text_parts))
        elif media_type == "application/json" or media_type.endswith("+json"):
            try:
                value = json.loads(_decode(content, declared_type))
            except json.JSONDecodeError as exc:
                raise UnsupportedWebContent("invalid JSON response") from exc
            text = json.dumps(value, ensure_ascii=False, indent=2)
        elif media_type.startswith("text/"):
            text = _clean_text(_decode(content, declared_type))
        elif media_type == "application/pdf":
            if self._pdf_extractor is None:
                raise UnsupportedWebContent("PDF extraction requires a configured extractor")
            text = await self._pdf_extractor.extract(content, source_url=current_url)
        else:
            raise UnsupportedWebContent(f"unsupported web content type: {media_type}")
        truncated = len(text) > self._limits.max_text_characters
        if truncated:
            text = text[: self._limits.max_text_characters]
        return FetchDocument(
            requested_url=requested_url,
            final_url=current_url,
            status=response.status,
            content_type=media_type,
            title=title,
            text=text,
            truncated=truncated,
        )


__all__ = [
    "FetchTransportBinding",
    "FetchTransportRegistry",
    "PdfTextExtractor",
    "WebFetchService",
]
