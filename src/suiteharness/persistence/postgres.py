"""PostgreSQL persistence with one bounded pool and explicit schema migrations.

The existing SQL store operations are shared deliberately: their revision,
idempotency, clearing and lease rules have one implementation. Their SQL is
executed only on PostgreSQL worker connections, never on SQLite. Constructors
perform no I/O. All blocking pool and driver work runs outside the event loop.
"""
from __future__ import annotations

import asyncio
import contextvars
import re
import sqlite3
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

from suiteharness.channels.feishu.processor import EventDeduplicator
from suiteharness.sandbox.protocols import SandboxQuarantineStore

from .errors import PersistenceClosedError
from .postgres_schema import postgres_migrations, beijing_function_ddl, REQUIRED_TABLES, BEIJING_DERIVED_COLUMNS
from .mcp import SQLMcpHttpResumeOperations
from .state import SQLProductStateOperations
from .events import SQLChannelEventOperations
from .sandbox import SQLSandboxQuarantineOperations

_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_CURRENT_CONNECTION: contextvars.ContextVar[Any] = contextvars.ContextVar("postgres_connection")
_CALLER_TASK: contextvars.ContextVar[Any] = contextvars.ContextVar("postgres_caller_task")
_ADVISORY_LOCK = 785428771234180


def postgres_statement(statement: str) -> str:
    """Translate the small, application-owned SQL dialect, preserving literals.

    This is not a general SQL parser. Store SQL uses single-quoted strings,
    quoted identifiers and qmark parameters; PostgreSQL parameter binding also
    requires escaping literal percent signs. User data is always bound separately.
    """
    # The only expression requiring an engine-specific rewrite in the shared
    # operations is the UTF-8 byte quota on session checkpoint JSON.
    statement = statement.replace("LENGTH(CAST(workflow_state_json AS BLOB))", "OCTET_LENGTH(workflow_state_json)")
    ignore = bool(re.match(r"\s*INSERT\s+OR\s+IGNORE\s+INTO\b", statement, flags=re.I))
    if ignore:
        statement = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", statement, count=1, flags=re.I)
        statement = statement.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    pieces: list[str] = []
    quote = None
    index = 0
    while index < len(statement):
        character = statement[index]
        if character == "%":
            pieces.append("%%")
        elif quote is not None:
            pieces.append(character)
            if character == quote:
                if index + 1 < len(statement) and statement[index + 1] == quote:
                    pieces.append(statement[index + 1])
                    index += 1
                else:
                    quote = None
        elif character in {"'", '"'}:
            quote = character
            pieces.append(character)
        elif character == "?":
            pieces.append("%s")
        else:
            pieces.append(character)
        index += 1
    return "".join(pieces)


class _Row:
    def __init__(self, values, names):
        self._values = tuple(values)
        self._names = tuple(names)
    def __getitem__(self, key):
        return self._values[self._names.index(key)] if isinstance(key, str) else self._values[key]
    def __iter__(self):
        return iter(self._values)


class _Cursor:
    def __init__(self, cursor):
        self._cursor = cursor
    @property
    def rowcount(self):
        return self._cursor.rowcount
    def _row(self, row):
        return None if row is None else _Row(row, [column.name for column in self._cursor.description])
    def fetchone(self):
        return self._row(self._cursor.fetchone())
    def fetchall(self):
        return [self._row(row) for row in self._cursor.fetchall()]
    def __iter__(self):
        for row in self._cursor:
            yield self._row(row)


class PostgresConnection:
    """Internal synchronous SQL facade, available only inside a worker transaction."""
    def execute(self, statement, parameters=()):
        if statement.strip().upper() in {"BEGIN IMMEDIATE", "BEGIN"}:
            return None  # The pool transaction and write lock already surround the entire operation.
        if statement.lstrip().upper().startswith(("CREATE ", "ALTER ", "DROP ", "PRAGMA")):
            raise RuntimeError("runtime PostgreSQL connections do not run DDL")
        connection = _CURRENT_CONNECTION.get(None)
        if connection is None:
            raise RuntimeError("PostgreSQL connection is only available in a worker transaction")
        import psycopg
        try:
            return _Cursor(connection.execute(postgres_statement(statement), parameters))
        except psycopg.IntegrityError as exc:
            # Shared operations translate this DB-API category into their public
            # domain exceptions. No SQLite connection is created or used.
            raise sqlite3.IntegrityError("PostgreSQL integrity constraint failed") from exc
    def commit(self):
        pass  # Pool transaction commits after the complete operation returns.
    def rollback(self):
        pass  # Propagated exceptions roll back the complete pool transaction.
    def close(self):
        pass  # Adapters borrow connections; only PostgresDatabase owns the pool.


