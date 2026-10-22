"""Construct explicitly selected storage; PostgreSQL never falls back to files."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from suiteharness.config import LoadedSuiteHarnessConfig


@dataclass(frozen=True, slots=True)
class PersistenceRuntime:
    database: Any
    sessions: Any
    audit: Any
    tool_results: Any
    product_state: Any
    mcp_resume: Any
    sandbox_quarantine: Any


def create_runtime_database(loaded: LoadedSuiteHarnessConfig) -> Any:
    """Create a pool owner without opening it or installing PostgreSQL tables."""
    storage = loaded.config.storage
    if storage.backend == "sqlite":
        from suiteharness.persistence import SQLiteDatabase
        return SQLiteDatabase(storage.runtime_path())
    from suiteharness.persistence.postgres import PostgresDatabase

    settings = storage.postgres
    if settings is None:
        raise ValueError("PostgreSQL storage configuration is required")
    credentials = loaded.secrets.databases.get(settings.dsn_secret_ref)
    if credentials is None:
        raise ValueError("PostgreSQL database credentials are unavailable")
    return PostgresDatabase(
        credentials.dsn.get_secret_value(),
        schema=settings.schema_name,
        pool_min_size=settings.pool_min_size,
        pool_max_size=settings.pool_max_size,
        connect_timeout_seconds=settings.connect_timeout_seconds,
        pool_timeout_seconds=settings.pool_timeout_seconds,
        statement_timeout_ms=settings.statement_timeout_ms,
        lock_timeout_ms=settings.lock_timeout_ms,
        idle_transaction_timeout_ms=settings.idle_transaction_timeout_ms,
    )


def create_persistence_runtime(loaded: LoadedSuiteHarnessConfig) -> PersistenceRuntime:
    """Select all stores together, sharing one PostgreSQL pool per process."""
    storage = loaded.config.storage
    database = create_runtime_database(loaded)
    if storage.backend == "postgresql":
        from suiteharness.execution.postgres import PostgresAuditJournal, PostgresToolResultStore
        from suiteharness.persistence.postgres import (
            PostgresMcpHttpResumeStore, PostgresProductStateStore, PostgresSandboxQuarantineStore,
        )
        from suiteharness.sessions.postgres import PostgresSessionStore
        state = PostgresProductStateStore(database)
        return PersistenceRuntime(
            database=database,
            sessions=PostgresSessionStore(database, limits=storage.session_limits),
            audit=PostgresAuditJournal(database),
            tool_results=PostgresToolResultStore(database),
            product_state=state,
            mcp_resume=PostgresMcpHttpResumeStore(state),
            sandbox_quarantine=PostgresSandboxQuarantineStore(
                database, deployment_id=loaded.config.deployment.instance_id,
            ),
        )
    from suiteharness.execution import SQLiteAuditJournal
    from suiteharness.execution.results import SQLiteToolResultStore
    from suiteharness.persistence import (
        SQLiteMcpHttpResumeStore, SQLiteProductStateStore, SQLiteSandboxQuarantineStore,
    )
    from suiteharness.sessions import SQLiteSessionStore
    state = SQLiteProductStateStore(database)
    return PersistenceRuntime(
        database=database,
        sessions=SQLiteSessionStore(storage.sessions_path(), limits=storage.session_limits),
        audit=SQLiteAuditJournal(storage.audit_path()),
        tool_results=SQLiteToolResultStore(storage.sessions_path().parent / "tool-results.sqlite3"),
        product_state=state,
        mcp_resume=SQLiteMcpHttpResumeStore(state),
        sandbox_quarantine=SQLiteSandboxQuarantineStore(
            database, deployment_id=loaded.config.deployment.instance_id,
        ),
    )


async def open_runtime_database(database: Any) -> None:
    """Open only a backend that requires asynchronous startup validation."""
    open_database = getattr(database, "open", None)
    if open_database is not None:
        await open_database()
