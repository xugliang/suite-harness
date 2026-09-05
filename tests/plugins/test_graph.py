from __future__ import annotations

import json
from pathlib import Path

import pytest

from suiteharness.plugins import PluginErrorCode, PluginPlanError, PluginPlanner
from tests.plugins._helpers import discovery_for, make_plugin


def _plan(tmp_path: Path, artifacts):
    # Trust IDs are taken from manifests because path normalization is not identity.
    trusted = {
        json.loads((artifact[0].path / "suiteharness-plugin.json").read_text("utf-8"))["plugin_id"]
        for artifact in artifacts
    }
    discovery = discovery_for(tmp_path, artifacts, trusted=trusted)
    sources = discovery.discover_all(item[0] for item in artifacts)
    return PluginPlanner(harness_version="0.1.0", attestation=discovery.attestation).plan(
        sources
    )


def test_provider_consumer_plan_is_topological_and_deterministic(tmp_path: Path) -> None:
    consumer = make_plugin(
        tmp_path,
        "z.consumer",
        requires=[{"service": "memory.api", "version": ">=2,<3"}],
    )
    independent = make_plugin(tmp_path, "a.independent")
    provider = make_plugin(
        tmp_path,
        "m.provider",
        provides=[{"service": "memory.api", "version": "2.1.0"}],
    )
    plan = _plan(tmp_path, [consumer, provider, independent])
    assert plan.plugin_ids == ("a.independent", "m.provider", "z.consumer")
    binding = plan.bindings["z.consumer"][0]
    assert binding.provider_plugin_id == "m.provider"


def test_missing_and_incompatible_provider_are_distinct(tmp_path: Path) -> None:
    missing = make_plugin(
        tmp_path,
        "consumer.missing",
        requires=[{"service": "memory.api", "version": ">=2"}],
    )
    with pytest.raises(PluginPlanError) as captured:
        _plan(tmp_path, [missing])
    assert captured.value.code is PluginErrorCode.MISSING_PROVIDER

    another_root = tmp_path / "second"
    another_root.mkdir()
    consumer = make_plugin(
        another_root,
        "consumer.version",
        requires=[{"service": "memory.api", "version": ">=2"}],
    )
    provider = make_plugin(
        another_root,
        "provider.old",
        provides=[{"service": "memory.api", "version": "1.9.0"}],
    )
    with pytest.raises(PluginPlanError) as captured:
        _plan(another_root, [consumer, provider])
    assert captured.value.code is PluginErrorCode.PROVIDER_VERSION_MISMATCH


def test_duplicate_provider_and_contribution_conflicts_fail(tmp_path: Path) -> None:
    first = make_plugin(
        tmp_path,
        "provider.first",
        provides=[{"service": "memory.api", "version": "1.0.0"}],
        contributions={"memory": [{"contribution_id": "main"}]},
    )
    second = make_plugin(
        tmp_path,
        "provider.second",
        provides=[{"service": "memory.api", "version": "1.1.0"}],
    )
    with pytest.raises(PluginPlanError) as captured:
        _plan(tmp_path, [first, second])
    assert captured.value.code is PluginErrorCode.DUPLICATE_PROVIDER

    third_root = tmp_path / "third"
    third_root.mkdir()
    one = make_plugin(
        third_root,
        "contributor.one",
        contributions={"tools": [{"contribution_id": "lookup"}]},
    )
    two = make_plugin(
        third_root,
        "contributor.two",
        contributions={"tools": [{"contribution_id": "lookup"}]},
    )
    with pytest.raises(PluginPlanError) as captured:
        _plan(third_root, [one, two])
    assert captured.value.code is PluginErrorCode.CONTRIBUTION_CONFLICT


def test_service_cycle_is_rejected(tmp_path: Path) -> None:
    first = make_plugin(
        tmp_path,
        "cycle.first",
        provides=[{"service": "first.api", "version": "1.0.0"}],
        requires=[{"service": "second.api", "version": ">=1"}],
    )
    second = make_plugin(
        tmp_path,
        "cycle.second",
        provides=[{"service": "second.api", "version": "1.0.0"}],
        requires=[{"service": "first.api", "version": ">=1"}],
    )
    with pytest.raises(PluginPlanError) as captured:
        _plan(tmp_path, [first, second])
    assert captured.value.code is PluginErrorCode.DEPENDENCY_CYCLE
    assert captured.value.detail["plugins"] == ["cycle.first", "cycle.second"]


def test_optional_missing_provider_is_injected_as_absent(tmp_path: Path) -> None:
    consumer = make_plugin(
        tmp_path,
        "optional.consumer",
        requires=[
            {"service": "optional.api", "version": ">=1", "optional": True}
        ],
    )
    plan = _plan(tmp_path, [consumer])
    assert plan.bindings["optional.consumer"][0].provider_plugin_id is None


def test_duplicate_plugin_and_harness_api_mismatch_fail_before_loading(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = make_plugin(first_root, "duplicate.plugin", version="1.0.0")
    second = make_plugin(second_root, "duplicate.plugin", version="2.0.0")
    with pytest.raises(PluginPlanError) as captured:
        _plan(tmp_path, [second, first])
    assert captured.value.code is PluginErrorCode.DUPLICATE_PLUGIN

    incompatible_root = tmp_path / "incompatible"
    incompatible_root.mkdir()
    incompatible = make_plugin(
        incompatible_root,
        "future.plugin",
        extra_manifest={"harness_api": ">=9,<10"},
    )
    with pytest.raises(PluginPlanError) as captured:
        _plan(incompatible_root, [incompatible])
    assert captured.value.code is PluginErrorCode.HARNESS_API_MISMATCH
