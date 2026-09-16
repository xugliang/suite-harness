"""Derived archives cannot regress or duplicate entries during terminal recovery."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
from pathlib import Path

import pytest

from suiteharness.memory.markdown_archive import MarkdownArchive
from suiteharness.server.application import ChannelApplication
from suiteharness.sessions import SQLiteSessionStore
from suiteharness.sessions.models import SessionIdentity
from tests.test_run_control_application import _fixture
from tests.test_run_control_recovery_regressions import (
    _ImmediateActivation,
    _InterruptedCheckpointStore,
)


class PausedRecovery(ChannelApplication):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.archive_entered = asyncio.Event()
        self.archive_release = asyncio.Event()

    async def _archive_completed(self, *args, **kwargs):
        self.archive_entered.set()
        await self.archive_release.wait()
        await super()._archive_completed(*args, **kwargs)


def test_old_recovery_cannot_overwrite_a_newer_archive(tmp_path):
    async def check():
        _, grants, scope, original, identity = _fixture()
        recovering_store = _InterruptedCheckpointStore(tmp_path / 'recovery.sqlite')
        other_store = SQLiteSessionStore(tmp_path / 'recovery.sqlite')
        archive = MarkdownArchive(tmp_path / 'archive', long_term_enabled=True)
        activation = _ImmediateActivation()
        recovering = PausedRecovery(activation=activation, sessions=recovering_store,
                                    grants=grants, archive=archive)
        other = ChannelApplication(activation=activation, sessions=other_store,
                                   grants=grants, archive=archive)
        pending = None

        async def collect(app, message):
            return [event async for event in app.handle(message, scope)]

        try:
            with pytest.raises(RuntimeError, match='simulated terminal checkpoint crash'):
                await collect(recovering, original)
            pending = asyncio.create_task(collect(recovering, original))
            await asyncio.wait_for(recovering.archive_entered.wait(), 3)
            newer = original.model_copy(update={
                'message_id': 'newer-archive', 'event_id': 'newer-archive',
                'text': 'newer preserved answer',
            })
            await collect(other, newer)
            before = archive.retrieve(identity, '', archive.max_file_bytes)
            assert 'newer preserved answer' in before
            recovering.archive_release.set()
            await asyncio.wait_for(pending, 3)
            assert archive.retrieve(identity, '', archive.max_file_bytes) == before
        finally:
            recovering.archive_release.set()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await recovering_store.close()
            await other_store.close()
    asyncio.run(check())


def test_rearchiving_the_same_terminal_is_idempotent(tmp_path):
    async def check():
        sessions, grants, _, _, identity = _fixture()
        await sessions.create(identity)
        archive = MarkdownArchive(tmp_path / 'archive', long_term_enabled=True)
        app = ChannelApplication(activation=_ImmediateActivation(), sessions=sessions,
                                 grants=grants, archive=archive)
        await app._archive_completed(identity, 0, 3, 'one question', 'one answer')
        first = archive.retrieve(identity, '', archive.max_file_bytes)
        await app._archive_completed(identity, 0, 3, 'one question', 'one answer')
        assert archive.retrieve(identity, '', archive.max_file_bytes) == first
    asyncio.run(check())


def _identity():
    return SessionIdentity(tenant_id='tenant', product_id='product', agent_id='agent',
                           session_id='session', principal_id='principal')


def _append_in_process(root, marker, attempting, entered, release):
    archive = MarkdownArchive(root, long_term_enabled=True)
    attempting.set()

    def transform(previous):
        entered.set()
        if release is not None and not release.wait(10):
            raise RuntimeError('test did not release the archive update')
        return previous + marker

    archive.update(_identity(), transform)


def test_atomic_update_serializes_two_processes(tmp_path):
    archive = MarkdownArchive(tmp_path / 'archive', long_term_enabled=True)
    archive.store(_identity(), 'initial')
    context = multiprocessing.get_context('spawn')
    attempted_first, entered_first = context.Event(), context.Event()
    attempted_second, entered_second = context.Event(), context.Event()
    release = context.Event()
    first = context.Process(target=_append_in_process,
                            args=(archive.root, '-first', attempted_first, entered_first, release))
    second = context.Process(target=_append_in_process,
                             args=(archive.root, '-second', attempted_second, entered_second, None))
    started = []
    try:
        first.start()
        started.append(first)
        assert entered_first.wait(5)
        second.start()
        started.append(second)
        assert attempted_second.wait(5)
        assert not entered_second.wait(0.2)
        release.set()
        for process in started:
            process.join(5)
            assert process.exitcode == 0
        assert archive.retrieve(_identity()) == 'initial-first-second'
    finally:
        release.set()
        for process in started:
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)


def test_disabled_update_performs_no_filesystem_io(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('disabled update attempted I/O or called its transform')

    archive = MarkdownArchive(tmp_path / 'absent')
    with monkeypatch.context() as isolated:
        for method in ('stat', 'lstat', 'open', 'mkdir', 'resolve', 'unlink'):
            isolated.setattr(Path, method, forbidden)
        assert archive.update(_identity(), forbidden) is False


def test_failed_update_leaves_the_previous_archive_intact(tmp_path):
    archive = MarkdownArchive(tmp_path / 'archive', long_term_enabled=True, max_file_bytes=32)
    archive.store(_identity(), 'previous')
    with pytest.raises(ValueError, match='size|bytes|limit'):
        archive.update(_identity(), lambda previous: 'x' * 33)
    assert archive.retrieve(_identity()) == 'previous'
    assert archive.update(_identity(), lambda previous: None) is False
    assert archive.retrieve(_identity()) == 'previous'


def test_update_rejects_a_hardlinked_lock_file(tmp_path):
    archive = MarkdownArchive(tmp_path / 'archive', long_term_enabled=True)
    archive.store(_identity(), 'previous')
    lock_file = next(archive.root.glob('.*.lock'))
    os.link(lock_file, tmp_path / 'lock-alias')
    with pytest.raises(ValueError, match='link|regular'):
        archive.update(_identity(), lambda previous: 'replacement')
    assert archive.retrieve(_identity()) == 'previous'
