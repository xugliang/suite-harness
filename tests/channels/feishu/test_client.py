from __future__ import annotations

import asyncio
import json

import pytest

from suiteharness.channels.feishu import (
    CachedTenantAccessTokenProvider,
    FeishuApiError,
    FeishuHttpResponse,
    FeishuMessageClient,
)


class Transport:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.requests = []
        self.gate: asyncio.Event | None = None

    async def send(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        if self.gate is not None:
            await self.gate.wait()
        payload = self.responses.pop(0)
        return FeishuHttpResponse(200, json.dumps(payload).encode())


def test_tenant_token_cache_is_concurrency_safe_and_redacts_secret() -> None:
    async def exercise():
        transport = Transport(
            [{"code": 0, "tenant_access_token": "token-secret", "expire": 7200}]
        )
        transport.gate = asyncio.Event()
        provider = CachedTenantAccessTokenProvider(
            transport,
            app_id="cli-app",
            app_secret="app-secret",
        )
        first = asyncio.create_task(provider.get_token())
        second = asyncio.create_task(provider.get_token())
        await asyncio.sleep(0)
        transport.gate.set()
        values = await asyncio.gather(first, second)
        return transport, values, provider

    transport, values, provider = asyncio.run(exercise())
    assert values == ["token-secret", "token-secret"]
    assert len(transport.requests) == 1
    assert "app-secret" not in repr(transport.requests[0])
    assert "token-secret" not in repr(provider.__dict__)


def test_message_client_checks_business_code_and_builds_text_message() -> None:
    class Tokens:
        async def get_token(self) -> str:
            return "tenant-token"

    transport = Transport([{"code": 0, "data": {"message_id": "om-result"}}])
    result = asyncio.run(
        FeishuMessageClient(transport, Tokens()).send_text("oc-chat", "你好，企业")
    )
    assert result.message_id == "om-result"
    request = transport.requests[0]
    assert request.headers["Authorization"] == "Bearer tenant-token"
    assert request.url.endswith("/open-apis/im/v1/messages?receive_id_type=chat_id")
    assert request.json_body["receive_id"] == "oc-chat"
    assert json.loads(request.json_body["content"]) == {"text": "你好，企业"}
    assert "tenant-token" not in repr(request)


def test_nonzero_feishu_business_code_is_failure_without_leaking_body() -> None:
    class Tokens:
        async def get_token(self) -> str:
            return "tenant-token"

    transport = Transport([{"code": 230001, "msg": "sensitive tenant detail"}])
    with pytest.raises(FeishuApiError) as caught:
        asyncio.run(FeishuMessageClient(transport, Tokens()).send_text("oc-chat", "hello"))
    assert "230001" in str(caught.value)
    assert "sensitive" not in str(caught.value)


def test_token_endpoint_business_code_is_checked() -> None:
    transport = Transport([{"code": 10003, "msg": "bad secret"}])
    provider = CachedTenantAccessTokenProvider(
        transport, app_id="cli-app", app_secret="app-secret"
    )
    with pytest.raises(FeishuApiError, match="business code 10003"):
        asyncio.run(provider.get_token())
