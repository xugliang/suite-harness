from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from suiteharness.channels import ChannelKind, InboundMessage, OutboundEventKind
from suiteharness.execution import (
    CapabilityGrant,
    FailureCode,
    RunFailure,
    RunResult,
    RunStatus,
)
from suiteharness.runtime import RequestScope, ScopePath
from suiteharness.server import (
    ChannelApplication,
    ChannelApplicationError,
    ChannelMessageConflictError,
    IssuedToolGrant,
)
from suiteharness.sessions import (
    InMemorySessionStore,
    SessionIdentity,
    SessionStatus,
    SQLiteSessionStore,
)


def _scope(
    *,
    principal: str = "alice",
    channel: str = "web",
    session_owner: str | None = None,
) -> RequestScope:
    return RequestScope(
        path=ScopePath.agent("tenant-a", "product-a", "agent-a", "session-a"),
        principal_id=principal,
        channel_id=channel,
        roles=frozenset({"employee"}),
        purpose="company-channel-message",
        request_id=f"request-{principal}",
        correlation_id=f"correlation-{principal}",
        session_owner_id=session_owner,
    )


def _message(
    *,
    event_id: str = "event-a",
    message_id: str = "message-a",
    text: str = "hello",
    channel: ChannelKind = ChannelKind.WEB,
    sender: str = "external-alice",
) -> InboundMessage:
    return InboundMessage(
        channel=channel,
        event_id=event_id,
        message_id=message_id,
        conversation_id="conversation-a",
        sender_external_id=sender,
        text=text,
        product_id="product-a",
        received_at=datetime(2026, 9, 4, tzinfo=UTC),
    )


class _GrantIssuer:
    def __init__(self, *, read_only: bool = False) -> None:
        self.read_only = read_only
        self.entered = 0
        self.exited = 0

    @asynccontextmanager
    async def lease(self, scope: RequestScope):  # type: ignore[no-untyped-def]
        now = datetime.now(UTC)
        grant = CapabilityGrant(
            grant_id=f"grant-{scope.principal_id}",
            tenant_id=scope.tenant_id,
            product_id=scope.product_id,
            principal_id=scope.principal_id,
            tool_identities=frozenset(),
            issued_at=now,
            expires_at=now + timedelta(minutes=1),
        )
        self.entered += 1
        try:
            yield IssuedToolGrant(grant=grant, read_only=self.read_only)
        finally:
            self.exited += 1


