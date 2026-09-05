from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from suiteharness.sessions import (
    InMemorySessionStore,
    SessionCapacityError,
    SessionIdempotencyConflictError,
    SessionIdentity,
    SessionPayloadTooLargeError,
    SessionRevisionConflictError,
    SessionRunClaimState,
    SessionStatus,
    SessionStoreLimits,
    SQLiteSessionStore,
    TranscriptEventType,
)


def _identity(*, product: str = "product-a", principal: str = "user-a") -> SessionIdentity:
    return SessionIdentity(
        tenant_id="tenant-a",
        product_id=product,
        agent_id="agent-a",
        session_id="session-a",
        principal_id=principal,
    )


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_store_isolation_checkpoint_resume_and_idempotency(tmp_path: Path, kind: str) -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        database = tmp_path / "sessions.sqlite3"
        store = InMemorySessionStore() if kind == "memory" else SQLiteSessionStore(database)
        identity = _identity()
        other = _identity(product="product-b")
        await store.create(identity)
        await store.create(other)

        await asyncio.gather(
            *(
                store.append(
                    identity,
                    event_id=f"event-{index}",
                    event_type=TranscriptEventType.USER_MESSAGE,
                    payload={"index": index},
                )
                for index in range(12)
            )
        )
        record = await store.get(identity)
        assert record is not None
        first = await store.append(
            identity,
            event_id="event-idempotent-a",
            event_type=TranscriptEventType.ASSISTANT_MESSAGE,
            payload={"text": "same"},
            idempotency_key="reply-key",
            expected_revision=record.revision,
        )
        retry = await store.append(
            identity,
            event_id="event-idempotent-b",
            event_type=TranscriptEventType.ASSISTANT_MESSAGE,
            payload={"text": "same"},
            idempotency_key="reply-key",
            expected_revision=0,
        )
        assert retry == first
        with pytest.raises(SessionIdempotencyConflictError):
            await store.append(
                identity,
                event_id="event-conflict",
                event_type=TranscriptEventType.ASSISTANT_MESSAGE,
                payload={"text": "different"},
                idempotency_key="reply-key",
            )

        record = await store.get(identity)
        assert record is not None
        checkpoint = await store.save_checkpoint(
            identity,
            checkpoint_id="checkpoint-a",
            workflow_state={"messages": ["saved"]},
            expected_revision=record.revision,
            idempotency_key="checkpoint-key",
        )
        retry_checkpoint = await store.save_checkpoint(
            identity,
            checkpoint_id="checkpoint-b",
            workflow_state={"messages": ["saved"]},
            expected_revision=0,
            idempotency_key="checkpoint-key",
        )
        assert retry_checkpoint == checkpoint
        with pytest.raises(SessionRevisionConflictError):
            await store.save_checkpoint(
                identity,
                checkpoint_id="checkpoint-stale",
                workflow_state={"messages": []},
                expected_revision=0,
                idempotency_key="checkpoint-stale-key",
            )

        await store.append(
            identity,
            event_id="event-after-checkpoint",
            event_type=TranscriptEventType.TOOL_RESULT,
            payload={"ok": True},
        )
        recent = await store.recent_transcript(identity, limit=2)
        if isinstance(store, SQLiteSessionStore):
            await store.close()
            store = SQLiteSessionStore(database)
        resumed = await store.resume(identity)
        isolated = await store.resume(other)
        current = await store.get(identity)
        assert current is not None
        completed = await store.set_status(
            identity,
            SessionStatus.COMPLETED,
            expected_revision=current.revision,
        )
        if isinstance(store, SQLiteSessionStore):
            await store.close()
        return resumed, isolated, completed, recent

    resumed, isolated, completed, recent = asyncio.run(exercise())
    assert resumed is not None
    assert resumed.checkpoint is not None
    assert resumed.checkpoint.workflow_state == {"messages": ["saved"]}
    assert [event.event_id for event in resumed.transcript] == ["event-after-checkpoint"]
    assert isolated is not None
    assert isolated.transcript == ()
    assert isolated.checkpoint is None
    assert completed.status is SessionStatus.COMPLETED
    assert [event.event_id for event in recent] == [
        "event-idempotent-a",
        "event-after-checkpoint",
    ]


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_recent_transcript_is_identity_scoped_and_bounded(tmp_path: Path, kind: str) -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        store = (
            InMemorySessionStore()
            if kind == "memory"
            else SQLiteSessionStore(tmp_path / "recent.sqlite3")
        )
        identity = _identity()
        other = _identity(principal="other")
        await store.create(identity)
        await store.create(other)
        for index in range(3):
            await store.append(
                identity,
                event_id=f"event-{index}",
                event_type=TranscriptEventType.USER_MESSAGE,
                payload={"index": index},
            )
        result = await store.recent_transcript(identity, limit=2)
        isolated = await store.recent_transcript(other, limit=2)
        with pytest.raises(ValueError, match="between 1 and 1000"):
            await store.recent_transcript(identity, limit=0)
        if isinstance(store, SQLiteSessionStore):
            await store.close()
        return result, isolated

    result, isolated = asyncio.run(exercise())
    assert [event.event_id for event in result] == ["event-1", "event-2"]
    assert isolated == ()


