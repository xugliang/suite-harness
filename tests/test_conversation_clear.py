import asyncio
import sqlite3
from datetime import UTC, datetime
from dataclasses import replace

import pytest

from suiteharness.sessions import InMemorySessionStore, SQLiteSessionStore, TranscriptEventType
from suiteharness.sessions.errors import SessionIdempotencyConflictError
from test_channel_stream import channel_fixture


@pytest.mark.parametrize('persistent', [False, True])
def test_clear_erases_payloads_and_snapshots_but_preserves_replay_fence(tmp_path, persistent):
    async def check():
        app, _, scope, _, identity, _ = channel_fixture(None)
        path = tmp_path / 'sessions.sqlite3'
        store = SQLiteSessionStore(path) if persistent else InMemorySessionStore()
        await store.create(identity)
        other = identity.model_copy(update={'principal_id': 'other'})
        await store.create(other)
        event = await store.append(identity, event_id='old', event_type=TranscriptEventType.USER_MESSAGE,
                                   payload={'text': 'private-body'}, idempotency_key='old-key')
        await store.append(other, event_id='other', event_type=TranscriptEventType.USER_MESSAGE,
                           payload={'text': 'keep-other'})
        await store.save_checkpoint(identity, checkpoint_id='snapshot',
            workflow_state={'summary': 'private-summary', 'runs': ['private-answer']},
            expected_revision=event.revision, idempotency_key='snapshot-key')
        before = (await store.get(identity)).revision
        report = await store.clear_conversation(identity, idempotency_key='clear-1')
        assert report.revision > before
        assert report.replayed is False
        assert all(e.payload is None for e in (await store.recent_transcript(identity, limit=100))
                   if e.revision < report.revision)
        assert (await store.resume(identity)).checkpoint.workflow_state is None
        assert (await store.recent_transcript(other, limit=10))[0].payload['text'] == 'keep-other'
        replay = await store.append(identity, event_id='old', event_type=TranscriptEventType.USER_MESSAGE,
                                   payload={'text': 'private-body'}, idempotency_key='old-key')
        assert replay.revision == event.revision and replay.payload is None
        with pytest.raises(SessionIdempotencyConflictError):
            await store.append(identity, event_id='new', event_type=TranscriptEventType.USER_MESSAGE,
                               payload={'text': 'different'}, idempotency_key='old-key')
        new = await store.append(identity, event_id='new', event_type=TranscriptEventType.USER_MESSAGE,
                                 payload={'text': 'new-body'})
        repeated = await store.clear_conversation(identity, idempotency_key='clear-1')
        assert repeated.replayed is True and repeated.revision == report.revision
        assert (await store.recent_transcript(identity, limit=1))[0].payload == new.payload
        if persistent:
            await store.close()
            reopened = SQLiteSessionStore(path)
            assert (await reopened.resume(identity)).checkpoint.workflow_state is None
            with sqlite3.connect(path) as db:
                bodies = db.execute('SELECT payload_json FROM transcript_events WHERE principal_id=?',
                                    (identity.principal_id,)).fetchall()
                snapshots = db.execute('SELECT workflow_state_json FROM session_checkpoints').fetchall()
            assert 'private-body' not in str(bodies) and 'private-summary' not in str(snapshots)
            await reopened.close()
    asyncio.run(check())


def test_clear_cancels_inflight_and_does_not_save_late_output():
    from suiteharness.execution.events import RunEvent
    class BlockingActivation:
        tenant_id = 'company'
        async def stream(self, request):
            yield RunEvent(run_id=request.run_id, sequence=1, kind='delta', text='private-partial')
            started.set()
            await asyncio.Event().wait()
    async def check():
        nonlocal started
        started = asyncio.Event()
        app, sessions, scope, message, identity, _ = channel_fixture(None)
        app._activation = BlockingActivation()
        async def old_run():
            return [e async for e in app.handle(message, scope)]
        pending = asyncio.create_task(old_run())
        await started.wait()
        clear = message.model_copy(update={'text': '/clear', 'message_id': 'clear-now'})
        events = await asyncio.wait_for(_collect(app.handle(clear, scope)), timeout=2)
        assert events[-1].payload['conversation_cleared'] is True
        await asyncio.gather(pending, return_exceptions=True)
        assert await app.history(scope) == ()
        assert await app.history(scope, include_invalidated=True) == ()
        assert 'private-partial' not in str(await sessions.resume(identity))
    started = None
    asyncio.run(check())


