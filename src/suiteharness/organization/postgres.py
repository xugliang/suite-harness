"""Current directory and synchronization leases in a dedicated logical database.

Runtime credentials never install DDL. External I/O happens outside transactions.
No directory row contains a tenant selector or product-specific business roles.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict

from .models import DirectorySnapshot

DDL = """
CREATE SCHEMA IF NOT EXISTS directory;
CREATE TABLE IF NOT EXISTS directory.schema_version (
 component TEXT PRIMARY KEY, version INTEGER NOT NULL
);
INSERT INTO directory.schema_version VALUES ('organization',1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS directory.sync_state (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 config_fingerprint TEXT NOT NULL DEFAULT '', lease_config_fingerprint TEXT NOT NULL DEFAULT '',
 active_config_fingerprint TEXT NOT NULL DEFAULT '',
 scope_fingerprint TEXT NOT NULL DEFAULT '', scope_keys JSONB NOT NULL DEFAULT '[]',
 last_success_at TIMESTAMPTZ, last_attempt_at TIMESTAMPTZ, retry_after TIMESTAMPTZ,
 lease_owner TEXT, lease_until TIMESTAMPTZ, fence BIGINT NOT NULL DEFAULT 0,
 restricted BOOLEAN NOT NULL DEFAULT FALSE, error_code TEXT,
 revision BIGINT NOT NULL DEFAULT 0, result_counts JSONB NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS directory.employees (
 employee_id UUID PRIMARY KEY, source_id TEXT NOT NULL UNIQUE,
 display_name TEXT NOT NULL, job_title TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL CHECK(status IN ('active','inactive','unknown')),
 manager_source_id TEXT, present BOOLEAN NOT NULL DEFAULT TRUE,
 content_hash TEXT NOT NULL, source_updated_at TIMESTAMPTZ NOT NULL,
 revision BIGINT NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS directory.departments (
 source_id TEXT PRIMARY KEY, name TEXT NOT NULL, parent_id TEXT, leader_id TEXT
);
CREATE TABLE IF NOT EXISTS directory.employee_departments (
 employee_id UUID NOT NULL REFERENCES directory.employees(employee_id),
 department_id TEXT NOT NULL, position INTEGER NOT NULL,
 PRIMARY KEY(employee_id,department_id)
);
CREATE TABLE IF NOT EXISTS directory.external_identities (
 kind TEXT NOT NULL, value TEXT NOT NULL, app_id TEXT NOT NULL DEFAULT '',
 employee_id UUID NOT NULL REFERENCES directory.employees(employee_id),
 PRIMARY KEY(kind,value,app_id)
);
CREATE TABLE IF NOT EXISTS directory.principal_bindings (
 product_id TEXT NOT NULL, principal_id TEXT NOT NULL,
 employee_id UUID NOT NULL REFERENCES directory.employees(employee_id),
 PRIMARY KEY(product_id,principal_id)
);
CREATE TABLE IF NOT EXISTS directory.consumers (
 product_id TEXT PRIMARY KEY, expires_at TIMESTAMPTZ NOT NULL
);
"""

_TABLES = frozenset({
    'schema_version', 'sync_state', 'employees', 'departments',
    'employee_departments', 'external_identities', 'principal_bindings', 'consumers',
})
_COLUMNS = {
    'schema_version': {'component', 'version'},
    'sync_state': {'singleton', 'config_fingerprint', 'lease_config_fingerprint', 'active_config_fingerprint',
                   'scope_fingerprint', 'scope_keys', 'last_success_at', 'last_attempt_at', 'retry_after',
                   'lease_owner', 'lease_until', 'fence', 'restricted', 'error_code', 'revision', 'result_counts'},
    'employees': {'employee_id', 'source_id', 'display_name', 'job_title', 'status', 'manager_source_id', 'present', 'content_hash', 'source_updated_at', 'revision'},
    'departments': {'source_id', 'name', 'parent_id', 'leader_id'},
    'employee_departments': {'employee_id', 'department_id', 'position'},
    'external_identities': {'kind', 'value', 'app_id', 'employee_id'},
    'principal_bindings': {'product_id', 'principal_id', 'employee_id'},
    'consumers': {'product_id', 'expires_at'},
}


def _identifier(value, label='identifier'):
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or '\x00' in value:
        raise ValueError(f'invalid {label}')
    return value


def _duration(value, *, zero=False):
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < (0 if zero else 1):
        raise ValueError('invalid duration')
    return value


class PostgresDirectoryStore:
    """Own one bounded pool. Construction is lazy; open only validates schema."""

    def __init__(self, dsn: str, *, expected_database: str = 'organization_directory', pool_max_size: int = 4):
        if not isinstance(dsn, str) or not dsn:
            raise ValueError('directory database credentials required')
        self._dsn = dsn
        self.expected_database = _identifier(expected_database)
        if type(pool_max_size) is not int or not 1 <= pool_max_size <= 16:
            raise ValueError('invalid pool bound')
        self._maximum = pool_max_size
        self._pool = None
        self._guard = threading.RLock()

    def __repr__(self):
        return 'PostgresDirectoryStore(<redacted>)'

    def migrate(self):
        """Explicit administrative operation, never called from open/acquire."""
        import psycopg
        with psycopg.connect(self._dsn, connect_timeout=10) as conn:
            self._check_database(conn)
            conn.execute('SELECT pg_advisory_xact_lock(43899547183311)')
            conn.execute(DDL)
            version = conn.execute("SELECT version FROM directory.schema_version WHERE component='organization'").fetchone()
            if version != (1,):
                raise ValueError('unsupported directory schema version')

    def _check_database(self, connection):
        row = connection.execute('SELECT current_database()').fetchone()
        current = row['current_database'] if isinstance(row, dict) else row[0]
        if current != self.expected_database:
            raise ValueError('unexpected directory database')

    def open(self):
        with self._guard:
            if self._pool is not None:
                return
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
            pool = ConnectionPool(self._dsn, min_size=0, max_size=self._maximum, timeout=10, open=False,
                                  kwargs={'row_factory': dict_row, 'connect_timeout': 10,
                                          'options': '-c statement_timeout=15000 -c lock_timeout=5000 -c idle_in_transaction_session_timeout=15000',
                                          'application_name': 'suite-organization-directory'})
            try:
                pool.open(wait=True)
                with pool.connection() as conn:
                    self._check_database(conn)
                    row = conn.execute("SELECT version FROM directory.schema_version WHERE component='organization'").fetchone()
                    if row is None or row['version'] != 1:
                        raise ValueError('directory migration required')
                    tables = conn.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='directory'").fetchall()
                    if not _TABLES.issubset({r['tablename'] for r in tables}):
                        raise ValueError('directory tables missing; explicit migration required')
                    columns = conn.execute("SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='directory'").fetchall()
                    for table, required in _COLUMNS.items():
                        if not required.issubset({r['column_name'] for r in columns if r['table_name'] == table}):
                            raise ValueError('directory columns missing; explicit migration required')
                        needed = ('SELECT',) if table == 'schema_version' else ('SELECT','INSERT','UPDATE','DELETE')
                        for privilege in needed:
                            if not conn.execute('SELECT has_table_privilege(current_user,%s,%s) AS allowed', ('directory.'+table, privilege)).fetchone()['allowed']:
                                raise ValueError('directory runtime privileges missing')
                self._pool = pool
            except BaseException:
                pool.close()
                raise

    @contextmanager
    def connection(self):
        self.open()
        with self._pool.connection() as conn:
            yield conn

    def close(self):
        with self._guard:
            if self._pool is not None:
                self._pool.close()
                self._pool = None

    def acquire(self, product_id: str, *, ttl_seconds: int = 600):
        _identifier(product_id, 'product')
        _duration(ttl_seconds)
        with self.connection() as conn:
            conn.execute("INSERT INTO directory.consumers VALUES (%s,clock_timestamp()+%s*interval '1 second') ON CONFLICT(product_id) DO UPDATE SET expires_at=excluded.expires_at", (product_id, ttl_seconds))

    def release(self, product_id: str):
        with self.connection() as conn:
            conn.execute('DELETE FROM directory.consumers WHERE product_id=%s', (_identifier(product_id),))

    def has_consumers(self):
        with self.connection() as conn:
            conn.execute('DELETE FROM directory.consumers WHERE expires_at<=clock_timestamp()')
            return conn.execute('SELECT EXISTS(SELECT 1 FROM directory.consumers)').fetchone()['exists']

    def claim(self, config_fingerprint: str, *, owner: str, fresh_seconds: float = 86400, lease_seconds: int = 180):
        _identifier(config_fingerprint)
        _identifier(owner)
        _duration(fresh_seconds, zero=True)
        _duration(lease_seconds)
        with self.connection() as conn:
            conn.execute('INSERT INTO directory.sync_state(singleton) VALUES(1) ON CONFLICT DO NOTHING')
            row = conn.execute('SELECT *,clock_timestamp() AS now FROM directory.sync_state WHERE singleton=1 FOR UPDATE').fetchone()
            now = row['now']
            if row['active_config_fingerprint'] and row['active_config_fingerprint'] != config_fingerprint:
                return {'status': 'config_mismatch'}
            if not row['active_config_fingerprint']:
                conn.execute('UPDATE directory.sync_state SET active_config_fingerprint=%s WHERE singleton=1', (config_fingerprint,))
            if (row['last_success_at'] is not None and not row['restricted'] and row['config_fingerprint'] == config_fingerprint
                    and (now-row['last_success_at']).total_seconds() < fresh_seconds):
                return {'status': 'fresh'}
            if row['lease_until'] is not None and row['lease_until'] > now:
                return {'status': 'busy'}
            if row['retry_after'] is not None and row['retry_after'] > now:
                return {'status': 'backoff'}
            token = row['fence'] + 1
            conn.execute("UPDATE directory.sync_state SET lease_config_fingerprint=%s,lease_owner=%s,lease_until=clock_timestamp()+%s*interval '1 second',fence=%s,last_attempt_at=clock_timestamp(),error_code=NULL WHERE singleton=1", (config_fingerprint, owner, lease_seconds, token))
            return {'status': 'claimed', 'token': token}

    def activate_config(self, fingerprint, *, expected_fingerprint):
        """Administrative CAS: a superseded process cannot reactivate its config."""
        _identifier(fingerprint)
        with self.connection() as conn:
            row = conn.execute('SELECT active_config_fingerprint FROM directory.sync_state WHERE singleton=1 FOR UPDATE').fetchone()
            if row is None or row['active_config_fingerprint'] != expected_fingerprint:
                raise ValueError('directory configuration changed; inspect current configuration')
            conn.execute('UPDATE directory.sync_state SET active_config_fingerprint=%s,lease_owner=NULL,lease_until=NULL,fence=fence+1,retry_after=NULL WHERE singleton=1', (fingerprint,))

    def renew(self, *, owner, token, lease_seconds=180):
        with self.connection() as conn:
            return conn.execute("UPDATE directory.sync_state SET lease_until=clock_timestamp()+%s*interval '1 second' WHERE singleton=1 AND lease_owner=%s AND fence=%s AND lease_until>clock_timestamp()", (lease_seconds, owner, token)).rowcount == 1

    def fail(self, *, owner, token, code, retry_seconds=300, restricted=False):
        _duration(retry_seconds, zero=True)
        _identifier(code, 'error code')
        with self.connection() as conn:
            conn.execute("UPDATE directory.sync_state SET error_code=%s,retry_after=clock_timestamp()+%s*interval '1 second',restricted=restricted OR %s,lease_owner=NULL,lease_until=NULL WHERE singleton=1 AND lease_owner=%s AND fence=%s", (code, retry_seconds, bool(restricted), owner, token))

    def publish(self, snapshot: DirectorySnapshot, *, owner, token, config_fingerprint):
        if not isinstance(snapshot, DirectorySnapshot) or not snapshot.complete:
            raise ValueError('incomplete directory snapshot')
        from psycopg.types.json import Jsonb
        with self.connection() as conn:
            state = conn.execute('SELECT *,clock_timestamp() AS now FROM directory.sync_state WHERE singleton=1 FOR UPDATE').fetchone()
            if (state is None or state['lease_owner'] != owner or state['fence'] != token
                    or state['lease_until'] is None or state['lease_until'] <= state['now']
                    or state['lease_config_fingerprint'] != config_fingerprint
                    or state['active_config_fingerprint'] != config_fingerprint):
                raise ValueError('directory lease lost')
            existing = conn.execute('SELECT * FROM directory.employees').fetchall()
            if existing and not snapshot.employees:
                raise ValueError('unexpected empty directory')
            if state['scope_fingerprint'] and state['scope_fingerprint'] != snapshot.scope_fingerprint:
                old_keys, new_keys = set(state['scope_keys']), set(snapshot.scope_keys)
                if not old_keys or not old_keys.issubset(new_keys):
                    raise ValueError('directory scope changed')
            previous = {r['source_id']: r for r in existing}
            names = {d.source_id: d.name for d in snapshot.departments}
            counts = {'added': 0, 'changed': 0, 'missing': 0, 'total': len(snapshot.employees)}
            live = set()
            for employee in snapshot.employees:
                canonical = {'employee': asdict(employee), 'departments': [names.get(i, '') for i in employee.department_ids]}
                digest = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                before = previous.get(employee.source_id)
                matched = set()
                for identity in employee.identities:
                    match = conn.execute('SELECT employee_id FROM directory.external_identities WHERE kind=%s AND value=%s AND app_id=%s', (identity.kind, identity.value, identity.app_id)).fetchone()
                    if match:
                        matched.add(match['employee_id'])
                if len(matched) > 1 or (before is not None and matched and matched != {before['employee_id']}):
                    raise ValueError('external identity conflict')
                if before is None and matched:
                    before = conn.execute('SELECT * FROM directory.employees WHERE employee_id=%s', (next(iter(matched)),)).fetchone()
                if before is not None:
                    stable = conn.execute("SELECT kind,value,app_id FROM directory.external_identities WHERE employee_id=%s AND kind IN ('open_id','union_id')", (before['employee_id'],)).fetchall()
                    incoming = {(identity.kind, identity.app_id): identity.value for identity in employee.identities if identity.kind in {'open_id', 'union_id'}}
                    for known in stable:
                        key = (known['kind'], known['app_id'])
                        if key in incoming and incoming[key] != known['value']:
                            raise ValueError('stable external identity changed')
                    if stable and not incoming:
                        raise ValueError('stable external identity missing')
                    if before['source_id'] != employee.source_id:
                        conn.execute('UPDATE directory.employees SET source_id=%s WHERE employee_id=%s', (employee.source_id, before['employee_id']))
                employee_id = before['employee_id'] if before else uuid.uuid4()
                if employee_id in live:
                    raise ValueError('duplicate employee identity')
                live.add(employee_id)
                changed = before is None or before['content_hash'] != digest or not before['present']
                if changed:
                    counts['added' if before is None else 'changed'] += 1
                    conn.execute("""INSERT INTO directory.employees(employee_id,source_id,display_name,job_title,status,manager_source_id,present,content_hash,source_updated_at)
                        VALUES(%s,%s,%s,%s,%s,%s,TRUE,%s,clock_timestamp()) ON CONFLICT(source_id) DO UPDATE SET display_name=excluded.display_name,job_title=excluded.job_title,status=excluded.status,manager_source_id=excluded.manager_source_id,present=TRUE,content_hash=excluded.content_hash,source_updated_at=excluded.source_updated_at,revision=directory.employees.revision+1""",
                                 (employee_id, employee.source_id, employee.display_name, employee.job_title, employee.status, employee.manager_id, digest))
                    conn.execute('DELETE FROM directory.employee_departments WHERE employee_id=%s', (employee_id,))
                    for position, department_id in enumerate(employee.department_ids):
                        conn.execute('INSERT INTO directory.employee_departments VALUES(%s,%s,%s)', (employee_id, department_id, position))
                for identity in employee.identities:
                    identity_row = conn.execute('SELECT employee_id FROM directory.external_identities WHERE kind=%s AND value=%s AND app_id=%s', (identity.kind, identity.value, identity.app_id)).fetchone()
                    if identity_row and identity_row['employee_id'] != employee_id:
                        raise ValueError('external identity conflict')
                    conn.execute('INSERT INTO directory.external_identities VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING', (identity.kind, identity.value, identity.app_id, employee_id))
            # Absence means currently not visible, not a claim of resignation.
            # Keep only the minimum identity necessary for existing business links.
            for row in previous.values():
                if row['employee_id'] not in live and row['present']:
                    counts['missing'] += 1
                    conn.execute("UPDATE directory.employees SET display_name='',job_title='',manager_source_id=NULL,status='unknown',present=FALSE,content_hash='',source_updated_at=clock_timestamp(),revision=revision+1 WHERE employee_id=%s", (row['employee_id'],))
                    conn.execute('DELETE FROM directory.employee_departments WHERE employee_id=%s', (row['employee_id'],))
            conn.execute('DELETE FROM directory.departments')
            for department in snapshot.departments:
                conn.execute('INSERT INTO directory.departments VALUES(%s,%s,%s,%s)', (department.source_id, department.name, department.parent_id, department.leader_id))
            conn.execute("UPDATE directory.sync_state SET last_success_at=clock_timestamp(),config_fingerprint=%s,scope_fingerprint=%s,scope_keys=%s,lease_owner=NULL,lease_until=NULL,retry_after=NULL,error_code=NULL,restricted=FALSE,revision=revision+1,result_counts=%s WHERE singleton=1", (config_fingerprint, snapshot.scope_fingerprint, Jsonb(list(snapshot.scope_keys)), Jsonb(counts)))
            return counts

    def status(self):
        with self.connection() as conn:
            row = conn.execute('SELECT last_success_at,last_attempt_at,retry_after,lease_until,error_code,restricted,revision,result_counts,config_fingerprint,active_config_fingerprint FROM directory.sync_state WHERE singleton=1').fetchone()
        if row is None:
            return {'last_success_at': None, 'restricted': False, 'revision': 0}
        return {key: value.isoformat() if hasattr(value, 'isoformat') else value for key, value in row.items()}

    @staticmethod
    def _available(conn):
        state = conn.execute('SELECT last_success_at,restricted FROM directory.sync_state WHERE singleton=1').fetchone()
        return state if state and state['last_success_at'] and not state['restricted'] else None

    def _record(self, conn, row, state):
        memberships = conn.execute('SELECT m.department_id,d.name FROM directory.employee_departments m LEFT JOIN directory.departments d ON d.source_id=m.department_id WHERE m.employee_id=%s ORDER BY m.position', (row['employee_id'],)).fetchall()
        return {'employee_id': str(row['employee_id']), 'source_id': row['source_id'],
                'display_name': row['display_name'], 'job_title': row['job_title'], 'status': row['status'],
                'department_ids': [r['department_id'] for r in memberships],
                'department_names': [r['name'] for r in memberships if r['name']],
                'updated_at': state['last_success_at'].isoformat(),
                'source_updated_at': row['source_updated_at'].isoformat(), 'revision': row['revision']}

    def list_employees(self, *, query=None, limit=100, offset=0):
        if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or not 0 <= offset <= 100000:
            raise ValueError('invalid directory pagination')
        if query is not None and (not isinstance(query, str) or len(query) > 256):
            raise ValueError('invalid directory query')
        with self.connection() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            state = self._available(conn)
            if state is None:
                return []
            if query:
                rows = conn.execute('SELECT * FROM directory.employees WHERE present AND display_name=%s ORDER BY source_id LIMIT %s OFFSET %s', (query, limit, offset)).fetchall()
            else:
                rows = conn.execute('SELECT * FROM directory.employees WHERE present ORDER BY source_id LIMIT %s OFFSET %s', (limit, offset)).fetchall()
            return [self._record(conn, row, state) for row in rows]

    def list_departments(self):
        with self.connection() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            if not self._available(conn):
                return []
            return conn.execute('SELECT source_id,name,parent_id,leader_id FROM directory.departments ORDER BY source_id').fetchall()

    def get_for_principal(self, product_id, principal_id):
        _identifier(product_id)
        _identifier(principal_id)
        with self.connection() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            state = self._available(conn)
            if state is None:
                return None
            row = conn.execute('SELECT e.* FROM directory.principal_bindings b JOIN directory.employees e USING(employee_id) WHERE b.product_id=%s AND b.principal_id=%s AND e.present', (product_id, principal_id)).fetchone()
            return self._record(conn, row, state) if row else None

    def bind(self, product_id, principal_id, *, kind, value, app_id=''):
        """Administrative identity bridge, intentionally not an HTTP/LLM tool."""
        for item in (product_id, principal_id, kind, value):
            _identifier(item)
        with self.connection() as conn:
            row = conn.execute('SELECT i.employee_id FROM directory.external_identities i JOIN directory.employees e USING(employee_id) WHERE i.kind=%s AND i.value=%s AND i.app_id=%s AND e.present', (kind, value, app_id)).fetchone()
            if row is None:
                raise ValueError('verified identity is unavailable')
            existing = conn.execute('SELECT employee_id FROM directory.principal_bindings WHERE product_id=%s AND principal_id=%s FOR UPDATE', (product_id, principal_id)).fetchone()
            if existing and existing['employee_id'] != row['employee_id']:
                raise ValueError('principal identity conflict')
            conn.execute('INSERT INTO directory.principal_bindings VALUES(%s,%s,%s) ON CONFLICT DO NOTHING', (product_id, principal_id, row['employee_id']))
