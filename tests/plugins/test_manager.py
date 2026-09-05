from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from suiteharness.plugins import (
    AllowlistedTrustPolicy,
    ContributionKind,
    InMemoryContributionRegistry,
    PluginDiscovery,
    PluginErrorCode,
    PluginLifecycleError,
    PluginManager,
    TrustMode,
)
from tests.plugins._helpers import discovery_for, make_plugin


class FakePrepared:
    def __init__(
        self,
        source,
        events: list[str],
        *,
        injected: dict[str, object | None],
        fail_activate: bool = False,
    ) -> None:
        self.source = source
        self.events = events
        self.injected = injected
        self.fail_activate = fail_activate
        self.exports = {
            item.service: f"service:{source.manifest.plugin_id}:{item.version}"
            for item in source.manifest.provides
        }

    async def activate(self, context) -> None:
        plugin_id = self.source.manifest.plugin_id
        self.events.append(f"activate:{plugin_id}")
        context.own(
            "activation-effect",
            lambda: self.events.append(f"close:activation-effect:{plugin_id}"),
        )
        for kind, declaration in self.source.manifest.contributions.items():
            context.contribute(
                kind,
                declaration.contribution_id,
                f"{plugin_id}@{self.source.manifest.version}",
            )
        if self.fail_activate:
            raise RuntimeError(f"activate failed: {plugin_id}")

    async def close(self) -> None:
        self.events.append(f"close:prepared:{self.source.manifest.plugin_id}")


class FakeRuntime:
    def __init__(
        self,
        source,
        events: list[str],
        *,
        fail_activate: bool = False,
    ) -> None:
        self.source = source
        self.events = events
        self.fail_activate = fail_activate

    async def prepare(self, context):
        plugin_id = self.source.manifest.plugin_id
        self.events.append(f"prepare:{plugin_id}")
        context.own(
            "preparation-effect",
            lambda: self.events.append(f"close:preparation-effect:{plugin_id}"),
        )
        injected = {
            item.service: context.inject(item.service)
            for item in self.source.manifest.requires
        }
        return FakePrepared(
            self.source,
            self.events,
            injected=injected,
            fail_activate=self.fail_activate,
        )

    async def close(self) -> None:
        self.events.append(f"close:runtime:{self.source.manifest.plugin_id}")


class FakeLoader:
    def __init__(
        self,
        events: list[str],
        fail: set[str] | None = None,
    ) -> None:
        self.events = events
        self.fail = fail or set()
        self.calls: list[str] = []

    async def load(self, source):
        self.calls.append(source.manifest.plugin_id)
        return FakeRuntime(
            source,
            self.events,
            fail_activate=source.manifest.plugin_id in self.fail,
        )


class FakeLauncher:
    def __init__(self, events: list[str], trust_mode=TrustMode.ISOLATED_WORKER) -> None:
        self.events = events
        self.trust_mode = trust_mode
        self.descriptors = []

    async def launch(self, descriptor, identity):
        self.descriptors.append(descriptor)
        source = type(
            "RemoteSource",
            (),
            {"manifest": descriptor.manifest},
        )()
        return FakeRuntime(source, self.events)


def _manager(discovery, registry, loader, *, launchers=()):
    return PluginManager(
        harness_version="0.1.0",
        discovery=discovery,
        registry=registry,
        trusted_loader=loader,
        launchers=launchers,
    )


def test_provider_injection_and_transactional_activation(tmp_path: Path) -> None:
    provider = make_plugin(
        tmp_path,
        "memory.provider",
        provides=[{"service": "memory.api", "version": "2.0.0"}],
        contributions={"memory": [{"contribution_id": "main"}]},
    )
    consumer = make_plugin(
        tmp_path,
        "workflow.consumer",
        requires=[{"service": "memory.api", "version": ">=2,<3"}],
        contributions={"workflow": [{"contribution_id": "react"}]},
    )
    discovery = discovery_for(
        tmp_path, [provider, consumer], trusted={"memory.provider", "workflow.consumer"}
    )
    registry = InMemoryContributionRegistry()
    events: list[str] = []
    loader = FakeLoader(events)
    manager = _manager(discovery, registry, loader)
    plan = manager.plan([consumer[0], provider[0]])

    async def exercise():
        await manager.activate(plan)
        memory = await registry.get(ContributionKind.MEMORY, "main")
        workflow = await registry.get(ContributionKind.WORKFLOW, "react")
        await manager.deactivate()
        return memory, workflow, await registry.snapshot()

    memory, workflow, final = asyncio.run(exercise())
    assert plan.plugin_ids == ("memory.provider", "workflow.consumer")
    assert memory is not None and memory.value == "memory.provider@1.0.0"
    assert workflow is not None and workflow.value == "workflow.consumer@1.0.0"
    assert final == ()
    assert loader.calls == ["memory.provider", "workflow.consumer"]


