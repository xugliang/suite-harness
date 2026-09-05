from __future__ import annotations

import asyncio

import pytest

from suiteharness.execution import (
    InMemoryCapabilityAuthority,
    InMemoryToolRegistry,
    ToolEffect,
    ToolIdentity,
    ToolSpec,
)
from suiteharness.runtime import RequestScope, ScopePath
from suiteharness.server import ConfiguredGrantIssuer, ToolAccessError, ToolAccessTemplate
from suiteharness.server.access import (
    AdditionalGrantMaterial,
    AdditionalGrantTool,
    McpChannelAccessRule,
)


async def _handler(context, arguments):  # type: ignore[no-untyped-def]
    return {"ok": True}


def _scope(*, channel: str = "feishu", product: str = "product-a") -> RequestScope:
    return RequestScope(
        path=ScopePath.agent("tenant-a", product, "agent-a", "session-a"),
        principal_id="alice",
        channel_id=channel,
        request_id="request-a",
        correlation_id="correlation-a",
    )


def _spec(name: str, *effects: ToolEffect, capabilities: tuple[str, ...] = ()) -> ToolSpec:
    return ToolSpec(
        name=name,
        effects=frozenset(effects),
        required_capabilities=frozenset(capabilities),
        input_schema={"type": "object"},
    )


def test_feishu_defaults_to_read_only_and_grant_is_exact_principal_bound() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        registry = InMemoryToolRegistry()
        identity = ToolIdentity(
            namespace="suiteharness",
            name="suiteharness.fs.read",
            origin="suiteharness.builtin.fs",
            version="1",
        )
        registry.register_protected(
            _spec("suiteharness.fs.read", ToolEffect.READ, capabilities=("workspace.read",)),
            _handler,
            identity=identity,
        )
        authority = InMemoryCapabilityAuthority()
        issuer = ConfiguredGrantIssuer(
            registry=registry,
            authority=authority,
            templates=(
                ToolAccessTemplate(
                    channel_id="feishu",
                    product_id="product-a",
                    read_aliases=("suiteharness.fs.read",),
                ),
            ),
        )
        async with issuer.lease(_scope()) as issued:
            assert issued.read_only is True
            assert issued.grant.principal_id == "alice"
            assert issued.grant.tool_identities == frozenset({identity})
            assert issued.grant.capabilities == frozenset({"workspace.read"})
            assert await authority.resolve(issued.grant_id) == issued.grant
            grant_id = issued.grant_id
        assert await authority.resolve(grant_id) is None

    asyncio.run(exercise())


def test_feishu_write_requires_explicit_builtin_file_template() -> None:
    with pytest.raises(ValueError, match="only the built-in file"):
        ToolAccessTemplate(
            channel_id="feishu",
            product_id="product-a",
            write_aliases=("crm.update",),
        )
    with pytest.raises(ValueError, match="destructive"):
        ToolAccessTemplate(
            channel_id="feishu",
            product_id="product-a",
            destructive_aliases=("suiteharness.shell.bash",),
        )

    async def exercise():  # type: ignore[no-untyped-def]
        registry = InMemoryToolRegistry()
        identity = ToolIdentity(
            namespace="suiteharness",
            name="suiteharness.fs.write",
            origin="suiteharness.builtin.fs",
            version="1",
        )
        registry.register_protected(
            _spec("suiteharness.fs.write", ToolEffect.WRITE, capabilities=("workspace.write",)),
            _handler,
            identity=identity,
        )
        issuer = ConfiguredGrantIssuer(
            registry=registry,
            authority=InMemoryCapabilityAuthority(),
            templates=(
                ToolAccessTemplate(
                    channel_id="feishu",
                    product_id="product-a",
                    write_aliases=("suiteharness.fs.write",),
                ),
            ),
        )
        async with issuer.lease(_scope()) as issued:
            assert issued.read_only is False
            assert issued.grant.tool_identities == frozenset({identity})

    asyncio.run(exercise())


def test_feishu_rejects_a_product_shadow_of_builtin_write_alias() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        registry = InMemoryToolRegistry()
        registry.register(
            ScopePath.product("tenant-a", "product-a"),
            _spec("suiteharness.fs.write", ToolEffect.WRITE),
            _handler,
        )
        issuer = ConfiguredGrantIssuer(
            registry=registry,
            authority=InMemoryCapabilityAuthority(),
            templates=(
                ToolAccessTemplate(
                    channel_id="feishu",
                    product_id="product-a",
                    write_aliases=("suiteharness.fs.write",),
                ),
            ),
        )
        with pytest.raises(ToolAccessError, match="exact built-in"):
            async with issuer.lease(_scope()):
                pass

    asyncio.run(exercise())


