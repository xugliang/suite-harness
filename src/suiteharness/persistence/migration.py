"""Explicit, offline SQLite migration. Never imports a product or starts its workers.

All target tables must already exist. Identifiers come from a reviewed mapping,
never from model/tool arguments. Data and resume checkpoints share a transaction.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class MigrationError(RuntimeError):
    """A fail-closed migration validation error; does not contain row contents."""

    def __init__(self, message: str, *, diagnostic: dict[str, Any] | None = None):
        super().__init__(message)
        self.diagnostic = dict(diagnostic or {})


@dataclass(frozen=True)
class Source:
    path: Path
    product: str
    owner_root: Path
    writers: tuple[str, ...]


@dataclass(frozen=True)
class TableMapping:
    source_table: str
    target_schema: str
    target_table: str
    key_columns: tuple[str, ...]
    # Empty means identical columns. Otherwise enumerate EVERY source column.
    columns: tuple[tuple[str, str], ...] = ()
    preserve_rowid_as: str | None = None


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value):
        raise MigrationError("invalid migration identifier")
    return value


def _owned(source: Source) -> Path:
    _identifier(source.product)
    root = Path(source.owner_root).absolute()
    path = Path(source.path).absolute()
    if root == Path(root.anchor) or not source.writers:
        raise MigrationError("source ownership and writers must be explicit")
    # A link at any component makes lexical ownership ambiguous.
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise MigrationError("source symlink is not permitted")
    if not path.resolve().is_relative_to(root.resolve()) or path.resolve() == root.resolve():
        raise MigrationError("source ownership root mismatch")
    if not path.is_file():
        raise MigrationError("source database is not a regular file")
    return path.resolve()


def _hash(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(part)
    return result.hexdigest()


def _fingerprint(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise MigrationError("fingerprint requires regular non-symlink file")
    stat = path.stat()
    digest = _hash(path)
    after = path.stat()
    if (stat.st_ino, stat.st_size, stat.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise MigrationError("file changed while fingerprinting")
    return {"path": str(path), "sha256": digest, "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns, "inode": stat.st_ino, "device": stat.st_dev}


def _sqlite(path: Path):
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _cell(value: Any) -> list[Any]:
    if value is None:
        return ["null"]
    if isinstance(value, bytes | memoryview):
        return ["blob", base64.b64encode(bytes(value)).decode("ascii")]
    if isinstance(value, bool):
        return ["integer", int(value)]
    if isinstance(value, int):
        return ["integer", str(value)]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MigrationError("non-finite numeric value cannot be migrated losslessly")
        return ["real", value.hex()]
    if isinstance(value, str):
        return ["text", value]
    raise MigrationError("unsupported database value type")


def _row_bytes(row: Iterable[Any]) -> bytes:
    return json.dumps([_cell(v) for v in row], ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _rows_digest(rows: Iterable[Iterable[Any]]) -> str:
    # Sorted row hashes preserve multiplicity and are independent of DB collation.
    hashes = sorted(hashlib.sha256(_row_bytes(row)).digest() for row in rows)
    return hashlib.sha256(b"".join(hashes)).hexdigest()


def _table_info(db, table: str) -> dict[str, Any]:
    info = db.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    columns = [row[1] for row in info]
    keys = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
    selection = ",".join(_quote(c) for c in columns)
    extended = db.execute(f"PRAGMA table_xinfo({_quote(table)})").fetchall()
    generated = [row[1] for row in extended if row[6] in (2, 3)]
    rowid_alias = None
    for alias in ("rowid", "_rowid_", "oid"):
        if alias in {c.lower() for c in columns + generated}:
            continue
        try:
            db.execute(f"SELECT {alias} FROM {_quote(table)} LIMIT 0")
            rowid_alias = alias
            break
        except sqlite3.OperationalError:
            continue
    rows = db.execute(f"SELECT {selection} FROM {_quote(table)}")
    return {"columns": columns, "primary_key": keys,
            "count": db.execute(f"SELECT count(*) FROM {_quote(table)}").fetchone()[0],
            "digest": _rows_digest(rows),
            "generated_columns": generated,
            "generated_digest": _rows_digest(db.execute(f"SELECT {','.join(_quote(c) for c in columns + generated)} FROM {_quote(table)}")),
            "schema_sql": db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0],
            "rowid_alias": rowid_alias,
            "rowid_max": db.execute(f"SELECT max({rowid_alias}) FROM {_quote(table)}").fetchone()[0] if rowid_alias else None,
            "rowid_digest": _rows_digest(db.execute(f"SELECT {rowid_alias},{selection} FROM {_quote(table)}")) if rowid_alias else None,
            "schema": [{"name": r[1], "type": r[2], "not_null": bool(r[3]),
                        "default": r[4], "pk_position": r[5]} for r in info]}


def _inspect(path: Path) -> dict[str, Any]:
    with _sqlite(path) as db:
        db.execute("BEGIN")
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise MigrationError("SQLite integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise MigrationError("SQLite foreign key check failed")
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        tables = {name: _table_info(db, name) for name in names}
        sequences = {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
            sequences = dict(db.execute("SELECT name, seq FROM sqlite_sequence"))
        raw = json.dumps({"tables": tables, "sequences": sequences},
                         sort_keys=True, ensure_ascii=False).encode("utf-8")
        return {"tables": tables, "sequences": sequences,
                "user_version": db.execute("PRAGMA user_version").fetchone()[0],
                "sqlite_version": sqlite3.sqlite_version,
                "logical_digest": hashlib.sha256(raw).hexdigest()}


def inventory(source: Source) -> dict[str, Any]:
    path = _owned(source)
    inspected = _inspect(path)
    files = [path, *(Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal"))]
    return {"path": str(path), "owner_root": str(Path(source.owner_root).resolve()),
            "product": source.product, "writers": list(source.writers),
            "files": [_fingerprint(p) for p in files if p.exists()], **inspected}


def snapshot(source: Source, destination: Path) -> dict[str, Any]:
    path = _owned(source)
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink() or destination == path:
        raise MigrationError("snapshot destination must be a new file")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for candidate in (destination.parent, *destination.parent.parents):
        if candidate.is_symlink():
            raise MigrationError("snapshot destination symlink is not permitted")
    # Reserve without following links and keep contents private from creation.
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with _sqlite(path) as origin, sqlite3.connect(destination) as backup:
            origin.backup(backup)
        inspected = _inspect(destination)
        return {"format_version": 1, "source": {"path": str(path),
                "owner_root": str(Path(source.owner_root).resolve()),
                "product": source.product, "writers": list(source.writers)},
                "snapshot_path": str(destination.resolve()),
                "snapshot_sha256": _hash(destination), "inventory": inspected}
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _validated(manifest: dict[str, Any], mappings: list[TableMapping]) -> tuple[Path, dict]:
    path = Path(manifest["snapshot_path"])
    if path.is_symlink() or not path.is_file() or _hash(path) != manifest["snapshot_sha256"]:
        raise MigrationError("snapshot changed since export")
    current = _inspect(path)
    if current["logical_digest"] != manifest["inventory"]["logical_digest"]:
        raise MigrationError("snapshot inventory mismatch")
    if {m.source_table for m in mappings} != set(current["tables"]) or len(mappings) != len(current["tables"]):
        raise MigrationError("mapping allowlist must cover each source table exactly once")
    targets = set()
    for mapping in mappings:
        for name in (mapping.source_table, mapping.target_schema, mapping.target_table, *mapping.key_columns):
            _identifier(name)
        if mapping.target_schema in {"pg_catalog", "information_schema", "suiteharness_migration"} or mapping.target_schema.startswith("pg_"):
            raise MigrationError("mapping targets a protected schema")
        target = (mapping.target_schema, mapping.target_table)
        if target in targets:
            raise MigrationError("mapping target duplicated")
        targets.add(target)
        info = current["tables"][mapping.source_table]
        columns = list(mapping.columns) or [(c, c) for c in info["columns"]]
        if mapping.preserve_rowid_as:
            _identifier(mapping.preserve_rowid_as)
            if not info["rowid_alias"]:
                raise MigrationError("source rowid unavailable or ambiguous")
            if mapping.preserve_rowid_as in {c[1] for c in columns} | set(info["generated_columns"]):
                raise MigrationError("source rowid target conflicts with a mapped column")
        if {c[0] for c in columns} != set(info["columns"]) or len(columns) != len(info["columns"]):
            raise MigrationError("column allowlist must preserve every source column")
        if not mapping.key_columns or not set(mapping.key_columns) <= set(info["columns"]):
            raise MigrationError("mapping requires explicit conflict-detection keys")
        if len({c[1] for c in columns}) != len(columns):
            raise MigrationError("mapping target column duplicated")
        for pair in columns:
            for name in pair:
                _identifier(name)
    return path, current


def _binding(db, manifest, mappings, target_database):
    actual = db.execute("SELECT current_database()").fetchone()[0]
    if actual != target_database:
        raise MigrationError("target database mismatch")
    # Database OID + server endpoint distinguish equal names on different servers.
    identity = db.execute("SELECT oid::text, coalesce(inet_server_addr()::text,'local'), inet_server_port() FROM pg_database WHERE datname=current_database()").fetchone()
    binding = {"format_version": 1, "snapshot_sha256": manifest["snapshot_sha256"],
               "target_database": target_database, "target_identity": list(identity),
               "product": manifest["source"]["product"],
               "mapping": [asdict(m) for m in mappings]}
    encoded = json.dumps(binding, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest(), encoded


def _target_columns(db, mapping, info):
    from psycopg import sql

    pairs = list(mapping.columns) or [(c, c) for c in info["columns"]]
    pairs = sorted(pairs, key=lambda pair: info["columns"].index(pair[0]))
    if mapping.preserve_rowid_as:
        pairs.append((info["rowid_alias"], mapping.preserve_rowid_as))
    available = dict(db.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema=%s AND table_name=%s", (mapping.target_schema, mapping.target_table)).fetchall())
    if not available or any(target not in available for _, target in pairs):
        raise MigrationError("target table/columns not provisioned")
    if any(available[target] in {"json", "jsonb"} for _, target in pairs):
        raise MigrationError("JSON original bytes require a text target column")
    table = sql.Identifier(mapping.target_schema, mapping.target_table)
    return pairs, available, table


def _source_selection(info, mapping, *, generated=False):
    columns = [_quote(c) for c in info["columns"]]
    if mapping.preserve_rowid_as:
        columns.append(info["rowid_alias"])
    if generated:
        columns.extend(_quote(c) for c in info["generated_columns"])
    return ",".join(columns)


def _adapt(row, pairs, types):
    values = []
    for value, (_, target) in zip(row, pairs, strict=True):
        if types[target] == "boolean" and value is not None:
            if value not in (0, 1):
                raise MigrationError("invalid SQLite boolean")
            value = bool(value)
        values.append(value)
    return values


def _sequence_floor(db, mapping, source, info):
    """ALTER SEQUENCE is transactional; setval is deliberately not used."""
    from psycopg import sql

    pairs = dict(mapping.columns) if mapping.columns else {c: c for c in info["columns"]}
    targets = []
    if len(mapping.key_columns) == 1:
        targets.append((pairs[mapping.key_columns[0]], source["sequences"].get(mapping.source_table, 0), mapping.source_table in source["sequences"]))
    if mapping.preserve_rowid_as:
        targets.append((mapping.preserve_rowid_as, info["rowid_max"] or 0, True))
    qualified = sql.Identifier(mapping.target_schema, mapping.target_table).as_string(db)
    for column, historical, required in targets:
        seq = db.execute("SELECT pg_get_serial_sequence(%s,%s)", (qualified, column)).fetchone()[0]
        if seq is None:
            if required:
                raise MigrationError("source AUTOINCREMENT/rowid requires a target identity/sequence")
            continue
        schema, name = db.execute("SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid=%s::regclass", (seq,)).fetchone()
        maximum = db.execute(sql.SQL("SELECT max({}) FROM {}").format(sql.Identifier(column), sql.Identifier(mapping.target_schema, mapping.target_table))).fetchone()[0] or 0
        last, called = db.execute(sql.SQL("SELECT last_value,is_called FROM {}").format(sql.Identifier(schema, name))).fetchone()
        next_value = max(int(maximum) + 1, int(historical) + 1, int(last) + int(called))
        db.execute(sql.SQL("ALTER SEQUENCE {} RESTART WITH {}").format(sql.Identifier(schema, name), sql.Literal(next_value)))


def import_snapshot(db, manifest: dict, mappings: list[TableMapping], *,
                    target_database: str, batch_size: int = 500) -> dict:
    from psycopg import sql

    if not 1 <= batch_size <= 10000:
        raise MigrationError("batch_size must be between 1 and 10000")
    path, source = _validated(manifest, mappings)
    if not db.autocommit:
        raise MigrationError("migration requires an autocommit connection with explicit transactions")
    migration_id, binding = _binding(db, manifest, mappings, target_database)
    locked = db.execute("SELECT pg_try_advisory_lock(hashtextextended(current_database() || ':suiteharness-migration',0))").fetchone()[0]
    if not locked:
        raise MigrationError("another migration owns this target database")
    context = {"source_table": None, "target_schema": None,
               "target_table": None, "batch_offset": None}
    try:
        with db.transaction():
            db.execute("CREATE SCHEMA IF NOT EXISTS suiteharness_migration")
            db.execute("CREATE TABLE IF NOT EXISTS suiteharness_migration.runs (migration_id text PRIMARY KEY, binding text NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS suiteharness_migration.checkpoints (migration_id text NOT NULL REFERENCES suiteharness_migration.runs(migration_id), source_table text NOT NULL, rows_completed bigint NOT NULL, PRIMARY KEY(migration_id,source_table))")
            db.execute("INSERT INTO suiteharness_migration.runs VALUES (%s,%s) ON CONFLICT DO NOTHING", (migration_id, binding))
            if db.execute("SELECT binding FROM suiteharness_migration.runs WHERE migration_id=%s", (migration_id,)).fetchone()[0] != binding:
                raise MigrationError("migration binding conflict")
        with _sqlite(path) as origin:
            for mapping in mappings:
                context.update(source_table=mapping.source_table,
                               target_schema=mapping.target_schema,
                               target_table=mapping.target_table, batch_offset=None)
                info = source["tables"][mapping.source_table]
                pairs, types, target = _target_columns(db, mapping, info)
                positions = {name: i for i, name in enumerate(info["columns"])}
                target_names = {a: b for a, b in pairs}
                columns_sql = sql.SQL(",").join(sql.Identifier(b) for _, b in pairs)
                condition = sql.SQL(" AND ").join(sql.SQL("{} IS NOT DISTINCT FROM %s").format(sql.Identifier(target_names[key])) for key in mapping.key_columns)
                selected = _source_selection(info, mapping)
                ordered = ",".join(_quote(c) for c in mapping.key_columns)
                # Explicit keys must actually be unique and non-null in this snapshot.
                if origin.execute(f"SELECT 1 FROM {_quote(mapping.source_table)} GROUP BY {ordered} HAVING count(*)>1 LIMIT 1").fetchone():
                    raise MigrationError("source conflict-detection keys are not unique")
                null_condition = " OR ".join(_quote(k) + " IS NULL" for k in mapping.key_columns)
                if origin.execute(f"SELECT 1 FROM {_quote(mapping.source_table)} WHERE {null_condition} LIMIT 1").fetchone():
                    raise MigrationError("source conflict-detection keys contain NULL")
                previous = db.execute("SELECT rows_completed FROM suiteharness_migration.checkpoints WHERE migration_id=%s AND source_table=%s", (migration_id, mapping.source_table)).fetchone()
                completed = previous[0] if previous else 0
                if completed > info["count"]:
                    raise MigrationError("checkpoint exceeds source rows")
                # Validate already committed rows on resume; never trust a stale checkpoint.
                for offset in range(0, info["count"] or 1, batch_size):
                    context["batch_offset"] = offset
                    rows = origin.execute(f"SELECT {selected} FROM {_quote(mapping.source_table)} ORDER BY {ordered} LIMIT ? OFFSET ?", (batch_size, offset)).fetchall()
                    with db.transaction():
                        for index, row in enumerate(rows, start=offset):
                            values = _adapt(row, pairs, types)
                            keys = [values[positions[key]] for key in mapping.key_columns]
                            existing = db.execute(sql.SQL("SELECT {} FROM {} WHERE {}").format(columns_sql, target, condition), keys).fetchall()
                            if len(existing) > 1:
                                raise MigrationError("target conflict-detection keys are not unique")
                            if existing:
                                if _row_bytes(existing[0]) != _row_bytes(values):
                                    raise MigrationError("target record conflict; no rows overwritten")
                            elif index < completed:
                                raise MigrationError("checkpoint row missing from target")
                            else:
                                placeholders = sql.SQL(",").join(sql.Placeholder() for _ in values)
                                db.execute(sql.SQL("INSERT INTO {} ({}) OVERRIDING SYSTEM VALUE VALUES ({})").format(target, columns_sql, placeholders), values)
                        done = offset + len(rows)
                        db.execute("INSERT INTO suiteharness_migration.checkpoints VALUES (%s,%s,%s) ON CONFLICT (migration_id,source_table) DO UPDATE SET rows_completed=EXCLUDED.rows_completed", (migration_id, mapping.source_table, done))
                        if done == info["count"]:
                            _sequence_floor(db, mapping, source, info)
        # Recheck physical hash: all batches must refer to the exact exported bytes.
        if _hash(path) != manifest["snapshot_sha256"]:
            raise MigrationError("snapshot changed during import; verification required")
        return {"ok": True, "migration_id": migration_id,
                "snapshot_sha256": manifest["snapshot_sha256"], "target_database": target_database}
    except MigrationError:
        raise
    except Exception as exc:
        # PostgreSQL error messages/diag.detail may contain an entire rejected
        # chat, answer or customer row. Only retain strict metadata, never SQL,
        # params, exception text, connection objects or DSNs.
        sqlstate = getattr(exc, "sqlstate", None)
        if not isinstance(sqlstate, str) or not re.fullmatch(r"[A-Z0-9]{5}", sqlstate):
            sqlstate = None
        exception_type = type(exc).__name__
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", exception_type):
            exception_type = "Exception"
        diagnostic = {"sqlstate": sqlstate, "exception_type": exception_type, **context}
        raise MigrationError("PostgreSQL import failed; current batch rolled back; inspect restricted database logs", diagnostic=diagnostic) from None
    finally:
        db.execute("SELECT pg_advisory_unlock(hashtextextended(current_database() || ':suiteharness-migration',0))")


def verify(db, manifest: dict, mappings: list[TableMapping], *, target_database: str) -> dict:
    from psycopg import sql

    path, source = _validated(manifest, mappings)
    migration_id, _ = _binding(db, manifest, mappings, target_database)
    result = {}
    # Repeatable read guarantees counts and row digests observe one target snapshot.
    with db.transaction():
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        with _sqlite(path) as origin:
            for mapping in mappings:
                info = source["tables"][mapping.source_table]
                pairs, types, target = _target_columns(db, mapping, info)
                for column in info["generated_columns"]:
                    if column not in types:
                        raise MigrationError("target generated column not provisioned")
                    pairs.append((column, column))
                rows = origin.execute("SELECT " + _source_selection(info, mapping, generated=True) + " FROM " + _quote(mapping.source_table))
                expected = _rows_digest(_adapt(row, pairs, types) for row in rows)
                count = db.execute(sql.SQL("SELECT count(*) FROM {}").format(target)).fetchone()[0]
                # Server-side cursor bounds raw payload memory; digest sorting only
                # retains one 32-byte hash per row, not whole chat/tool payloads.
                with db.cursor(name="suite_migration_verify") as cursor:
                    cursor.execute(sql.SQL("SELECT {} FROM {}").format(sql.SQL(",").join(sql.Identifier(b) for _, b in pairs), target))
                    actual = _rows_digest(cursor)
                if info["count"] != count or expected != actual:
                    raise MigrationError("target verification failed: row count/key/value digest mismatch")
                result[mapping.source_table] = {"count": count, "digest": actual}
    return {"ok": True, "migration_id": migration_id, "target_database": target_database,
            "snapshot_sha256": manifest["snapshot_sha256"],
            "source_digest": source["logical_digest"], "tables": result}


_CONDITIONS = ("writers_stopped", "consumers_postgresql", "business_regression_passed",
               "postgres_backup_restored", "sqlite_backup_restored", "config_references_checked")


def _evidence(evidence: dict):
    for condition in _CONDITIONS:
        if evidence.get(condition) is not True:
            raise MigrationError("cleanup condition missing: " + condition)
    if evidence.get("unclassified_paths") != [] or not evidence.get("evidence_reference"):
        raise MigrationError("cleanup requires a complete ownership/evidence report")


def _no_open_files(paths: set[Path]):
    # Fail closed outside Linux: a portable local scan cannot prove absence of users.
    proc = Path("/proc")
    if not (proc / "self" / "fd").is_dir():
        raise MigrationError("cleanup open-file inspection requires Linux /proc")
    identities = {(path.stat().st_dev, path.stat().st_ino) for path in paths}
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            descriptors = list((process / "fd").iterdir())
        except FileNotFoundError:
            continue
        except PermissionError:
            raise MigrationError("cannot inspect process open files") from None
        for descriptor in descriptors:
            try:
                raw = os.readlink(descriptor)
                identity = descriptor.stat()
            except FileNotFoundError:
                continue
            except PermissionError:
                raise MigrationError("cannot inspect process file descriptor") from None
            if (identity.st_dev, identity.st_ino) in identities or Path(raw.removesuffix(" (deleted)")).absolute() in paths:
                raise MigrationError("SQLite files are still open by a process")


def cleanup_plan(source: Source, manifest: dict, verification: dict, evidence: dict) -> dict:
    _evidence(evidence)
    path = _owned(source)
    if str(path) != manifest["source"]["path"] or source.product != manifest["source"]["product"]:
        raise MigrationError("cleanup source ownership mismatch")
    snapshot_path = Path(manifest["snapshot_path"])
    if _hash(snapshot_path) != manifest["snapshot_sha256"]:
        raise MigrationError("cleanup backup changed")
    live = inventory(source)
    if live["logical_digest"] != manifest["inventory"]["logical_digest"]:
        raise MigrationError("live SQLite changed after snapshot")
    if verification.get("ok") is not True or verification.get("snapshot_sha256") != manifest["snapshot_sha256"] or verification.get("source_digest") != live["logical_digest"]:
        raise MigrationError("cleanup requires matching successful PostgreSQL verification")
    return {"format_version": 1, "source": manifest["source"],
            "files": live["files"], "source_digest": live["logical_digest"],
            "snapshot_path": str(snapshot_path), "snapshot_sha256": manifest["snapshot_sha256"],
            "verification": verification, "evidence": evidence}


def cleanup(plan: dict, *, evidence: dict, db, manifest: dict,
            mappings: list[TableMapping], target_database: str) -> dict:
    """Delete only exact fingerprints after a fresh external consumer-stop check.

    Evidence is an operator assertion referencing a real report, not auto-detected
    proof of scheduler/config state. Callers must keep writers stopped throughout.
    """
    _evidence(evidence)
    _evidence(plan["evidence"])
    source = Source(Path(plan["source"]["path"]), plan["source"]["product"],
                    Path(plan["source"]["owner_root"]), tuple(plan["source"]["writers"]))
    path = _owned(source)
    allowed = {path, *(Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal"))}
    paths = {Path(entry["path"]) for entry in plan["files"]}
    if path not in paths or not paths <= allowed:
        raise MigrationError("cleanup path allowlist mismatch")
    actual_files = {p for p in allowed if p.exists()}
    if actual_files != paths:
        raise MigrationError("SQLite sidecar set changed")
    if _hash(Path(plan["snapshot_path"])) != plan["snapshot_sha256"]:
        raise MigrationError("cleanup backup changed")
    verification = plan["verification"]
    if verification.get("ok") is not True or verification.get("source_digest") != plan["source_digest"] or verification.get("snapshot_sha256") != plan["snapshot_sha256"]:
        raise MigrationError("cleanup verification evidence mismatch")
    if manifest["snapshot_sha256"] != plan["snapshot_sha256"]:
        raise MigrationError("cleanup manifest mismatch")
    current_verification = verify(db, manifest, mappings, target_database=target_database)
    if current_verification != verification:
        raise MigrationError("cleanup fresh PostgreSQL verification differs from plan")
    for entry in plan["files"]:
        if _fingerprint(Path(entry["path"])) != entry:
            raise MigrationError("SQLite file changed since cleanup plan")
    _no_open_files(paths)
    # Recheck all fingerprints immediately before deletion. This is not a service
    # stop mechanism; externally enforced writer isolation is required.
    for entry in plan["files"]:
        if _fingerprint(Path(entry["path"])) != entry:
            raise MigrationError("SQLite file changed during cleanup check")
    deleted = []
    for entry in plan["files"]:
        Path(entry["path"]).unlink()
        deleted.append(entry["path"])
    return {"deleted": deleted, "retained_backup": plan["snapshot_path"]}


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["inventory", "snapshot", "import", "verify", "preflight", "cleanup-plan", "cleanup"])
    parser.add_argument("--spec", required=True, help="private JSON operator specification")
    parser.add_argument("--output", required=True, help="new private JSON report (never stdout contents)")
    args = parser.parse_args(argv)
    output_fd = None
    try:
        # Reserve the report before any snapshot/import/cleanup side effect.
        output_fd = os.open(Path(args.output), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(output_fd, b'{"ok":false,"status":"operation_started"}\n')
        spec = _load(args.spec)
        operation = args.operation
        if operation in {"inventory", "snapshot", "cleanup-plan"}:
            item = spec["source"]
            source = Source(Path(item["path"]), item["product"], Path(item["owner_root"]), tuple(item["writers"]))
        if operation == "inventory":
            report = inventory(source)
        elif operation == "snapshot":
            report = snapshot(source, Path(spec["destination"]))
        elif operation == "cleanup-plan":
            report = cleanup_plan(source, _load(spec["manifest"]), _load(spec["verification"]), spec["evidence"])
        else:
            import psycopg

            dsn = os.environ.get(spec.get("dsn_env", "SUITE_MIGRATION_DSN"))
            if not dsn:
                raise MigrationError("DSN environment variable is not configured")
            with psycopg.connect(dsn, autocommit=True, connect_timeout=10,
                                 options="-c statement_timeout=60000 -c lock_timeout=5000 -c idle_in_transaction_session_timeout=60000") as db:
                if operation == "preflight":
                    actual, version = db.execute("SELECT current_database(), current_setting('server_version_num')::int").fetchone()
                    if actual != spec["target_database"] or version < 140000:
                        raise MigrationError("preflight target/version mismatch")
                    report = {"ok": True, "target_database": actual, "server_version_num": version,
                              "free_space_checked": False, "role_grants_checked": False}
                else:
                    mappings = [TableMapping(m["source_table"], m["target_schema"], m["target_table"], tuple(m["key_columns"]), tuple(tuple(p) for p in m.get("columns", [])), m.get("preserve_rowid_as")) for m in spec["mappings"]]
                    if operation == "cleanup":
                        report = cleanup(_load(spec["plan"]), evidence=spec["evidence"], db=db, manifest=_load(spec["manifest"]), mappings=mappings, target_database=spec["target_database"])
                    else:
                        method = import_snapshot if operation == "import" else verify
                        report = method(db, _load(spec["manifest"]), mappings, target_database=spec["target_database"])
        encoded = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        os.lseek(output_fd, 0, os.SEEK_SET)
        os.ftruncate(output_fd, 0)
        os.write(output_fd, encoded)
        os.fsync(output_fd)
        print("Migration report written.")
        return 0
    except (MigrationError, OSError, KeyError, ValueError) as exc:
        # Never emit connection exceptions, SQL parameters, row contents or DSNs.
        if isinstance(exc, MigrationError) and exc.diagnostic:
            failed = {"ok": False, "error_code": "migration_import_failed", "diagnostic": exc.diagnostic}
            encoded = (json.dumps(failed, ensure_ascii=False, indent=2) + "\n").encode()
            if output_fd is not None:
                os.lseek(output_fd, 0, os.SEEK_SET)
                os.ftruncate(output_fd, 0)
                os.write(output_fd, encoded)
                os.fsync(output_fd)
            print(json.dumps(failed, ensure_ascii=False), file=sys.stderr)
        else:
            print(str(exc) if isinstance(exc, MigrationError) else "Migration input/output failed; inspect private specification and file permissions.", file=sys.stderr)
        return 2
    except Exception:
        print("Migration failed; inspect restricted database logs.", file=sys.stderr)
        return 2
    finally:
        if output_fd is not None:
            os.close(output_fd)


if __name__ == "__main__":
    raise SystemExit(main())