def test_principal_is_part_of_session_isolation_key() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        store = InMemorySessionStore()
        alice = _identity(principal="alice")
        bob = _identity(principal="bob")
        await store.create(alice)
        await store.create(bob)
        await store.append(
            alice,
            event_id="alice-event",
            event_type=TranscriptEventType.USER_MESSAGE,
            payload="private",
        )
        return await store.resume(alice), await store.resume(bob)

    alice, bob = asyncio.run(exercise())
    assert alice is not None and len(alice.transcript) == 1
    assert bob is not None and bob.transcript == ()


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_expired_run_claim_fences_old_owner_without_allowing_reexecution(
    tmp_path: Path,
    kind: str,
) -> None:
    async def exercise() -> tuple[
        SessionRunClaimState,
        bool,
        SessionRunClaimState,
        bool,
        SessionRunClaimState,
    ]:
        current = datetime(2026, 9, 5, tzinfo=UTC)

        def clock() -> datetime:
            return current

        store = (
            InMemorySessionStore(clock=clock)
            if kind == "memory"
            else SQLiteSessionStore(tmp_path / "claims.sqlite3", clock=clock)
        )
        identity = _identity()
        await store.create(identity)
        acquired = await store.claim_run(
            identity,
            run_id="run-a",
            owner_token="a" * 32,
            lease_seconds=10,
        )
        wrong_owner = await store.finalize_run(
            identity,
            run_id="run-a",
            owner_token="b" * 32,
            lease_seconds=10,
        )
        current += timedelta(seconds=11)
        stale = await store.claim_run(
            identity,
            run_id="run-a",
            owner_token="b" * 32,
            lease_seconds=10,
        )
        old_owner = await store.finalize_run(
            identity,
            run_id="run-a",
            owner_token="a" * 32,
            lease_seconds=10,
        )
        still_stale = await store.claim_run(
            identity,
            run_id="run-a",
            owner_token="c" * 32,
            lease_seconds=10,
        )
        if isinstance(store, SQLiteSessionStore):
            await store.close()
        return acquired, wrong_owner, stale, old_owner, still_stale

    acquired, wrong_owner, stale, old_owner, still_stale = asyncio.run(exercise())
    assert acquired is SessionRunClaimState.ACQUIRED
    assert wrong_owner is False
    assert stale is SessionRunClaimState.STALE
    assert old_owner is False
    assert still_stale is SessionRunClaimState.STALE