def test_web_may_explicitly_lease_write_and_bash_but_categories_fail_closed() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        registry = InMemoryToolRegistry()
        write_identity = ToolIdentity(
            namespace="suiteharness",
            name="suiteharness.fs.write",
            origin="suiteharness.builtin.fs",
            version="1",
        )
        bash_identity = ToolIdentity(
            namespace="suiteharness",
            name="suiteharness.shell.bash",
            origin="suiteharness.builtin.shell",
            version="1",
        )
        registry.register_protected(
            _spec("suiteharness.fs.write", ToolEffect.WRITE),
            _handler,
            identity=write_identity,
        )
        registry.register_protected(
            _spec(
                "suiteharness.shell.bash",
                ToolEffect.WRITE,
                ToolEffect.DESTRUCTIVE,
                ToolEffect.EXTERNAL,
            ),
            _handler,
            identity=bash_identity,
        )
        authority = InMemoryCapabilityAuthority()
        issuer = ConfiguredGrantIssuer(
            registry=registry,
            authority=authority,
            templates=(
                ToolAccessTemplate(
                    channel_id="web",
                    product_id="product-a",
                    write_aliases=("suiteharness.fs.write",),
                    destructive_aliases=("suiteharness.shell.bash",),
                ),
            ),
        )
        async with issuer.lease(_scope(channel="web")) as issued:
            assert issued.read_only is False
            assert issued.grant.tool_identities == frozenset(
                {write_identity, bash_identity}
            )

        bad = ConfiguredGrantIssuer(
            registry=registry,
            authority=authority,
            templates=(
                ToolAccessTemplate(
                    channel_id="web",
                    product_id="product-b",
                    read_aliases=("suiteharness.fs.write",),
                ),
            ),
        )
        with pytest.raises(ToolAccessError, match="incompatible effects"):
            async with bad.lease(_scope(channel="web", product="product-b")):
                pass

    asyncio.run(exercise())


def test_missing_or_duplicate_routes_and_missing_aliases_fail_closed() -> None:
    template = ToolAccessTemplate(channel_id="web", product_id="product-a")
    with pytest.raises(ValueError, match="duplicate"):
        ConfiguredGrantIssuer(
            registry=InMemoryToolRegistry(),
            authority=InMemoryCapabilityAuthority(),
            templates=(template, template),
        )

    async def exercise():  # type: ignore[no-untyped-def]
        issuer = ConfiguredGrantIssuer(
            registry=InMemoryToolRegistry(),
            authority=InMemoryCapabilityAuthority(),
            templates=(
                ToolAccessTemplate(
                    channel_id="web",
                    product_id="product-a",
                    read_aliases=("suiteharness.fs.read",),
                ),
            ),
        )
        with pytest.raises(ToolAccessError, match="unavailable"):
            async with issuer.lease(_scope(channel="web")):
                pass
        with pytest.raises(ToolAccessError, match="no tool access template"):
            async with issuer.lease(_scope(channel="feishu")):
                pass

    asyncio.run(exercise())


def _mcp_inventory(
    registry: InMemoryToolRegistry,
) -> tuple[AdditionalGrantMaterial, ToolIdentity, ToolIdentity]:
    read = registry.register(
        ScopePath.product("tenant-a", "product-a"),
        _spec(
            "mcp.crm.crm-lookup-a1",
            ToolEffect.READ,
            ToolEffect.EXTERNAL,
            capabilities=("mcp.crm.call",),
        ),
        _handler,
    )
    write = registry.register(
        ScopePath.product("tenant-a", "product-a"),
        _spec(
            "mcp.crm.crm-update-b2",
            ToolEffect.WRITE,
            ToolEffect.EXTERNAL,
            capabilities=("mcp.crm.call", "crm.records.write"),
        ),
        _handler,
    )
    return (
        AdditionalGrantMaterial(
            tools=(
                AdditionalGrantTool(
                    server_id="crm",
                    remote_name="crm.lookup",
                    identity=read.identity,
                    capabilities=frozenset({"mcp.crm.call"}),
                ),
                AdditionalGrantTool(
                    server_id="crm",
                    remote_name="crm.update",
                    identity=write.identity,
                    capabilities=frozenset({"mcp.crm.call", "crm.records.write"}),
                ),
            )
        ),
        read.identity,
        write.identity,
    )


