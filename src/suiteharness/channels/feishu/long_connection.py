"""Official-SDK seam for Feishu long-connection delivery."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from .models import FeishuEventOutcome, FeishuPayloadError
from .processor import FeishuEventProcessor

VerifiedEventCallback = Callable[[bytes], Awaitable[FeishuEventOutcome]]


class FeishuLongConnectionSdk(Protocol):
    """Narrow adapter implemented around Feishu's official WebSocket SDK.

    The SDK owns authentication, reconnect and acknowledgement. It must pass
    the verified inner event as UTF-8 JSON bytes to ``callback`` and must wait
    for the callback result before acknowledging the event.
    """

    async def run(self, callback: VerifiedEventCallback) -> None: ...

    async def close(self) -> None: ...


class FeishuLongConnectionRunner:
    """Feed SDK-authenticated events through the same normalizer as webhooks."""

    def __init__(
        self,
        sdk: FeishuLongConnectionSdk,
        processor: FeishuEventProcessor,
        *,
        max_event_bytes: int = 1_048_576,
    ) -> None:
        if max_event_bytes <= 0:
            raise ValueError("max_event_bytes must be positive")
        self._sdk = sdk
        self._processor = processor
        self._maximum = max_event_bytes

    async def run(self) -> None:
        await self._sdk.run(self.accept_verified_event)

    async def accept_verified_event(self, body: bytes) -> FeishuEventOutcome:
        if not isinstance(body, bytes):
            raise FeishuPayloadError("Feishu SDK event must be bytes")
        if len(body) > self._maximum:
            raise FeishuPayloadError("Feishu SDK event is too large")
        return await self._processor.accept_verified_json(body)

    async def close(self) -> None:
        await self._sdk.close()


__all__ = [
    "FeishuLongConnectionRunner",
    "FeishuLongConnectionSdk",
    "VerifiedEventCallback",
]