def test_activation_failure_rolls_back_every_effect_in_lifo_order(tmp_path: Path) -> None:
    first = make_plugin(
        tmp_path,
        "a.provider",
        contributions={"tools": [{"contribution_id": "a"}]},
    )
    second = make_plugin(
        tmp_path,
        "b.failure",
        contributions={"tools": [{"contribution_id": "b"}]},
    )
    discovery = discovery_for(
        tmp_path, [first, second], trusted={"a.provider", "b.failure"}
    )
    registry = InMemoryContributionRegistry()
    events: list[str] = []
    manager = _manager(discovery, registry, FakeLoader(events, {"b.failure"}))
    plan = manager.plan([first[0], second[0]])

    async def exercise():
        with pytest.raises(PluginLifecycleError) as captured:
            await manager.activate(plan)
        return captured.value, await registry.snapshot()

    error, visible = asyncio.run(exercise())
    assert error.code is PluginErrorCode.ACTIVATION_FAILED
    assert visible == ()
    assert events[-8:] == [
        "close:activation-effect:b.failure",
        "close:prepared:b.failure",
        "close:preparation-effect:b.failure",
        "close:runtime:b.failure",
        "close:activation-effect:a.provider",
        "close:prepared:a.provider",
        "close:preparation-effect:a.provider",
        "close:runtime:a.provider",
    ]


def test_reload_publishes_replacement_before_old_handles_close(tmp_path: Path) -> None:
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    old_root.mkdir()
    new_root.mkdir()
    old = make_plugin(
        old_root,
        "reloadable.tool",
        version="1.0.0",
        contributions={"tools": [{"contribution_id": "lookup"}]},
    )
    new = make_plugin(
        new_root,
        "reloadable.tool",
        version="2.0.0",
        contributions={"tools": [{"contribution_id": "lookup"}]},
    )
    policy = AllowlistedTrustPolicy(
        allowed_digests={"reloadable.tool": frozenset({old[1], new[1]})},
        trusted_in_process_plugins=frozenset({"reloadable.tool"}),
    )
    discovery = PluginDiscovery(allowed_roots=[tmp_path], trust_policy=policy)
    registry = InMemoryContributionRegistry()
    events: list[str] = []
    manager = _manager(discovery, registry, FakeLoader(events))
    old_plan = manager.plan([old[0]])
    new_plan = manager.plan([new[0]])

    async def exercise():
        await manager.activate(old_plan)
        before = await registry.get(ContributionKind.TOOL, "lookup")
        await manager.reload(new_plan)
        after = await registry.get(ContributionKind.TOOL, "lookup")
        await manager.deactivate()
        return before, after

    before, after = asyncio.run(exercise())
    assert before is not None and before.value.endswith("@1.0.0")
    assert after is not None and after.value.endswith("@2.0.0")
    assert before.activation_id != after.activation_id


@pytest.mark.parametrize("trust_mode", [TrustMode.ISOLATED_WORKER, TrustMode.MCP])
def test_isolated_modes_use_launcher_and_never_trusted_loader(
    tmp_path: Path, trust_mode: TrustMode
) -> None:
    artifact = make_plugin(
        tmp_path,
        "isolated.tool",
        trust_mode=trust_mode,
    )
    discovery = discovery_for(tmp_path, [artifact])
    registry = InMemoryContributionRegistry()
    events: list[str] = []

    class ForbiddenLoader:
        async def load(self, source):
            raise AssertionError("isolated source must not be imported")

    launcher = FakeLauncher(events, trust_mode)
    manager = _manager(
        discovery, registry, ForbiddenLoader(), launchers=(launcher,)
    )
    plan = manager.plan([artifact[0]])

    async def exercise():
        await manager.activate(plan)
        await manager.deactivate()

    asyncio.run(exercise())
    assert len(launcher.descriptors) == 1
    assert launcher.descriptors[0].artifact_digest == artifact[1]


def test_manager_serializes_concurrent_activation(tmp_path: Path) -> None:
    artifact = make_plugin(tmp_path, "concurrent.plugin")
    discovery = discovery_for(tmp_path, [artifact], trusted={"concurrent.plugin"})
    registry = InMemoryContributionRegistry()
    manager = _manager(discovery, registry, FakeLoader([]))
    plan = manager.plan([artifact[0]])

    async def attempt() -> PluginErrorCode | None:
        try:
            await manager.activate(plan)
        except PluginLifecycleError as exc:
            return exc.code
        return None

    async def exercise():
        outcomes = await asyncio.gather(attempt(), attempt())
        await manager.deactivate()
        return outcomes

    outcomes = asyncio.run(exercise())
    assert sorted(str(item) for item in outcomes) == [
        "None",
        "PluginErrorCode.ALREADY_ACTIVE",
    ]