@pytest.mark.parametrize('text', ['请解释 /clear', '/clear other-user', '"/clear"', '/clearance'])
def test_only_standalone_clear_triggers_deletion(text):
    async def check():
        app, sessions, scope, message, identity, _ = channel_fixture(None)
        await sessions.create(identity)
        await sessions.append(identity, event_id='keep', event_type=TranscriptEventType.USER_MESSAGE,
                              payload={'text': 'keep-body'})
        [e async for e in app.handle(message.model_copy(update={'text': text}), scope)]
        assert (await sessions.recent_transcript(identity, limit=10))[0].payload == {'text': 'keep-body'}
    asyncio.run(check())


def test_clear_rejects_shared_scope_not_owned_by_sender():
    async def check():
        app, sessions, scope, _, identity, _ = channel_fixture(None)
        from suiteharness.server.application import ChannelApplicationError
        shared = replace(scope, session_owner_id='other')
        with pytest.raises(ChannelApplicationError):
            await app.clear_conversation(shared, idempotency_key='reject')
        assert await sessions.get(identity) is None
    asyncio.run(check())


def test_crashed_clear_blocks_other_host_until_same_request_resumes(tmp_path):
    async def check():
        app, _, scope, message, identity, calls = channel_fixture(None)
        path = tmp_path / 'shared.sqlite3'
        first = SQLiteSessionStore(path)
        await first.create(identity)
        await first.append(identity, event_id='old', event_type=TranscriptEventType.USER_MESSAGE,
                           payload={'text': 'old-private'})
        await first.clear_conversation(identity, idempotency_key='crashed-clear')
        await first.close()
        second = SQLiteSessionStore(path)
        app._sessions = second
        events = [e async for e in app.handle(message, scope)]
        assert events[-1].payload['failure']['code'] == 'conversation_clear_pending'
        assert calls == []
        result = await app.clear_conversation(scope, idempotency_key='crashed-clear')
        assert result.failed_layers == ()
        assert await second.pending_clear(identity) is False
        assert await app.history(scope) == ()
        await second.close()
    asyncio.run(check())


async def _collect(source):
    return [e async for e in source]


def test_archive_clear_fences_late_writes_and_clears_when_disabled(tmp_path):
    from suiteharness.memory.markdown_archive import MarkdownArchive
    _, _, _, _, identity, _ = channel_fixture(None)
    enabled = MarkdownArchive(tmp_path, long_term_enabled=True)
    enabled.store(identity, '<!-- suiteharness-memory-v1 revision=4 -->\nprivate-archive')
    disabled = MarkdownArchive(tmp_path, long_term_enabled=False)
    disabled.clear(identity, 5)
    assert enabled.retrieve(identity) == ''
    with pytest.raises(ValueError):
        enabled.store(identity, '<!-- suiteharness-memory-v1 revision=4 -->\nlate-private')
    enabled.store(identity, '<!-- suiteharness-memory-v1 revision=6 -->\nnew-memory')
    assert 'new-memory' in enabled.retrieve(identity)


def test_old_pending_clear_retry_cannot_erase_new_archive(tmp_path):
    from suiteharness.memory.markdown_archive import MarkdownArchive
    async def check():
        app, _, scope, _, identity, _ = channel_fixture(None)
        app._sessions = SQLiteSessionStore(tmp_path / 'sessions.sqlite3')
        app._archive = MarkdownArchive(tmp_path / 'archive', long_term_enabled=True)
        await app._sessions.create(identity)
        old = await app._sessions.clear_conversation(identity, idempotency_key='old-crashed')
        newer = await app.clear_conversation(scope, idempotency_key='new-clear')
        fresh = f'<!-- suiteharness-memory-v1 revision={newer.cutoff_revision + 1} -->\nnew-private-memory'
        app._archive.store(identity, fresh)
        await app.clear_conversation(scope, idempotency_key='old-crashed')
        assert app._archive.retrieve(identity) == fresh
        # Defense in depth: even an obsolete cleanup worker cannot unlink a
        # newer archive, independently of the session receipt replay check.
        app._archive.clear(identity, old.revision)
        assert app._archive.retrieve(identity) == fresh
        await app._sessions.close()
    asyncio.run(check())


