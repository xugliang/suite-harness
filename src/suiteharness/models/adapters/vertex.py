"""Google Vertex Gemini adapter using a server service identity."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from pydantic import SecretStr

from suiteharness.models.adapters._common import response_id, response_object
from suiteharness.models.adapters.gemini import GeminiAdapter, _usage
from suiteharness.models.errors import ModelErrorCode, ModelProviderError
from suiteharness.models.protocols import AccessTokenProvider
from suiteharness.models.transport import HttpRequest, HttpTransport, iter_sse_json
from suiteharness.models.types import (
    FinishReason,
    ModelProfile,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelUsage,
    ProviderDescriptor,
    TextContent,
)


class GoogleAuthTokenProvider:
    """Optional google-auth bridge for workload/service-account credentials."""

    def __init__(
        self,
        *,
        service_account_info: Mapping[str, object] | str | None = None,
        allow_application_default: bool = False,
        scopes: tuple[str, ...] = ("https://www.googleapis.com/auth/cloud-platform",),
    ) -> None:
        try:
            import google.auth
            from google.auth.transport.requests import Request
        except ImportError as exc:  # pragma: no cover - deployment diagnostic
            raise ModelProviderError(
                ModelErrorCode.CONFIGURATION,
                "Vertex support requires google-auth or an injected AccessTokenProvider",
                provider_id="vertex",
            ) from exc
        if service_account_info is not None:
            try:
                from google.oauth2.service_account import Credentials

                raw = (
                    json.loads(service_account_info)
                    if isinstance(service_account_info, str)
                    else dict(service_account_info)
                )
                if not isinstance(raw, dict):
                    raise ValueError("service account JSON root must be an object")
                credentials = Credentials.from_service_account_info(raw, scopes=list(scopes))
            except Exception as exc:
                raise ModelProviderError(
                    ModelErrorCode.CONFIGURATION,
                    "Vertex service_account_json is invalid",
                    provider_id="vertex",
                ) from exc
        elif allow_application_default:
            credentials, _ = google.auth.default(scopes=list(scopes))
        else:
            raise ModelProviderError(
                ModelErrorCode.CREDENTIAL_MISSING,
                "Vertex requires service-account configuration or an injected token provider",
                provider_id="vertex",
            )
        self._credentials = credentials
        self._request_type = Request
        self._lock = asyncio.Lock()

    async def access_token(self, audience: str) -> str:
        del audience  # OAuth scopes are fixed by the server credential configuration.
        async with self._lock:
            expiry = getattr(self._credentials, "expiry", None)
            now = datetime.now(UTC)
            if expiry is not None and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            valid = bool(getattr(self._credentials, "valid", False))
            if not valid or (expiry is not None and expiry <= now + timedelta(minutes=2)):
                await asyncio.to_thread(self._credentials.refresh, self._request_type())
            token = getattr(self._credentials, "token", None)
            if not isinstance(token, str) or not token:
                raise ModelProviderError(
                    ModelErrorCode.AUTHENTICATION,
                    "Google service identity returned no access token",
                    provider_id="vertex",
                )
            return token


class VertexGeminiAdapter(GeminiAdapter):
    def __init__(
        self,
        descriptor: ProviderDescriptor,
        profile: ModelProfile,
        transport: HttpTransport,
        token_provider: AccessTokenProvider,
    ) -> None:
        # The inherited conversion/parser helpers do not use this placeholder;
        # every HTTP request below obtains a short-lived service token.
        super().__init__(descriptor, profile, transport, SecretStr("vertex-service-identity"))
        self._token_provider = token_provider
        project = profile.provider_options.get("project")
        location = profile.provider_options.get("location", "us-central1")
        publisher = profile.provider_options.get("publisher", "google")
        api_version = profile.provider_options.get("api_version", "v1")
        if not all(isinstance(value, str) and value for value in (project, location, publisher)):
            raise ModelProviderError(
                ModelErrorCode.CONFIGURATION,
                "Vertex profile requires string project, location and publisher options",
                provider_id=self.provider_id,
            )
        if api_version not in {"v1", "v1beta1"}:
            raise ModelProviderError(
                ModelErrorCode.CONFIGURATION,
                "Vertex api_version must be v1 or v1beta1",
                provider_id=self.provider_id,
            )
        base = profile.base_url or f"https://{location}-aiplatform.googleapis.com/{api_version}"
        self._vertex_base = (
            f"{base.rstrip('/')}/projects/{quote(project, safe='')}/locations/"
            f"{quote(location, safe='')}/publishers/{quote(publisher, safe='')}/models"
        )
        self._audience = "https://www.googleapis.com/auth/cloud-platform"

    async def _vertex_request(self, request: ModelRequest, *, stream: bool) -> HttpRequest:
        model = request.model or self._profile.model
        operation = "streamGenerateContent?alt=sse" if stream else "generateContent"
        token = await self._token_provider.access_token(self._audience)
        return HttpRequest(
            method="POST",
            url=f"{self._vertex_base}/{quote(model, safe='')}:" + operation,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                **self._profile.default_headers,
            },
            json_body=self._body(request),
            timeout_seconds=self._profile.timeout_seconds,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        wire_request = await self._vertex_request(request, stream=False)
        response = await self._transport.send(wire_request)
        payload = response_object(response, self.provider_id)
        text, calls, reason = self._candidate(payload)
        return ModelResponse(
            response_id=response_id(payload.get("responseId")),
            provider_id=self.provider_id,
            model=request.model or self._profile.model,
            content=tuple(TextContent(text=value) for value in text),
            tool_calls=tuple(calls),
            finish_reason=reason,
            usage=_usage(payload.get("usageMetadata")),
        )

    async def _vertex_stream(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        wire_request = await self._vertex_request(request, stream=True)
        identifier: str | None = None
        reason = FinishReason.UNKNOWN
        usage = ModelUsage()
        async for payload in iter_sse_json(self._transport.stream(wire_request)):
            if identifier is None:
                identifier = response_id(payload.get("responseId"))
                yield ModelStreamEvent(
                    type=ModelStreamEventType.MESSAGE_START,
                    response_id=identifier,
                )
            values, calls, current_reason = self._candidate(payload)
            if current_reason is not FinishReason.UNKNOWN:
                reason = current_reason
            for value in values:
                if value:
                    yield ModelStreamEvent(
                        type=ModelStreamEventType.TEXT_DELTA,
                        text_delta=value,
                    )
            for call in calls:
                yield ModelStreamEvent(
                    type=ModelStreamEventType.TOOL_CALL_DELTA,
                    tool_call=call,
                )
            raw_usage = payload.get("usageMetadata")
            if isinstance(raw_usage, dict):
                usage = _usage(raw_usage)
                yield ModelStreamEvent(type=ModelStreamEventType.USAGE, usage=usage)
        if identifier is None:
            identifier = response_id(None)
            yield ModelStreamEvent(
                type=ModelStreamEventType.MESSAGE_START,
                response_id=identifier,
            )
        yield ModelStreamEvent(
            type=ModelStreamEventType.MESSAGE_END,
            response_id=identifier,
            finish_reason=reason,
            usage=usage,
        )

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        return self._vertex_stream(request)


def vertex_factory(token_provider: AccessTokenProvider):
    """Return a registry factory bound to a deployment service identity."""

    def create(
        descriptor: ProviderDescriptor,
        profile: ModelProfile,
        transport: HttpTransport,
        credential: SecretStr | None,
    ) -> VertexGeminiAdapter:
        del credential
        return VertexGeminiAdapter(descriptor, profile, transport, token_provider)

    return create