class _OperationLock:
    """A borrowed operation already owns its database transaction and write lock."""
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_):
        pass


class PostgresDatabase:
    """Own one bounded psycopg pool. Runtime open validates; migrate is explicit.

    Worker transactions serialize mutations with one database/schema advisory
    lock. This preserves existing whole-store quotas and CAS/clear semantics
    across processes. Reads use REPEATABLE READ snapshots without that lock.
    """
    def __init__(self, dsn: str, *, schema: str = "harness", expected_database: str | None = None, min_size: int = 1,
                 max_size: int = 6, pool_min_size: int | None = None,
                 pool_max_size: int | None = None, connect_timeout_seconds: float = 10,
                 pool_timeout_seconds: float = 10, statement_timeout_ms: int = 30000,
                 lock_timeout_ms: int = 5000, idle_transaction_timeout_ms: int = 15000):
        if not isinstance(dsn, str) or urlsplit(dsn).scheme not in {"postgres", "postgresql"}:
            raise ValueError("a PostgreSQL URL DSN is required")
        if not _SCHEMA_NAME.fullmatch(schema) or schema in {"pg_catalog", "information_schema"}:
            raise ValueError("invalid PostgreSQL schema name")
        min_size = min_size if pool_min_size is None else pool_min_size
        max_size = max_size if pool_max_size is None else pool_max_size
        if not (isinstance(min_size, int) and isinstance(max_size, int) and 0 <= min_size <= max_size <= 64 and max_size > 0):
            raise ValueError("invalid PostgreSQL pool sizes")
        if connect_timeout_seconds <= 0 or pool_timeout_seconds <= 0:
            raise ValueError("PostgreSQL timeouts must be positive")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in (statement_timeout_ms, lock_timeout_ms, idle_transaction_timeout_ms)):
            raise ValueError("PostgreSQL statement timeouts must be positive integers")
        self._dsn, self.schema = dsn, schema
        self._expected_database = expected_database
        self._min_size, self._max_size = min_size, max_size
        self._connect_timeout, self._pool_timeout = connect_timeout_seconds, pool_timeout_seconds
        self._timeouts = (statement_timeout_ms, lock_timeout_ms, idle_transaction_timeout_ms)
        self._pool = None
        self._closed = False
        self._required: dict[str, int] = {"beijing_time": 1}
        self._versions: dict[str, int] = {}
        self._lifecycle = threading.RLock()
        self._idle = threading.Condition(self._lifecycle)
        self._active_workers = 0
        self._connection = PostgresConnection()

    def install_schema(self, component, version, _statements):
        """Register an application schema requirement; never execute constructor DDL."""
        if component not in postgres_migrations() or version != 1:
            raise ValueError("unsupported PostgreSQL schema component")
        self._required[component] = version
        if self._pool is not None and self._versions.get(component) != version:
            raise RuntimeError(f"PostgreSQL schema {component} requires explicit migration")

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("PostgreSQL requires suite-harness[postgres]") from exc
        return psycopg.connect(self._dsn, connect_timeout=max(1, int(self._connect_timeout)))

    async def migrate(self):
        """Install static schemas with a migration credential; never called by open()."""
        await asyncio.to_thread(self._migrate)

    def _migrate(self):
        from psycopg import sql
        with self._connect() as connection:
            self._validate_database(connection)
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK,))
            connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}" ).format(sql.Identifier(self.schema)))
            connection.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(self.schema)))
            connection.execute("CREATE TABLE IF NOT EXISTS suiteharness_schema_versions (component TEXT PRIMARY KEY, version BIGINT NOT NULL CHECK(version > 0))")
            for component, (version, statements) in postgres_migrations().items():
                row = connection.execute("SELECT version FROM suiteharness_schema_versions WHERE component=%s", (component,)).fetchone()
                if row is not None:
                    if row[0] != version:
                        raise RuntimeError(f"unsupported PostgreSQL schema version for {component}")
                    continue
                for statement in statements:
                    connection.execute(statement)
                connection.execute("INSERT INTO suiteharness_schema_versions VALUES (%s,%s)", (component, version))

    async def open(self):
        await asyncio.to_thread(self._open)

    def _open(self):
        from psycopg_pool import ConnectionPool
        from psycopg import sql
        with self._lifecycle:
            if self._closed:
                raise PersistenceClosedError("PostgreSQL database is closed")
            if self._pool is not None:
                return
            def configure(connection):
                connection.execute(sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(self.schema)))
                for setting, value in zip(("statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout"), self._timeouts):
                    connection.execute("SELECT set_config(%s,%s,false)", (setting, str(value)))
                connection.commit()
            pool = ConnectionPool(self._dsn, min_size=self._min_size, max_size=self._max_size,
                                  timeout=self._pool_timeout, kwargs={"connect_timeout": max(1, int(self._connect_timeout))},
                                  configure=configure, open=False, name="suiteharness")
            try:
                pool.open(wait=True, timeout=self._connect_timeout + self._pool_timeout)
                with pool.connection() as connection:
                    self._validate_database(connection)
                    rows = connection.execute("SELECT component, version FROM suiteharness_schema_versions").fetchall()
                    tables = connection.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname=%s", (self.schema,)).fetchall()
                    if set(REQUIRED_TABLES) - {row[0] for row in tables}:
                        raise RuntimeError("PostgreSQL schema has missing tables; explicit migration or repair is required")
                    derived = connection.execute("SELECT table_name,column_name FROM information_schema.columns WHERE table_schema=%s AND is_generated='ALWAYS'", (self.schema,)).fetchall()
                    expected_derived = {(table, target) for table, columns in BEIJING_DERIVED_COLUMNS.items() for target, _, _ in columns}
                    if expected_derived - {(row[0], row[1]) for row in derived}:
                        raise RuntimeError("PostgreSQL schema has missing derived columns; explicit migration is required")
                versions = {row[0]: int(row[1]) for row in rows}
                required = self._required or {name: version for name, (version, _) in postgres_migrations().items()}
                if any(versions.get(component) != version for component, version in required.items()):
                    raise RuntimeError("PostgreSQL schema requires explicit migration")
                self._versions, self._pool = versions, pool
            except BaseException:
                pool.close()
                raise

    def _validate_database(self, connection):
        if self._expected_database is not None:
            actual = connection.execute("SELECT current_database()").fetchone()[0]
            if actual != self._expected_database:
                raise RuntimeError("PostgreSQL connection targeted an unexpected database")

    async def run(self, operation: Callable[[PostgresConnection], Any], *, write: bool = True):
        with self._lifecycle:
            if self._closed:
                raise PersistenceClosedError("PostgreSQL database is closed")
            if self._pool is None:
                raise RuntimeError("PostgreSQL database must be opened and schema migrated before use")
            self._active_workers += 1
        future = asyncio.create_task(asyncio.to_thread(self._run_sync, operation, write))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # Do not abandon a write whose result may have committed. Wait for
            # atomic completion; the caller still observes cancellation.
            try:
                await future
            except Exception:
                pass
            raise

    def _run_sync(self, operation, write):
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    if write:
                        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(current_database() || %s, %s))", (self.schema, _ADVISORY_LOCK))
                    else:
                        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    token = _CURRENT_CONNECTION.set(connection)
                    try:
                        return operation(self._connection)
                    finally:
                        _CURRENT_CONNECTION.reset(token)
        finally:
            with self._idle:
                self._active_workers -= 1
                self._idle.notify_all()

    @asynccontextmanager
    async def transaction(self, *, write=False):
        """Internal shared-operation context, valid inside run() worker only."""
        if _CURRENT_CONNECTION.get(None) is None:
            raise RuntimeError("use await database.run(callback) for PostgreSQL transactions")
        yield self._connection

    async def healthcheck(self):
        result = await self.run(lambda connection: connection.execute("SELECT 1").fetchone()[0], write=False)
        if result != 1:
            raise RuntimeError("PostgreSQL healthcheck failed")

    async def close(self):
        await asyncio.to_thread(self._close)

    def _close(self):
        with self._lifecycle:
            if self._closed and self._pool is None:
                return
            self._closed = True
            while self._active_workers:
                self._idle.wait()
            if self._pool is not None:
                self._pool.close()
                self._pool = None

    async def __aenter__(self):
        await self.open()
        return self
    async def __aexit__(self, *_):
        await self.close()


