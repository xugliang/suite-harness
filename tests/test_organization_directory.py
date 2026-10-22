"""Behavioral contracts against an explicitly isolated directory PostgreSQL."""
import asyncio
import os

import pytest

from suiteharness.organization.models import (
    Department,
    DirectorySnapshot,
    Employee,
    ExternalIdentity,
)
from suiteharness.organization.postgres import PostgresDirectoryStore


@pytest.fixture
def store():
    dsn = os.environ.get("SUITE_ORGANIZATION_TEST_DSN")
    if not dsn:
        pytest.skip("explicit isolated organization database required")
    db = PostgresDirectoryStore(dsn, expected_database="organization_directory_test")
    db.migrate()
    db.open()
    with db.connection() as conn:
        conn.execute("TRUNCATE directory.principal_bindings, directory.external_identities, directory.employee_departments, directory.employees, directory.departments, directory.consumers, directory.sync_state RESTART IDENTITY CASCADE")
    yield db
    db.close()


def snapshot(name="员工甲", *, scope="scope1", extra=False):
    employees = [Employee("u1", name, ("d1",), "工程师", "active", identities=(ExternalIdentity("user_id", "u1"), ExternalIdentity("union_id", "on_a")))]
    if extra:
        employees.append(Employee("u2", "员工乙", ("d1",), status="active"))
    return DirectorySnapshot(tuple(employees), (Department("d1", "研发部", "0"),), scope)


def publish(store, value, *, config='config1'):
    claim = store.claim(config, owner="test", fresh_seconds=0)
    assert claim["status"] == "claimed"
    return store.publish(value, owner="test", token=claim["token"], config_fingerprint=config)


def test_current_directory_is_shared_and_fresh_sync_is_skipped(store):
    publish(store, snapshot())
    store.acquire("product-a", ttl_seconds=300)
    store.acquire("product-b", ttl_seconds=300)
    assert store.claim("config1", owner="other")["status"] == "fresh"
    rows = store.list_employees()
    assert [(r["display_name"], r["department_names"]) for r in rows] == [("员工甲", ["研发部"])]


def test_publish_changes_current_record_without_history_and_preserves_identity(store):
    publish(store, snapshot())
    first = store.list_employees()[0]
    store.bind("product-a", "legacy-a", kind="union_id", value="on_a")
    publish(store, snapshot("员工甲新名"))
    row = store.get_for_principal("product-a", "legacy-a")
    assert row["employee_id"] == first["employee_id"]
    assert row["display_name"] == "员工甲新名"
    assert row["revision"] > first["revision"]
    assert len(store.list_employees()) == 1


def test_concurrent_claims_have_only_one_winner_and_expired_owner_cannot_publish(store):
    first = store.claim("config1", owner="old", fresh_seconds=0)
    assert store.claim("config1", owner="new", fresh_seconds=0)["status"] == "busy"
    with store.connection() as conn:
        conn.execute("UPDATE directory.sync_state SET lease_until=clock_timestamp()-interval '1 second'")
    second = store.claim("config1", owner="new", fresh_seconds=0)
    with pytest.raises(ValueError, match="lease"):
        store.publish(snapshot(), owner="old", token=first["token"], config_fingerprint="config1")
    store.publish(snapshot("新执行者"), owner="new", token=second["token"], config_fingerprint="config1")
    assert store.list_employees()[0]["display_name"] == "新执行者"


def test_empty_or_partial_sync_never_wipes_valid_directory(store):
    publish(store, snapshot())
    for value in (DirectorySnapshot((), (), "scope1"), DirectorySnapshot((), (), "scope1", False)):
        claim = store.claim("config1", owner="test", fresh_seconds=0)
        with pytest.raises(ValueError):
            store.publish(value, owner="test", token=claim["token"], config_fingerprint="config1")
        store.fail(owner="test", token=claim["token"], code="incomplete", retry_seconds=0)
    assert store.list_employees()[0]["display_name"] == "员工甲"


def test_missing_employee_loses_directory_visibility_without_being_declared_resigned(store):
    publish(store, snapshot(extra=True))
    old = store.list_employees()[1]
    publish(store, snapshot())
    assert len(store.list_employees()) == 1
    with store.connection() as conn:
        row = conn.execute("SELECT display_name,status,present FROM directory.employees WHERE employee_id=%s", (old["employee_id"],)).fetchone()
    assert row == {"display_name": "", "status": "unknown", "present": False}


