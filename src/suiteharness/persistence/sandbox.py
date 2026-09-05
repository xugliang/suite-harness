"""Durable quarantine inventory for exact-name Docker cleanup reconciliation."""

from __future__ import annotations

import re
from collections.abc import Mapping

from .database import SQLiteDatabase
from .models import utc_now

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CONTAINER = re.compile(r"^suiteharness-sandbox-[0-9a-f]{24}$")
_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS suiteharness_sandbox_quarantine (
        deployment_id TEXT NOT NULL,
        backend_id TEXT NOT NULL,
        container_name TEXT NOT NULL,
        reason TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        PRIMARY KEY (deployment_id, backend_id, container_name)
    )
    """,
)
_OWNER_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS suiteharness_sandbox_quarantine_owners (
        deployment_id TEXT NOT NULL,
        backend_id TEXT NOT NULL,
        container_name TEXT NOT NULL,
        owner_token TEXT NOT NULL,
        PRIMARY KEY (deployment_id, backend_id, container_name),
        FOREIGN KEY (deployment_id, backend_id, container_name)
            REFERENCES suiteharness_sandbox_quarantine(
                deployment_id, backend_id, container_name
            ) ON DELETE CASCADE
    )
    """,
)


class SQLiteSandboxQuarantineStore:
    """Persist only deterministic container names and sanitized failure reasons."""

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        deployment_id: str,
        backend_id: str = "docker",
    ) -> None:
        if not _IDENTIFIER.fullmatch(deployment_id):
            raise ValueError("invalid sandbox quarantine deployment_id")
        if not _IDENTIFIER.fullmatch(backend_id):
            raise ValueError("invalid sandbox quarantine backend_id")
        self._database = database
        self._deployment_id = deployment_id
        self._backend_id = backend_id
        database.install_schema("sandbox_quarantine", 1, _SCHEMA)
        database.install_schema("sandbox_quarantine_owners", 1, _OWNER_SCHEMA)

    async def load(self) -> Mapping[str, str]:
        async with self._database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT container_name, reason
                FROM suiteharness_sandbox_quarantine
                WHERE deployment_id=? AND backend_id=?
                ORDER BY container_name
                """,
                (self._deployment_id, self._backend_id),
            ).fetchall()
        result = {str(row["container_name"]): str(row["reason"]) for row in rows}
        for name, reason in result.items():
            self._validate(name, reason)
        return result

    async def mark(self, container_name: str, reason: str) -> None:
        self._validate(container_name, reason)
        async with self._database.transaction(write=True) as connection:
            connection.execute(
                """
                DELETE FROM suiteharness_sandbox_quarantine_owners
                WHERE deployment_id=? AND backend_id=? AND container_name=?
                """,
                (self._deployment_id, self._backend_id, container_name),
            )
            connection.execute(
                """
                INSERT INTO suiteharness_sandbox_quarantine(
                    deployment_id, backend_id, container_name, reason, recorded_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(deployment_id, backend_id, container_name)
                DO UPDATE SET reason=excluded.reason, recorded_at=excluded.recorded_at
                """,
                (
                    self._deployment_id,
                    self._backend_id,
                    container_name,
                    reason,
                    utc_now().isoformat(),
                ),
            )

    async def claim(
        self,
        container_name: str,
        reason: str,
        *,
        owner_token: str,
    ) -> bool:
        """Atomically create an owner-qualified active-container lease."""

        self._validate(container_name, reason)
        self._validate_owner_token(owner_token)
        async with self._database.transaction(write=True) as connection:
            existing = connection.execute(
                """
                SELECT 1 FROM suiteharness_sandbox_quarantine
                WHERE deployment_id=? AND backend_id=? AND container_name=?
                """,
                (self._deployment_id, self._backend_id, container_name),
            ).fetchone()
            if existing is not None:
                return False
            connection.execute(
                """
                INSERT INTO suiteharness_sandbox_quarantine(
                    deployment_id, backend_id, container_name, reason, recorded_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    self._deployment_id,
                    self._backend_id,
                    container_name,
                    reason,
                    utc_now().isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO suiteharness_sandbox_quarantine_owners(
                    deployment_id, backend_id, container_name, owner_token
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    self._deployment_id,
                    self._backend_id,
                    container_name,
                    owner_token,
                ),
            )
        return True

    async def clear(
        self,
        container_name: str,
        *,
        owner_token: str | None = None,
    ) -> bool:
        self._validate(container_name, "reconciled")
        if owner_token is not None:
            self._validate_owner_token(owner_token)
        async with self._database.transaction(write=True) as connection:
            if owner_token is None:
                cursor = connection.execute(
                    """
                    DELETE FROM suiteharness_sandbox_quarantine
                    WHERE deployment_id=? AND backend_id=? AND container_name=?
                    """,
                    (self._deployment_id, self._backend_id, container_name),
                )
            else:
                cursor = connection.execute(
                    """
                    DELETE FROM suiteharness_sandbox_quarantine
                    WHERE deployment_id=? AND backend_id=? AND container_name=?
                        AND EXISTS (
                            SELECT 1 FROM suiteharness_sandbox_quarantine_owners AS owner
                            WHERE owner.deployment_id=
                                suiteharness_sandbox_quarantine.deployment_id
                                AND owner.backend_id=suiteharness_sandbox_quarantine.backend_id
                                AND owner.container_name=
                                    suiteharness_sandbox_quarantine.container_name
                                AND owner.owner_token=?
                        )
                    """,
                    (
                        self._deployment_id,
                        self._backend_id,
                        container_name,
                        owner_token,
                    ),
                )
            return cursor.rowcount == 1

    @staticmethod
    def _validate(container_name: str, reason: str) -> None:
        if not _CONTAINER.fullmatch(container_name):
            raise ValueError("invalid quarantined container name")
        if not reason or len(reason) > 512 or any(char in reason for char in "\x00\r\n"):
            raise ValueError("invalid sandbox quarantine reason")

    @staticmethod
    def _validate_owner_token(owner_token: str) -> None:
        if (
            not isinstance(owner_token, str)
            or len(owner_token) < 32
            or len(owner_token) > 256
            or not all(char.isascii() and (char.isalnum() or char in "_-") for char in owner_token)
        ):
            raise ValueError("invalid sandbox lease owner_token")


__all__ = ["SQLiteSandboxQuarantineStore"]
