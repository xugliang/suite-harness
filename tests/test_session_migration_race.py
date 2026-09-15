"""Exercise additive SQLite upgrades using real files and separate processes."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import textwrap

import pytest

from suiteharness.sessions import SQLiteSessionStore


def legacy_database(path):
    with sqlite3.connect(path) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('''CREATE TABLE sessions (
            tenant_id TEXT NOT NULL, product_id TEXT NOT NULL,
            agent_id TEXT NOT NULL, session_id TEXT NOT NULL, principal_id TEXT NOT NULL,
            status TEXT NOT NULL, revision INTEGER NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, product_id, agent_id, session_id, principal_id))''')
        connection.execute('''INSERT INTO sessions VALUES
            ('company','product','agent','session','owner','active',0,
             '2026-09-14T00:00:00+00:00','2026-09-14T00:00:00+00:00')''')


_WORKER = textwrap.dedent('''
    import asyncio
    import os
    from pathlib import Path
    import sqlite3
    import sys
    import time
    from suiteharness.sessions import SQLiteSessionStore, SessionIdentity

    original_connect = sqlite3.connect
    rendezvous = Path(sys.argv[2])

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            cursor = super().execute(sql, *args, **kwargs)
            if sql == 'PRAGMA table_info(sessions)':
                # Widen only the vulnerable read/ALTER window. The baseline
                # allows both processes to observe missing columns. A guarded
                # migration holds the write lock, so its peer cannot reach
                # this point until commit and the bounded wait expires.
                rows = cursor.fetchall()
                if 'memory_invalidated_revision' not in {row['name'] for row in rows}:
                    (rendezvous / ('observed-' + str(os.getpid()))).touch()
                    until = time.monotonic() + 3
                    while len(list(rendezvous.glob('observed-*'))) < 2 and time.monotonic() < until:
                        time.sleep(0.01)
                return rows
            return cursor

    def connect(*args, **kwargs):
        return original_connect(*args, factory=ObservedConnection, **kwargs)

    sqlite3.connect = connect
    async def run():
        store = SQLiteSessionStore(Path(sys.argv[1]))
        try:
            record = await store.get(SessionIdentity(tenant_id='company', product_id='product',
                agent_id='agent', session_id='session', principal_id='owner'))
            assert record is not None and record.revision == 0
            assert record.memory_invalidated_revision == 0 and record.memory_source_versions == {}
        finally:
            await store.close()
    asyncio.run(run())
''')


def test_two_processes_upgrade_the_same_legacy_database(tmp_path):
    database = tmp_path / 'legacy.sqlite'
    legacy_database(database)
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
    workers = [subprocess.Popen([sys.executable, '-c', _WORKER, str(database), str(tmp_path)],
        env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    try:
        outcomes = [(worker.communicate(timeout=20), worker.returncode) for worker in workers]
        assert all(code == 0 for _, code in outcomes), outcomes
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.communicate(timeout=5)
    with sqlite3.connect(database) as connection:
        names = [row[1] for row in connection.execute('PRAGMA table_info(sessions)')]
        assert names.count('memory_invalidated_revision') == 1
        assert names.count('memory_source_versions') == 1
        assert connection.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 1


def test_failed_second_column_rolls_back_entire_upgrade(tmp_path, monkeypatch):
    database = tmp_path / 'rollback.sqlite'
    legacy_database(database)
    original_connect = sqlite3.connect
    opened = []

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.startswith('ALTER TABLE sessions ADD COLUMN memory_source_versions'):
                raise sqlite3.OperationalError('injected second-column failure')
            return super().execute(sql, *args, **kwargs)

    def connect(*args, **kwargs):
        connection = original_connect(*args, factory=FailingConnection, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, 'connect', connect)
    try:
        with pytest.raises(sqlite3.OperationalError, match='injected second-column failure'):
            SQLiteSessionStore(database)
        assert not opened[0].in_transaction
        with original_connect(database) as connection:
            names = {row[1] for row in connection.execute('PRAGMA table_info(sessions)')}
            assert 'memory_invalidated_revision' not in names
            assert 'memory_source_versions' not in names
    finally:
        for connection in opened:
            connection.close()
