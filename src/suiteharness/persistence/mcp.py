"""Restart-safe MCP Streamable HTTP resume checkpoints."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit

from suiteharness.mcp.models import HttpResumeState
from suiteharness.mcp.protocols import McpHttpResumeCheckpoint

from .models import ProductStateKey
from .state import SQLiteProductStateStore

_SERVER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class SQLiteMcpHttpResumeStore:
    """Persist only MCP session/event cursors, never credentials or auth headers.

    The endpoint is represented by a SHA-256 fingerprint in the state key. A
    configuration change therefore cannot accidentally resume a session issued
    by a different server endpoint, and the endpoint URL itself is not copied
    into the database.
    """

    _NAMESPACE = "mcp.http_resume"

    def __init__(self, state_store: SQLiteProductStateStore) -> None:
        self._state_store = state_store

    async def load(
        self,
        *,
        tenant_id: str,
        product_id: str,
        server_id: str,
        endpoint: str,
    ) -> McpHttpResumeCheckpoint | None:
        key = self._key(tenant_id, product_id, server_id, endpoint)
        record = await self._state_store.get(key)
        if record is None:
            return None
        if not isinstance(record.value, dict):
            raise RuntimeError("stored MCP HTTP resume state is malformed")
        try:
            state = HttpResumeState.model_validate(record.value)
        except ValueError as exc:
            raise RuntimeError("stored MCP HTTP resume state is malformed") from exc
        self._validate_state(state)
        return McpHttpResumeCheckpoint(
            state=state,
            revision=record.revision,
            updated_at=record.updated_at,
        )

    async def save(
        self,
        *,
        tenant_id: str,
        product_id: str,
        server_id: str,
        endpoint: str,
        state: HttpResumeState,
        expected_revision: int,
        idempotency_key: str,
    ) -> McpHttpResumeCheckpoint:
        if not isinstance(state, HttpResumeState):
            raise TypeError("state must be HttpResumeState")
        self._validate_state(state)
        key = self._key(tenant_id, product_id, server_id, endpoint)
        result = await self._state_store.compare_and_set(
            key,
            {
                "session_id": state.session_id,
                "last_event_id": state.last_event_id,
            },
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )
        return McpHttpResumeCheckpoint(
            state=state,
            revision=result.record.revision,
            updated_at=result.record.updated_at,
        )

    @classmethod
    def _key(
        cls,
        tenant_id: str,
        product_id: str,
        server_id: str,
        endpoint: str,
    ) -> ProductStateKey:
        if not _SERVER_ID.fullmatch(server_id):
            raise ValueError("invalid MCP server_id")
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("MCP HTTP endpoint must be an absolute HTTPS URL")
        endpoint_digest = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
        return ProductStateKey(
            tenant_id=tenant_id,
            product_id=product_id,
            namespace=cls._NAMESPACE,
            key=f"{server_id}:{endpoint_digest}",
        )

    @staticmethod
    def _validate_state(state: HttpResumeState) -> None:
        for label, value in (
            ("session_id", state.session_id),
            ("last_event_id", state.last_event_id),
        ):
            if value is not None and (
                not value or len(value) > 4096 or "\x00" in value or "\r" in value or "\n" in value
            ):
                raise ValueError(f"invalid MCP {label}")


__all__ = ["McpHttpResumeCheckpoint", "SQLiteMcpHttpResumeStore"]
