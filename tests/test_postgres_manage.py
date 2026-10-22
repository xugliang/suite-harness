"""Database administration never initializes the legacy file backend."""
import asyncio
from types import SimpleNamespace

import pytest
from suiteharness.config.models import StorageConfig
from suiteharness.persistence.manage import manage


def test_postgres_management_refuses_sqlite_before_any_file_is_created(tmp_path):
    loaded = SimpleNamespace(config=SimpleNamespace(storage=StorageConfig(root=tmp_path)))
    with pytest.raises(ValueError, match="PostgreSQL"):
        asyncio.run(manage(loaded, "migrate"))
    assert list(tmp_path.iterdir()) == []


def test_unknown_administration_action_is_rejected_without_connecting():
    with pytest.raises(ValueError, match="action"):
        asyncio.run(manage(None, "delete-all"))
