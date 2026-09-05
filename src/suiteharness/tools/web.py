"""Execution-runner-compatible handlers for controlled web services."""

from __future__ import annotations

from pydantic import JsonValue

from suiteharness.execution import ToolCallContext
from suiteharness.web import SearchRequest, WebFetchService, WebSearchService

from .egress import ProductEgressArgumentPolicy


class WebSearchTool:
    def __init__(
        self,
        service: WebSearchService,
        egress_policy: ProductEgressArgumentPolicy | None = None,
    ) -> None:
        self._service = service
        self._egress_policy = egress_policy

    async def __call__(
        self,
        context: ToolCallContext,
        arguments: dict[str, JsonValue],
    ) -> JsonValue:
        query = arguments.get("query")
        count = arguments.get("count", 10)
        provider = arguments.get("provider")
        freshness = arguments.get("freshness")
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        if not isinstance(count, int) or isinstance(count, bool):
            raise ValueError("count must be an integer")
        if provider is not None and not isinstance(provider, str):
            raise ValueError("provider must be a string")
        if freshness is not None and not isinstance(freshness, str):
            raise ValueError("freshness must be a string")
        if self._egress_policy is not None:
            provider = self._egress_policy.authorize_search(context, provider)
        response = await self._service.search(
            SearchRequest(query=query, count=count, freshness=freshness),
            provider_id=provider,
        )
        return {
            "provider": response.provider_id,
            "query": response.query,
            "results": [
                {
                    "title": item.title,
                    "url": item.url,
                    "snippet": item.snippet,
                    "published_at": item.published_at,
                }
                for item in response.results
            ],
        }


class WebFetchTool:
    def __init__(
        self,
        service: WebFetchService,
        egress_policy: ProductEgressArgumentPolicy | None = None,
    ) -> None:
        self._service = service
        self._egress_policy = egress_policy

    async def __call__(
        self,
        context: ToolCallContext,
        arguments: dict[str, JsonValue],
    ) -> JsonValue:
        url = arguments.get("url")
        profile = arguments.get("egress_profile")
        if not isinstance(url, str):
            raise ValueError("url must be a string")
        if profile is not None and not isinstance(profile, str):
            raise ValueError("egress_profile must be a string")
        if self._egress_policy is not None:
            profile = self._egress_policy.authorize_fetch(context, profile)
        document = await self._service.fetch(url, egress_profile=profile)
        return {
            "requested_url": document.requested_url,
            "final_url": document.final_url,
            "status": document.status,
            "content_type": document.content_type,
            "title": document.title,
            "text": document.text,
            "truncated": document.truncated,
        }


__all__ = ["WebFetchTool", "WebSearchTool"]