def test_session_store_limits_are_strict_and_internally_consistent() -> None:
    with pytest.raises(ValueError):
        SessionStoreLimits(max_sessions=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SessionStoreLimits(max_sessions="2")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="transcript_events_total"):
        SessionStoreLimits(
            max_transcript_events_per_session=2,
            max_transcript_events_total=1,
        )
    with pytest.raises(ValueError, match="checkpoint_bytes_per_session"):
        SessionStoreLimits(
            max_checkpoint_state_bytes=2_048,
            max_checkpoint_bytes_per_session=1_024,
        )


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_session_store_fails_closed_at_durable_idempotency_limits(
    tmp_path: Path,
    kind: str,
) -> None:
    async def exercise() -> None:
        limits = SessionStoreLimits(
            max_sessions=2,
            max_transcript_payload_bytes=1_024,
            max_transcript_events_per_session=1,
            max_transcript_events_total=1,
            max_run_claims_per_session=1,
            max_run_claims_total=1,
        )
        store = (
            InMemorySessionStore(limits=limits)
            if kind == "memory"
            else SQLiteSessionStore(tmp_path / "bounded.sqlite3", limits=limits)
        )
        first = _identity()
        second = _identity(principal="user-b")
        await store.create(first)
        await store.create(second)
        with pytest.raises(SessionCapacityError, match="max_sessions"):
            await store.create(_identity(principal="user-c"))

        with pytest.raises(SessionPayloadTooLargeError, match="transcript payload"):
            await store.append(
                first,
                event_id="oversized-event",
                event_type=TranscriptEventType.USER_MESSAGE,
                payload="x" * 1_100,
            )
        before = await store.get(first)
        assert before is not None
        await store.append(
            first,
            event_id="event-a",
            event_type=TranscriptEventType.USER_MESSAGE,
            payload={"ok": True},
            expected_revision=before.revision,
        )
        with pytest.raises(SessionCapacityError, match="per_session"):
            await store.append(
                first,
                event_id="event-b",
                event_type=TranscriptEventType.USER_MESSAGE,
                payload={"ok": True},
            )
        with pytest.raises(SessionCapacityError, match="total"):
            await store.append(
                second,
                event_id="event-c",
                event_type=TranscriptEventType.USER_MESSAGE,
                payload={"ok": True},
            )

        assert await store.claim_run(
            first,
            run_id="run-a",
            owner_token="a" * 32,
            lease_seconds=10,
        ) is SessionRunClaimState.ACQUIRED
        with pytest.raises(SessionCapacityError, match="per_session"):
            await store.claim_run(
                first,
                run_id="run-b",
                owner_token="b" * 32,
                lease_seconds=10,
            )
        with pytest.raises(SessionCapacityError, match="total"):
            await store.claim_run(
                second,
                run_id="run-c",
                owner_token="c" * 32,
                lease_seconds=10,
            )
        after = await store.get(first)
        assert after is not None and after.revision == before.revision + 1
        if isinstance(store, SQLiteSessionStore):
            await store.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
@pytest.mark.parametrize(
    ("max_checkpoints", "max_bytes", "padding_size", "expected_retained"),
    [
        (2, 4_096, 100, 2),
        (8, 1_024, 600, 1),
    ],
    ids=("count", "bytes"),
)
def test_checkpoint_window_is_bounded_by_count_and_bytes(
    tmp_path: Path,
    kind: str,
    max_checkpoints: int,
    max_bytes: int,
    padding_size: int,
    expected_retained: int,
) -> None:
    async def exercise() -> tuple[str, str]:
        limits = SessionStoreLimits(
            max_checkpoint_state_bytes=1_024,
            max_checkpoints_per_session=max_checkpoints,
            max_checkpoint_bytes_per_session=max_bytes,
        )
        store = (
            InMemorySessionStore(limits=limits)
            if kind == "memory"
            else SQLiteSessionStore(tmp_path / "checkpoints.sqlite3", limits=limits)
        )
        identity = _identity()
        record = await store.create(identity)
        for index in range(3):
            checkpoint = await store.save_checkpoint(
                identity,
                checkpoint_id=f"checkpoint-{index}",
                workflow_state={"value": str(index), "padding": "x" * padding_size},
                expected_revision=record.revision,
                idempotency_key=f"checkpoint-key-{index}",
            )
            record = await store.get(identity)
            assert record is not None and record.revision == checkpoint.revision
        resumed = await store.resume(identity)
        assert resumed is not None and resumed.checkpoint is not None
        with pytest.raises(SessionRevisionConflictError):
            await store.save_checkpoint(
                identity,
                checkpoint_id="checkpoint-old-retry",
                workflow_state={"value": "0", "padding": "x" * padding_size},
                expected_revision=0,
                idempotency_key="checkpoint-key-0",
            )
        with pytest.raises(SessionPayloadTooLargeError, match="checkpoint state"):
            await store.save_checkpoint(
                identity,
                checkpoint_id="checkpoint-oversized",
                workflow_state={"padding": "x" * 1_100},
                expected_revision=record.revision,
                idempotency_key="checkpoint-key-oversized",
            )
        if isinstance(store, SQLiteSessionStore):
            retained = store._connection.execute(  # noqa: SLF001
                "SELECT COUNT(*) FROM session_checkpoints"
            ).fetchone()[0]
            assert retained == expected_retained
            await store.close()
        else:
            assert len(store._checkpoints[identity]) == expected_retained  # noqa: SLF001
        return resumed.checkpoint.checkpoint_id, resumed.checkpoint.workflow_state["value"]

    checkpoint_id, value = asyncio.run(exercise())
    assert (checkpoint_id, value) == ("checkpoint-2", "2")