def test_mcp_inventory_is_denied_by_default_and_remote_tool_rule_is_exact() -> None:
    async def exercise() -> None:
        registry = InMemoryToolRegistry()
        material, read_identity, _write_identity = _mcp_inventory(registry)
        authority = InMemoryCapabilityAuthority()
        common = {
            "registry": registry,
            "authority": authority,
            "templates": (
                ToolAccessTemplate(channel_id="web", product_id="product-a"),
            ),
            "additional_material_by_product": {"product-a": material},
        }
        denied = ConfiguredGrantIssuer(**common)
        async with denied.lease(_scope(channel="web")) as issued:
            assert issued.grant.tool_identities == frozenset()
            assert issued.grant.capabilities == frozenset()

        exact = ConfiguredGrantIssuer(
            **common,
            mcp_access_by_route={
                ("web", "product-a"): McpChannelAccessRule(
                    tools_by_server={"crm": frozenset({"crm.lookup"})}
                )
            },
        )
        async with exact.lease(_scope(channel="web")) as issued:
            assert issued.read_only is True
            assert issued.grant.tool_identities == frozenset({read_identity})
            assert issued.grant.capabilities == frozenset({"mcp.crm.call"})

    asyncio.run(exercise())


def test_mcp_server_rule_selects_all_tools_and_web_write_remains_non_read_only() -> None:
    async def exercise() -> None:
        registry = InMemoryToolRegistry()
        material, read_identity, write_identity = _mcp_inventory(registry)
        issuer = ConfiguredGrantIssuer(
            registry=registry,
            authority=InMemoryCapabilityAuthority(),
            templates=(ToolAccessTemplate(channel_id="web", product_id="product-a"),),
            additional_material_by_product={"product-a": material},
            mcp_access_by_route={
                ("web", "product-a"): McpChannelAccessRule(
                    server_ids=frozenset({"crm"})
                )
            },
        )
        async with issuer.lease(_scope(channel="web")) as issued:
            assert issued.read_only is False
            assert issued.grant.tool_identities == frozenset(
                {read_identity, write_identity}
            )
            assert issued.grant.capabilities == frozenset(
                {"mcp.crm.call", "crm.records.write"}
            )

    asyncio.run(exercise())


def test_feishu_mcp_allowlist_still_rejects_write_and_destructive_tools() -> None:
    async def exercise() -> None:
        registry = InMemoryToolRegistry()
        material, read_identity, _write_identity = _mcp_inventory(registry)
        common = {
            "registry": registry,
            "authority": InMemoryCapabilityAuthority(),
            "templates": (
                ToolAccessTemplate(channel_id="feishu", product_id="product-a"),
            ),
            "additional_material_by_product": {"product-a": material},
        }
        read_only = ConfiguredGrantIssuer(
            **common,
            mcp_access_by_route={
                ("feishu", "product-a"): McpChannelAccessRule(
                    tools_by_server={"crm": frozenset({"crm.lookup"})}
                )
            },
        )
        async with read_only.lease(_scope()) as issued:
            assert issued.read_only is True
            assert issued.grant.tool_identities == frozenset({read_identity})

        unsafe_server = ConfiguredGrantIssuer(
            **common,
            mcp_access_by_route={
                ("feishu", "product-a"): McpChannelAccessRule(
                    server_ids=frozenset({"crm"})
                )
            },
        )
        with pytest.raises(ToolAccessError, match="read-only tools only"):
            async with unsafe_server.lease(_scope()):
                pass

    asyncio.run(exercise())


def test_mcp_missing_tool_and_capability_drift_fail_closed() -> None:
    async def exercise() -> None:
        registry = InMemoryToolRegistry()
        material, _read_identity, _write_identity = _mcp_inventory(registry)
        common = {
            "registry": registry,
            "authority": InMemoryCapabilityAuthority(),
            "templates": (
                ToolAccessTemplate(channel_id="web", product_id="product-a"),
            ),
            "additional_material_by_product": {"product-a": material},
        }
        missing = ConfiguredGrantIssuer(
            **common,
            mcp_access_by_route={
                ("web", "product-a"): McpChannelAccessRule(
                    tools_by_server={"crm": frozenset({"crm.missing"})}
                )
            },
        )
        with pytest.raises(ToolAccessError, match="were not discovered"):
            async with missing.lease(_scope(channel="web")):
                pass

        read_tool = material.tools[0]
        drifted = AdditionalGrantMaterial(
            tools=(
                AdditionalGrantTool(
                    server_id=read_tool.server_id,
                    remote_name=read_tool.remote_name,
                    identity=read_tool.identity,
                    capabilities=frozenset({"mcp.crm.call", "crm.unregistered"}),
                ),
            )
        )
        drift = ConfiguredGrantIssuer(
            registry=registry,
            authority=InMemoryCapabilityAuthority(),
            templates=(ToolAccessTemplate(channel_id="web", product_id="product-a"),),
            additional_material_by_product={"product-a": drifted},
            mcp_access_by_route={
                ("web", "product-a"): McpChannelAccessRule(
                    tools_by_server={"crm": frozenset({"crm.lookup"})}
                )
            },
        )
        with pytest.raises(ToolAccessError, match="capabilities do not match"):
            async with drift.lease(_scope(channel="web")):
                pass

    asyncio.run(exercise())
