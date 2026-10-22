from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from suiteharness.persistence.migration import (
    MigrationError,
    Source,
    TableMapping,
    cleanup,
    cleanup_plan,
    import_snapshot,
    inventory,
    main,
    snapshot,
    verify,
)


def make_source(tmp_path: Path) -> Source:
    root = tmp_path / "product-data"
    root.mkdir()
    path = root / "state.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT, raw BLOB); INSERT INTO items VALUES (7, '{\"x\": 1}', X'00FF');")
    return Source(path, "feature_assistant", root, ("test-fixture",))


def test_inventory_captures_owned_paths_schema_counts_and_byte_sensitive_digest(tmp_path):
    source = make_source(tmp_path)
    before = inventory(source)
    assert before["product"] == "feature_assistant"
    assert before["tables"]["items"]["count"] == 1
    assert before["tables"]["items"]["primary_key"] == ["id"]
    with sqlite3.connect(source.path) as db:
        db.execute("UPDATE items SET body = ?", ('{"x":1}',))
    after = inventory(source)
    assert after["tables"]["items"]["digest"] != before["tables"]["items"]["digest"]


def test_snapshot_uses_sqlite_backup_including_committed_wal(tmp_path):
    source = make_source(tmp_path)
    db = sqlite3.connect(source.path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("INSERT INTO items(body) VALUES ('committed WAL')")
    db.commit()
    result = snapshot(source, tmp_path / "backups" / "state.sqlite3")
    with sqlite3.connect(result["snapshot_path"]) as backup:
        assert backup.execute("SELECT count(*) FROM items").fetchone()[0] == 2
    assert result["snapshot_sha256"]
    assert result["inventory"]["tables"]["items"]["count"] == 2
    db.close()


def test_source_refuses_outside_root_and_symlink(tmp_path):
    source = make_source(tmp_path)
    with pytest.raises(MigrationError, match="ownership"):
        inventory(Source(source.path, source.product, tmp_path / "another", ("test",)))
    link = source.owner_root / "alias.sqlite3"
    try:
        link.symlink_to(source.path)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    with pytest.raises(MigrationError, match="symlink"):
        inventory(Source(link, source.product, source.owner_root, ("test",)))


def cleanup_evidence():
    return {
        "writers_stopped": True,
        "consumers_postgresql": True,
        "business_regression_passed": True,
        "postgres_backup_restored": True,
        "sqlite_backup_restored": True,
        "config_references_checked": True,
        "unclassified_paths": [],
        "evidence_reference": "isolated-test-report",
    }


def prepared_cleanup(tmp_path, pg_connection):
    db, database = pg_connection
    source = make_source(tmp_path)
    snap = snapshot(source, tmp_path / "backup.sqlite3")
    mapping = [TableMapping("items", "pgtest_migration_fixture", "items", ("id",))]
    import_snapshot(db, snap, mapping, target_database=database)
    checked = verify(db, snap, mapping, target_database=database)
    plan = cleanup_plan(source, snap, checked, cleanup_evidence())
    return source, snap, plan, dict(db=db, manifest=snap, mappings=mapping, target_database=database)


def test_cleanup_refuses_missing_conditions_and_changed_file(tmp_path, pg_connection):
    source = make_source(tmp_path)
    snap = snapshot(source, tmp_path / "backup.sqlite3")
    verified = {"ok": True, "snapshot_sha256": snap["snapshot_sha256"], "source_digest": snap["inventory"]["logical_digest"], "migration_id": "fixture", "target_database": "feature_assistant"}
    with pytest.raises(MigrationError, match="writers_stopped"):
        cleanup_plan(source, snap, verified, {})
    db, database = pg_connection
    mapping = [TableMapping("items", "pgtest_migration_fixture", "items", ("id",))]
    import_snapshot(db, snap, mapping, target_database=database)
    checked = verify(db, snap, mapping, target_database=database)
    plan = cleanup_plan(source, snap, checked, cleanup_evidence())
    with sqlite3.connect(source.path) as sq:
        sq.execute("INSERT INTO items(body) VALUES ('new data')")
    with pytest.raises(MigrationError, match="changed"):
        cleanup(plan, evidence=cleanup_evidence(), db=db, manifest=snap, mappings=mapping, target_database=database)
    assert source.path.exists()


def test_cleanup_refuses_live_descriptor_then_deletes_only_bound_files(tmp_path, pg_connection):
    if not Path("/proc/self/fd").exists():
        pytest.skip("Linux file descriptor inspection")
    source, snap, plan, arguments = prepared_cleanup(tmp_path, pg_connection)
    untouched = source.owner_root / "keep.txt"
    untouched.write_text("keep")
    with source.path.open("rb"):
        with pytest.raises(MigrationError, match="open"):
            cleanup(plan, evidence=cleanup_evidence(), **arguments)
    assert cleanup(plan, evidence=cleanup_evidence(), **arguments)["deleted"] == [str(source.path)]
    assert untouched.exists()
    assert Path(snap["snapshot_path"]).exists()


def test_cleanup_rechecks_postgresql_instead_of_trusting_old_report(tmp_path, pg_connection):
    source, snap, plan, arguments = prepared_cleanup(tmp_path, pg_connection)
    arguments["db"].execute("DELETE FROM pgtest_migration_fixture.items")
    with pytest.raises(MigrationError, match="verification"):
        cleanup(plan, evidence=cleanup_evidence(), **arguments)
    assert source.path.exists()


def test_cleanup_refuses_descriptor_opened_through_hardlink_alias(tmp_path, pg_connection):
    if not Path("/proc/self/fd").exists():
        pytest.skip("Linux file descriptor inspection")
    source, _, plan, arguments = prepared_cleanup(tmp_path, pg_connection)
    alias = source.owner_root / "different-name.sqlite3"
    os.link(source.path, alias)
    with alias.open("rb"):
        with pytest.raises(MigrationError, match="open"):
            cleanup(plan, evidence=cleanup_evidence(), **arguments)
    assert source.path.exists()
    assert alias.exists()


def test_snapshot_refuses_damaged_foreign_key_relationships(tmp_path):
    source = make_source(tmp_path)
    with sqlite3.connect(source.path) as db:
        db.executescript("CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES items(id)); INSERT INTO child VALUES (1,999)")
    with pytest.raises(MigrationError, match="foreign key"):
        snapshot(source, tmp_path / "bad.sqlite3")


def test_cli_refuses_existing_report_before_creating_snapshot(tmp_path):
    import json

    source = make_source(tmp_path)
    destination = tmp_path / "must-not-exist.sqlite3"
    spec = tmp_path / "private-spec.json"
    spec.write_text(json.dumps({"source": {"path": str(source.path), "owner_root": str(source.owner_root), "product": source.product, "writers": ["test-fixture"]}, "destination": str(destination)}))
    report = tmp_path / "existing-report.json"
    report.write_text("old report")
    assert main(["snapshot", "--spec", str(spec), "--output", str(report)]) == 2
    assert report.read_text() == "old report"
    assert not destination.exists()


@pytest.fixture
def pg_connection():
    dsn = os.getenv("SUITE_MIGRATION_TEST_DSN")
    if not dsn:
        pytest.skip("SUITE_MIGRATION_TEST_DSN requires isolated PostgreSQL database")
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as db:
        # Only an explicitly opted-in, isolated database may run destructive fixture setup.
        database = db.execute("SELECT current_database()").fetchone()[0]
        if not (database.startswith("test_") or database == "suiteharness_pgtest_20261020"):
            pytest.fail("migration integration tests require database prefixed test_")
        db.execute("DROP SCHEMA IF EXISTS pgtest_migration_fixture CASCADE")
        db.execute("DROP SCHEMA IF EXISTS suiteharness_migration CASCADE")
        db.execute("CREATE SCHEMA pgtest_migration_fixture")
        db.execute("CREATE TABLE pgtest_migration_fixture.items (id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, body text, raw bytea)")
        yield db, database
        db.execute("DROP SCHEMA pgtest_migration_fixture CASCADE")
        db.execute("DROP SCHEMA IF EXISTS suiteharness_migration CASCADE")


def test_import_resume_conflicts_and_exact_json_verification(tmp_path, pg_connection):
    db, database = pg_connection
    source = make_source(tmp_path)
    snap = snapshot(source, tmp_path / "backup.sqlite3")
    mapping = [TableMapping("items", "pgtest_migration_fixture", "items", ("id",))]
    report = import_snapshot(db, snap, mapping, target_database=database, batch_size=1)
    again = import_snapshot(db, snap, mapping, target_database=database, batch_size=1)
    assert again["migration_id"] == report["migration_id"]
    assert db.execute("SELECT count(*) FROM pgtest_migration_fixture.items").fetchone()[0] == 1
    checked = verify(db, snap, mapping, target_database=database)
    assert checked["ok"]
    # AUTOINCREMENT reservations must survive; nextval cannot reuse a historical id.
    assert db.execute("INSERT INTO pgtest_migration_fixture.items(body) VALUES ('next') RETURNING id").fetchone()[0] == 8
    db.execute("DELETE FROM pgtest_migration_fixture.items WHERE id=8")
    db.execute("UPDATE pgtest_migration_fixture.items SET body = '{\"x\":1}'")
    with pytest.raises(MigrationError, match="verification"):
        verify(db, snap, mapping, target_database=database)
    with pytest.raises(MigrationError, match="conflict"):
        import_snapshot(db, snap, mapping, target_database=database, batch_size=1)


def test_import_rejects_changed_snapshot_and_incomplete_allowlist(tmp_path, pg_connection):
    db, database = pg_connection
    source = make_source(tmp_path)
    snap = snapshot(source, tmp_path / "backup.sqlite3")
    with pytest.raises(MigrationError, match="allowlist"):
        import_snapshot(db, snap, [], target_database=database)
    with sqlite3.connect(snap["snapshot_path"]) as modified:
        modified.execute("UPDATE items SET body='changed'")
    with pytest.raises(MigrationError, match="snapshot changed"):
        import_snapshot(db, snap, [TableMapping("items", "pgtest_migration_fixture", "items", ("id",))], target_database=database)


def test_import_failure_rolls_back_batch_and_checkpoint(tmp_path, pg_connection):
    db, database = pg_connection
    source = make_source(tmp_path)
    with sqlite3.connect(source.path) as sq:
        sq.execute("INSERT INTO items(id,body) VALUES (8,'second')")
    snap = snapshot(source, tmp_path / "backup.sqlite3")
    db.execute("ALTER TABLE pgtest_migration_fixture.items ADD CONSTRAINT reject_second CHECK (id<>8)")
    mapping = [TableMapping("items", "pgtest_migration_fixture", "items", ("id",))]
    with pytest.raises(MigrationError, match="import failed"):
        import_snapshot(db, snap, mapping, target_database=database, batch_size=2)
    assert db.execute("SELECT count(*) FROM pgtest_migration_fixture.items").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM suiteharness_migration.checkpoints").fetchone()[0] == 0
    db.execute("ALTER TABLE pgtest_migration_fixture.items DROP CONSTRAINT reject_second")
    assert import_snapshot(db, snap, mapping, target_database=database, batch_size=2)["ok"]


def test_import_rejects_competing_advisory_lock(tmp_path, pg_connection):
    db, database = pg_connection
    import psycopg

    source = make_source(tmp_path)
    snap = snapshot(source, tmp_path / "backup.sqlite3")
    with psycopg.connect(os.environ["SUITE_MIGRATION_TEST_DSN"], autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(hashtextextended(current_database() || ':suiteharness-migration',0))")
        with pytest.raises(MigrationError, match="another migration"):
            import_snapshot(db, snap, [TableMapping("items", "pgtest_migration_fixture", "items", ("id",))], target_database=database)


def test_rowid_migration_preserves_latest_order_not_primary_key_sort(tmp_path, pg_connection):
    db, database = pg_connection
    root = tmp_path / "data"
    root.mkdir()
    path = root / "assessment.sqlite3"
    with sqlite3.connect(path) as sq:
        sq.executescript("CREATE TABLE assignments(id TEXT PRIMARY KEY, payload TEXT); INSERT INTO assignments(rowid,id,payload) VALUES (19,'z','first'),(35,'a','latest');")
    source = Source(path, "feature_assistant", root, ("test-fixture",))
    snap = snapshot(source, tmp_path / "backup.sqlite3")
    db.execute("CREATE TABLE pgtest_migration_fixture.assignments (id text PRIMARY KEY,payload text,rowid bigint GENERATED BY DEFAULT AS IDENTITY UNIQUE)")
    mapping = [TableMapping("assignments", "pgtest_migration_fixture", "assignments", ("id",), preserve_rowid_as="rowid")]
    import_snapshot(db, snap, mapping, target_database=database)
    assert db.execute("SELECT id FROM pgtest_migration_fixture.assignments ORDER BY rowid DESC LIMIT 1").fetchone()[0] == "a"
    assert db.execute("INSERT INTO pgtest_migration_fixture.assignments(id) VALUES ('next') RETURNING rowid").fetchone()[0] == 36
    db.execute("DELETE FROM pgtest_migration_fixture.assignments WHERE id='next'")
    assert verify(db, snap, mapping, target_database=database)["ok"]
    db.execute("UPDATE pgtest_migration_fixture.assignments SET rowid=100 WHERE id='a'")
    with pytest.raises(MigrationError, match="verification"):
        verify(db, snap, mapping, target_database=database)


def test_generated_columns_are_not_inserted_and_are_verified(tmp_path, pg_connection):
    db, database = pg_connection
    source = make_source(tmp_path)
    with sqlite3.connect(source.path) as sq:
        sq.execute("ALTER TABLE items ADD COLUMN display TEXT GENERATED ALWAYS AS (body || '+08:00') VIRTUAL")
    snap = snapshot(source, tmp_path / "backup.sqlite3")
    assert snap["inventory"]["tables"]["items"]["generated_columns"] == ["display"]
    db.execute("ALTER TABLE pgtest_migration_fixture.items ADD COLUMN display text GENERATED ALWAYS AS (body || '+08:00') STORED")
    mapping = [TableMapping("items", "pgtest_migration_fixture", "items", ("id",))]
    assert import_snapshot(db, snap, mapping, target_database=database)["ok"]
    assert verify(db, snap, mapping, target_database=database)["ok"]
    db.execute("ALTER TABLE pgtest_migration_fixture.items DROP COLUMN display")
    db.execute("ALTER TABLE pgtest_migration_fixture.items ADD COLUMN display text GENERATED ALWAYS AS (body || '+09:00') STORED")
    with pytest.raises(MigrationError, match="verification"):
        verify(db, snap, mapping, target_database=database)


def test_rowid_mapping_rejects_without_rowid_source(tmp_path, pg_connection):
    db, database = pg_connection
    root = tmp_path / "data"
    root.mkdir()
    path = root / "without.sqlite3"
    with sqlite3.connect(path) as sq:
        sq.execute("CREATE TABLE assignments(id TEXT PRIMARY KEY) WITHOUT ROWID")
    snap = snapshot(Source(path, "feature_assistant", root, ("test",)), tmp_path / "backup.sqlite3")
    with pytest.raises(MigrationError, match="rowid"):
        import_snapshot(db, snap, [TableMapping("assignments", "pgtest_migration_fixture", "assignments", ("id",), preserve_rowid_as="rowid")], target_database=database)


@pytest.mark.parametrize("failure,sqlstate,exception_type", [("overflow", "22003", "NumericValueOutOfRange"), ("constraint", "23514", "CheckViolation")])
def test_import_diagnostics_preserve_only_safe_postgresql_metadata(tmp_path, pg_connection, capsys, failure, sqlstate, exception_type):
    import json

    db, database = pg_connection
    source = make_source(tmp_path)
    marker = "synthetic-private-answer-do-not-log"
    with sqlite3.connect(source.path) as sq:
        sq.execute("UPDATE items SET body=?", (marker,))
        if failure == "overflow":
            sq.execute("UPDATE items SET id=2147483648")
    if failure == "overflow":
        db.execute("ALTER TABLE pgtest_migration_fixture.items ALTER COLUMN id TYPE integer")
    else:
        db.execute("ALTER TABLE pgtest_migration_fixture.items ADD CONSTRAINT reject_private_row CHECK(id<>7)")
    snap = snapshot(source, tmp_path / "backup.sqlite3")
    mapping = [TableMapping("items", "pgtest_migration_fixture", "items", ("id",))]
    with pytest.raises(MigrationError) as caught:
        import_snapshot(db, snap, mapping, target_database=database, batch_size=1)
    expected = {"sqlstate": sqlstate, "exception_type": exception_type, "source_table": "items", "target_schema": "pgtest_migration_fixture", "target_table": "items", "batch_offset": 0}
    assert caught.value.diagnostic == expected
    assert marker not in str(caught.value)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(snap))
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"dsn_env": "SUITE_MIGRATION_TEST_DSN", "target_database": database, "manifest": str(manifest), "mappings": [{"source_table": "items", "target_schema": "pgtest_migration_fixture", "target_table": "items", "key_columns": ["id"]}]}))
    report = tmp_path / "failed-report.json"
    assert main(["import", "--spec", str(spec), "--output", str(report)]) == 2
    reported = json.loads(report.read_text())
    assert reported["ok"] is False
    assert reported["diagnostic"] == expected
    output = capsys.readouterr()
    for text in (report.read_text(), output.out, output.err, json.dumps(caught.value.diagnostic)):
        assert marker not in text
        assert "2147483648" not in text
        assert os.environ["SUITE_MIGRATION_TEST_DSN"] not in text
    assert db.execute("SELECT count(*) FROM pgtest_migration_fixture.items").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM suiteharness_migration.checkpoints").fetchone()[0] == 0
