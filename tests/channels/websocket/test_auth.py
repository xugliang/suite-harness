from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json

import pytest

from suiteharness.channels import AuthenticatedPrincipal
from suiteharness.channels.websocket import (
    HmacSessionTokenCodec,
    ServerSessionWebSocketAuthenticator,
    SessionTokenError,
    WebSocketHandshake,
)

KEY = b"company-server-session-key-32-bytes-minimum"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _signed_raw(header: bytes, payload: bytes, *, header_suffix: str = "") -> str:
    header_segment = _b64(header) + header_suffix
    payload_segment = _b64(payload)
    signed = f"{header_segment}.{payload_segment}".encode("ascii")
    signature = _b64(hmac.digest(KEY, signed, hashlib.sha256))
    return f"{header_segment}.{payload_segment}.{signature}"


def _codec(clock: list[float], **overrides: object) -> HmacSessionTokenCodec:
    values: dict[str, object] = {
        "issuer": "https://sso.acme.example",
        "audience": "suiteharness-websocket",
        "tenant_id": "acme",
        "max_lifetime_seconds": 600,
        "clock": lambda: clock[0],
    }
    values.update(overrides)
    return HmacSessionTokenCodec(KEY, **values)  # type: ignore[arg-type]


def _principal(*, tenant_id: str = "acme") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        tenant_id=tenant_id,
        principal_id="employee-42",
        roles=frozenset({"employee", "sales"}),
    )


def test_bearer_ticket_round_trip_binds_identity_and_redacts_key() -> None:
    clock = [1_900_000_000.0]
    codec = _codec(clock)
    token = codec.issue(_principal(), lifetime_seconds=300)
    authenticator = ServerSessionWebSocketAuthenticator(codec)

    principal = asyncio.run(
        authenticator.authenticate(
            WebSocketHandshake(
                origin="https://assistant.acme.example",
                authorization=f"Bearer {token}",
            )
        )
    )

    assert principal == _principal()
    claims = codec.verify(token)
    assert claims.issuer == "https://sso.acme.example"
    assert claims.audience == "suiteharness-websocket"
    assert claims.iat == 1_900_000_000
    assert claims.exp == 1_900_000_300
    assert KEY.decode("ascii") not in repr(codec)
    assert "<redacted>" in repr(codec)


def test_exact_cookie_is_optional_and_ambiguous_credentials_are_rejected() -> None:
    clock = [1_900_000_000.0]
    codec = _codec(clock)
    token = codec.issue(_principal())
    authenticator = ServerSessionWebSocketAuthenticator(codec, cookie_name="suiteharness_session")

    cookie_principal = asyncio.run(
        authenticator.authenticate(
            WebSocketHandshake(
                origin="https://assistant.acme.example",
                cookie_header=f"unrelated=1; suiteharness_session={token}; SuiteHarness_session=ignored",
            )
        )
    )
    wrong_case = asyncio.run(
        authenticator.authenticate(
            WebSocketHandshake(
                origin="https://assistant.acme.example",
                cookie_header=f"SuiteHarness_session={token}",
            )
        )
    )
    duplicate = asyncio.run(
        authenticator.authenticate(
            WebSocketHandshake(
                origin="https://assistant.acme.example",
                cookie_header=f"suiteharness_session={token}; suiteharness_session={token}",
            )
        )
    )
    malformed_header_with_cookie = asyncio.run(
        authenticator.authenticate(
            WebSocketHandshake(
                origin="https://assistant.acme.example",
                authorization=f"Bearer  {token}",
                cookie_header=f"suiteharness_session={token}",
            )
        )
    )
    other_token = token[:-1] + ("A" if token[-1] != "A" else "B")
    conflicting = asyncio.run(
        authenticator.authenticate(
            WebSocketHandshake(
                origin="https://assistant.acme.example",
                authorization=f"Bearer {token}",
                cookie_header=f"suiteharness_session={other_token}",
            )
        )
    )

    assert cookie_principal == _principal()
    assert wrong_case is None
    assert duplicate is None
    assert malformed_header_with_cookie is None
    assert conflicting is None


def test_expired_and_future_tickets_are_rejected() -> None:
    clock = [1_900_000_000.0]
    codec = _codec(clock)
    expired = codec.issue(_principal(), lifetime_seconds=10)
    clock[0] += 10
    with pytest.raises(SessionTokenError, match="expired"):
        codec.verify(expired)

    clock[0] = 1_900_000_100.0
    future = codec.issue(_principal(), lifetime_seconds=10)
    clock[0] = 1_900_000_099.0
    with pytest.raises(SessionTokenError, match="future"):
        codec.verify(future)


def test_cross_tenant_audience_and_issuer_tickets_are_rejected() -> None:
    clock = [1_900_000_000.0]
    verifier = _codec(clock)
    other_tenant = _codec(clock, tenant_id="other")
    other_audience = _codec(clock, audience="other-service")
    other_issuer = _codec(clock, issuer="https://evil.example")

    cases = (
        (other_tenant.issue(_principal(tenant_id="other")), "tenant"),
        (other_audience.issue(_principal()), "audience"),
        (other_issuer.issue(_principal()), "issuer"),
    )
    for token, match in cases:
        with pytest.raises(SessionTokenError, match=match):
            verifier.verify(token)


def test_algorithm_confusion_and_duplicate_json_keys_are_rejected() -> None:
    payload = json.dumps(
        {
            "aud": "suiteharness-websocket",
            "exp": 1_900_000_300,
            "iat": 1_900_000_000,
            "iss": "https://sso.acme.example",
            "principal_id": "employee-42",
            "roles": ["employee"],
            "tenant_id": "acme",
        },
        separators=(",", ":"),
    ).encode()
    confused = _signed_raw(b'{"alg":"none","typ":"SUITEHARNESS-SESSION","v":1}', payload)
    duplicate = _signed_raw(
        b'{"alg":"HS256","typ":"SUITEHARNESS-SESSION","v":1}',
        payload[:-1] + b',"tenant_id":"acme"}',
    )
    codec = _codec([1_900_000_000.0])

    with pytest.raises(SessionTokenError, match="algorithm"):
        codec.verify(confused)
    with pytest.raises(SessionTokenError, match="duplicate"):
        codec.verify(duplicate)


def test_noncanonical_base64_and_tampering_are_rejected() -> None:
    clock = [1_900_000_000.0]
    codec = _codec(clock)
    token = codec.issue(_principal())
    header, payload, signature = token.split(".")
    noncanonical = _signed_raw(
        base64.urlsafe_b64decode(header + "=="),
        base64.urlsafe_b64decode(payload + "=="),
        header_suffix="=",
    )
    tampered_payload = payload[:-1] + ("A" if payload[-1] != "A" else "B")

    with pytest.raises(SessionTokenError, match="encoding|invalid"):
        codec.verify(noncanonical)
    with pytest.raises(SessionTokenError, match="signature"):
        codec.verify(f"{header}.{tampered_payload}.{signature}")


def test_ticket_lifetime_and_key_strength_are_bounded() -> None:
    clock = [1_900_000_000.0]
    codec = _codec(clock)
    with pytest.raises(ValueError, match="configured limit"):
        codec.issue(_principal(), lifetime_seconds=601)
    with pytest.raises(ValueError, match="32 bytes"):
        HmacSessionTokenCodec(
            b"weak",
            issuer="issuer",
            audience="audience",
            tenant_id="acme",
        )
