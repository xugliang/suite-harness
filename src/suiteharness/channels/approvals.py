"""WebSocket-oriented exact-call approval coordination."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from pydantic import JsonValue

from suiteharness.execution import (
    ApprovalBinding,
    ApprovalTarget,
    RunRequest,
    ToolSpec,
)


@dataclass(frozen=True, slots=True)
class ApprovalChallenge:
    challenge_id: str
    principal_id: str
    run_id: str
    call_id: str
    tool_name: str
    tool_identity: str
    arguments: dict[str, JsonValue]
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))


ApprovalPublisher = Callable[[RunRequest, ApprovalChallenge], Awaitable[None]]


@dataclass(slots=True)
class _Pending:
    challenge: ApprovalChallenge
    target: ApprovalTarget
    future: asyncio.Future[str | None]


class InteractiveApprovalCoordinator:
    """One-shot broker used by the runner and an authenticated WebSocket UI."""

    def __init__(self, publish: ApprovalPublisher, *, timeout_seconds: float = 60.0) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 600:
            raise ValueError("approval timeout_seconds must be in (0, 600]")
        self._publish = publish
        self._timeout_seconds = timeout_seconds
        self._pending: dict[str, _Pending] = {}
        self._lock = asyncio.Lock()

    async def request_approval(
        self,
        request: RunRequest,
        target: ApprovalTarget,
        spec: ToolSpec,
        arguments: dict[str, JsonValue],
    ) -> ApprovalBinding | None:
        now = datetime.now(UTC)
        challenge = ApprovalChallenge(
            challenge_id=secrets.token_urlsafe(24),
            principal_id=request.scope.principal_id,
            run_id=request.run_id,
            call_id=target.call_id,
            tool_name=spec.name,
            tool_identity=target.tool_identity.canonical_id,
            arguments=dict(arguments),
            expires_at=now + timedelta(seconds=self._timeout_seconds),
        )
        future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        pending = _Pending(challenge, target, future)
        async with self._lock:
            self._pending[challenge.challenge_id] = pending
        try:
            await self._publish(request, challenge)
            approver = await asyncio.wait_for(future, timeout=self._timeout_seconds)
        except TimeoutError:
            if not future.done():
                future.cancel()
            return None
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            raise
        finally:
            async with self._lock:
                self._pending.pop(challenge.challenge_id, None)
        if approver is None:
            return None
        decided_at = datetime.now(UTC)
        return ApprovalBinding(
            target=target,
            approved_by=approver,
            issued_at=decided_at,
            expires_at=decided_at + timedelta(seconds=30),
        )

    async def decide(
        self,
        challenge_id: str,
        *,
        principal_id: str,
        approved: bool,
        approved_by: str,
    ) -> bool:
        """Resolve one pending challenge after Web authentication/authorization."""

        async with self._lock:
            pending = self._pending.get(challenge_id)
            if pending is None or pending.future.done():
                return False
            if pending.challenge.principal_id != principal_id:
                return False
            pending.future.set_result(approved_by if approved else None)
            return True

    async def close(self) -> None:
        async with self._lock:
            pending = tuple(self._pending.values())
            self._pending.clear()
        for item in pending:
            if not item.future.done():
                item.future.set_result(None)


__all__ = ["ApprovalChallenge", "ApprovalPublisher", "InteractiveApprovalCoordinator"]
