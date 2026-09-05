"""Optional Starlette ASGI route for Feishu webhook callbacks."""

from __future__ import annotations

from typing import Any

from .models import FeishuAuthenticationError, FeishuPayloadError
from .processor import FeishuWebhookHandler


def _require_starlette() -> tuple[type[Any], type[Any]]:
    try:
        from starlette.responses import JSONResponse
        from starlette.routing import Route
    except ImportError as exc:  # pragma: no cover - deployment diagnostic
        raise RuntimeError(
            "the Feishu webhook ASGI adapter requires the optional 'starlette' dependency"
        ) from exc
    return Route, JSONResponse


def create_starlette_feishu_endpoint(handler: FeishuWebhookHandler) -> Any:
    """Create an endpoint that preserves raw bytes until the handler verifies them."""

    _route, response_type = _require_starlette()

    async def endpoint(request: Any) -> Any:
        declared_length = _content_length(request)
        if declared_length is False:
            return response_type({"code": "invalid_content_length"}, status_code=400)
        if isinstance(declared_length, int) and declared_length > handler.max_body_bytes:
            return response_type({"code": "payload_too_large"}, status_code=413)
        body = bytearray()
        try:
            async for chunk in request.stream():
                if not isinstance(chunk, bytes):
                    return response_type({"code": "invalid_payload"}, status_code=400)
                if len(body) + len(chunk) > handler.max_body_bytes:
                    return response_type({"code": "payload_too_large"}, status_code=413)
                body.extend(chunk)
        except Exception:
            return response_type({"code": "temporary_failure"}, status_code=500)
        try:
            response = await handler.handle(request.headers, bytes(body))
        except FeishuAuthenticationError:
            return response_type({"code": "authentication_failed"}, status_code=401)
        except FeishuPayloadError:
            return response_type({"code": "invalid_payload"}, status_code=400)
        except Exception:
            # An application failure must produce a retryable status without
            # exposing tenant data or internal exception details to Feishu.
            return response_type({"code": "temporary_failure"}, status_code=500)
        return response_type(response.body, status_code=response.status_code)

    return endpoint


def _content_length(request: Any) -> int | None | bool:
    """Return one valid declared length; ``False`` denotes an invalid header."""

    values = [
        value.decode("latin-1")
        for name, value in request.scope.get("headers", ())
        if name.lower() == b"content-length"
    ]
    if not values:
        return None
    if len(values) != 1 or not values[0].isdigit():
        return False
    try:
        value = int(values[0])
    except ValueError:  # pragma: no cover - guarded by ``isdigit``
        return False
    return value


def create_starlette_feishu_route(path: str, handler: FeishuWebhookHandler) -> Any:
    route_type, _response = _require_starlette()
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("Feishu webhook route path must be absolute")
    return route_type(path, create_starlette_feishu_endpoint(handler), methods=["POST"])


__all__ = ["create_starlette_feishu_endpoint", "create_starlette_feishu_route"]