class PostgresStoreAdapter:
    """Delegate shared store rules to a worker while borrowing a pooled transaction."""
    def _setup(self, database, operations):
        if not isinstance(database, PostgresDatabase):
            raise TypeError("database must be PostgresDatabase")
        self._database, self._operations, self._closed = database, operations, False
    async def _invoke(self, method, *args, write=True, **kwargs):
        if self._closed:
            raise PersistenceClosedError("PostgreSQL store is closed")
        caller = asyncio.current_task()
        def invoke(_connection):
            token = _CALLER_TASK.set(caller)
            try:
                result = getattr(self._operations, method)(*args, **kwargs)
                return asyncio.run(result) if hasattr(result, "__await__") else result
            finally:
                _CALLER_TASK.reset(token)
        return await self._database.run(invoke, write=write)
    async def close(self):
        self._closed = True
    async def healthcheck(self):
        if self._closed:
            raise PersistenceClosedError("PostgreSQL store is closed")
        await self._database.healthcheck()


class PostgresProductStateStore(PostgresStoreAdapter):
    def __init__(self, database: PostgresDatabase, **kwargs):
        self._setup(database, SQLProductStateOperations(database, **kwargs))
    async def get(self, state_key):
        return await self._invoke("get", state_key, write=False)
    async def compare_and_set(self, state_key, value, **kwargs):
        return await self._invoke("compare_and_set", state_key, value, **kwargs)
    async def list_namespace(self, **kwargs):
        return await self._invoke("list_namespace", write=False, **kwargs)


