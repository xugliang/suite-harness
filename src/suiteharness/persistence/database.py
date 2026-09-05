"""Small SQLite connection and transaction owner for one server process."""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import threading
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path

from .errors import PersistenceClosedError

_COMPONENT = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


class SQLiteDatabase:
    """Own one hardened SQLite connection and serialize its transactions.

    The class targets a single application server. SQLite still arbitrates
    writes correctly when another process opens the same file, while the
    asynchronous lock prevents two coroutines from sharing this connection at
    the same time. Schema statements must be static application-owned SQL.
    """

    def __init__(
        self,
        database: str | Path,
        *,
        busy_timeout_seconds: float = 30.0,
        durable: bool = True,
    ) -> None:
        if busy_timeout_seconds <= 0 or busy_timeout_seconds > 300:
            raise ValueError("busy_timeout_seconds must be in (0, 300]")
        database_text = str(database)
        if database_text == ":memory:":
            path: Path | None = None
        else:
            path = Path(database_text).expanduser()
            if not path.is_absolute():
                raise ValueError("SQLite database path must be absolute")
            path = path.resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            database_text = str(path)

        created = path is not None and not path.exists()
        self._connection = sqlite3.connect(
            database_text,
            timeout=busy_timeout_seconds,
            isolation_level=None,
            check_same_thread=False,
        )
        if created and os.name != "nt":
            path.chmod(0o600)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_seconds * 1000)}")
        if path is not None:
            self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(f"PRAGMA synchronous={'FULL' if durable else 'NORMAL'}")
        self._lock = asyncio.Lock()
        self._schema_lock = threading.RLock()
        self._closed = False
        self._install_metadata_schema()

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the connection for diagnostics; application writes use transaction()."""

        self._require_open()
        return self._connection

    def install_schema(
        self,
        component: str,
        version: int,
        statements: Iterable[str],
    ) -> None:
        """Install one static component schema exactly once.

        This intentionally does not perform implicit upgrades. A newer on-disk
        schema or a requested version change must be handled by an explicit
        migration in a future release rather than guessed at startup.
        """

        if not _COMPONENT.fullmatch(component):
            raise ValueError("invalid persistence schema component")
        if version < 1:
            raise ValueError("schema version must be positive")
        schema_statements = tuple(statements)
        if not schema_statements or any(not item.strip() for item in schema_statements):
            raise ValueError("schema statements must not be empty")
        with self._schema_lock:
            self._require_open()
            try:
                # Recheck only after the file-level write lock is held. Two
                # server processes may initialize the same database together.
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    "SELECT version FROM suiteharness_schema_versions WHERE component=?",
                    (component,),
                ).fetchone()
                if row is not None:
                    installed = int(row["version"])
                    if installed != version:
                        raise RuntimeError(
                            f"unsupported {component} schema version: "
                            f"database={installed}, runtime={version}"
                        )
                    self._connection.commit()
                    return
                for statement in schema_statements:
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO suiteharness_schema_versions(component, version) VALUES (?, ?)",
                    (component, version),
                )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    @asynccontextmanager
    async def transaction(self, *, write: bool = False) -> AsyncIterator[sqlite3.Connection]:
        """Yield one atomic transaction while holding the connection lock.

        Code inside the context should execute SQLite statements only and must
        not await unrelated I/O, because the database lock remains held.
        """

        async with self._lock:
            self._require_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield self._connection
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    async def healthcheck(self) -> None:
        """Verify that the owned connection can execute a read transaction.

        The method deliberately returns no database details.  Callers may use
        success/failure for a readiness gate without exposing paths, SQL text,
        or driver errors through a public health endpoint.
        """

        async with self.transaction() as connection:
            row = connection.execute("SELECT 1").fetchone()
            if row is None or int(row[0]) != 1:
                raise RuntimeError("SQLite runtime healthcheck failed")

    async def __aenter__(self) -> SQLiteDatabase:
        self._require_open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    def _install_metadata_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS suiteharness_schema_versions (
                component TEXT PRIMARY KEY,
                version INTEGER NOT NULL CHECK(version > 0)
            )
            """
        )

    def _require_open(self) -> None:
        if self._closed:
            raise PersistenceClosedError("SQLite persistence database is closed")


__all__ = ["SQLiteDatabase"]
