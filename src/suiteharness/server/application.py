"""Company-channel application pipeline from authenticated input to a product run."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

from pydantic import JsonValue

from suiteharness.channels import (
    InboundMessage,
    OutboundEvent,
    OutboundEventKind,
)
from suiteharness.channels.dispatch import HistoryPage
from suiteharness.channels.gateway import ChannelApplication as ChannelApplicationPort
from suiteharness.execution import (
    ConversationContext,
    ConversationMessage,
    ExecutionBudget,
    RunRequest,
    RunResult,
    RunStatus,
)
from suiteharness.execution.events import RunEvent
from suiteharness.execution.models import RunResumeState
from suiteharness.memory.markdown_archive import MarkdownArchive
from suiteharness.memory.resolver import DefaultSessionMemoryResolver
from suiteharness.memory.session_memory import MemoryState, SessionConversationMemory
from suiteharness.runtime import RequestScope, ScopeKind
from suiteharness.sessions import (
    RunResumePoint,
    RunStopOutcome,
    SessionIdentity,
    SessionRunClaimState,
    SessionStatus,
    SessionStore,
    TranscriptEvent,
    TranscriptEventType,
)
from suiteharness.sessions.errors import SessionRevisionConflictError
from suiteharness.sessions.models import canonical_json, memory_invalidation_source

from .access import ChannelGrantIssuer

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CHECKPOINT_SCHEMA = "suiteharness.channel-application.v1"
_USER_EVENT_SCHEMA = "suiteharness.channel-user-message.v1"
_RUN_LEASE_GRACE_SECONDS = 30.0
_RUN_FINALIZATION_SECONDS = 30.0
_ARCHIVE_HEADER = re.compile(r'<!-- suiteharness-memory-v1 revision=([0-9]+) -->\n')
_LOGGER = logging.getLogger(__name__)


class ChannelApplicationError(RuntimeError):
    """The authenticated request cannot safely enter the product runtime."""


class ChannelMessageConflictError(ChannelApplicationError):
    """One external message identity was reused with different content."""


class CustomerBundleRunner(Protocol):
    """Narrow view of ``ActivatedCustomerBundle`` used by the server layer."""

    @property
    def tenant_id(self) -> str: ...

    async def run(self, request: RunRequest) -> RunResult: ...

    def stream(self, request: RunRequest) -> AsyncIterator[RunEvent]: ...

    async def cancel(self, scope: RequestScope, run_id: str) -> bool: ...


class RunInputBuilder(Protocol):
    """Product-neutral seam for mapping a channel envelope to workflow input."""

    def __call__(self, message: InboundMessage, scope: RequestScope) -> JsonValue: ...


class SessionMemoryResolver(Protocol):
    """Resolve the conversation-memory policy for one trusted request scope."""

    def resolve(self, scope: RequestScope) -> SessionConversationMemory: ...


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


class ChannelApplication(ChannelApplicationPort):
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
        memory_resolver: SessionMemoryResolver | None = None,
        archive: MarkdownArchive | None = None,
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
        if (
            conversation_history_bytes < 1024
            or conversation_history_bytes > 4 * 1024 * 1024
        ):
            raise ValueError(
                "conversation_history_bytes must be between 1024 and 4194304"
            )
        if (
            conversation_scan_limit < conversation_history_limit
            or conversation_scan_limit > 1000
        ):
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
        self._memory_resolver = memory_resolver or DefaultSessionMemoryResolver()
        self._archive = archive
        self._locks = _SessionLocks()

    def _validate_memory_scope(self, scope: RequestScope) -> SessionIdentity:
        if not isinstance(scope, RequestScope) or scope.path.kind is not ScopeKind.AGENT:
            raise ChannelApplicationError('conversation operations require an authenticated agent scope')
        if scope.tenant_id != self._activation.tenant_id:
            raise ChannelApplicationError('memory tenant does not match active customer bundle')
        products = getattr(self._activation, 'products', None)
        if products is not None and scope.product_id not in products:
            raise ChannelApplicationError('memory product is not active')
        return self._session_identity(scope)

    async def history(self, scope: RequestScope, *, limit: int = 100,
                      include_invalidated: bool = False,
                      include_cancelled: bool = False) -> tuple[TranscriptEvent, ...]:
        """Visible messages, optionally including explicitly marked inactive audit copies.

        The caller must authenticate and authorize the exact business session.
        This is a display API, never an alternative source of model context.
        Raw audit events remain unchanged on the host-owned SessionStore.
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('history limit must be between 1 and 1000')
        if type(include_invalidated) is not bool:
            raise ValueError('include_invalidated must be a boolean')
        if type(include_cancelled) is not bool:
            raise ValueError('include_cancelled must be a boolean')
        identity = self._validate_memory_scope(scope)
        record = await self._sessions.get(identity)
        if record is None:
            return ()
        events = await self._sessions.recent_transcript(identity, limit=1000)
        # Re-read the watermark so a concurrent invalidation cannot expose a
        # suffix selected before its commit.
        record = await self._sessions.get(identity)
        assert record is not None
        visible_types = {TranscriptEventType.USER_MESSAGE, TranscriptEventType.ASSISTANT_MESSAGE}
        if include_cancelled:
            visible_types.add(TranscriptEventType.CANCEL)
        selected = tuple(event for event in events
                     if (include_invalidated or event.revision > record.memory_invalidated_revision)
                     and event.type in visible_types
                     and not (event.type is TranscriptEventType.USER_MESSAGE
                              and isinstance(event.payload, dict)
                              and 'continuation_of_run_id' in event.payload))[-limit:]
        if include_invalidated:
            return tuple(event.model_copy(update={'payload': {
                **event.payload, 'memory_eligible': event.revision > record.memory_invalidated_revision,
            }}) for event in selected)
        return selected

    async def history_page(
        self, scope: RequestScope, *, limit: int = 100, before_sequence: int | None = None,
    ) -> HistoryPage:
        """Page display records by durable sequence, never by a sliding recent window."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('history limit must be between 1 and 1000')
        if before_sequence is not None and (
            type(before_sequence) is not int or before_sequence < 1
        ):
            raise ValueError('history cursor must be a positive sequence')
        identity = self._validate_memory_scope(scope)
        record = await self._sessions.get(identity)
        if record is None:
            return HistoryPage(())
        cursor = before_sequence
        newest_first = []
        visible_types = {TranscriptEventType.USER_MESSAGE, TranscriptEventType.ASSISTANT_MESSAGE,
                         TranscriptEventType.CANCEL}
        while len(newest_first) <= limit:
            try:
                events = await self._sessions.transcript_page(identity, limit=1000, before_sequence=cursor)
            except NotImplementedError:
                if before_sequence is not None or cursor is not None:
                    raise
                return HistoryPage(
                    await self.history(scope, limit=limit, include_cancelled=True),
                    pagination_supported=False,
                )
            if not events:
                break
            reached_cutoff = False
            for event in reversed(events):
                if event.revision <= record.memory_invalidated_revision:
                    reached_cutoff = True
                    break
                if (event.type in visible_types
                        and not (event.type is TranscriptEventType.USER_MESSAGE
                                 and isinstance(event.payload, dict)
                                 and 'continuation_of_run_id' in event.payload)):
                    newest_first.append(event)
                    if len(newest_first) > limit:
                        break
            if reached_cutoff or len(events) < 1000:
                break
            cursor = events[0].sequence
        # Invalidation may advance while scanning several storage pages.
        current = await self._sessions.get(identity)
        if current is None:
            return HistoryPage(())
        newest_first = [event for event in newest_first
                        if event.revision > current.memory_invalidated_revision]
        has_more = len(newest_first) > limit
        selected = tuple(reversed(newest_first[:limit]))
        return HistoryPage(selected, selected[0].sequence if has_more else None)

    async def stop_message(
        self, scope: RequestScope, *, target_message_id: str | None = None,
    ) -> RunStopOutcome:
        """Persist a stop request without waiting on the executing session lock."""
        identity = self._validate_memory_scope(scope)
        if target_message_id is None:
            active = await self._sessions.active_run_ids(identity, limit=2)
            if not active:
                return RunStopOutcome.NOT_FOUND
            if len(active) != 1:
                raise ChannelApplicationError('select the message to stop; multiple runs are active')
            run_id = active[0]
        else:
            if not isinstance(target_message_id, str) or not _IDENTIFIER.fullmatch(target_message_id):
                raise ValueError('target_message_id must be a bounded message identifier')
            run_id = self._stable_id(
                'run', identity.tenant_id, identity.product_id, identity.agent_id,
                identity.session_id, identity.principal_id, scope.principal_id,
                scope.channel_id, target_message_id,
            )
        return await self._sessions.request_stop(identity, run_id=run_id)

    async def continue_task(
        self, message: InboundMessage, scope: RequestScope, *, target_message_id: str,
    ) -> AsyncIterator[OutboundEvent]:
        """Start a new run from a trusted safe point of the latest cancelled run."""
        self._validate_boundary(message, scope)
        if not isinstance(target_message_id, str) or not _IDENTIFIER.fullmatch(target_message_id):
            raise ValueError('target_message_id must be a bounded message identifier')
        identity = self._session_identity(scope)
        source_run_id = self._stable_id(
            'run', identity.tenant_id, identity.product_id, identity.agent_id,
            identity.session_id, identity.principal_id, scope.principal_id,
            scope.channel_id, target_message_id,
        )
        new_run_id = self._stable_id(
            'run', identity.tenant_id, identity.product_id, identity.agent_id,
            identity.session_id, identity.principal_id, scope.principal_id,
            scope.channel_id, message.message_id,
        )
        # A continuation is a distinct operation, not an empty ordinary message.
        # Bind its trusted source even when a retry takes the cached-run path.
        digest = hashlib.sha256(canonical_json({
            'operation': 'continue', 'source_run_id': source_run_id,
            'message_digest': self._message_digest(message, scope),
        }).encode('utf-8')).hexdigest()
        async with self._locks.acquire(identity):
            record = await self._sessions.get(identity)
            snapshot = await self._sessions.resume(identity)
            point = await self._sessions.get_resume_point(identity, run_id=source_run_id)
            events = await self._sessions.recent_transcript(identity, limit=1000)
            terminals = tuple(event for event in events if event.type in {
                TranscriptEventType.ASSISTANT_MESSAGE, TranscriptEventType.ERROR,
                TranscriptEventType.CANCEL,
            })
            terminal = terminals[-1] if terminals else None
            if (
                terminal is not None and isinstance(terminal.payload, dict)
                and terminal.payload.get('run_id') == new_run_id
            ):
                async with aclosing(self._handle_locked(
                    message, scope, identity, new_run_id, digest, None,
                )) as source:
                    async for event in source:
                        yield event
                return
            continuation_event_id = self._stable_id('event', new_run_id, 'user')
            if any(event.event_id == continuation_event_id for event in events):
                # Idempotent retry while the continuation is still active (or
                # its terminal commit is incomplete) follows ordinary run
                # replay/fencing instead of revalidating the old source run.
                async with aclosing(self._handle_locked(
                    message, scope, identity, new_run_id, digest, None,
                )) as source:
                    async for event in source:
                        yield event
                return
            retained_runs = self._load_history(
                snapshot.checkpoint.workflow_state
                if snapshot is not None and snapshot.checkpoint is not None else None
            )
            latest_user = next((event for event in reversed(events)
                                if event.type is TranscriptEventType.USER_MESSAGE), None)
            if (
                record is None or point is None or terminal is None
                or not retained_runs or retained_runs[-1]['run_id'] != source_run_id
                or latest_user is None
                or latest_user.event_id != self._stable_id('event', source_run_id, 'user')
                or terminal.type is not TranscriptEventType.CANCEL
                or not isinstance(terminal.payload, dict)
                or terminal.payload.get('run_id') != source_run_id
                or terminal.payload.get('resume_unsafe') is True
                or point.memory_invalidated_revision != record.memory_invalidated_revision
                or point.memory_source_versions != record.memory_source_versions
            ):
                raise ChannelApplicationError('the cancelled run has no current safe continuation point')
            payload = point.payload
            if not isinstance(payload, dict):
                raise ChannelApplicationError('the continuation point is invalid')
            try:
                resume_state = RunResumeState.model_validate_json(
                    canonical_json(payload['resume_state'])
                )
                conversation = ConversationContext.model_validate_json(
                    canonical_json(payload['conversation'])
                )
                run_input = copy.deepcopy(payload['input'])
                canonical_json(run_input)
            except (KeyError, TypeError, ValueError) as exc:
                raise ChannelApplicationError('the continuation point is invalid') from exc
            if resume_state.source_run_id != source_run_id:
                raise ChannelApplicationError('the continuation point has invalid lineage')
            source_user = next((event for event in events if
                event.type is TranscriptEventType.USER_MESSAGE
                and event.event_id == self._stable_id('event', source_run_id, 'user')), None)
            if source_user is None or not isinstance(source_user.payload, dict):
                raise ChannelApplicationError('the continuation source input is unavailable')
            continuation_user_payload = copy.deepcopy(source_user.payload)
            continuation_user_payload['continuation_of_run_id'] = source_run_id
            raw_source_message = continuation_user_payload.get('message')
            source_question = (raw_source_message.get('text')
                               if isinstance(raw_source_message, dict) else None)
            if not isinstance(source_question, str):
                raise ChannelApplicationError('the continuation source input is unavailable')
            async with aclosing(self._handle_locked(
                message, scope, identity, new_run_id, digest, None,
                resume_state=resume_state, run_input_override=run_input,
                conversation_override=conversation, continuation_of=source_run_id,
                user_payload_override=continuation_user_payload,
                archive_question_override=source_question,
                expected_resume_point=point,
                expected_resume_revision=record.revision,
            )) as source:
                async for event in source:
                    yield event

    async def _observe_stop(
        self, identity: SessionIdentity, scope: RequestScope, run_id: str,
        seen: asyncio.Event,
    ) -> None:
        """Observe a stop requested by this or another server process."""
        while True:
            try:
                requested = await self._sessions.stop_requested(identity, run_id=run_id)
            except Exception:
                # A broken stop-intent read cannot authorize a late success.
                requested = True
            if requested:
                seen.set()
                cancel = getattr(self._activation, 'cancel', None)
                if callable(cancel):
                    while True:
                        try:
                            if await cancel(scope, run_id):
                                return
                        except Exception:
                            _LOGGER.warning('run_stop_signal_failed')
                            return
                        # The durable claim can exist before Runner registers
                        # its in-memory task. Keep signalling through that gap.
                        await asyncio.sleep(0.01)
                return
            await asyncio.sleep(0.05)

    async def invalidate_memory(self, scope: RequestScope, *, reason: str = 'source_changed',
                                idempotency_key: str | None = None,
                                source_namespace: str | None = None,
                                source_version: int | None = None) -> None:
        """Atomically advance a durable cutoff without deleting audit or changing identity.

        This deliberately does not acquire the long-running conversation lock:
        an in-flight run must be fenced immediately, including in other hosts.
        """
        identity = self._validate_memory_scope(scope)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
            raise ValueError('memory invalidation requires a bounded reason')
        if idempotency_key is not None and (not isinstance(idempotency_key, str)
                or not idempotency_key.strip() or len(idempotency_key) > 512):
            raise ValueError('memory invalidation idempotency key must be a bounded string')
        payload = {'schema': 'suiteharness.memory-invalidation.v1', 'reason': reason}
        if source_namespace is not None or source_version is not None:
            payload.update(source_namespace=source_namespace, source_version=source_version)
            memory_invalidation_source(payload)
        await self._sessions.create(identity)
        durable_key = (None if idempotency_key is None else
                       'memory-invalidation-' + hashlib.sha256(idempotency_key.encode('utf-8')).hexdigest())
        await self._sessions.append(identity, event_id=f'memory-invalidated-{secrets.token_hex(16)}',
            event_type=TranscriptEventType.MEMORY_INVALIDATED,
            payload=payload,
            idempotency_key=durable_key)
        # Do not race a newer completion with an asynchronous file deletion.
        # The durable cutoff rejects old derived archives, even after restart;
        # the next authorized completion replaces their contents.

    async def _archive_text(self, identity: SessionIdentity, cutoff: int, revision: int) -> str:
        if self._archive is None:
            return ''
        document = await asyncio.to_thread(self._archive.retrieve, identity, '', self._archive.max_file_bytes)
        header = _ARCHIVE_HEADER.match(document)
        if header is None or not cutoff < int(header[1]) <= revision:
            return ''
        return document[header.end():]

    async def _archive_completed(self, identity: SessionIdentity, cutoff: int, revision: int,
                                 question: str, output: JsonValue) -> None:
        if self._archive is None:
            return
        try:
            record = await self._sessions.get(identity)
            if record is None or record.memory_invalidated_revision != cutoff:
                return
            answer = output if isinstance(output, str) else canonical_json(output)
            header = f'<!-- suiteharness-memory-v1 revision={revision} -->\n'
            budget = self._archive.max_file_bytes - len(header.encode('utf-8'))

            def append_if_newer(document: str) -> str | None:
                saved_header = _ARCHIVE_HEADER.match(document)
                saved_revision = int(saved_header[1]) if saved_header is not None else None
                if saved_revision is not None and saved_revision >= revision:
                    return None
                previous = (document[saved_header.end():]
                            if saved_header is not None and cutoff < saved_revision else '')
                updated = previous + f'\n\n## User\n\n{question}\n\n## Assistant\n\n{answer}\n'
                bounded = updated.encode('utf-8')[-budget:].decode('utf-8', errors='ignore')
                return header + bounded

            await asyncio.to_thread(self._archive.update, identity, append_if_newer)
        except (OSError, ValueError):
            # Chat facts already committed; a derived archive cannot turn a
            # successful answer into a failed run or authorize model reexecution.
            _LOGGER.warning('memory_archive_write_failed')

    async def handle(
        self,
        message: InboundMessage,
        scope: RequestScope,
        *,
        before_commit: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[OutboundEvent]:
        self._validate_boundary(message, scope)
        if before_commit is not None and not callable(before_commit):
            raise TypeError('before_commit must be callable')
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
            async with aclosing(self._handle_locked(
                message,
                scope,
                identity,
                run_id,
                message_digest,
                before_commit,
            )) as source:
                async for event in source:
                    yield event

    async def _handle_locked(
        self,
        message: InboundMessage,
        scope: RequestScope,
        identity: SessionIdentity,
        run_id: str,
        message_digest: str,
        before_commit: Callable[[], Awaitable[None]] | None,
        *,
        resume_state: RunResumeState | None = None,
        run_input_override: JsonValue | None = None,
        conversation_override: ConversationContext | None = None,
        continuation_of: str | None = None,
        user_payload_override: dict[str, JsonValue] | None = None,
        archive_question_override: str | None = None,
        expected_resume_point: RunResumePoint | None = None,
        expected_resume_revision: int | None = None,
    ) -> AsyncIterator[OutboundEvent]:
        await self._sessions.create(identity)
        owner_token = secrets.token_urlsafe(32)
        run_lease_seconds = self._budget.timeout_seconds + _RUN_LEASE_GRACE_SECONDS
        snapshot = await self._sessions.resume(identity)
        if snapshot is None:
            raise ChannelApplicationError("session disappeared after creation")
        if expected_resume_point is not None:
            fresh_point = await self._sessions.get_resume_point(
                identity, run_id=expected_resume_point.run_id,
            )
            if (
                fresh_point != expected_resume_point
                or snapshot.session.revision != expected_resume_revision
                or snapshot.session.memory_invalidated_revision
                    != expected_resume_point.memory_invalidated_revision
                or snapshot.session.memory_source_versions
                    != expected_resume_point.memory_source_versions
            ):
                raise ChannelApplicationError(
                    'conversation changed before continuation started'
                )
        history = self._load_history(
            snapshot.checkpoint.workflow_state if snapshot.checkpoint else None
        )
        memory_state = self._load_memory_state(
            snapshot.checkpoint.workflow_state if snapshot.checkpoint else None
        )
        memory_cutoff = snapshot.session.memory_invalidated_revision
        if memory_state.covered_revision < memory_cutoff:
            memory_state = MemoryState(covered_revision=memory_cutoff)
        if snapshot.checkpoint is not None and snapshot.checkpoint.revision <= memory_cutoff:
            history = []
        cached_index = self._find_run(history, run_id)
        if cached_index is not None:
            cached = history[cached_index]
            if cached["message_digest"] != message_digest:
                raise ChannelMessageConflictError(
                    "external message identity was reused with different content"
                )
            yield self._started(scope, identity, run_id, replayed=True)
            if cached["state"] == "started":
                terminal_events = await self._sessions.recent_transcript(
                    identity, limit=self._conversation_scan_limit,
                )
                durable_terminal = next((event for event in reversed(terminal_events)
                    if event.event_id == self._stable_id('event', run_id, 'terminal')
                    and event.type in {TranscriptEventType.ASSISTANT_MESSAGE,
                                       TranscriptEventType.ERROR,
                                       TranscriptEventType.CANCEL}), None)
                if durable_terminal is not None and isinstance(durable_terminal.payload, dict):
                    payload = copy.deepcopy(durable_terminal.payload)
                    kind = (OutboundEventKind.COMPLETED
                            if durable_terminal.type is TranscriptEventType.ASSISTANT_MESSAGE
                            else OutboundEventKind.FAILED)
                    if kind is OutboundEventKind.COMPLETED and before_commit is not None:
                        await before_commit()
                    recovered, latest = await self._merge_recovered_terminal(
                        identity, run_id, message_digest, durable_terminal, memory_cutoff,
                    )
                    if not recovered:
                        invalidated = self._application_failure_payload(identity, run_id)
                        invalidated['failure'] = {
                            'code': 'memory_invalidated',
                            'message': 'conversation sources changed before terminal recovery',
                            'retryable': False,
                        }
                        yield self._outbound(scope, OutboundEventKind.FAILED, invalidated)
                        return
                    if kind is OutboundEventKind.COMPLETED and latest:
                        source_user = next((event for event in terminal_events if
                            event.event_id == self._stable_id('event', run_id, 'user')
                            and event.type is TranscriptEventType.USER_MESSAGE), None)
                        question = message.text
                        if source_user is not None and isinstance(source_user.payload, dict):
                            raw = source_user.payload.get('message')
                            if isinstance(raw, dict) and isinstance(raw.get('text'), str):
                                question = raw['text']
                        await self._archive_completed(
                            identity, memory_cutoff, durable_terminal.revision,
                            question, payload.get('output'),
                        )
                    after_recovery = await self._sessions.get(identity)
                    if (after_recovery is None
                            or after_recovery.memory_invalidated_revision != memory_cutoff):
                        invalidated = self._application_failure_payload(identity, run_id)
                        invalidated['failure'] = {
                            'code': 'memory_invalidated',
                            'message': 'conversation sources changed during terminal recovery',
                            'retryable': False,
                        }
                        yield self._outbound(scope, OutboundEventKind.FAILED, invalidated)
                        return
                    payload['replayed'] = True
                    yield self._outbound(scope, kind, payload)
                    return
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
                    run_id,
                    message_digest,
                    snapshot.session.revision,
                    payload,
                    memory_cutoff=memory_cutoff,
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
            if cached['state'] == OutboundEventKind.COMPLETED.value and before_commit is not None:
                await before_commit()
            current = await self._sessions.get(identity)
            if current is None or current.memory_invalidated_revision != memory_cutoff:
                payload = self._application_failure_payload(identity, run_id)
                payload['failure'] = {'code': 'memory_invalidated',
                    'message': 'conversation sources changed before replay', 'retryable': False}
                yield self._outbound(scope, OutboundEventKind.FAILED, payload)
                return
            yield self._cached_terminal(scope, cached)
            return

        recent = await self._sessions.recent_transcript(
            identity,
            limit=self._conversation_scan_limit,
        )
        recent = tuple(event for event in recent if event.revision > memory_cutoff)
        memory_preparation = await self._memory_resolver.resolve(scope).prepare(
            identity,
            recent,
            memory_state,
        )
        conversation = memory_preparation.context
        memory_state = memory_preparation.state
        if self._archive is not None:
            try:
                archive_text = await self._archive_text(identity, memory_cutoff, snapshot.session.revision)
                conversation = conversation.model_copy(update={'long_term_context': archive_text[:4000]})
            except (OSError, ValueError):
                _LOGGER.warning('memory_archive_read_failed')
                conversation = conversation.model_copy(update={'memory_degraded': True})
        if conversation_override is not None:
            conversation = conversation_override.model_copy(deep=True)

        prior_revision = snapshot.session.revision
        user_event = await self._sessions.append(
            identity,
            event_id=self._stable_id("event", run_id, "user"),
            event_type=TranscriptEventType.USER_MESSAGE,
            payload=(copy.deepcopy(user_payload_override) if user_payload_override is not None else {
                **self._message_payload(message, scope),
                **({'continuation_of_run_id': continuation_of} if continuation_of else {}),
            }),
            idempotency_key=self._stable_id(
                "idem", identity.principal_id, run_id, "user"
            ),
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
            workflow_state=self._history_state(history, memory_state),
            expected_revision=revision,
            idempotency_key=self._stable_id(
                "idem", identity.principal_id, run_id, "started"
            ),
        )
        interrupted: BaseException | None = None
        partial_parts: list[str] = []
        partial_bytes = 0
        partial_truncated = False
        resume_unsafe = False
        # Keep incomplete output bounded independently of product/model behavior.
        partial_limit = min(128 * 1024, self._history_bytes // 8)
        stop_seen = asyncio.Event()
        stop_watcher = asyncio.create_task(self._observe_stop(identity, scope, run_id, stop_seen))
        try:
            yield self._started(scope, identity, run_id, replayed=False)
            run_input = (copy.deepcopy(run_input_override) if run_input_override is not None
                         else self._input_builder(message, scope))
            canonical_json(run_input)
            async with self._grants.lease(scope) as issued:
                request = RunRequest(
                    run_id=run_id,
                    scope=scope,
                    grant_id=issued.grant_id,
                    input=run_input,
                    conversation=conversation,
                    # A continuation always narrows a newly issued mixed grant
                    # to the protected read-only resume surface.
                    read_only=True if resume_state is not None else issued.read_only,
                    budget=self._budget,
                    resume=resume_state,
                )
                result = None
                async with aclosing(self._activation.stream(request)) as source:
                    async for event in source:
                        record = await self._sessions.get(identity)
                        if record is None or record.memory_invalidated_revision != memory_cutoff:
                            raise ChannelApplicationError('conversation memory was invalidated during execution')
                        if event.run_id != run_id:
                            raise ValueError('execution stream belongs to another run')
                        if event.kind == 'tool_started':
                            # A started call without a later complete checkpoint
                            # makes an older point unsafe to replay.
                            resume_unsafe = True
                        if event.kind == 'checkpoint':
                            # Runner checkpoints are an application/store-only
                            # control signal, never a user-visible STATUS. Store
                            # rejection is nonfatal to the ordinary run.
                            try:
                                point = RunResumePoint(
                                    identity=identity, run_id=run_id,
                                    payload={**event.data, 'message_digest': message_digest},
                                    memory_invalidated_revision=record.memory_invalidated_revision,
                                    memory_source_versions=record.memory_source_versions,
                                    created_at=datetime.now(UTC),
                                )
                                saved = await self._sessions.save_resume_point(identity, point)
                                if saved is not None:
                                    resume_unsafe = False
                            except (TypeError, ValueError):
                                _LOGGER.warning('run_resume_point_rejected')
                            continue
                        if event.result is not None:
                            if result is not None:
                                raise ValueError('execution stream repeated a terminal result')
                            result = event.result
                        elif event.kind != 'started':
                            if stop_seen.is_set():
                                continue
                            if event.kind == 'delta':
                                encoded = event.text.encode('utf-8')
                                retained = encoded[:max(0, partial_limit - partial_bytes)].decode('utf-8', errors='ignore')
                                if retained:
                                    partial_parts.append(retained)
                                partial_bytes += len(retained.encode('utf-8'))
                                partial_truncated |= retained != event.text
                            yield self._outbound(
                                scope,
                                OutboundEventKind.DELTA if event.kind == 'delta' else OutboundEventKind.STATUS,
                                {'run_id': run_id, 'session_id': identity.session_id,
                                 'product_id': identity.product_id, 'sequence': event.sequence,
                                 'type': event.kind, 'text': event.text, 'data': event.data},
                            )
                if not isinstance(result, RunResult):
                    raise TypeError("customer bundle returned an invalid run result")
        except (asyncio.CancelledError, GeneratorExit) as exc:
            interrupted = exc
            # Closing the application stream can discard already-buffered
            # TOOL_STARTED events. Without observing a later complete
            # checkpoint, automatic continuation must fail closed.
            resume_unsafe = True
            kind = OutboundEventKind.FAILED
            payload = self._application_failure_payload(identity, run_id)
            payload.update(run_status=RunStatus.CANCELLED.value, failure={
                'code': 'cancelled', 'message': 'channel stream was closed', 'retryable': False,
            })
            transcript_type = TranscriptEventType.CANCEL
            session_status = SessionStatus.CANCELLED
        except Exception:
            kind = OutboundEventKind.FAILED
            payload = self._application_failure_payload(identity, run_id)
            transcript_type = TranscriptEventType.ERROR
            session_status = SessionStatus.FAILED
        else:
            kind, payload, transcript_type, session_status = self._terminal_from_result(
                identity, result
            )
        finally:
            stop_watcher.cancel()
            await asyncio.gather(stop_watcher, return_exceptions=True)

        if kind is OutboundEventKind.FAILED and partial_parts:
            payload.update(partial_output=''.join(partial_parts), incomplete=True,
                            partial_truncated=partial_truncated)
        if session_status is SessionStatus.CANCELLED:
            payload['resume_unsafe'] = resume_unsafe

        record = await self._sessions.get(identity)
        if record is None or record.memory_invalidated_revision != memory_cutoff:
            # A source mutation committed while the old run was executing. Never
            # publish or persist its answer/summary over the newer watermark.
            if interrupted is not None:
                raise interrupted
            invalidated = self._application_failure_payload(identity, run_id)
            invalidated['failure'] = {'code': 'memory_invalidated',
                'message': 'conversation sources changed during execution', 'retryable': False}
            yield self._outbound(scope, OutboundEventKind.FAILED, invalidated)
            return

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
            if interrupted is not None:
                raise interrupted
            yield self._outbound(scope, OutboundEventKind.FAILED, payload)
            return

        if stop_seen.is_set() or await self._sessions.stop_requested(identity, run_id=run_id):
            kind = OutboundEventKind.FAILED
            payload = self._application_failure_payload(identity, run_id)
            payload.update(run_status=RunStatus.CANCELLED.value, failure={
                'code': 'cancelled', 'message': 'the run was stopped', 'retryable': False,
            })
            if partial_parts:
                payload.update(partial_output=''.join(partial_parts), incomplete=True,
                               partial_truncated=partial_truncated)
            payload['resume_unsafe'] = resume_unsafe
            transcript_type = TranscriptEventType.CANCEL
            session_status = SessionStatus.CANCELLED
            if isinstance(interrupted, asyncio.CancelledError):
                interrupted = None

        if kind is OutboundEventKind.COMPLETED and before_commit is not None:
            try:
                await before_commit()
            except BaseException as exc:
                # Keep the submitted user input and a safe failed outcome for
                # audit, but never save the denied assistant output or archive.
                interrupted = exc
                kind = OutboundEventKind.FAILED
                payload = self._application_failure_payload(identity, run_id)
                payload['failure'] = {'code': 'commit_rejected',
                    'message': 'request authorization changed before completion', 'retryable': False}
                transcript_type = TranscriptEventType.ERROR
                session_status = SessionStatus.FAILED
                if isinstance(exc, asyncio.CancelledError | GeneratorExit):
                    payload.update(run_status=RunStatus.CANCELLED.value, failure={
                        'code': 'cancelled', 'message': 'channel stream was closed', 'retryable': False,
                    })
                    transcript_type = TranscriptEventType.CANCEL
                    session_status = SessionStatus.CANCELLED

        terminal_event = await self._sessions.append(
            identity,
            event_id=self._stable_id("event", run_id, "terminal"),
            event_type=transcript_type,
            payload=payload,
            idempotency_key=self._stable_id(
                "idem", identity.principal_id, run_id, "terminal"
            ),
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
            workflow_state=self._history_state(history, memory_state),
            expected_revision=terminal_event.revision,
            idempotency_key=self._stable_id(
                "idem", identity.principal_id, run_id, "terminal-state"
            ),
        )
        await self._sessions.set_status(
            identity,
            session_status,
            expected_revision=checkpoint.revision,
        )
        if kind is OutboundEventKind.COMPLETED:
            await self._archive_completed(identity, memory_cutoff, terminal_event.revision,
                                          archive_question_override or message.text,
                                          payload.get('output'))
        if interrupted is not None:
            raise interrupted
        record = await self._sessions.get(identity)
        if record is None or record.memory_invalidated_revision != memory_cutoff:
            kind = OutboundEventKind.FAILED
            payload = self._application_failure_payload(identity, run_id)
            payload['failure'] = {'code': 'memory_invalidated',
                'message': 'conversation sources changed during execution', 'retryable': False}
        yield self._outbound(scope, kind, payload)

    async def _merge_recovered_terminal(
        self, identity: SessionIdentity, run_id: str, message_digest: str,
        terminal: TranscriptEvent, memory_cutoff: int,
    ) -> tuple[bool, bool]:
        """Merge one terminal into a fresh snapshot using that snapshot's revision.

        The second flag is true only for the newest input: replaying an older
        run must not move session status or its derived archive backwards.
        """
        for _ in range(4):
            snapshot = await self._sessions.resume(identity)
            if (snapshot is None
                    or snapshot.session.memory_invalidated_revision != memory_cutoff):
                return False, False
            state = snapshot.checkpoint.workflow_state if snapshot.checkpoint else None
            history = self._load_history(state)
            index = self._find_run(history, run_id)
            if index is None:
                # A newer checkpoint can have evicted this completed run. Its
                # transcript is still replayable; do not resurrect old state.
                return True, False
            cached = history[index]
            if cached['message_digest'] != message_digest:
                raise ChannelMessageConflictError(
                    'external message identity was reused with different content'
                )
            recent = await self._sessions.recent_transcript(
                identity, limit=self._conversation_scan_limit,
            )
            latest_user = next((event for event in reversed(recent)
                                if event.type is TranscriptEventType.USER_MESSAGE), None)
            latest = (index == len(history) - 1 and latest_user is not None
                      and latest_user.event_id == self._stable_id('event', run_id, 'user'))
            if (any(item['state'] == 'started' for item in history[index + 1:])
                    or latest_user is None
                    or latest_user.event_id != self._stable_id(
                        'event', cast(str, history[-1]['run_id']), 'user',
                    )):
                # A newer input may be between its USER and started checkpoint,
                # or another run may still own the session's execution revision.
                # Replay this durable terminal without mutating their revision.
                return True, False
            revision = snapshot.session.revision
            status = (SessionStatus.COMPLETED
                      if terminal.type is TranscriptEventType.ASSISTANT_MESSAGE
                      else (SessionStatus.CANCELLED
                            if terminal.type is TranscriptEventType.CANCEL
                            else SessionStatus.FAILED))
            try:
                if cached['state'] == 'started':
                    history[index] = {
                        'run_id': run_id, 'message_digest': message_digest,
                        'state': (OutboundEventKind.COMPLETED.value
                                  if terminal.type is TranscriptEventType.ASSISTANT_MESSAGE
                                  else OutboundEventKind.FAILED.value),
                        'payload': copy.deepcopy(terminal.payload),
                    }
                    checkpoint = await self._sessions.save_checkpoint(
                        identity,
                        checkpoint_id=self._stable_id(
                            'checkpoint', run_id, 'terminal-recovered', str(revision),
                        ),
                        workflow_state=self._history_state(history, self._load_memory_state(state)),
                        expected_revision=revision,
                        idempotency_key=self._stable_id(
                            'idem', identity.principal_id, run_id, 'terminal-recovered', str(revision),
                        ),
                    )
                    revision = checkpoint.revision
                if latest and snapshot.session.status is not status:
                    await self._sessions.set_status(identity, status, expected_revision=revision)
                return True, latest
            except SessionRevisionConflictError:
                # Retry the merge, never substitute a newer revision onto an
                # older history or memory projection.
                continue
        raise ChannelApplicationError('conversation changed during terminal recovery; retry')

    async def _recover_uncertain(
        self,
        identity: SessionIdentity,
        run_id: str,
        message_digest: str,
        expected_revision: int,
        payload: dict[str, JsonValue],
        *,
        memory_cutoff: int,
    ) -> None:
        terminal = await self._sessions.append(
            identity,
            event_id=self._stable_id("event", run_id, "uncertain"),
            event_type=TranscriptEventType.ERROR,
            payload=payload,
            idempotency_key=self._stable_id(
                "idem", identity.principal_id, run_id, "uncertain"
            ),
            expected_revision=expected_revision,
        )
        # The append can race another instance's complete run or invalidation.
        # Merge only this terminal into a fresh snapshot; a changed cutoff
        # leaves the newer projection untouched while the caller reports the
        # already-durable uncertain outcome.
        await self._merge_recovered_terminal(
            identity, run_id, message_digest, terminal, memory_cutoff,
        )

    def _append_started(
        self,
        history: list[dict[str, JsonValue]],
        run_id: str,
        message_digest: str,
    ) -> None:
        if len(history) >= self._history_limit:
            removable = next(
                (
                    index
                    for index, item in enumerate(history)
                    if item["state"] != "started"
                ),
                None,
            )
            if removable is None:
                raise ChannelApplicationError(
                    "checkpoint contains too many unfinished runs"
                )
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
        memory_state: MemoryState,
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
                "memory": cast(
                    dict[str, JsonValue],
                    memory_state.model_dump(mode="json"),
                ),
            }
            if len(canonical_json(state).encode("utf-8")) <= self._history_bytes:
                return cast(dict[str, JsonValue], copy.deepcopy(state))
            removable = next(
                (
                    index
                    for index, item in enumerate(history)
                    if item["state"] != "started"
                ),
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
            raise ChannelApplicationError(
                "session checkpoint belongs to an incompatible owner"
            )
        raw_runs = state.get("runs")
        if not isinstance(raw_runs, list):
            raise ChannelApplicationError(
                "session checkpoint has an invalid run history"
            )
        history: list[dict[str, JsonValue]] = []
        seen: set[str] = set()
        for item in raw_runs:
            if not isinstance(item, dict):
                raise ChannelApplicationError(
                    "session checkpoint has an invalid run entry"
                )
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
                not in {
                    "started",
                    OutboundEventKind.COMPLETED.value,
                    OutboundEventKind.FAILED.value,
                }
                or (run_state != "started" and not isinstance(payload, dict))
            ):
                raise ChannelApplicationError(
                    "session checkpoint has an invalid run entry"
                )
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

    def _load_memory_state(self, state: JsonValue) -> MemoryState:
        if state is None:
            return MemoryState()
        if len(canonical_json(state).encode("utf-8")) > self._history_bytes:
            raise ChannelApplicationError("session checkpoint exceeds its byte limit")
        if not isinstance(state, dict) or state.get("schema") != _CHECKPOINT_SCHEMA:
            raise ChannelApplicationError(
                "session checkpoint belongs to an incompatible owner"
            )
        raw_memory = state.get("memory")
        if raw_memory is None:
            return MemoryState()
        if not isinstance(raw_memory, dict):
            raise ChannelApplicationError("session checkpoint has invalid memory state")
        try:
            return MemoryState.model_validate(raw_memory)
        except ValueError as exc:
            raise ChannelApplicationError(
                "session checkpoint has invalid memory state"
            ) from exc

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
        if not isinstance(message, InboundMessage) or not isinstance(
            scope, RequestScope
        ):
            raise TypeError("message and scope must be validated boundary models")
        if scope.path.kind is not ScopeKind.AGENT:
            raise ChannelApplicationError(
                "company-channel requests require an agent scope"
            )
        if not scope.request_id or not scope.correlation_id:
            raise ChannelApplicationError(
                "company-channel scope requires request correlation ids"
            )
        if message.channel.value != scope.channel_id:
            raise ChannelApplicationError(
                "message channel does not match authenticated scope"
            )
        if message.product_id is not None and message.product_id != scope.product_id:
            raise ChannelApplicationError(
                "message product does not match authenticated scope"
            )
        if self._activation.tenant_id != scope.tenant_id:
            raise ChannelApplicationError(
                "request tenant does not match active customer bundle"
            )

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
