"""Optional, fail-open observation seam.

The core defines an event contract, not a tracing vendor or exporter. A
deployment may provide ``SUITEHARNESS_OBSERVER_FACTORY=module:function``. No
observer is imported or instantiated when that setting is absent. Sinks must
enqueue promptly; network or disk I/O belongs outside the request path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from importlib import import_module
import os
import re
import sys
from threading import Lock
from typing import Any, Callable, Mapping


class ObservationSink(ABC):
    @abstractmethod
    def record(self, event: str, payload: Mapping[str, Any]) -> None:
        """Accept one event without waiting for external I/O."""


class NoopObservationSink(ObservationSink):
    def record(self, event: str, payload: Mapping[str, Any]) -> None:
        return None


_sink: ObservationSink = NoopObservationSink()
_configured = False
_lock = Lock()
_factory_name = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$")


def configure(sink: ObservationSink | None) -> None:
    """Explicitly set a sink; ``None`` disables observation."""
    global _sink, _configured
    with _lock:
        _sink = sink if sink is not None else NoopObservationSink()
        _configured = True


def _load_from_env() -> None:
    global _sink, _configured
    if _configured:
        return
    with _lock:
        if _configured:
            return
        name = os.environ.get("SUITEHARNESS_OBSERVER_FACTORY", "").strip()
        if name:
            try:
                if not _factory_name.fullmatch(name):
                    raise ValueError("invalid observation factory name")
                module_name, function_name = name.split(":", 1)
                candidate = getattr(import_module(module_name), function_name)()
                if not isinstance(candidate, ObservationSink):
                    raise TypeError("observation factory must return ObservationSink")
                _sink = candidate
            except Exception:
                # Observation is never an authority for business execution.
                sys.stderr.write("suiteharness observer unavailable; running without tracing\n")
        _configured = True


def enabled() -> bool:
    _load_from_env()
    return not isinstance(_sink, NoopObservationSink)


def publish(event: str, **payload: Any) -> None:
    if not enabled():
        return
    try:
        try:
            from suiteharness.diagnostics import current_fields
        except ImportError:
            context = {}
        else:
            context = current_fields()
        context = dict(context)
        for key in ("request_id", "run_id", "product_id", "channel_id", "correlation_id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                context[key] = value
        _sink.record(event, {"context": context, **payload})
    except Exception:
        # A broken exporter cannot break a model or tool call.
        sys.stderr.write("suiteharness observer event dropped\n")


def capture(event: str, make_payload: Callable[[], Mapping[str, Any]]) -> None:
    """Build expensive observation content only when enabled, fail-open."""
    if not enabled():
        return
    try:
        publish(event, **make_payload())
    except Exception:
        sys.stderr.write("suiteharness observer event dropped\n")


_SECRET_KEYS = frozenset({
    "apikey", "xapikey", "secret", "password", "authorization", "credential",
    "accesstoken", "refreshtoken", "clientsecret", "privatekey", "accesskey",
    "token", "bearertoken", "secretkey", "sessionkey",
})


def safe_value(value: Any, *, _schema: bool = False) -> Any:
    """Keep text verbatim while excluding binary bodies and named credentials."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            lowered = name.lower()
            normalized = re.sub(r"[^a-z0-9]", "", lowered)
            if normalized in _SECRET_KEYS and not _schema:
                result[name] = "[REDACTED]"
            elif lowered == "data_base64":
                result[name] = f"[binary omitted: {len(item)} base64 characters]" if isinstance(item, str) else "[binary omitted]"
            elif lowered == "url" and isinstance(item, str) and item.startswith("data:"):
                result[name] = "[data URL omitted]"
            else:
                result[name] = safe_value(item, _schema=_schema or lowered in {"input_schema", "response_schema"})
        return result
    if isinstance(value, (list, tuple)):
        return [safe_value(item, _schema=_schema) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return f"[binary omitted: {len(value)} bytes]"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def model_request_payload(request: Any) -> dict[str, Any]:
    """The exact prepared model-visible messages and tools, excluding metadata."""
    data = request.model_dump(mode="json", exclude_none=True, exclude={"metadata"})
    result = safe_value(data)
    result["message_categories"] = [
        None if getattr(message, "context_category", None) is None else getattr(
            message.context_category, "value", message.context_category
        )
        for message in request.messages
    ]
    return result


def model_response_payload(response: Any) -> dict[str, Any]:
    data = response.model_dump(mode="json", exclude_none=True, exclude={"provider_metadata"})
    return safe_value(data)
