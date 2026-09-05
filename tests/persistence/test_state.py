from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from suiteharness.persistence import (
    PersistenceCapacityError,
    ProductStateKey,
    SQLiteDatabase,
    SQLiteProductStateStore,
    StateIdempotencyConflictError,
    StatePayloadTooLargeError,
    StateRevisionConflictError,
)


def _key(*, tenant: str = "tenant-a", product: str = "product-a", key: str = "cursor"):
    return ProductStateKey(
        tenant_id=tenant,
        product_id=product,
        namespace="plugin.example",
        key=key,
    )


def test_state_survives_restart_is_isolated_and_replays_original_result(tmp_path: Path) -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        path = (tmp_path / "state.sqlite3").resolve()
        first_database = SQLiteDatabase(path)
        store = SQLiteProductStateStore(first_database)
        created = await store.compare_and_set(
            _key(), {"position": 1}, expected_revision=0, idempotency_key="create-1"
        )
        updated = await store.compare_and_set(
            _key(), {"position": 2}, expected_revision=1, idempotency_key="update-1"
        )
        await store.compare_and_set(
            _key(product="product-b"),
            {"position": 99},
            expected_revision=0,
            idempotency_key="create-other",
        )
        replayed = await store.compare_and_set(
            _key(), {"position": 1}, expected_revision=0, idempotency_key="create-1"
        )
        await first_database.close()

        second_database = SQLiteDatabase(path)
        reopened = SQLiteProductStateStore(second_database)
        current = await reopened.get(_key())
        other = await reopened.get(_key(product="product-b"))
        missing = await reopened.get(_key(tenant="tenant-b"))
        listed = await reopened.list_namespace(
            tenant_id="tenant-a", product_id="product-a", namespace="plugin.example"
        )
        await second_database.close()
        return created, updated, replayed, current, other, missing, listed

    created, updated, replayed, current, other, missing, listed = asyncio.run(exercise())
    assert created.record.revision == 1
    assert updated.record.revision == 2
    assert replayed.replayed is True
    assert replayed.record == created.record
    assert current is not None and current.value == {"position": 2}
    assert other is not None and other.value == {"position": 99}
    assert missing is None
    assert [item.state_key.key for item in listed] == ["cursor"]


def test_state_rejects_stale_cas_and_idempotency_key_reuse() -> None:
    async def exercise() -> None:
        database = SQLiteDatabase(":memory:")
        store = SQLiteProductStateStore(database)
        await store.compare_and_set(
            _key(), {"ok": True}, expected_revision=0, idempotency_key="command-1"
        )
        with pytest.raises(StateRevisionConflictError) as revision:
            await store.compare_and_set(
                _key(), {"ok": False}, expected_revision=0, idempotency_key="command-2"
            )
        assert revision.value.actual_revision == 1
        with pytest.raises(StateIdempotencyConflictError):
            await store.compare_and_set(
                _key(), {"ok": False}, expected_revision=0, idempotency_key="command-1"
            )
        assert (await store.get(_key())).value == {"ok": True}  # type: ignore[union-attr]
        await database.close()

    asyncio.run(exercise())


def test_state_enforces_payload_record_and_idempotency_limits() -> None:
    async def exercise() -> None:
        database = SQLiteDatabase(":memory:")
        store = SQLiteProductStateStore(
            database,
            max_value_bytes=20,
            max_records_per_namespace=1,
            max_idempotency_records_per_namespace=2,
        )
        with pytest.raises(StatePayloadTooLargeError):
            await store.compare_and_set(
                _key(), "四" * 10, expected_revision=0, idempotency_key="too-large"
            )
        await store.compare_and_set(
            _key(), 1, expected_revision=0, idempotency_key="first"
        )
        with pytest.raises(PersistenceCapacityError, match="record limit"):
            await store.compare_and_set(
                _key(key="second"), 2, expected_revision=0, idempotency_key="second"
            )
        await store.compare_and_set(
            _key(), 2, expected_revision=1, idempotency_key="update"
        )
        with pytest.raises(PersistenceCapacityError, match="idempotency"):
            await store.compare_and_set(
                _key(), 3, expected_revision=2, idempotency_key="third-command"
            )
        await database.close()

    asyncio.run(exercise())


def test_state_forbids_approval_bearer_material() -> None:
    async def exercise() -> None:
        database = SQLiteDatabase(":memory:")
        store = SQLiteProductStateStore(database)
        with pytest.raises(ValueError, match="approval credentials"):
            await store.compare_and_set(
                _key(),
                {"nested": {"approval_token": "plaintext-bearer"}},
                expected_revision=0,
                idempotency_key="forbidden",
            )
        await database.close()

    with pytest.raises(ValueError, match="approval state"):
        ProductStateKey(
            tenant_id="tenant-a",
            product_id="product-a",
            namespace="plugin.approvals",
            key="pending",
        )
    asyncio.run(exercise())


def test_two_connections_cannot_both_win_the_same_cas(tmp_path: Path) -> None:
    async def exercise() -> tuple[int, int]:
        path = (tmp_path / "concurrent.sqlite3").resolve()
        first_database = SQLiteDatabase(path)
        second_database = SQLiteDatabase(path)
        first = SQLiteProductStateStore(first_database)
        second = SQLiteProductStateStore(second_database)

        async def attempt(store: SQLiteProductStateStore, value: int, command: str) -> int:
            try:
                await store.compare_and_set(
                    _key(), value, expected_revision=0, idempotency_key=command
                )
            except StateRevisionConflictError:
                return 0
            return 1

        results = await asyncio.gather(
            attempt(first, 1, "first"), attempt(second, 2, "second")
        )
        await first_database.close()
        await second_database.close()
        return results[0], results[1]

    assert sum(asyncio.run(exercise())) == 1
