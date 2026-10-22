"""Explicit PostgreSQL migrate/check/versions, without activating products."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from suiteharness.config import SuiteHarnessConfigLoader
from suiteharness.server.storage import create_persistence_runtime


async def manage(loaded, action: str) -> dict:
    if action not in {"migrate", "check", "versions"}:
        raise ValueError("unsupported PostgreSQL administration action")
    if loaded.config.storage.backend != "postgresql":
        raise ValueError("this administration command requires explicit PostgreSQL storage")
    stores = create_persistence_runtime(loaded)
    database = stores.database
    try:
        if action == "migrate":
            await database.migrate()
        await database.open()
        await database.healthcheck()
        versions = await database.run(
            lambda connection: [
                {"component": row["component"], "version": row["version"]}
                for row in connection.execute("SELECT component,version FROM suiteharness_schema_versions ORDER BY component").fetchall()
            ], write=False,
        )
        actual = await database.run(lambda connection: connection.execute("SELECT current_database()").fetchone()[0], write=False)
        return {"ok": True, "action": action, "database": actual,
                "schema": loaded.config.storage.postgres.schema_name, "versions": versions,
                "product_services_started": False}
    finally:
        await database.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("migrate", "check", "versions"))
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--secrets", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        loaded = SuiteHarnessConfigLoader().load(args.config, args.secrets)
        print(json.dumps(asyncio.run(manage(loaded, args.action)), ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"ok": False, "action": args.action, "error_type": type(error).__name__,
                          "message": "PostgreSQL administration failed; check credentials, schema compatibility and database availability locally"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
