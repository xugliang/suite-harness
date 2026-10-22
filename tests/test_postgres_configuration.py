"""Configuration must fail closed before constructing any SQLite store."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from suiteharness.config.models import StorageConfig, SuiteHarnessSecrets


def test_postgres_storage_accepts_bounded_connection_configuration():
    config = StorageConfig.model_validate({
        "root": "/srv/product/state",
        "backend": "postgresql",
        "postgres": {"dsn_secret_ref": "product_database", "schema": "harness"},
    })
    assert config.backend == "postgresql"
    assert config.postgres.schema_name == "harness"
    assert config.postgres.pool_max_size == 6
    assert StorageConfig.model_validate(config.model_dump()) == config
    with pytest.raises(ValueError, match="PostgreSQL"):
        config.runtime_path()


@pytest.mark.parametrize("extra", [
    {},
    {"postgres": {"dsn_secret_ref": "db", "pool_min_size": 5, "pool_max_size": 2}},
    {"postgres": {"dsn_secret_ref": "db", "schema": "public; DROP TABLE sessions"}},
    {"postgres": {"dsn_secret_ref": "db", "schema": "pg_catalog"}},
    {"postgres": {"dsn_secret_ref": "db"}, "runtime_database": "old.sqlite3"},
])
def test_invalid_postgres_configuration_never_falls_back_to_sqlite(extra):
    with pytest.raises(ValidationError):
        StorageConfig.model_validate({"root": "/srv/state", "backend": "postgresql", **extra})


def test_database_credentials_are_redacted():
    secrets = SuiteHarnessSecrets.model_validate({
        "databases": {"product_database": {"dsn": "postgresql://runtime:private-value@localhost/product"}}
    })
    assert "private-value" not in repr(secrets)
    assert "private-value" not in secrets.model_dump_json()
    assert secrets.databases["product_database"].dsn.get_secret_value().endswith("/product")


def test_legacy_sqlite_requires_no_postgres_configuration():
    config = StorageConfig(root=Path("/srv/state"))
    assert config.sessions_path() == Path("/srv/state/sessions.sqlite3")
    with pytest.raises(ValidationError):
        StorageConfig.model_validate({"root": "/srv/state", "postgres": {"dsn_secret_ref": "db"}})


def test_postgres_missing_secret_fails_without_creating_local_databases(tmp_path):
    from types import SimpleNamespace
    from suiteharness.server.storage import create_persistence_runtime
    storage = StorageConfig.model_validate({
        "root": str(tmp_path), "backend": "postgresql",
        "postgres": {"dsn_secret_ref": "missing"},
    })
    loaded = SimpleNamespace(config=SimpleNamespace(storage=storage), secrets=SuiteHarnessSecrets())
    with pytest.raises(ValueError, match="credentials"):
        create_persistence_runtime(loaded)
    assert list(tmp_path.iterdir()) == []


def test_postgres_factory_builds_all_stores_without_connecting_or_creating_files(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from suiteharness.server.storage import create_persistence_runtime
    storage = StorageConfig.model_validate({
        "root": str(tmp_path), "backend": "postgresql",
        "postgres": {"dsn_secret_ref": "db"},
    })
    loaded = SimpleNamespace(
        config=SimpleNamespace(storage=storage, deployment=SimpleNamespace(instance_id="test")),
        secrets=SuiteHarnessSecrets.model_validate({"databases": {"db": {"dsn": "postgresql://user:dummy@127.0.0.1:1/unused"}}}),
    )
    runtime = create_persistence_runtime(loaded)
    assert runtime.sessions is not None and runtime.tool_results is not None
    assert list(tmp_path.iterdir()) == []
    asyncio.run(runtime.database.close())
