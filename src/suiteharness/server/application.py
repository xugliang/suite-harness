"""Company-channel application pipeline from authenticated input to a product run."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol, cast

from pydantic import JsonValue

from suiteharness.channels import (
    InboundMessage,
    OutboundEvent,
    OutboundEventKind,
)
from suiteharness.execution import (
    ConversationContext,
    ConversationMessage,
    ExecutionBudget,
    RunRequest,
    RunResult,
    RunStatus,
)
from suiteharness.runtime import RequestScope, ScopeKind
from suiteharness.sessions import (
    SessionIdentity,
    SessionRunClaimState,
    SessionStatus,
    SessionStore,
    TranscriptEvent,
    TranscriptEventType,
)
from suiteharness.sessions.models import canonical_json

from .access import ChannelGrantIssuer

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CHECKPOINT_SCHEMA = "suiteharness.channel-application.v1"
_USER_EVENT_SCHEMA = "suiteharness.channel-user-message.v1"
_RUN_LEASE_GRACE_SECONDS = 30.0
_RUN_FINALIZATION_SECONDS = 30.0


class ChannelApplicationError(RuntimeError):
    """The authenticated request cannot safely enter the product runtime."""


class ChannelMessageConflictError(ChannelApplicationError):
    """One external message identity was reused with different content."""


class CustomerBundleRunner(Protocol):
    """Narrow view of ``ActivatedCustomerBundle`` used by the server layer."""

    @property
    def tenant_id(self) -> str: ...

    async def run(self, request: RunRequest) -> RunResult: ...


class RunInputBuilder(Protocol):
    """Product-neutral seam for mapping a channel envelope to workflow input."""

    def __call__(self, message: InboundMessage, scope: RequestScope) -> JsonValue: ...


def default_run_input(message: InboundMessage, scope: RequestScope) -> JsonValue:
    """Preserve the verified channel envelope without trusting it as identity."""

    return {
        "message": message.text,
        "channel": message.channel.value,
        "message_id": message.message_id,
        "conversation_id": message.conversation_id,
        "attachments": [item.model_dump(mode="json") for item in message.attachments],
        "metadata": copy.deepcopy(message.metadata),
        "authenticated_principal_id": scope.principal_id,
    }


@dataclass(slots=True)
class _LockEntry:
    lock: asyncio.Lock
    users: int = 0


class _SessionLocks:
    """Serialize a conversation in-process without retaining inactive sessions."""

    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._entries: dict[SessionIdentity, _LockEntry] = {}

    @asynccontextmanager
    async def acquire(self, identity: SessionIdentity) -> AsyncIterator[None]:
        async with self._guard:
            entry = self._entries.get(identity)
            if entry is None:
                entry = _LockEntry(asyncio.Lock())
                self._entries[identity] = entry
            entry.users += 1
        try:
            await entry.lock.acquire()
        except BaseException:
            await self._release_user(identity, entry)
            raise
        try:
            yield
        finally:
            entry.lock.release()
            await self._release_user(identity, entry)

    async def _release_user(self, identity: SessionIdentity, entry: _LockEntry) -> None:
        async with self._guard:
            entry.users -= 1
            if entry.users == 0 and self._entries.get(identity) is entry:
                self._entries.pop(identity, None)


class ChannelApplication:
    """Persist, authorize, execute, checkpoint, and emit one channel message.

    A per-session lock gives deterministic ordering inside one process.  Every
    store mutation additionally uses the session revision as compare-and-swap,
    so two server processes fail closed instead of concurrently running the same
    conversation.  A durable ``started`` checkpoint prevents an uncertain retry
    from repeating tool side effects after a crash.
    """

    def __init__(
        self,
        *,
        activation: CustomerBundleRunner,
        sessions: SessionStore,
        grants: ChannelGrantIssuer,
        input_builder: RunInputBuilder = default_run_input,
        budget: ExecutionBudget | None = None,
        checkpoint_history_limit: int = 256,
        checkpoint_history_bytes: int = 4 * 1024 * 1024,
        conversation_history_limit: int = 32,
        conversation_history_bytes: int = 128 * 1024,
        conversation_scan_limit: int = 256,
    ) -> None:
        if not callable(input_builder):
            raise TypeError("input_builder must be callable")
        if (
            isinstance(checkpoint_history_limit, bool)
            or not isinstance(checkpoint_history_limit, int)
            or checkpoint_history_limit < 1
            or checkpoint_history_limit > 10_000
        ):
            raise ValueError("checkpoint_history_limit must be between 1 and 10000")
        if (
            isinstance(checkpoint_history_bytes, bool)
            or not isinstance(checkpoint_history_bytes, int)
            or checkpoint_history_bytes < 4_096
            or checkpoint_history_bytes > 4 * 1024 * 1024
        ):
            raise ValueError(
                "checkpoint_history_bytes must be between 4096 and 4194304"
            )
        if conversation_history_limit < 2 or conversation_history_limit > 256:
            raise ValueError("conversation_history_limit must be between 2 and 256")
        if conversation_history_bytes < 1024 or conversation_history_bytes > 4 * 1024 * 1024:
            raise ValueError("conversation_history_bytes must be between 1024 and 4194304")
        if conversation_scan_limit < conversation_history_limit or conversation_scan_limit > 1000:
            raise ValueError(
                "conversation_scan_limit must cover history_limit and be at most 1000"
            )
        self._activation = activation
        self._sessions = sessions
        self._grants = grants
        self._input_builder = input_builder
        self._budget = budget or ExecutionBudget()
        self._history_limit = checkpoint_history_limit
        self._history_bytes = checkpoint_history_bytes
        self._conversation_history_limit = conversation_history_limit
        self._conversation_history_bytes = conversation_history_bytes
        self._conversation_scan_limit = conversation_scan_limit
        self._locks = _SessionLocks()

    async def handle(
        self,
        message: InboundMessage,
        scope: RequestScope,
    ) -> AsyncIterator[OutboundEvent]:
        self._validate_boundary(message, scope)
        identity = self._session_identity(scope)
        run_id = self._stable_id(
            "run",
            identity.tenant_id,
            identity.product_id,
            identity.agent_id,
            identity.session_id,
            identity.principal_id,
            scope.principal_id,
            scope.channel_id,
            message.message_id,
        )
        message_digest = self._message_digest(message, scope)

        async with self._locks.acquire(identity):
            async for event in self._handle_locked(
                message,
                scope,
                identity,
                run_id,
                message_digest,
            ):
                yield event

    async def _handle_locked(
        self,
        message: InboundMessage,
        scope: RequestScope,
        identity: SessionIdentity,
        run_id: str,
        message_digest: str,
    ) -> AsyncIterator[OutboundEvent]:
        await self._sessions.create(identity)
        owner_token = secrets.token_urlsafe(32)
        run_lease_seconds = self._budget.timeout_seconds + _RUN_LEASE_GRACE_SECONDS
        snapshot = await self._sessions.resume(identity)
        if snapshot is None:
            raise ChannelApplicationError("session disappeared after creation")
        history = self._load_history(snapshot.checkpoint.workflow_state if snapshot.checkpoint else None)
        cached_index = self._find_run(history, run_id)
        if cached_index is not None:
            cached = history[cached_index]
            if cached["message_digest"] != message_digest:
                raise ChannelMessageConflictError(
                    "external message identity was reused with different content"
                )
            yield self._started(scope, identity, run_id, replayed=True)
            if cached["state"] == "started":
                claim_state = await self._sessions.claim_run(
                    identity,
                    run_id=run_id,
                    owner_token=owner_token,
                    lease_seconds=run_lease_seconds,
                )
                if claim_state is SessionRunClaimState.ACTIVE:
                    yield self._outbound(
                        scope,
                        OutboundEventKind.FAILED,
                        self._in_progress_payload(identity, run_id),
                    )
                    return
                if claim_state is SessionRunClaimState.ACQUIRED:
                    # A v1 checkpoint can predate the claim table. Fence this
                    # already-started run before recording it as uncertain;
                    # acquiring here must never authorize re-execution.
                    await self._sessions.finalize_run(
                        identity,
                        run_id=run_id,
                        owner_token=owner_token,
                        lease_seconds=_RUN_FINALIZATION_SECONDS,
                    )
                payload = self._uncertain_payload(identity, run_id, replayed=True)
                await self._recover_uncertain(
                    identity,
                    history,
                    cached_index,
                    snapshot.session.revision,
                    payload,
                    update_status=cached_index == len(history) - 1,
                )
                yield self._outbound(scope, OutboundEventKind.FAILED, payload)
                return
            if cached_index == len(history) - 1:
                expected_status = self._status_for_cached(cached)
                if snapshot.session.status is not expected_status:
                    await self._sessions.set_status(
                        identity,
                        expected_status,
                        expected_revision=snapshot.session.revision,
                    )
            yield self._cached_terminal(scope, cached)
            return

        recent = await self._sessions.recent_transcript(
            identity,
            limit=self._conversation_scan_limit,
        )
        conversation = self._conversation_context(recent)

        prior_revision = snapshot.session.revision
        user_event = await self._sessions.append(
            identity,
            event_id=self._stable_id("event", run_id, "user"),
            event_type=TranscriptEventType.USER_MESSAGE,
            payload=self._message_payload(message, scope),
            idempotency_key=self._stable_id("idem", identity.principal_id, run_id, "user"),
            expected_revision=prior_revision,
            occurred_at=message.received_at,
        )
        if user_event.revision <= prior_revision:
            # The durable input exists but its outcome is outside retained history.
            # Re-running could duplicate side effects, so the only safe result is a
            # deterministic failure that asks an operator to reconcile the run.
            yield self._started(scope, identity, run_id, replayed=True)
            yield self._outbound(
                scope,
                OutboundEventKind.FAILED,
                self._unavailable_payload(identity, run_id),
            )
            return

        revision = user_event.revision
        if snapshot.session.status is not SessionStatus.ACTIVE:
            active = await self._sessions.set_status(
                identity,
                SessionStatus.ACTIVE,
                expected_revision=revision,
            )
            revision = active.revision

        claim_state = await self._sessions.claim_run(
            identity,
            run_id=run_id,
            owner_token=owner_token,
            lease_seconds=run_lease_seconds,
        )
        if claim_state is not SessionRunClaimState.ACQUIRED:
            yield self._started(scope, identity, run_id, replayed=True)
            payload = (
                self._in_progress_payload(identity, run_id)
                if claim_state is SessionRunClaimState.ACTIVE
                else self._uncertain_payload(identity, run_id, replayed=True)
            )
            yield self._outbound(scope, OutboundEventKind.FAILED, payload)
            return

        self._append_started(history, run_id, message_digest)
        started_checkpoint = await self._sessions.save_checkpoint(
            identity,
            checkpoint_id=self._stable_id("checkpoint", run_id, "started"),
            workflow_state=self._history_state(history),
            expected_revision=revision,
            idempotency_key=self._stable_id("idem", identity.principal_id, run_id, "started"),
        )
        yield self._started(scope, identity, run_id, replayed=False)

        try:
            run_input = self._input_builder(message, scope)
            canonical_json(run_input)
            async with self._grants.lease(scope) as issued:
                request = RunRequest(
                    run_id=run_id,
                    scope=scope,
                    grant_id=issued.grant_id,
                    input=run_input,
                    conversation=conversation,
                    read_only=issued.read_only,
                    budget=self._budget,
                )
                result = await self._activation.run(request)
                if not isinstance(result, RunResult):
                    raise TypeError("customer bundle returned an invalid run result")
        except Exception:
            kind = OutboundEventKind.FAILED
            payload = self._application_failure_payload(identity, run_id)
            transcript_type = TranscriptEventType.ERROR
            session_status = SessionStatus.FAILED
        else:
            kind, payload, transcript_type, session_status = self._terminal_from_result(
                identity, result
            )

        finalized = await self._sessions.finalize_run(
            identity,
            run_id=run_id,
            owner_token=owner_token,
            lease_seconds=_RUN_FINALIZATION_SECONDS,
        )
        if not finalized:
            # Another process observed an expired execution lease and fenced
            # this owner. Never let the old generation publish terminal state.
            payload = self._uncertain_payload(identity, run_id, replayed=False)
            yield self._outbound(scope, OutboundEventKind.FAILED, payload)
            return

        terminal_event = await self._sessions.append(
            identity,
            event_id=self._stable_id("event", run_id, "terminal"),
            event_type=transcript_type,
            payload=payload,
            idempotency_key=self._stable_id("idem", identity.principal_id, run_id, "terminal"),
            expected_revision=started_checkpoint.revision,
        )
        run_index = self._find_run(history, run_id)
        assert run_index is not None
        history[run_index] = {
            "run_id": run_id,
            "message_digest": message_digest,
            "state": kind.value,
            "payload": copy.deepcopy(payload),
        }
        checkpoint = await self._sessions.save_checkpoint(
            identity,
            checkpoint_id=self._stable_id("checkpoint", run_id, "terminal"),
            workflow_state=self._history_state(history),
            expected_revision=terminal_event.revision,
            idempotency_key=self._stable_id("idem", identity.principal_id, run_id, "terminal-state"),
        )
        await self._sessions.set_status(
            identity,
            session_status,
            expected_revision=checkpoint.revision,
        )
        yield self._outbound(scope, kind, payload)

    async def _recover_uncertain(
        self,
        identity: SessionIdentity,
        history: list[dict[str, JsonValue]],
        index: int,
        expected_revision: int,
        payload: dict[str, JsonValue],
        *,
        update_status: bool,
    ) -> None:
        run_id = cast(str, history[index]["run_id"])
        await self._sessions.append(
            identity,
            event_id=self._stable_id("event", run_id, "uncertain"),
            event_type=TranscriptEventType.ERROR,
            payload=payload,
            idempotency_key=self._stable_id("idem", identity.principal_id, run_id, "uncertain"),
            expected_revision=expected_revision,
        )
        current = await self._sessions.get(identity)
        if current is None:
            raise ChannelApplicationError("session disappeared during uncertain-run recovery")
        history[index] = {
            **history[index],
            "state": OutboundEventKind.FAILED.value,
            "payload": copy.deepcopy(payload),
        }
        checkpoint = await self._sessions.save_checkpoint(
            identity,
            checkpoint_id=self._stable_id("checkpoint", run_id, "uncertain"),
            workflow_state=self._history_state(history),
            # An idempotent retry may return an older event after a newer run
            # changed the session.  Re-read the CAS revision rather than using
            # that historical event's revision.
            expected_revision=current.revision,
            idempotency_key=self._stable_id(
                "idem", identity.principal_id, run_id, "uncertain-state"
            ),
        )
        if update_status:
            await self._sessions.set_status(
                identity,
                SessionStatus.FAILED,
                expected_revision=checkpoint.revision,
            )

    def _append_started(
        self,
        history: list[dict[str, JsonValue]],
        run_id: str,
        message_digest: str,
    ) -> None:
        if len(history) >= self._history_limit:
            removable = next(
                (index for index, item in enumerate(history) if item["state"] != "started"),
                None,
            )
            if removable is None:
                raise ChannelApplicationError("checkpoint contains too many unfinished runs")
            history.pop(removable)
        history.append(
            {
                "run_id": run_id,
                "message_digest": message_digest,
                "state": "started",
                "payload": None,
            }
        )

    @staticmethod
    def _find_run(history: list[dict[str, JsonValue]], run_id: str) -> int | None:
        return next(
            (index for index, item in enumerate(history) if item["run_id"] == run_id),
            None,
        )

    def _history_state(
        self,
        history: list[dict[str, JsonValue]],
    ) -> dict[str, JsonValue]:
        """Bound replay history by entries and serialized bytes.

        Completed entries may be discarded because transcript/run-claim
        idempotency remains durable in the store.  An unfinished entry is never
        removed: losing it could turn an uncertain side effect into a retry.
        """

        while True:
            state: dict[str, JsonValue] = {
                "schema": _CHECKPOINT_SCHEMA,
                "runs": history,
            }
            if len(canonical_json(state).encode("utf-8")) <= self._history_bytes:
                return cast(dict[str, JsonValue], copy.deepcopy(state))
            removable = next(
                (index for index, item in enumerate(history) if item["state"] != "started"),
                None,
            )
            if removable is None:
                raise ChannelApplicationError(
                    "checkpoint unfinished run history exceeds its byte limit"
                )
            history.pop(removable)

    def _load_history(self, state: JsonValue) -> list[dict[str, JsonValue]]:
        if state is None:
            return []
        if len(canonical_json(state).encode("utf-8")) > self._history_bytes:
            raise ChannelApplicationError("session checkpoint exceeds its byte limit")
        if not isinstance(state, dict) or state.get("schema") != _CHECKPOINT_SCHEMA:
            raise ChannelApplicationError("session checkpoint belongs to an incompatible owner")
        raw_runs = state.get("runs")
        if not isinstance(raw_runs, list):
            raise ChannelApplicationError("session checkpoint has an invalid run history")
        history: list[dict[str, JsonValue]] = []
        seen: set[str] = set()
        for item in raw_runs:
            if not isinstance(item, dict):
                raise ChannelApplicationError("session checkpoint has an invalid run entry")
            run_id = item.get("run_id")
            digest = item.get("message_digest")
            run_state = item.get("state")
            payload = item.get("payload")
            if (
                not isinstance(run_id, str)
                or not _IDENTIFIER.fullmatch(run_id)
                or run_id in seen
                or not isinstance(digest, str)
                or not _DIGEST.fullmatch(digest)
                or run_state
                not in {"started", OutboundEventKind.COMPLETED.value, OutboundEventKind.FAILED.value}
                or (run_state != "started" and not isinstance(payload, dict))
            ):
                raise ChannelApplicationError("session checkpoint has an invalid run entry")
            seen.add(run_id)
            history.append(
                {
                    "run_id": run_id,
                    "message_digest": digest,
                    "state": cast(str, run_state),
                    "payload": copy.deepcopy(cast(JsonValue, payload)),
                }
            )
        return history

    def _cached_terminal(
        self,
        scope: RequestScope,
        cached: dict[str, JsonValue],
    ) -> OutboundEvent:
        state = cast(str, cached["state"])
        payload = copy.deepcopy(cast(dict[str, JsonValue], cached["payload"]))
        payload["replayed"] = True
        kind = OutboundEventKind(state)
        return self._outbound(scope, kind, payload)

    @staticmethod
    def _status_for_cached(cached: dict[str, JsonValue]) -> SessionStatus:
        if cached["state"] == OutboundEventKind.COMPLETED.value:
            return SessionStatus.COMPLETED
        payload = cast(dict[str, JsonValue], cached["payload"])
        return (
            SessionStatus.CANCELLED
            if payload.get("run_status") == RunStatus.CANCELLED.value
            else SessionStatus.FAILED
        )

    @staticmethod
    def _terminal_from_result(
        identity: SessionIdentity,
        result: RunResult,
    ) -> tuple[
        OutboundEventKind,
        dict[str, JsonValue],
        TranscriptEventType,
        SessionStatus,
    ]:
        common: dict[str, JsonValue] = {
            "run_id": result.run_id,
            "session_id": identity.session_id,
            "product_id": identity.product_id,
            "run_status": result.status.value,
            "replayed": False,
        }
        if result.status is RunStatus.SUCCEEDED:
            return (
                OutboundEventKind.COMPLETED,
                {**common, "output": copy.deepcopy(result.output)},
                TranscriptEventType.ASSISTANT_MESSAGE,
                SessionStatus.COMPLETED,
            )
        assert result.failure is not None
        failure = cast(dict[str, JsonValue], result.failure.model_dump(mode="json"))
        status = (
            SessionStatus.CANCELLED
            if result.status is RunStatus.CANCELLED
            else SessionStatus.FAILED
        )
        return (
            OutboundEventKind.FAILED,
            {**common, "failure": failure},
            TranscriptEventType.ERROR,
            status,
        )

    @staticmethod
    def _application_failure_payload(
        identity: SessionIdentity,
        run_id: str,
    ) -> dict[str, JsonValue]:
        return {
            "run_id": run_id,
            "session_id": identity.session_id,
            "product_id": identity.product_id,
            "run_status": RunStatus.FAILED.value,
            "failure": {
                "code": "application_error",
                "message": "agent execution failed",
                "retryable": False,
            },
            "replayed": False,
        }

    @staticmethod
    def _uncertain_payload(
        identity: SessionIdentity,
        run_id: str,
        *,
        replayed: bool,
    ) -> dict[str, JsonValue]:
        return {
            "run_id": run_id,
            "session_id": identity.session_id,
            "product_id": identity.product_id,
            "run_status": RunStatus.FAILED.value,
            "failure": {
                "code": "run_outcome_uncertain",
                "message": "the prior run started but no durable terminal checkpoint exists",
                "retryable": False,
            },
            "replayed": replayed,
        }

    @staticmethod
    def _in_progress_payload(
        identity: SessionIdentity,
        run_id: str,
    ) -> dict[str, JsonValue]:
        return {
            "run_id": run_id,
            "session_id": identity.session_id,
            "product_id": identity.product_id,
            "run_status": RunStatus.FAILED.value,
            "failure": {
                "code": "run_in_progress",
                "message": "the message is already executing in another server process",
                "retryable": True,
            },
            "replayed": True,
        }

    @staticmethod
    def _unavailable_payload(
        identity: SessionIdentity,
        run_id: str,
    ) -> dict[str, JsonValue]:
        return {
            "run_id": run_id,
            "session_id": identity.session_id,
            "product_id": identity.product_id,
            "run_status": RunStatus.FAILED.value,
            "failure": {
                "code": "idempotent_result_unavailable",
                "message": "the message was processed previously but its result is no longer retained",
                "retryable": False,
            },
            "replayed": True,
        }

    @staticmethod
    def _message_payload(
        message: InboundMessage,
        scope: RequestScope,
    ) -> dict[str, JsonValue]:
        return {
            "schema": _USER_EVENT_SCHEMA,
            "authenticated_principal_id": scope.principal_id,
            "message": cast(dict[str, JsonValue], message.model_dump(mode="json")),
        }

    def _conversation_context(
        self,
        events: tuple[TranscriptEvent, ...],
    ) -> ConversationContext:
        """Rebuild bounded role history from trusted event types only.

        Client metadata, sender IDs, roles, attachment bodies and failed runs are
        deliberately excluded.  They remain available in the audit transcript
        but never become server-trusted prompt instructions.
        """

        completed_pairs: list[tuple[ConversationMessage, ConversationMessage]] = []
        pending_user: ConversationMessage | None = None
        for event in events:
            message = self._conversation_message(event)
            if message is not None and message.role == "user":
                pending_user = message
            elif message is not None and message.role == "assistant":
                if pending_user is not None:
                    completed_pairs.append((pending_user, message))
                pending_user = None
            elif event.type in {
                TranscriptEventType.ERROR,
                TranscriptEventType.CANCEL,
            }:
                pending_user = None

        selected_pairs: list[tuple[ConversationMessage, ConversationMessage]] = []
        used_bytes = 0
        truncated = len(events) == self._conversation_scan_limit
        for pair in reversed(completed_pairs):
            pair_size = sum(
                len(canonical_json(message.model_dump(mode="json")).encode("utf-8"))
                for message in pair
            )
            if (
                len(selected_pairs) * 2 + 2 > self._conversation_history_limit
                or used_bytes + pair_size > self._conversation_history_bytes
            ):
                truncated = True
                break
            selected_pairs.append(pair)
            used_bytes += pair_size
        selected_pairs.reverse()
        selected = [message for pair in selected_pairs for message in pair]
        return ConversationContext(messages=tuple(selected), truncated=truncated)

    @staticmethod
    def _conversation_message(event: TranscriptEvent) -> ConversationMessage | None:
        if event.type is TranscriptEventType.USER_MESSAGE:
            payload = event.payload
            if (
                not isinstance(payload, dict)
                or payload.get("schema") != _USER_EVENT_SCHEMA
                or not isinstance(payload.get("authenticated_principal_id"), str)
                or not isinstance(payload.get("message"), dict)
            ):
                return None
            raw_message = cast(dict[str, JsonValue], payload["message"])
            content = raw_message.get("text")
            if not isinstance(content, str):
                return None
            return ConversationMessage(
                role="user",
                content=content,
                principal_id=cast(str, payload["authenticated_principal_id"]),
                occurred_at=event.occurred_at,
            )
        if event.type is TranscriptEventType.ASSISTANT_MESSAGE:
            if not isinstance(event.payload, dict) or "output" not in event.payload:
                return None
            return ConversationMessage(
                role="assistant",
                content=copy.deepcopy(cast(JsonValue, event.payload["output"])),
                occurred_at=event.occurred_at,
            )
        return None

    @classmethod
    def _message_digest(cls, message: InboundMessage, scope: RequestScope) -> str:
        metadata = copy.deepcopy(message.metadata)
        if message.channel.value == "web":
            # Connection IDs change after reconnect and are delivery metadata,
            # not part of the logical client message.
            metadata.pop("connection_id", None)
        payload: JsonValue = {
            # ``received_at`` is adapter observation time, not message content;
            # WebSocket redelivery legitimately gets a different value.  The
            # event/request IDs likewise identify a delivery; message_id plus
            # the isolated session identifies the logical user message.
            "message": {
                "channel": message.channel.value,
                "message_id": message.message_id,
                "conversation_id": message.conversation_id,
                "sender_external_id": message.sender_external_id,
                "text": message.text,
                "product_id": message.product_id,
                "attachments": [
                    item.model_dump(mode="json") for item in message.attachments
                ],
                "metadata": metadata,
            },
            "scope": {
                "tenant_id": scope.tenant_id,
                "product_id": scope.product_id,
                "agent_id": scope.path.agent_id,
                "session_id": scope.path.session_id,
                "principal_id": scope.principal_id,
                "channel_id": scope.channel_id,
                "roles": sorted(scope.roles),
                "correlation_id": scope.correlation_id,
            },
        }
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    @staticmethod
    def _stable_id(prefix: str, *values: str) -> str:
        digest = hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()
        return f"{prefix}-{digest}"

    @staticmethod
    def _session_identity(scope: RequestScope) -> SessionIdentity:
        assert scope.path.agent_id is not None and scope.path.session_id is not None
        return SessionIdentity(
            tenant_id=scope.tenant_id,
            product_id=scope.product_id,
            agent_id=scope.path.agent_id,
            session_id=scope.path.session_id,
            principal_id=scope.effective_session_owner_id,
        )

    def _validate_boundary(self, message: InboundMessage, scope: RequestScope) -> None:
        if not isinstance(message, InboundMessage) or not isinstance(scope, RequestScope):
            raise TypeError("message and scope must be validated boundary models")
        if scope.path.kind is not ScopeKind.AGENT:
            raise ChannelApplicationError("company-channel requests require an agent scope")
        if not scope.request_id or not scope.correlation_id:
            raise ChannelApplicationError("company-channel scope requires request correlation ids")
        if message.channel.value != scope.channel_id:
            raise ChannelApplicationError("message channel does not match authenticated scope")
        if message.product_id is not None and message.product_id != scope.product_id:
            raise ChannelApplicationError("message product does not match authenticated scope")
        if self._activation.tenant_id != scope.tenant_id:
            raise ChannelApplicationError("request tenant does not match active customer bundle")

    @staticmethod
    def _started(
        scope: RequestScope,
        identity: SessionIdentity,
        run_id: str,
        *,
        replayed: bool,
    ) -> OutboundEvent:
        return ChannelApplication._outbound(
            scope,
            OutboundEventKind.STARTED,
            {
                "run_id": run_id,
                "session_id": identity.session_id,
                "product_id": identity.product_id,
                "replayed": replayed,
            },
        )

    @staticmethod
    def _outbound(
        scope: RequestScope,
        kind: OutboundEventKind,
        payload: dict[str, JsonValue],
    ) -> OutboundEvent:
        return OutboundEvent(
            kind=kind,
            request_id=scope.request_id,
            correlation_id=scope.correlation_id,
            payload=payload,
        )


__all__ = [
    "ChannelApplication",
    "ChannelApplicationError",
    "ChannelMessageConflictError",
    "CustomerBundleRunner",
    "RunInputBuilder",
    "default_run_input",
]