class _Activation:
    tenant_id = "tenant-a"

    def __init__(self, *, fail: bool = False, delay: float = 0) -> None:
        self.fail = fail
        self.delay = delay
        self.requests = []
        self.active = 0
        self.max_active = 0

    async def run(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail:
                raise RuntimeError("do-not-leak-this-secret")
            now = datetime.now(UTC)
            return RunResult(
                run_id=request.run_id,
                status=RunStatus.SUCCEEDED,
                output={"echo": request.input["message"]},
                iterations_used=1,
                tool_calls_used=0,
                started_at=now,
                finished_at=now,
            )
        finally:
            self.active -= 1


async def _collect(application, message, scope):  # type: ignore[no-untyped-def]
    return [event async for event in application.handle(message, scope)]


def _identity(principal: str = "alice") -> SessionIdentity:
    return SessionIdentity(
        tenant_id="tenant-a",
        product_id="product-a",
        agent_id="agent-a",
        session_id="session-a",
        principal_id=principal,
    )


def test_successful_message_runs_once_and_is_durably_checkpointed() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        sessions = InMemorySessionStore()
        activation = _Activation()
        grants = _GrantIssuer(read_only=True)
        application = ChannelApplication(
            activation=activation,
            sessions=sessions,
            grants=grants,
        )
        events = await _collect(application, _message(), _scope())
        snapshot = await sessions.resume(_identity())
        return events, snapshot, activation, grants

    events, snapshot, activation, grants = asyncio.run(exercise())
    assert [event.kind for event in events] == [
        OutboundEventKind.STARTED,
        OutboundEventKind.COMPLETED,
    ]
    assert events[1].payload["output"] == {"echo": "hello"}
    assert events[1].payload["replayed"] is False
    assert len(activation.requests) == 1
    assert activation.requests[0].read_only is True
    assert activation.requests[0].scope.principal_id == "alice"
    assert grants.entered == grants.exited == 1
    assert snapshot is not None
    assert snapshot.session.status is SessionStatus.COMPLETED
    assert snapshot.checkpoint is not None
    assert snapshot.checkpoint.workflow_state["schema"] == "suiteharness.channel-application.v1"
    assert snapshot.checkpoint.workflow_state["runs"][0]["state"] == "completed"
    # The terminal checkpoint covers both transcript entries.
    assert snapshot.transcript == ()


def test_redelivery_replays_terminal_checkpoint_without_rerunning() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        activation = _Activation()
        application = ChannelApplication(
            activation=activation,
            sessions=InMemorySessionStore(),
            grants=_GrantIssuer(),
        )
        message = _message()
        first = await _collect(application, message, _scope())
        redelivery = message.model_copy(
            update={
                "event_id": "new-connection:event-a",
                "received_at": datetime(2026, 9, 5, tzinfo=UTC),
                "metadata": {"connection_id": "new-connection"},
            }
        )
        redelivery_scope = replace(_scope(), request_id="request-redelivery")
        second = await _collect(application, redelivery, redelivery_scope)
        return first, second, activation

    first, second, activation = asyncio.run(exercise())
    assert len(activation.requests) == 1
    assert first[0].payload["run_id"] == second[0].payload["run_id"]
    assert second[0].payload["replayed"] is True
    assert second[1].kind is OutboundEventKind.COMPLETED
    assert second[1].payload["replayed"] is True


def test_checkpoint_history_byte_limit_never_reexecutes_trimmed_message() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        sessions = InMemorySessionStore()
        activation = _Activation()
        application = ChannelApplication(
            activation=activation,
            sessions=sessions,
            grants=_GrantIssuer(),
            checkpoint_history_bytes=4_096,
        )
        first = _message(event_id="event-first", message_id="message-first", text="a" * 2_800)
        second = _message(
            event_id="event-second",
            message_id="message-second",
            text="b" * 2_800,
        )
        await _collect(application, first, _scope())
        await _collect(application, second, replace(_scope(), request_id="request-second"))
        snapshot = await sessions.resume(_identity())
        replay = await _collect(
            application,
            first,
            replace(_scope(), request_id="request-first-redelivery"),
        )
        return activation, snapshot, replay

    activation, snapshot, replay = asyncio.run(exercise())
    assert len(activation.requests) == 2
    assert snapshot is not None and snapshot.checkpoint is not None
    assert len(snapshot.checkpoint.workflow_state["runs"]) == 1
    assert replay[-1].kind is OutboundEventKind.FAILED
    assert replay[-1].payload["failure"]["code"] == "idempotent_result_unavailable"


def test_same_external_ids_with_changed_content_fail_closed() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        activation = _Activation()
        application = ChannelApplication(
            activation=activation,
            sessions=InMemorySessionStore(),
            grants=_GrantIssuer(),
        )
        await _collect(application, _message(text="first"), _scope())
        with pytest.raises(ChannelMessageConflictError):
            await _collect(application, _message(text="tampered"), _scope())
        return activation

    activation = asyncio.run(exercise())
    assert len(activation.requests) == 1


def test_application_failure_is_persisted_without_leaking_exception_detail() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        sessions = InMemorySessionStore()
        application = ChannelApplication(
            activation=_Activation(fail=True),
            sessions=sessions,
            grants=_GrantIssuer(),
        )
        events = await _collect(application, _message(), _scope())
        return events, await sessions.resume(_identity())

    events, snapshot = asyncio.run(exercise())
    assert events[-1].kind is OutboundEventKind.FAILED
    assert events[-1].payload["failure"]["code"] == "application_error"
    assert "do-not-leak" not in str(events[-1].payload)
    assert snapshot is not None
    assert snapshot.session.status is SessionStatus.FAILED
    assert snapshot.checkpoint is not None
    assert snapshot.checkpoint.workflow_state["runs"][0]["state"] == "failed"


def test_structured_runner_failure_is_preserved() -> None:
    class FailedActivation(_Activation):
        async def run(self, request):  # type: ignore[no-untyped-def]
            self.requests.append(request)
            now = datetime.now(UTC)
            return RunResult(
                run_id=request.run_id,
                status=RunStatus.FAILED,
                failure=RunFailure(
                    code=FailureCode.POLICY_DENIED,
                    message="tool denied by company channel policy",
                ),
                iterations_used=1,
                tool_calls_used=1,
                started_at=now,
                finished_at=now,
            )

    async def exercise():  # type: ignore[no-untyped-def]
        application = ChannelApplication(
            activation=FailedActivation(),
            sessions=InMemorySessionStore(),
            grants=_GrantIssuer(),
        )
        return await _collect(application, _message(), _scope())

    events = asyncio.run(exercise())
    assert events[-1].kind is OutboundEventKind.FAILED
    assert events[-1].payload["failure"]["code"] == "policy_denied"


def test_same_session_runs_are_serialized_and_different_principals_do_not_collide() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        sessions = InMemorySessionStore()
        activation = _Activation(delay=0.02)
        application = ChannelApplication(
            activation=activation,
            sessions=sessions,
            grants=_GrantIssuer(),
        )
        first, second = await asyncio.gather(
            _collect(application, _message(event_id="event-1", message_id="message-1"), _scope()),
            _collect(application, _message(event_id="event-2", message_id="message-2"), _scope()),
        )
        bob = await _collect(
            application,
            _message(event_id="event-1", message_id="message-1"),
            _scope(principal="bob"),
        )
        return first, second, bob, activation, await sessions.resume(_identity("bob"))

    first, second, bob, activation, bob_snapshot = asyncio.run(exercise())
    assert activation.max_active == 1
    assert first[0].payload["run_id"] != second[0].payload["run_id"]
    assert first[0].payload["run_id"] != bob[0].payload["run_id"]
    assert bob_snapshot is not None
    assert len(activation.requests) == 3
    assert activation.requests[2].conversation.messages == ()


def test_same_message_across_sqlite_instances_has_one_execution(tmp_path: Path) -> None:
    class BlockingActivation:
        tenant_id = "tenant-a"

        def __init__(self) -> None:
            self.requests = []
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run(self, request):  # type: ignore[no-untyped-def]
            self.requests.append(request)
            self.started.set()
            await self.release.wait()
            now = datetime.now(UTC)
            return RunResult(
                run_id=request.run_id,
                status=RunStatus.SUCCEEDED,
                output={"ok": True},
                iterations_used=1,
                tool_calls_used=0,
                started_at=now,
                finished_at=now,
            )

    async def exercise():  # type: ignore[no-untyped-def]
        database = tmp_path / "shared-sessions.sqlite3"
        first_store = SQLiteSessionStore(database)
        second_store = SQLiteSessionStore(database)
        activation = BlockingActivation()
        first_app = ChannelApplication(
            activation=activation,
            sessions=first_store,
            grants=_GrantIssuer(),
        )
        second_app = ChannelApplication(
            activation=activation,
            sessions=second_store,
            grants=_GrantIssuer(),
        )
        first_task = asyncio.create_task(_collect(first_app, _message(), _scope()))
        started_waiter = asyncio.create_task(activation.started.wait())
        done, _ = await asyncio.wait(
            (first_task, started_waiter),
            timeout=5,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if first_task in done:
            await first_task
            raise AssertionError("first run exited before activation")
        assert started_waiter in done
        duplicate = await _collect(second_app, _message(), _scope())
        activation.release.set()
        first = await first_task
        replay = await _collect(second_app, _message(), _scope())
        await first_store.close()
        await second_store.close()
        return first, duplicate, replay, activation.requests

    first, duplicate, replay, requests = asyncio.run(exercise())
    assert [item.kind for item in first] == [
        OutboundEventKind.STARTED,
        OutboundEventKind.COMPLETED,
    ]
    assert [item.kind for item in duplicate] == [
        OutboundEventKind.STARTED,
        OutboundEventKind.FAILED,
    ]
    assert duplicate[-1].payload["failure"]["code"] == "run_in_progress"
    assert replay[-1].kind is OutboundEventKind.COMPLETED
    assert replay[-1].payload["replayed"] is True
    assert len(requests) == 1


def test_second_turn_receives_only_completed_bounded_role_history() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        activation = _Activation()
        application = ChannelApplication(
            activation=activation,
            sessions=InMemorySessionStore(),
            grants=_GrantIssuer(),
        )
        await _collect(
            application,
            _message(event_id="event-1", message_id="message-1", text="first question"),
            _scope(),
        )
        await _collect(
            application,
            _message(event_id="event-2", message_id="message-2", text="follow up"),
            _scope(),
        )
        return activation.requests

    requests = asyncio.run(exercise())
    assert requests[0].conversation.messages == ()
    history = requests[1].conversation
    assert [message.role for message in history.messages] == ["user", "assistant"]
    assert history.messages[0].content == "first question"
    assert history.messages[0].principal_id == "alice"
    assert history.messages[1].content == {"echo": "first question"}
    assert history.truncated is False


def test_explicit_shared_session_preserves_actor_attribution_and_distinct_run_ids() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        activation = _Activation()
        sessions = InMemorySessionStore()
        application = ChannelApplication(
            activation=activation,
            sessions=sessions,
            grants=_GrantIssuer(),
        )
        await _collect(
            application,
            _message(
                event_id="event-a",
                message_id="message-a",
                text="alice says hello",
                sender="external-alice",
            ),
            _scope(principal="alice", session_owner="shared-room"),
        )
        await _collect(
            application,
            _message(
                event_id="event-b",
                message_id="message-b",
                text="bob follows up",
                sender="external-bob",
            ),
            _scope(principal="bob", session_owner="shared-room"),
        )
        snapshot = await sessions.resume(_identity("shared-room"))
        return activation.requests, snapshot

    requests, snapshot = asyncio.run(exercise())
    assert requests[0].run_id != requests[1].run_id
    assert requests[1].scope.principal_id == "bob"
    assert requests[1].scope.effective_session_owner_id == "shared-room"
    assert requests[1].conversation.messages[0].principal_id == "alice"
    assert snapshot is not None
    assert snapshot.session.identity.principal_id == "shared-room"


def test_failed_exchange_is_not_replayed_as_conversation_context() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        activation = _Activation(fail=True)
        application = ChannelApplication(
            activation=activation,
            sessions=InMemorySessionStore(),
            grants=_GrantIssuer(),
        )
        await _collect(
            application,
            _message(event_id="event-fail", message_id="message-fail", text="failed input"),
            _scope(),
        )
        activation.fail = False
        await _collect(
            application,
            _message(event_id="event-next", message_id="message-next", text="try again"),
            _scope(),
        )
        return activation.requests

    requests = asyncio.run(exercise())
    assert len(requests) == 2
    assert requests[1].conversation.messages == ()


def test_started_checkpoint_is_not_rerun_after_uncertain_crash() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        sessions = InMemorySessionStore()
        identity = _identity()
        scope = _scope()
        message = _message()
        activation = _Activation()
        application = ChannelApplication(
            activation=activation,
            sessions=sessions,
            grants=_GrantIssuer(),
        )
        run_id = application._stable_id(  # noqa: SLF001
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
        digest = application._message_digest(message, scope)  # noqa: SLF001
        record = await sessions.create(identity)
        user = await sessions.append(
            identity,
            event_id=application._stable_id("event", run_id, "user"),  # noqa: SLF001
            event_type="user_message",
            payload=message.model_dump(mode="json"),
            idempotency_key=application._stable_id(  # noqa: SLF001
                "idem", identity.principal_id, run_id, "user"
            ),
            expected_revision=record.revision,
        )
        await sessions.save_checkpoint(
            identity,
            checkpoint_id=application._stable_id("checkpoint", run_id, "started"),  # noqa: SLF001
            workflow_state={
                "schema": "suiteharness.channel-application.v1",
                "runs": [
                    {
                        "run_id": run_id,
                        "message_digest": digest,
                        "state": "started",
                        "payload": None,
                    }
                ],
            },
            expected_revision=user.revision,
            idempotency_key=application._stable_id(  # noqa: SLF001
                "idem", identity.principal_id, run_id, "started"
            ),
        )
        events = await _collect(application, message, scope)
        return events, activation, await sessions.resume(identity)

    events, activation, snapshot = asyncio.run(exercise())
    assert len(activation.requests) == 0
    assert [event.kind for event in events] == [
        OutboundEventKind.STARTED,
        OutboundEventKind.FAILED,
    ]
    assert events[-1].payload["failure"]["code"] == "run_outcome_uncertain"
    assert snapshot is not None and snapshot.session.status is SessionStatus.FAILED


def test_storage_failure_propagates_and_product_is_not_run() -> None:
    class FailingStore:
        def __init__(self) -> None:
            self.delegate = InMemorySessionStore()

        def __getattr__(self, name: str):
            return getattr(self.delegate, name)

        async def save_checkpoint(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise OSError("disk is unavailable")

    async def exercise():  # type: ignore[no-untyped-def]
        activation = _Activation()
        application = ChannelApplication(
            activation=activation,
            sessions=FailingStore(),
            grants=_GrantIssuer(),
        )
        with pytest.raises(OSError, match="disk is unavailable"):
            await _collect(application, _message(), _scope())
        return activation

    activation = asyncio.run(exercise())
    assert len(activation.requests) == 0


def test_terminal_checkpoint_repairs_a_status_write_that_failed() -> None:
    class FailStatusOnce:
        def __init__(self) -> None:
            self.delegate = InMemorySessionStore()
            self.failed = False

        def __getattr__(self, name: str):
            return getattr(self.delegate, name)

        async def set_status(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            if not self.failed:
                self.failed = True
                raise OSError("status fsync failed")
            return await self.delegate.set_status(*args, **kwargs)

    async def exercise():  # type: ignore[no-untyped-def]
        store = FailStatusOnce()
        activation = _Activation()
        application = ChannelApplication(
            activation=activation,
            sessions=store,
            grants=_GrantIssuer(),
        )
        with pytest.raises(OSError, match="status fsync failed"):
            await _collect(application, _message(), _scope())
        replay = await _collect(application, _message(), _scope())
        return replay, activation, await store.resume(_identity())

    replay, activation, snapshot = asyncio.run(exercise())
    assert len(activation.requests) == 1
    assert replay[-1].kind is OutboundEventKind.COMPLETED
    assert replay[-1].payload["replayed"] is True
    assert snapshot is not None and snapshot.session.status is SessionStatus.COMPLETED


def test_cancelled_lock_waiter_does_not_leak_session_lock() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        application = ChannelApplication(
            activation=_Activation(),
            sessions=InMemorySessionStore(),
            grants=_GrantIssuer(),
        )
        first_stream = application.handle(_message(), _scope())
        assert (await anext(first_stream)).kind is OutboundEventKind.STARTED
        waiter = asyncio.create_task(
            _collect(
                application,
                _message(event_id="event-b", message_id="message-b"),
                _scope(),
            )
        )
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await first_stream.aclose()
        assert application._locks._entries == {}  # noqa: SLF001

    asyncio.run(exercise())


def test_boundary_mismatches_are_rejected_before_persistence() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        application = ChannelApplication(
            activation=_Activation(),
            sessions=InMemorySessionStore(),
            grants=_GrantIssuer(),
        )
        with pytest.raises(ChannelApplicationError, match="channel"):
            await _collect(application, _message(channel=ChannelKind.FEISHU), _scope(channel="web"))
        product_scope = RequestScope(
            path=ScopePath.product("tenant-a", "product-a"),
            principal_id="alice",
            channel_id="web",
            request_id="request-a",
            correlation_id="correlation-a",
        )
        with pytest.raises(ChannelApplicationError, match="agent scope"):
            await _collect(application, _message(), product_scope)

    asyncio.run(exercise())
