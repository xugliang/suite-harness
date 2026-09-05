from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from suiteharness.plugins import (
    AllowlistedTrustPolicy,
    PluginDiscovery,
    PluginSourceDeclaration,
    TrustMode,
    deterministic_directory_digest,
)


def make_plugin(
    root: Path,
    plugin_id: str,
    *,
    version: str = "1.0.0",
    trust_mode: TrustMode = TrustMode.TRUSTED_IN_PROCESS,
    provides: list[dict[str, Any]] | None = None,
    requires: list[dict[str, Any]] | None = None,
    contributions: dict[str, list[dict[str, Any]]] | None = None,
    permissions: list[str] | None = None,
    config_schema: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    module_source: str = "VALUE = 1\n",
    extra_manifest: dict[str, Any] | None = None,
) -> tuple[PluginSourceDeclaration, str]:
    source = root / plugin_id.replace("/", "_")
    source.mkdir()
    (source / "plugin.py").write_text(module_source, encoding="utf-8")
    digest = deterministic_directory_digest(source)
    document: dict[str, Any] = {
        "schema_version": "1",
        "plugin_id": plugin_id,
        "version": version,
        "harness_api": ">=0.1,<1",
        "trust_mode": trust_mode.value,
        "entrypoint": "plugin:create",
        "artifact": {"digest": digest, "signature": None},
        "provides": provides or [],
        "requires": requires or [],
        "contributions": contributions or {},
        "permissions": {"values": permissions or []},
        "config_schema": config_schema
        or {"type": "object", "additionalProperties": False},
    }
    if extra_manifest:
        document.update(extra_manifest)
    (source / "suiteharness-plugin.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    return (
        PluginSourceDeclaration(
            path=source,
            expected_digest=digest,
            config=config or {},
        ),
        digest,
    )


def discovery_for(
    root: Path,
    artifacts: list[tuple[PluginSourceDeclaration, str]],
    *,
    trusted: set[str] | None = None,
) -> PluginDiscovery:
    allowlist: dict[str, frozenset[str]] = {}
    for declaration, digest in artifacts:
        document = json.loads((declaration.path / "suiteharness-plugin.json").read_text("utf-8"))
        plugin_id = document["plugin_id"]
        allowlist[plugin_id] = allowlist.get(plugin_id, frozenset()).union({digest})
    return PluginDiscovery(
        allowed_roots=[root],
        trust_policy=AllowlistedTrustPolicy(
            allowed_digests=allowlist,
            trusted_in_process_plugins=frozenset(trusted or set()),
        ),
    )
