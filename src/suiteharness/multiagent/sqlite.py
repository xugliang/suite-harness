"""SQLite child-task store; use a dedicated task database, not the session DB."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from suiteharness.persistence.database import SQLiteDatabase
from suiteharness.runtime.scopes import RequestScope

from .tasks import (
    TERMINAL_TASK_STATUSES,
    AgentTaskCancelledError,
    AgentTaskIdempotencyConflictError,
    AgentTaskRecord,
    AgentTaskStatus,
    AgentTaskStore,
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_TRANSITIONS = {
    AgentTaskStatus.QUEUED: frozenset(
        {
            AgentTaskStatus.RUNNING,
            AgentTaskStatus.FAILED,
            AgentTaskStatus.CANCELLED,
            AgentTaskStatus.TIMED_OUT,
        }
    ),
    AgentTaskStatus.RUNNING: TERMINAL_TASK_STATUSES,
}
_SCHEMA = (
    """CREATE TABLE agent_tasks (
        task_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL, product_id TEXT NOT NULL,
        principal_id TEXT NOT NULL, parent_run_id TEXT NOT NULL,
        call_id TEXT NOT NULL, role_id TEXT NOT NULL, task_digest TEXT NOT NULL,
        child_run_id TEXT NOT NULL UNIQUE, child_agent_id TEXT NOT NULL,
        child_session_id TEXT NOT NULL, status TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        started_at TEXT, finished_at TEXT, result TEXT, failure TEXT,
        UNIQUE (tenant_id, product_id, principal_id, parent_run_id, call_id)
    )""",
    """CREATE INDEX agent_tasks_by_parent ON agent_tasks
        (tenant_id, product_id, principal_id, parent_run_id, status)""",
    """CREATE TABLE agent_task_parent_fences (
        tenant_id TEXT NOT NULL, product_id TEXT NOT NULL,
        principal_id TEXT NOT NULL, parent_run_id TEXT NOT NULL,
        cancelled_at TEXT NOT NULL,
        PRIMARY KEY (tenant_id, product_id, principal_id, parent_run_id)
    )""",
)


def _owner(scope: RequestScope) -> tuple[str, str, str]:
    if not isinstance(scope, RequestScope):
        raise TypeError("parent_scope must be a RequestScope")
    return scope.tenant_id, scope.product_id, scope.principal_id


def _identifier(value: str, label: str, *, digest: bool = False) -> str:
    pattern = _DIGEST if digest else _IDENTIFIER
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _bounded(value: str | None, label: str, limit: int) -> str | None:
    if value is not None and (not isinstance(value, str) or len(value.encode("utf-8")) > limit):
        raise ValueError(f"{label} exceeds {limit} bytes")
    return value


def _record(row: sqlite3.Row) -> AgentTaskRecord:
    return AgentTaskRecord(
        task_id=row["task_id"],
        tenant_id=row["tenant_id"],
        product_id=row["product_id"],
        principal_id=row["principal_id"],
        parent_run_id=row["parent_run_id"],
        call_id=row["call_id"],
        role_id=row["role_id"],
        task_digest=row["task_digest"],
        child_run_id=row["child_run_id"],
        child_agent_id=row["child_agent_id"],
        child_session_id=row["child_session_id"],
        status=AgentTaskStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        started_at=(
            None if row["started_at"] is None else datetime.fromisoformat(row["started_at"])
        ),
        finished_at=(
            None if row["finished_at"] is None else datetime.fromisoformat(row["finished_at"])
        ),
        result=row["result"],
        failure=row["failure"],
    )


class SQLiteAgentTaskStore(AgentTaskStore):
    """Atomic, scoped state for one framework-owned task database."""

    def __init__(self, database: SQLiteDatabase) -> None:
        if not isinstance(database, SQLiteDatabase):
            raise TypeError("database must be a SQLiteDatabase")
        self._database = database
        database.install_schema("multiagent.tasks", 1, _SCHEMA)

    async def create(
        self,
        parent_scope: RequestScope,
        parent_run_id: str,
        call_id: str,
        role_id: str,
        task_digest: str,
    ) -> AgentTaskRecord:
        owner = _owner(parent_scope)
        for label, value in (
            ("parent_run_id", parent_run_id),
            ("call_id", call_id),
            ("role_id", role_id),
        ):
            _identifier(value, label)
        _identifier(task_digest, "task_digest", digest=True)
        key = (*owner, parent_run_id)
        now = datetime.now(UTC).isoformat()
        async with self._database.transaction(write=True) as connection:
            prior = connection.execute(
                "SELECT * FROM agent_tasks WHERE tenant_id=? AND product_id=? "
                "AND principal_id=? AND parent_run_id=? AND call_id=?",
                (*key, call_id),
            ).fetchone()
            if prior is not None:
                if prior["role_id"] != role_id or prior["task_digest"] != task_digest:
                    raise AgentTaskIdempotencyConflictError(
                        "delegation call was reused with different role or task digest"
                    )
                return _record(prior)
            fence = connection.execute(
                "SELECT 1 FROM agent_task_parent_fences WHERE tenant_id=? "
                "AND product_id=? AND principal_id=? AND parent_run_id=?",
                key,
            ).fetchone()
            if fence is not None:
                raise AgentTaskCancelledError("parent run was cancelled")
            task_id = uuid4().hex
            connection.execute(
                "INSERT INTO agent_tasks VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)",
                (
                    task_id,
                    *key,
                    call_id,
                    role_id,
                    task_digest,
                    uuid4().hex,
                    uuid4().hex,
                    uuid4().hex,
                    AgentTaskStatus.QUEUED.value,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            assert row is not None
            return _record(row)

    async def get_by_call(
        self,
        parent_scope: RequestScope,
        parent_run_id: str,
        call_id: str,
    ) -> AgentTaskRecord | None:
        key = (
            *_owner(parent_scope),
            _identifier(parent_run_id, "parent_run_id"),
            _identifier(call_id, "call_id"),
        )
        async with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM agent_tasks WHERE tenant_id=? AND product_id=? "
                "AND principal_id=? AND parent_run_id=? AND call_id=?",
                key,
            ).fetchone()
            return None if row is None else _record(row)

    async def get_owned(
        self,
        parent_scope: RequestScope,
        task_id: str,
        *,
        parent_run_id: str,
    ) -> AgentTaskRecord | None:
        owner = _owner(parent_scope)
        _identifier(parent_run_id, "parent_run_id")
        if not isinstance(task_id, str) or not _IDENTIFIER.fullmatch(task_id):
            return None
        query = (
            "SELECT * FROM agent_tasks WHERE tenant_id=? AND product_id=? "
            "AND principal_id=? AND task_id=? AND parent_run_id=?"
        )
        params: tuple[str, ...] = (*owner, task_id, parent_run_id)
        async with self._database.transaction() as connection:
            row = connection.execute(query, params).fetchone()
            return None if row is None else _record(row)

    async def transition(
        self,
        parent_scope: RequestScope,
        task_id: str,
        expected: AgentTaskStatus,
        target: AgentTaskStatus,
        *,
        parent_run_id: str,
        result: str | None = None,
        failure: str | None = None,
    ) -> AgentTaskRecord | None:
        owner = _owner(parent_scope)
        _identifier(parent_run_id, "parent_run_id")
        if not isinstance(task_id, str) or not _IDENTIFIER.fullmatch(task_id):
            return None
        expected = AgentTaskStatus(expected)
        target = AgentTaskStatus(target)
        if target not in _TRANSITIONS.get(expected, frozenset()):
            return None
        result = _bounded(result, "result", 8192)
        failure = _bounded(failure, "failure", 2048)
        if result is not None and target is not AgentTaskStatus.SUCCEEDED:
            raise ValueError("result requires SUCCEEDED status")
        if failure is not None and target not in {
            AgentTaskStatus.FAILED,
            AgentTaskStatus.TIMED_OUT,
        }:
            raise ValueError("failure requires FAILED or TIMED_OUT status")
        now = datetime.now(UTC).isoformat()
        predicate = (
            "tenant_id=? AND product_id=? AND principal_id=? AND task_id=? AND parent_run_id=?"
        )
        params: tuple[str, ...] = (*owner, task_id, parent_run_id)
        async with self._database.transaction(write=True) as connection:
            cursor = connection.execute(
                "UPDATE agent_tasks SET status=?, updated_at=?, "
                "started_at=CASE WHEN ?='RUNNING' THEN ? ELSE started_at END, "
                "finished_at=CASE WHEN ? IN ('SUCCEEDED','FAILED','CANCELLED','TIMED_OUT') "
                "THEN ? ELSE finished_at END, result=?, failure=? "
                f"WHERE {predicate} AND status=?",
                (
                    target.value,
                    now,
                    target.value,
                    now,
                    target.value,
                    now,
                    result,
                    failure,
                    *params,
                    expected.value,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                f"SELECT * FROM agent_tasks WHERE {predicate}", params
            ).fetchone()
            assert row is not None
            return _record(row)

    async def cancel_children(self, parent_scope: RequestScope, parent_run_id: str) -> int:
        key = (*_owner(parent_scope), _identifier(parent_run_id, "parent_run_id"))
        now = datetime.now(UTC).isoformat()
        async with self._database.transaction(write=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO agent_task_parent_fences VALUES (?, ?, ?, ?, ?)",
                (*key, now),
            )
            cursor = connection.execute(
                "UPDATE agent_tasks SET status=?, updated_at=?, finished_at=? "
                "WHERE tenant_id=? AND product_id=? AND principal_id=? AND parent_run_id=? "
                "AND status IN ('QUEUED','RUNNING')",
                (AgentTaskStatus.CANCELLED.value, now, now, *key),
            )
            return cursor.rowcount

    async def recover_interrupted(self) -> int:
        now = datetime.now(UTC).isoformat()
        async with self._database.transaction(write=True) as connection:
            cursor = connection.execute(
                "UPDATE agent_tasks SET status=?, updated_at=?, finished_at=?, failure=? "
                "WHERE status IN (?, ?)",
                (
                    AgentTaskStatus.FAILED.value,
                    now,
                    now,
                    "interrupted by service restart",
                    AgentTaskStatus.QUEUED.value,
                    AgentTaskStatus.RUNNING.value,
                ),
            )
            return cursor.rowcount


__all__ = ["SQLiteAgentTaskStore"]
