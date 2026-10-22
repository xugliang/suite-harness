"""PostgreSQL audit journal and execution-scoped tool result persistence."""
from __future__ import annotations

from suiteharness.persistence.postgres import PostgresDatabase, PostgresStoreAdapter, _OperationLock
from .protocols import AuditJournal
from .sqlite import SQLAuditOperations
from .results import ToolResultStore, SQLToolResultOperations


class _AuditOperations(SQLAuditOperations):
    def __init__(self, database):
        self._connection = database._connection
        self._lock = _OperationLock()
        self._closed = False


class PostgresAuditJournal(PostgresStoreAdapter, AuditJournal):
    def __init__(self, database: PostgresDatabase):
        database.install_schema("audit", 1, ())
        self._setup(database, _AuditOperations(database))
    async def append(self, event):
        return await self._invoke("append", event)
    async def events(self, **kwargs):
        return await self._invoke("events", write=False, **kwargs)


class _ResultOperations(SQLToolResultOperations):
    """Reuse result slicing/owner hashing with only PostgreSQL transactions."""
    def __init__(self, database, maximum):
        self.maximum = maximum
        self._database = database
    def _connect(self):
        from contextlib import contextmanager
        @contextmanager
        def borrow():
            yield self._database._connection
        return borrow()


class PostgresToolResultStore(PostgresStoreAdapter, ToolResultStore):
    def __init__(self, database: PostgresDatabase, *, max_total_bytes: int = 256 * 1024 * 1024):
        if isinstance(max_total_bytes, bool) or not isinstance(max_total_bytes, int) or max_total_bytes < 1:
            raise ValueError("result quota must be positive")
        database.install_schema("tool_results", 1, ())
        self._setup(database, _ResultOperations(database, max_total_bytes))
    async def put(self, scope, run_id: str, content: str):
        # Invoke the synchronous operation directly. Its owning async method in
        # the file store would schedule another thread and lose the transaction.
        return await self._invoke("_put", self._operations._owner(scope, run_id), content)
    async def read(self, scope, run_id: str, result_id: str, *, offset: int = 0, limit: int = 2048):
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative character offset")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 4096:
            raise ValueError("limit must be between 1 and 4096 characters")
        return await self._invoke("_read", self._operations._owner(scope, run_id), result_id, offset, limit, write=False)
    async def clear_runs(self, scope, run_ids: tuple[str, ...]):
        return await self._invoke("_clear_runs", tuple(self._operations._owner(scope, run) for run in run_ids))
    async def tool(self, context, arguments):
        return await self.read(context.scope, context.run_id, arguments["result_id"],
                               offset=arguments.get("offset", 0), limit=arguments.get("limit", 2048))


__all__ = ["PostgresAuditJournal", "PostgresToolResultStore"]
