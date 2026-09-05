"""Short-lived company Web session tickets and WebSocket authentication.

These tickets are issued only after the surrounding Web application completes
company SSO.  They are not a personal-login mechanism.  The small verifier is a
server-side reference implementation; deployments can inject an OIDC/JWT-backed
``WebSocketAuthenticator`` with the same protocol instead.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from suiteharness.channels.models import AuthenticatedPrincipal

from .models import WebSocketHandshake

_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_COOKIE_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HEADER = {"alg": "HS256", "typ": "SUITEHARNESS-SESSION", "v": 1}
_CLAIM_KEYS = {"aud", "exp", "iat", "iss", "principal_id", "roles", "tenant_id"}


class SessionTokenError(ValueError):
    """A safe, non-secret-bearing company session ticket error."""


@dataclass(frozen=True, slots=True)
class CompanySessionClaims:
    tenant_id: str
    principal_id: str
    roles: frozenset[str]
    issuer: str
    audience: str
    iat: int
    exp: int

    def principal(self) -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(
            tenant_id=self.tenant_id,
            principal_id=self.principal_id,
            roles=self.roles,
        )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    if not value or not _BASE64URL.fullmatch(value):
        raise SessionTokenError("invalid company session ticket")
    padding_length = (-len(value)) % 4
    try:
        decoded = base64.b64decode(
            value + ("=" * padding_length),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise SessionTokenError("invalid company session ticket") from exc
    if _base64url_encode(decoded) != value:
        raise SessionTokenError("non-canonical company session ticket encoding")
    return decoded


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SessionTokenError("duplicate company session ticket JSON key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise SessionTokenError("invalid company session ticket JSON number")


def _decode_json_object(value: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(
            value.decode("utf-8"),
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_json_constant,
        )
    except SessionTokenError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise SessionTokenError("invalid company session ticket JSON") from exc
    if not isinstance(parsed, dict):
        raise SessionTokenError("company session ticket JSON must be an object")
    return parsed


def _bounded_text(value: str, *, field: str) -> str:
    if not value or len(value) > 512 or "\x00" in value:
        raise ValueError(f"invalid session token {field}")
    return value


class HmacSessionTokenCodec:
    """Issue and verify fixed-algorithm HMAC-SHA256 company session tickets."""

    def __init__(
        self,
        key: bytes,
        *,
        issuer: str,
        audience: str,
        tenant_id: str,
        max_lifetime_seconds: int = 900,
        future_clock_skew_seconds: int = 0,
        max_token_chars: int = 8192,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("session token key must contain at least 32 bytes")
        if not isinstance(max_lifetime_seconds, int) or max_lifetime_seconds <= 0:
            raise ValueError("max_lifetime_seconds must be a positive integer")
        if not isinstance(future_clock_skew_seconds, int) or future_clock_skew_seconds < 0:
            raise ValueError("future_clock_skew_seconds must be a non-negative integer")
        if not isinstance(max_token_chars, int) or max_token_chars <= 0:
            raise ValueError("max_token_chars must be a positive integer")
        try:
            principal = AuthenticatedPrincipal(
                tenant_id=tenant_id,
                principal_id="validation-principal",
            )
        except ValidationError as exc:
            raise ValueError("invalid session token tenant_id") from exc
        self._key = key
        self._issuer = _bounded_text(issuer, field="issuer")
        self._audience = _bounded_text(audience, field="audience")
        self._tenant_id = principal.tenant_id
        self._maximum_lifetime = max_lifetime_seconds
        self._future_clock_skew = future_clock_skew_seconds
        self._maximum_chars = max_token_chars
        self._clock = clock

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(issuer={self._issuer!r}, "
            f"audience={self._audience!r}, tenant_id={self._tenant_id!r}, key=<redacted>)"
        )

    def issue(
        self,
        principal: AuthenticatedPrincipal,
        *,
        lifetime_seconds: int = 300,
    ) -> str:
        if principal.tenant_id != self._tenant_id:
            raise ValueError("cannot issue a session ticket for another tenant")
        if (
            not isinstance(lifetime_seconds, int)
            or isinstance(lifetime_seconds, bool)
            or lifetime_seconds <= 0
            or lifetime_seconds > self._maximum_lifetime
        ):
            raise ValueError("session ticket lifetime is outside the configured limit")
        issued_at = int(self._clock())
        header = _base64url_encode(_canonical_json(_HEADER))
        payload = _base64url_encode(
            _canonical_json(
                {
                    "aud": self._audience,
                    "exp": issued_at + lifetime_seconds,
                    "iat": issued_at,
                    "iss": self._issuer,
                    "principal_id": principal.principal_id,
                    "roles": sorted(principal.roles),
                    "tenant_id": principal.tenant_id,
                }
            )
        )
        signed = f"{header}.{payload}".encode("ascii")
        signature = _base64url_encode(hmac.digest(self._key, signed, hashlib.sha256))
        return f"{header}.{payload}.{signature}"

    def verify(self, token: str) -> CompanySessionClaims:
        if (
            not isinstance(token, str)
            or not token
            or len(token) > self._maximum_chars
            or not token.isascii()
        ):
            raise SessionTokenError("invalid company session ticket")
        segments = token.split(".")
        if len(segments) != 3:
            raise SessionTokenError("invalid company session ticket")
        header_segment, payload_segment, signature_segment = segments
        signature = _base64url_decode(signature_segment)
        if len(signature) != hashlib.sha256().digest_size:
            raise SessionTokenError("invalid company session ticket signature")
        signed = f"{header_segment}.{payload_segment}".encode("ascii")
        expected = hmac.digest(self._key, signed, hashlib.sha256)
        if not hmac.compare_digest(signature, expected):
            raise SessionTokenError("invalid company session ticket signature")

        header = _decode_json_object(_base64url_decode(header_segment))
        if header != _HEADER:
            raise SessionTokenError("unsupported company session ticket algorithm")
        payload = _decode_json_object(_base64url_decode(payload_segment))
        if set(payload) != _CLAIM_KEYS:
            raise SessionTokenError("invalid company session ticket claims")
        return self._validate_claims(payload)

    def _validate_claims(self, payload: dict[str, Any]) -> CompanySessionClaims:
        issuer = payload["iss"]
        audience = payload["aud"]
        tenant_id = payload["tenant_id"]
        principal_id = payload["principal_id"]
        roles = payload["roles"]
        issued_at = payload["iat"]
        expires_at = payload["exp"]
        if not all(isinstance(item, str) for item in (issuer, audience, tenant_id, principal_id)):
            raise SessionTokenError("invalid company session ticket claims")
        if not hmac.compare_digest(issuer, self._issuer):
            raise SessionTokenError("company session ticket issuer mismatch")
        if not hmac.compare_digest(audience, self._audience):
            raise SessionTokenError("company session ticket audience mismatch")
        if not hmac.compare_digest(tenant_id, self._tenant_id):
            raise SessionTokenError("company session ticket tenant mismatch")
        if (
            not isinstance(issued_at, int)
            or isinstance(issued_at, bool)
            or not isinstance(expires_at, int)
            or isinstance(expires_at, bool)
        ):
            raise SessionTokenError("invalid company session ticket timestamps")
        now = int(self._clock())
        if issued_at > now + self._future_clock_skew:
            raise SessionTokenError("company session ticket was issued in the future")
        if expires_at <= now:
            raise SessionTokenError("company session ticket has expired")
        if expires_at <= issued_at or expires_at - issued_at > self._maximum_lifetime:
            raise SessionTokenError("company session ticket lifetime is invalid")
        if (
            not isinstance(roles, list)
            or any(not isinstance(role, str) for role in roles)
            or len(roles) != len(set(roles))
        ):
            raise SessionTokenError("invalid company session ticket roles")
        try:
            principal = AuthenticatedPrincipal(
                tenant_id=tenant_id,
                principal_id=principal_id,
                roles=frozenset(roles),
            )
        except ValidationError as exc:
            raise SessionTokenError("invalid company session ticket identity") from exc
        return CompanySessionClaims(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            roles=principal.roles,
            issuer=issuer,
            audience=audience,
            iat=issued_at,
            exp=expires_at,
        )


def _authorization_token(value: str | None) -> tuple[str | None, bool]:
    if value is None:
        return None, True
    match = re.fullmatch(r"(?i:Bearer) ([A-Za-z0-9._-]+)", value)
    if match is None:
        return None, False
    return match.group(1), True


def _cookie_token(header: str | None, name: str | None) -> tuple[str | None, bool]:
    if header is None or name is None:
        return None, True
    found: list[str] = []
    for part in header.split(";"):
        pair = part.strip()
        if not pair or "=" not in pair:
            continue
        candidate_name, value = pair.split("=", 1)
        if candidate_name == name:
            found.append(value)
    if len(found) > 1:
        return None, False
    if not found:
        return None, True
    token = found[0]
    if not token or re.fullmatch(r"[A-Za-z0-9._-]+", token) is None:
        return None, False
    return token, True


class ServerSessionWebSocketAuthenticator:
    """Authenticate a bearer header or one exact, configured session cookie."""

    def __init__(
        self,
        codec: HmacSessionTokenCodec,
        *,
        cookie_name: str | None = None,
    ) -> None:
        if cookie_name is not None and not _COOKIE_NAME.fullmatch(cookie_name):
            raise ValueError("invalid company session cookie name")
        self._codec = codec
        self._cookie_name = cookie_name

    async def authenticate(
        self,
        handshake: WebSocketHandshake,
    ) -> AuthenticatedPrincipal | None:
        bearer, valid_authorization = _authorization_token(handshake.authorization)
        cookie, valid_cookie = _cookie_token(handshake.cookie_header, self._cookie_name)
        if not valid_authorization or not valid_cookie:
            return None
        if bearer is not None and cookie is not None and not hmac.compare_digest(bearer, cookie):
            return None
        token = bearer or cookie
        if token is None:
            return None
        try:
            return self._codec.verify(token).principal()
        except SessionTokenError:
            return None


__all__ = [
    "CompanySessionClaims",
    "HmacSessionTokenCodec",
    "ServerSessionWebSocketAuthenticator",
    "SessionTokenError",
]
