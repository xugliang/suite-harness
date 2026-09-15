"""Memory derives bounded complete turns from the host's durable transcript."""

import asyncio
from datetime import UTC, datetime

import pytest

from suiteharness.sessions.models import SessionIdentity, TranscriptEvent, TranscriptEventType


def identity(**changes):
    values = dict(tenant_id="tenant", product_id="product", agent_id="agent",
                  session_id="session", principal_id="sales")
    values.update(changes)
    return SessionIdentity(**values)


IDENTITY = identity()


def event(sequence, kind, text="", *, scope=IDENTITY):
    if kind == "user_message":
        payload = {"schema": "suiteharness.channel-user-message.v1",
                   "authenticated_principal_id": scope.principal_id,
                   "message": {"text": text}}
    elif kind == "assistant_message":
        payload = {"output": text}
    else:
        payload = {"detail": text}
    return TranscriptEvent(identity=scope, event_id=f"event-{sequence}", sequence=sequence,
                           revision=sequence, type=TranscriptEventType(kind), payload=payload,
                           occurred_at=datetime(2026, 9, 10, tzinfo=UTC))


def turns(count):
    return tuple(item for index in range(count)
                 for item in (event(index * 2 + 1, "user_message", f"question-{index}"),
                              event(index * 2 + 2, "assistant_message", f"answer-{index}")))


def prepare(events, previous=None, **settings):
    from suiteharness.memory.session_memory import MemoryState, SessionConversationMemory
    return asyncio.run(SessionConversationMemory(**settings).prepare(
        IDENTITY, events, previous if previous is not None else MemoryState()))


def test_default_selects_last_twelve_complete_pairs_and_degrades_to_excerpt():
    result = prepare(turns(14))
    assert len(result.context.messages) == 24
    assert result.context.messages[0].content == "question-2"
    assert result.context.truncated
    assert result.state.covered_revision == 4
    assert result.state.degraded
    assert "excerpt" in result.state.summary.lower()
    assert "question-0" in result.state.summary


def test_errors_cancel_and_orphans_never_become_completed_pairs():
    events = (event(1, "assistant_message", "orphan"),
              event(2, "user_message", "failed question"), event(3, "error"),
              event(4, "assistant_message", "partial failure"),
              event(5, "user_message", "cancelled question"), event(6, "cancel"),
              event(7, "assistant_message", "partial cancel"),
              event(8, "user_message", "good"), event(9, "tool_result", "hidden detail"),
              event(10, "assistant_message", "complete"), event(11, "user_message", "pending"))
    result = prepare(events)
    assert [m.content for m in result.context.messages] == ["good", "complete"]
    assert result.state.summary == ""


@pytest.mark.parametrize("dimension", ["tenant_id", "product_id", "agent_id", "session_id", "principal_id"])
def test_rejects_any_event_from_another_identity_even_non_message(dimension):
    foreign = IDENTITY.model_copy(update={dimension: "another"})
    with pytest.raises(ValueError, match="identity"):
        prepare((event(1, "status", scope=foreign),))


def test_shared_user_payload_keeps_authenticated_actor():
    forged = event(1, "user_message", "bad").model_copy(update={"payload": {
        "schema": "suiteharness.channel-user-message.v1", "authenticated_principal_id": "other",
        "message": {"text": "bad"}}})
    result = prepare((forged, event(2, "assistant_message", "answer")))
    assert result.context.messages[0].principal_id == "other"
    assert result.context.messages[0].content == "bad"


def test_shared_session_keeps_authenticated_actor_without_relaxing_identity_scope():
    from suiteharness.memory.session_memory import MemoryState, SessionConversationMemory

    shared = identity(principal_id="shared-session")
    user = event(1, "user_message", "hello", scope=shared).model_copy(update={"payload": {
        "schema": "suiteharness.channel-user-message.v1",
        "authenticated_principal_id": "seller-a",
        "message": {"text": "hello"},
    }})
    result = asyncio.run(SessionConversationMemory().prepare(
        shared,
        (user, event(2, "assistant_message", "answer", scope=shared)),
        MemoryState(),
    ))
    assert result.context.messages[0].principal_id == "seller-a"


def test_malformed_user_cuts_previous_unanswered_user():
    broken = event(2, "user_message").model_copy(update={"payload": {"message": {"text": "bad"}}})
    result = prepare((event(1, "user_message", "old"), broken,
                      event(3, "assistant_message", "unpaired")))
    assert result.context.messages == ()


def test_summary_only_receives_new_removed_visible_turns_and_reentry_is_stable():
    calls = []

    async def summary(previous, messages):
        calls.append((previous, tuple(m.content for m in messages)))
        return previous + "|" + ",".join(str(m.content) for m in messages)

    first = prepare(turns(2), short_term_pairs=1, summary_callback=summary)
    restored = type(first.state).model_validate_json(first.state.model_dump_json())
    repeated = prepare(turns(2), restored, short_term_pairs=1, summary_callback=summary)
    final = prepare(turns(3), repeated.state, short_term_pairs=1, summary_callback=summary)
    assert repeated.state == first.state
    assert calls == [("", ("question-0", "answer-0")),
                     (first.state.summary, ("question-1", "answer-1"))]
    assert final.state.covered_revision == 4
    assert not final.state.degraded


def test_summary_failure_is_bounded_explicit_excerpt():
    async def broken(previous, messages):
        raise RuntimeError("backend unavailable")

    result = prepare(turns(6), short_term_pairs=1, max_summary_chars=200,
                     summary_callback=broken)
    assert result.state.degraded
    assert "excerpt" in result.state.summary.lower()
    assert len(result.state.summary) <= 200
    assert result.state.covered_revision == 10


def test_chinese_byte_budget_keeps_complete_pairs_and_never_splits_json():
    events = (event(1, "user_message", "中" * 1000),
              event(2, "assistant_message", "文" * 1000))
    result = prepare(events, max_short_term_bytes=512, max_summary_chars=180)
    assert result.context.messages == ()
    assert result.context.truncated
    assert len(result.state.summary) <= 180
    assert "\ufffd" not in result.state.summary
    assert result.state.covered_revision == 2


def test_explicit_state_reset_does_not_revive_removed_source():
    from suiteharness.memory.session_memory import MemoryState
    previous = prepare(turns(3), short_term_pairs=1).state
    assert previous.summary
    cleared = prepare((), MemoryState(), short_term_pairs=1)
    assert cleared.context.messages == ()
    assert cleared.state.summary == ""


@pytest.mark.parametrize("settings", [{"short_term_pairs": 0}, {"short_term_pairs": 10001},
                                        {"max_short_term_bytes": 0}, {"max_summary_chars": 0}])
def test_configuration_has_hard_bounds(settings):
    with pytest.raises(ValueError):
        prepare((), **settings)
