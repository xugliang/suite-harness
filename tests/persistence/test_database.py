from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from suiteharness.persistence import PersistenceClosedError, SQLiteDatabase


def test_transaction_commits_and_rolls_back_atomically(tmp_path: Path) -> None:
    async def exercise() -> tuple[int, int]:
        database = SQLiteDatabase((tmp_path / "state" / "suiteharness.sqlite3").resolve())
        database.install_schema(
            "test_component",
            1,
            ("CREATE TABLE test_items(value INTEGER NOT NULL)",),
        )
        async with database.transaction(write=True) as connection:
            connection.execute("INSERT INTO test_items(value) VALUES (1)")
        with pytest.raises(RuntimeError, match="abort"):
            async with database.transaction(write=True) as connection:
                connection.execute("INSERT INTO test_items(value) VALUES (2)")
                raise RuntimeError("abort")
        async with database.transaction() as connection:
            count = int(connection.execute("SELECT COUNT(*) FROM test_items").fetchone()[0])
            version = int(
                connection.execute(
                    "SELECT version FROM suiteharness_schema_versions WHERE component='test_component'"
                ).fetchone()[0]
            )
        await database.close()
        await database.close()
        with pytest.raises(PersistenceClosedError):
            async with database.transaction():
                pass
        return count, version

    assert asyncio.run(exercise()) == (1, 1)


def test_database_requires_absolute_path_and_rejects_schema_guessing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        SQLiteDatabase("state.sqlite3")

    async def exercise() -> None:
        database = SQLiteDatabase((tmp_path / "schema.sqlite3").resolve())
        database.install_schema("component", 1, ("CREATE TABLE component_data(id INTEGER)",))
        database.install_schema("component", 1, ("CREATE TABLE ignored(id INTEGER)",))
        with pytest.raises(RuntimeError, match="schema version"):
            database.install_schema("component", 2, ("CREATE TABLE newer(id INTEGER)",))
        await database.close()

    asyncio.run(exercise())
