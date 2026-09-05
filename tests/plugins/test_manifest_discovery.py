from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from suiteharness.plugins import (
    AllowlistedTrustPolicy,
    ArtifactIntegrity,
    Contributions,
    PermissionSet,
    PluginDiscovery,
    PluginDiscoveryError,
    PluginErrorCode,
    PluginManifest,
    PluginSourceDeclaration,
    PythonEntrypointLoader,
    SignatureMetadata,
    TrustMode,
    deterministic_directory_digest,
)
from tests.plugins._helpers import discovery_for, make_plugin


def test_manifest_is_strict_and_root_authority_is_not_a_permission() -> None:
    base = {
        "schema_version": "1",
        "plugin_id": "acme.memory",
        "version": "1.0.0",
        "harness_api": ">=0.1,<1",
        "trust_mode": "isolated_worker",
        "entrypoint": "plugin:create",
        "artifact": {"digest": "sha256:" + "0" * 64},
    }
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PluginManifest.model_validate({**base, "mystery": True})
    with pytest.raises(ValidationError, match="runner.root"):
        PluginManifest.model_validate(
            {**base, "permissions": {"values": ["runner.root"]}}
        )

    manifest = PluginManifest(
        schema_version="1",
        plugin_id="acme.memory",
        version="1.0.0",
        harness_api=">=0.1,<1",
        trust_mode=TrustMode.ISOLATED_WORKER,
        entrypoint="plugin:create",
        artifact=ArtifactIntegrity(digest="sha256:" + "0" * 64),
        contributions=Contributions(),
        permissions=PermissionSet(),
    )
    assert manifest.model_config["extra"] == "forbid"

    with pytest.raises(ValidationError, match="outside the plugin permission set"):
        PluginManifest.model_validate(
            {
                **base,
                "contributions": {
                    "tools": [
                        {
                            "contribution_id": "lookup",
                            "permissions": {"values": ["network.egress"]},
                        }
                    ]
                },
            }
        )


def test_discovery_does_not_import_and_detects_tampering(tmp_path: Path) -> None:
    sentinel = tmp_path / "imported.txt"
    artifact = make_plugin(
        tmp_path,
        "acme.safe",
        module_source=(
            "from pathlib import Path\n"
            f"Path({str(sentinel)!r}).write_text('imported', encoding='utf-8')\n"
        ),
    )
    discovery = discovery_for(tmp_path, [artifact], trusted={"acme.safe"})
    source = discovery.discover(artifact[0])
    assert not sentinel.exists()

    (source.root / "plugin.py").write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(PluginDiscoveryError) as captured:
        discovery.assert_current(source)
    assert captured.value.code is PluginErrorCode.DIGEST_MISMATCH
    assert not sentinel.exists()


def test_config_schema_and_exact_allowlist_are_enforced(tmp_path: Path) -> None:
    artifact = make_plugin(
        tmp_path,
        "acme.config",
        config_schema={
            "type": "object",
            "properties": {"endpoint": {"type": "string"}},
            "required": ["endpoint"],
            "additionalProperties": False,
        },
        config={"endpoint": 7},
    )
    discovery = discovery_for(tmp_path, [artifact], trusted={"acme.config"})
    with pytest.raises(PluginDiscoveryError) as captured:
        discovery.discover(artifact[0])
    assert captured.value.code is PluginErrorCode.INVALID_MANIFEST

    wrong = PluginSourceDeclaration(
        path=artifact[0].path,
        expected_digest="sha256:" + "f" * 64,
        config={"endpoint": "https://example.invalid"},
    )
    with pytest.raises(PluginDiscoveryError) as captured:
        discovery.discover(wrong)
    assert captured.value.code is PluginErrorCode.DIGEST_MISMATCH