def test_scope_change_restricts_old_data_until_successfully_reconciled(store):
    publish(store, snapshot())
    claim = store.claim("config1", owner="test", fresh_seconds=0)
    with pytest.raises(ValueError, match="scope"):
        store.publish(snapshot(scope="reduced"), owner="test", token=claim["token"], config_fingerprint="config1")
    store.fail(owner="test", token=claim["token"], code="scope_changed", restricted=True, retry_seconds=0)
    assert store.list_employees() == []
    assert store.status()["restricted"] is True


def test_missing_bindings_never_match_by_name(store):
    publish(store, snapshot())
    assert store.get_for_principal("product-a", "员工甲") is None
    with pytest.raises(ValueError):
        store.bind("product-a", "legacy", kind="user_id", value="missing")


def test_transient_failure_keeps_current_data_and_last_success(store):
    publish(store, snapshot())
    before = store.status()["last_success_at"]
    claim = store.claim("config1", owner="test", fresh_seconds=0)
    store.fail(owner="test", token=claim["token"], code="upstream_unavailable", retry_seconds=300)
    assert store.status()["last_success_at"] == before
    assert store.list_employees()[0]["display_name"] == "员工甲"
    assert store.claim("config1", owner="test", fresh_seconds=0)["status"] == "backoff"


def test_changed_app_configuration_is_not_mistaken_for_successful_refresh(store):
    publish(store, snapshot())
    attempt = store.claim("config2", owner="second-app")
    assert attempt['status'] == 'config_mismatch'
    store.activate_config('config2', expected_fingerprint='config1')
    attempt = store.claim("config2", owner="second-app")
    assert attempt['status'] == 'claimed'
    store.fail(owner='second-app', token=attempt['token'], code='token_error', retry_seconds=0)
    assert store.claim('config2', owner='again')['status'] == 'claimed'


def test_expanding_direct_user_scope_allows_new_employees(store):
    old = DirectorySnapshot(snapshot().employees, snapshot().departments, 'first', scope_keys=('user:u1',))
    new = DirectorySnapshot(snapshot(extra=True).employees, snapshot().departments, 'second', scope_keys=('user:u1','user:u2'))
    publish(store, old)
    publish(store, new)
    assert len(store.list_employees()) == 2


@pytest.mark.parametrize('code', ['insufficient_permissions', 'authentication_failed', 'missing_employee_fields'])
def test_actual_feishu_authorization_errors_stop_serving_previous_directory(store, code):
    from suiteharness.organization.config import DirectoryConfig
    from suiteharness.organization.service import DirectoryCoordinator
    from suiteharness.organization.source import SourceError
    config = DirectoryConfig(enabled=True, app_id='cli_test')
    publish(store, snapshot(), config=config.fingerprint)
    with store.connection() as conn:
        conn.execute("UPDATE directory.sync_state SET last_success_at=clock_timestamp()-interval '25 hours'")
    class Source:
        async def fetch(self):
            raise SourceError(code=code)
    result = asyncio.run(DirectoryCoordinator(store, Source(), config).synchronize())
    assert result['status'] == 'failed'
    assert store.status()['restricted'] is True
    assert store.list_employees() == []


def test_source_user_id_rename_preserves_verified_employee_and_product_binding(store):
    publish(store, snapshot())
    store.bind('product-a', 'legacy-a', kind='union_id', value='on_a')
    old_id = store.get_for_principal('product-a', 'legacy-a')['employee_id']
    value = DirectorySnapshot((Employee('u-renamed', '员工甲', ('d1',), status='active', identities=(ExternalIdentity('user_id', 'u-renamed'), ExternalIdentity('union_id', 'on_a'))),), snapshot().departments, 'scope1')
    publish(store, value)
    assert store.get_for_principal('product-a', 'legacy-a')['employee_id'] == old_id
    assert len(store.list_employees()) == 1


def test_reused_user_id_must_not_attach_new_person_to_old_product_account(store):
    publish(store, snapshot())
    store.bind('product-a', 'legacy-a', kind='union_id', value='on_a')
    replacement = DirectorySnapshot((Employee('u1', '另一个人', ('d1',), status='active', identities=(ExternalIdentity('user_id', 'u1'), ExternalIdentity('union_id', 'on_new'))),), snapshot().departments, 'scope1')
    with pytest.raises(ValueError, match='identity'):
        publish(store, replacement)
    assert store.get_for_principal('product-a', 'legacy-a')['display_name'] == '员工甲'
