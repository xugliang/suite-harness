"""Raw-body authentication and decryption seams for Feishu callbacks."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable, Mapping
from typing import Protocol

from .models import FeishuAuthenticationError, FeishuPayloadError


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value
    return None


class RawEventDecryptor(Protocol):
    """Decrypt a raw callback envelope and return the inner JSON bytes.

    Implementations may parse only the provider's encrypted *envelope* in
    order to obtain its ciphertext. The event payload itself is parsed only
    after this method returns.
    """

    async def decrypt(self, body: bytes) -> bytes: ...


class RawSignatureVerifier(Protocol):
    """Authenticate headers and the exact request bytes before JSON parsing."""

    def verify(self, headers: Mapping[str, str], body: bytes) -> None: ...


class FeishuHeaderSignatureVerifier:
    """Verify Feishu's SHA-256 callback signature over the untouched body.

    Feishu defines the input as ``timestamp + nonce + encrypt_key + body``.
    Header lookup is case-insensitive and comparison is constant-time.
    """

    def __init__(
        self,
        encrypt_key: str,
        *,
        max_clock_skew_seconds: int | None = 300,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not encrypt_key:
            raise ValueError("Feishu encrypt_key must not be blank")
        if max_clock_skew_seconds is not None and max_clock_skew_seconds < 0:
            raise ValueError("max_clock_skew_seconds must be non-negative or None")
        if clock is None:
            import time

            clock = time.time
        self._key = encrypt_key.encode("utf-8")
        self._max_clock_skew = max_clock_skew_seconds
        self._clock = clock

    def verify(self, headers: Mapping[str, str], body: bytes) -> None:
        timestamp = _header(headers, "X-Lark-Request-Timestamp")
        nonce = _header(headers, "X-Lark-Request-Nonce")
        supplied = _header(headers, "X-Lark-Signature")
        if timestamp is None or nonce is None or supplied is None:
            raise FeishuAuthenticationError("missing Feishu callback signature headers")
        if len(timestamp) > 20 or not timestamp.isascii() or not timestamp.isdecimal():
            raise FeishuAuthenticationError("invalid Feishu callback timestamp")
        if not nonce or len(nonce) > 256 or "\x00" in nonce:
            raise FeishuAuthenticationError("invalid Feishu callback nonce")
        if self._max_clock_skew is not None:
            difference = abs(float(self._clock()) - int(timestamp))
            if difference > self._max_clock_skew:
                raise FeishuAuthenticationError("stale Feishu callback timestamp")
        expected = hashlib.sha256(
            timestamp.encode("ascii") + nonce.encode("utf-8") + self._key + body
        ).hexdigest()
        if not re.fullmatch(r"[0-9A-Fa-f]{64}", supplied) or not hmac.compare_digest(
            supplied.casefold(), expected
        ):
            raise FeishuAuthenticationError("invalid Feishu callback signature")


class IdentityDecryptor:
    """Explicit no-encryption implementation, useful for verified plaintext events."""

    async def decrypt(self, body: bytes) -> bytes:
        return body


class BoundedDecryptor:
    """Defend the parser from a decryptor returning an oversized plaintext."""

    def __init__(self, delegate: RawEventDecryptor, *, max_plaintext_bytes: int) -> None:
        if max_plaintext_bytes <= 0:
            raise ValueError("max_plaintext_bytes must be positive")
        self._delegate = delegate
        self._maximum = max_plaintext_bytes

    async def decrypt(self, body: bytes) -> bytes:
        plaintext = await self._delegate.decrypt(body)
        if not isinstance(plaintext, bytes):
            raise FeishuPayloadError("Feishu decryptor returned non-bytes data")
        if len(plaintext) > self._maximum:
            raise FeishuPayloadError("decrypted Feishu event is too large")
        return plaintext


__all__ = [
    "BoundedDecryptor",
    "FeishuHeaderSignatureVerifier",
    "IdentityDecryptor",
    "RawEventDecryptor",
    "RawSignatureVerifier",
]