def test_tool_result_clear_is_scoped_and_rejects_late_old_run_write(tmp_path):
    from suiteharness.execution.results import SQLiteToolResultStore
    async def check():
        _, _, scope, _, _, _ = channel_fixture(None)
        results = SQLiteToolResultStore(tmp_path / 'tools.sqlite3')
        old = await results.put(scope, 'old-run', 'private-tool')
        other_scope = replace(scope, principal_id='other')
        other = await results.put(other_scope, 'old-run', 'keep-other-tool')
        assert await results.clear_runs(scope, ('old-run',)) == 1
        with pytest.raises(LookupError):
            await results.read(scope, 'old-run', old['result_id'])
        with pytest.raises(LookupError):
            await results.put(scope, 'old-run', 'late-private-tool')
        assert (await results.read(other_scope, 'old-run', other['result_id']))['content'] == 'keep-other-tool'
        assert (await results.put(scope, 'new-run', 'new-tool'))['characters'] == 8
    asyncio.run(check())


def test_product_cleanup_hook_is_optional_and_failure_is_reported():
    from suiteharness.memory.cleanup import ConversationCleanup
    class BrokenCleanup(ConversationCleanup):
        async def clear_product_memory(self, context):
            raise OSError('private configuration error must not be exposed')
    async def check():
        app, sessions, scope, _, identity, _ = channel_fixture(None)
        app._cleanup_resolver = lambda scope: BrokenCleanup()
        await sessions.create(identity)
        await sessions.append(identity, event_id='private', event_type=TranscriptEventType.USER_MESSAGE,
                              payload={'text': 'private-body'})
        outcome = await app.clear_conversation(scope, idempotency_key='clear-hook')
        assert outcome.failed_layers == ('product_memory',)
        assert await app.history(scope) == ()
        assert await ConversationCleanup().clear_product_memory(None) == 0
    asyncio.run(check())


def test_clear_command_never_calls_model_and_next_turn_has_no_history(tmp_path):
    from suiteharness.execution.events import RunEvent
    from suiteharness.execution.models import RunResult, RunStatus
    class Activation:
        tenant_id = 'company'
        requests = []
        async def stream(self, request):
            self.requests.append(request)
            now = datetime.now(UTC)
            result = RunResult(run_id=request.run_id, status=RunStatus.SUCCEEDED, output='answer',
                               iterations_used=1, tool_calls_used=0, started_at=now, finished_at=now)
            yield RunEvent(run_id=request.run_id, sequence=1, kind='completed', result=result)
    async def check():
        app, _, scope, message, identity, _ = channel_fixture(None)
        app._sessions = SQLiteSessionStore(tmp_path / 'chat.sqlite3')
        activation = Activation()
        app._activation = activation
        assert [e async for e in app.handle(message, scope)][-1].payload['output'] == 'answer'
        clear = message.model_copy(update={'text': '/clear', 'message_id': 'clear-message'})
        events = [e async for e in app.handle(clear, scope)]
        assert len(activation.requests) == 1
        assert events[-1].payload['conversation_cleared'] is True
        assert await app.history(scope) == ()
        later = message.model_copy(update={'text': 'next', 'message_id': 'next-message'})
        [e async for e in app.handle(later, scope)]
        assert activation.requests[-1].conversation.messages == ()
        assert activation.requests[-1].conversation.working_summary == ''
        # Re-delivery of clear must not clear later text or execute the model.
        [e async for e in app.handle(clear, scope)]
        assert len(await app.history(scope)) == 2
        [e async for e in app.handle(message, scope)]
        assert len(activation.requests) == 2
        await app._sessions.close()
    asyncio.run(check())