class _EventOperations(SQLChannelEventOperations):
    @staticmethod
    def _current_task():
        task = _CALLER_TASK.get(None)
        if task is None:
            raise RuntimeError("event operations require an asyncio caller task")
        return task


class PostgresChannelEventDeduplicator(PostgresStoreAdapter, EventDeduplicator):
    def __init__(self, database: PostgresDatabase, **kwargs):
        self._setup(database, _EventOperations(database, **kwargs))
    @property
    def processing_lease_seconds(self):
        return self._operations.processing_lease_seconds
    async def claim(self, event_id):
        return await self._invoke("claim", event_id)
    async def complete(self, event_id):
        return await self._invoke("complete", event_id)
    async def release(self, event_id):
        return await self._invoke("release", event_id)
    async def purge_expired(self):
        return await self._invoke("purge_expired")


class PostgresMcpHttpResumeStore(SQLMcpHttpResumeOperations):
    """MCP protocol operations use the PostgreSQL product state adapter."""
    def __init__(self, state_store: PostgresProductStateStore):
        if not isinstance(state_store, PostgresProductStateStore):
            raise TypeError("MCP PostgreSQL resume requires PostgresProductStateStore")
        super().__init__(state_store)


class PostgresSandboxQuarantineStore(PostgresStoreAdapter, SandboxQuarantineStore):
    def __init__(self, database: PostgresDatabase, **kwargs):
        self._setup(database, SQLSandboxQuarantineOperations(database, **kwargs))
    async def load(self):
        return await self._invoke("load", write=False)
    async def mark(self, container_name, reason):
        return await self._invoke("mark", container_name, reason)
    async def claim(self, container_name, reason, *, owner_token):
        return await self._invoke("claim", container_name, reason, owner_token=owner_token)
    async def clear(self, container_name, *, owner_token=None):
        return await self._invoke("clear", container_name, owner_token=owner_token)


__all__ = ["PostgresDatabase", "PostgresConnection", "PostgresStoreAdapter", "PostgresProductStateStore",
           "PostgresChannelEventDeduplicator", "PostgresMcpHttpResumeStore", "PostgresSandboxQuarantineStore",
           "postgres_statement", "postgres_migrations", "beijing_function_ddl"]