@pytest.mark.parametrize(
    "config_schema",
    (
        {"$ref": "#"},
        {"type": "string", "pattern": "(a+)+$"},
        {"type": "object", "patternProperties": {".*": {}}},
        {"type": "array", "items": {}, "uniqueItems": True},
        {"type": "array", "contains": {"const": "x"}},
    ),
)
def test_trusted_plugin_config_schema_uses_the_bounded_untrusted_subset(
    tmp_path: Path,
    config_schema: dict[str, object],
) -> None:
    artifact = make_plugin(
        tmp_path,
        "acme.unsafe-schema",
        config_schema=config_schema,
    )
    discovery = discovery_for(tmp_path, [artifact], trusted={"acme.unsafe-schema"})
    with pytest.raises(PluginDiscoveryError) as captured:
        discovery.discover(artifact[0])
    assert captured.value.code is PluginErrorCode.INVALID_MANIFEST


def test_plugin_schema_is_not_executed_before_artifact_trust(tmp_path: Path) -> None:
    artifact = make_plugin(
        tmp_path,
        "acme.untrusted-schema",
        config_schema={"$ref": "#"},
    )
    discovery = PluginDiscovery(
        allowed_roots=[tmp_path],
        trust_policy=AllowlistedTrustPolicy(allowed_digests={}),
    )
    with pytest.raises(PluginDiscoveryError) as captured:
        discovery.discover(artifact[0])
    assert captured.value.code is PluginErrorCode.TRUST_REJECTED


def test_verified_plugin_manifest_contains_the_hardened_schema_copy(tmp_path: Path) -> None:
    schema = {
        "type": "object",
        "properties": {
            "values": {
                "type": "array",
                "items": {"type": "integer"},
                "maxItems": 100_000,
            }
        },
    }
    artifact = make_plugin(
        tmp_path,
        "acme.bounded-schema",
        config_schema=schema,
        config={"values": [1]},
    )
    discovery = discovery_for(tmp_path, [artifact], trusted={"acme.bounded-schema"})
    source = discovery.discover(artifact[0])
    values = source.manifest.config_schema["properties"]["values"]
    assert values["maxItems"] == 4_096
    assert schema["properties"]["values"]["maxItems"] == 100_000


def test_manifest_change_after_discovery_is_rejected(tmp_path: Path) -> None:
    artifact = make_plugin(tmp_path, "acme.manifest")
    discovery = discovery_for(tmp_path, [artifact], trusted={"acme.manifest"})
    source = discovery.discover(artifact[0])
    document = json.loads(source.manifest_path.read_text("utf-8"))
    document["entrypoint"] = "plugin:changed"
    source.manifest_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(PluginDiscoveryError) as captured:
        discovery.assert_current(source)
    assert captured.value.code is PluginErrorCode.INVALID_MANIFEST


def test_signature_is_verified_and_policy_can_require_it(tmp_path: Path) -> None:
    signed = make_plugin(tmp_path, "acme.signed")
    manifest_path = signed[0].path / "suiteharness-plugin.json"
    document = json.loads(manifest_path.read_text("utf-8"))
    document["artifact"]["signature"] = {
        "algorithm": "ed25519",
        "key_id": "production-2026",
        "value": "signed-value",
    }
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    class Verifier:
        def __init__(self, accepted: bool) -> None:
            self.accepted = accepted
            self.calls: list[tuple[bytes, SignatureMetadata]] = []

        def verify(self, payload: bytes, signature: SignatureMetadata) -> bool:
            self.calls.append((payload, signature))
            return self.accepted

    verifier = Verifier(True)
    policy = AllowlistedTrustPolicy(
        allowed_digests={"acme.signed": frozenset({signed[1]})},
        trusted_in_process_plugins=frozenset({"acme.signed"}),
        require_signatures=True,
    )
    discovery = PluginDiscovery(
        allowed_roots=[tmp_path], trust_policy=policy, signature_verifier=verifier
    )
    source = discovery.discover(signed[0])
    assert source.signature_verified
    assert len(verifier.calls) == 1
    assert b'"signature":null' in verifier.calls[0][0]

    rejected = PluginDiscovery(
        allowed_roots=[tmp_path],
        trust_policy=policy,
        signature_verifier=Verifier(False),
    )
    with pytest.raises(PluginDiscoveryError) as captured:
        rejected.discover(signed[0])
    assert captured.value.code is PluginErrorCode.SIGNATURE_REJECTED


