"""Bounded result references, private to one authenticated execution.

The model receives no filesystem path. Re-reading still crosses the Runner's
tool authorization boundary, and another run cannot reuse an old reference.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4


class ToolResultStore(ABC):
    @abstractmethod
    async def put(self, scope, run_id: str, content: str) -> dict: ...

    @abstractmethod
    async def read(self, scope, run_id: str, result_id: str, *, offset: int = 0, limit: int = 2048) -> dict: ...


class SQLiteToolResultStore(ToolResultStore):
    def __init__(self, path: Path, *, max_total_bytes: int = 256 * 1024 * 1024):
        path = Path(path).absolute()
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("tool result database must not be a symlink")
        if max_total_bytes < 1:
            raise ValueError("result quota must be positive")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.maximum = max_total_bytes
        created = not path.exists()
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS tool_results (id TEXT PRIMARY KEY, owner TEXT NOT NULL, content TEXT NOT NULL, size INTEGER NOT NULL, expires REAL NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS tool_results_expiry ON tool_results(expires)")
        if created and os.name != "nt":
            path.chmod(0o600)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute(f"PRAGMA max_page_count={max(64, (self.maximum + 16 * 1024 * 1024) // 4096)}")
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _owner(scope, run_id):
        material = [scope.tenant_id, scope.product_id, scope.principal_id,
                    scope.path.agent_id, scope.path.session_id, run_id]
        return hashlib.sha256(json.dumps(material, ensure_ascii=False).encode()).hexdigest()

    async def put(self, scope, run_id: str, content: str) -> dict:
        return await asyncio.to_thread(self._put, self._owner(scope, run_id), content)

    def _put(self, owner, content):
        size = len(content.encode("utf-8"))
        if size > 1024 * 1024:
            raise ValueError("tool result exceeds storage bound")
        result_id = uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM tool_results WHERE expires < ?", (time.time(),))
            total, count = db.execute("SELECT COALESCE(SUM(size),0), COUNT(*) FROM tool_results").fetchone()
            if total + size > self.maximum or count >= 10000:
                raise ValueError("tool result storage quota exceeded")
            db.execute("INSERT INTO tool_results VALUES(?,?,?,?,?)", (result_id, owner, content, size, time.time() + 86400))
        return {"result_id": result_id, "read_tool": "suiteharness.result.read", "characters": len(content), "bytes": size}

    async def read(self, scope, run_id: str, result_id: str, *, offset: int = 0, limit: int = 2048) -> dict:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative character offset")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 4096:
            raise ValueError("limit must be between 1 and 4096 characters")
        return await asyncio.to_thread(self._read, self._owner(scope, run_id), result_id, offset, limit)

    def _read(self, owner, result_id, offset, limit):
        with self._connect() as db:
            row = db.execute("SELECT substr(content, ?, ?), length(content) FROM tool_results WHERE id=? AND owner=? AND expires>=?",
                             (offset + 1, limit, result_id, owner, time.time())).fetchone()
        if row is None:
            raise LookupError("tool result is unavailable in this execution")
        content, size = row
        return {"content": content, "offset": offset, "next_offset": offset + len(content) if offset + len(content) < size else None}

    async def tool(self, context, arguments):
        return await self.read(context.scope, context.run_id, arguments["result_id"],
                               offset=arguments.get("offset", 0), limit=arguments.get("limit", 2048))