def test_required_signature_rejects_unsigned_artifact(tmp_path: Path) -> None:
    unsigned = make_plugin(tmp_path, "acme.unsigned")
    policy = AllowlistedTrustPolicy(
        allowed_digests={"acme.unsigned": frozenset({unsigned[1]})},
        trusted_in_process_plugins=frozenset({"acme.unsigned"}),
        require_signatures=True,
    )
    discovery = PluginDiscovery(allowed_roots=[tmp_path], trust_policy=policy)
    with pytest.raises(PluginDiscoveryError) as captured:
        discovery.discover(unsigned[0])
    assert captured.value.code is PluginErrorCode.TRUST_REJECTED


def test_plugin_config_is_detached_and_deeply_immutable(tmp_path: Path) -> None:
    config = {"nested": {"items": ["a", "b"]}}
    artifact = make_plugin(
        tmp_path,
        "acme.immutable",
        config_schema={"type": "object"},
        config=config,
    )
    discovery = discovery_for(tmp_path, [artifact], trusted={"acme.immutable"})
    source = discovery.discover(artifact[0])
    config["nested"]["items"].append("caller-change")
    assert source.config["nested"]["items"] == ("a", "b")
    with pytest.raises(TypeError):
        source.config["new"] = True  # type: ignore[index]


def test_directory_digest_is_deterministic_and_rejects_source_outside_allowlist(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    artifact = make_plugin(outside, "acme.outside")
    discovery = discovery_for(allowed, [], trusted=set())
    with pytest.raises(PluginDiscoveryError) as captured:
        discovery.discover(artifact[0])
    assert captured.value.code is PluginErrorCode.PATH_NOT_ALLOWED

    first = deterministic_directory_digest(artifact[0].path)
    manifest = artifact[0].path / "suiteharness-plugin.json"
    manifest.write_text(manifest.read_text("utf-8") + " ", encoding="utf-8")
    assert deterministic_directory_digest(artifact[0].path) == first


def test_trusted_loader_imports_only_after_verification(tmp_path: Path) -> None:
    sentinel = tmp_path / "loader-imported.txt"
    code = f'''\
from pathlib import Path
Path({str(sentinel)!r}).write_text("yes", encoding="utf-8")

class Prepared:
    exports = {{}}
    async def activate(self, context):
        return None
    async def close(self):
        return None

class Runtime:
    async def prepare(self, context):
        return Prepared()
    async def close(self):
        return None

def create(config):
    return Runtime()
'''
    artifact = make_plugin(tmp_path, "acme.loader", module_source=code)
    discovery = discovery_for(tmp_path, [artifact], trusted={"acme.loader"})
    source = discovery.discover(artifact[0])
    assert not sentinel.exists()
    async def exercise():
        runtime = await PythonEntrypointLoader(discovery).load(source)
        assert sentinel.read_text("utf-8") == "yes"
        await runtime.close()

    asyncio.run(exercise())


def test_trusted_loader_rechecks_only_after_acquiring_the_import_lock(
    tmp_path: Path,
) -> None:
    artifact = make_plugin(
        tmp_path,
        "acme.locked-loader",
        module_source="""
class Runtime:
    async def prepare(self, context):
        raise AssertionError("prepare is not part of this test")
    async def close(self):
        return None

def create(config):
    return Runtime()
""",
    )
    discovery = discovery_for(tmp_path, [artifact], trusted={"acme.locked-loader"})
    source = discovery.discover(artifact[0])
    checked: list[object] = []
    original_assert_current = discovery.assert_current

    def record_check(candidate: object) -> None:
        checked.append(candidate)
        original_assert_current(candidate)  # type: ignore[arg-type]

    discovery.assert_current = record_check  # type: ignore[method-assign]

    async def exercise() -> None:
        loader = PythonEntrypointLoader(discovery)
        await loader._lock.acquire()  # type: ignore[attr-defined]
        task = asyncio.create_task(loader.load(source))
        await asyncio.sleep(0)
        assert checked == []
        loader._lock.release()  # type: ignore[attr-defined]
        runtime = await task
        assert checked == [source]
        await runtime.close()

    asyncio.run(exercise())
