"""A delegated grant must never outlive or out-authorize its parent."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from suiteharness.execution import in_memory
from suiteharness.execution.in_memory import InMemoryCapabilityAuthority
from suiteharness.execution.models import ToolIdentity
from suiteharness.runtime.scopes import ScopePath

READ = ToolIdentity(namespace="core", name="read", origin="test:lineage", version="1")
WRITE = ToolIdentity(namespace="core", name="write", origin="test:lineage", version="1")
EXTRA = ToolIdentity(namespace="core", name="extra", origin="test:lineage", version="1")
PRODUCT = ScopePath.product("tenant", "product")
CHILD = ScopePath.agent("tenant", "product", "child", "child-session")


def test_child_grant_is_narrowed_and_bounded_by_parent():
    async def scenario():
        authority = InMemoryCapabilityAuthority()
        parent = await authority.issue(
            PRODUCT,
            tool_identities=(READ, WRITE),
            capabilities=("search", "edit"),
            principal_id="seller",
            lifetime=timedelta(minutes=10),
        )
        child = await authority.issue_child(
            parent.grant_id,
            CHILD,
            tool_identities=(READ,),
            capabilities=frozenset({"search"}),
            principal_id="seller",
            lifetime=timedelta(hours=1),
        )
        assert child.tool_identities == frozenset({READ})
        assert child.capabilities == frozenset({"search"})
        assert child.principal_id == "seller"
        assert child.expires_at <= parent.expires_at
        assert await authority.resolve(child.grant_id) == child

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("scope", "tools", "capabilities", "principal"),
    [
        (ScopePath.agent("tenant", "other", "child", "s"), (READ,), frozenset(), "seller"),
        (CHILD, (EXTRA,), frozenset(), "seller"),
        (CHILD, (READ,), frozenset({"edit"}), "seller"),
        (CHILD, (READ,), frozenset(), "other-seller"),
        (PRODUCT, (READ,), frozenset(), "seller"),
    ],
)
def test_child_issue_refuses_scope_or_authority_expansion(
    scope, tools, capabilities, principal
):
    async def scenario():
        authority = InMemoryCapabilityAuthority()
        parent = await authority.issue(
            PRODUCT,
            tool_identities=(READ,),
            capabilities=("search",),
            principal_id="seller",
        )
        with pytest.raises(ValueError):
            await authority.issue_child(
                parent.grant_id,
                scope,
                tool_identities=tools,
                capabilities=capabilities,
                principal_id=principal,
                lifetime=timedelta(minutes=1),
            )
        assert await authority.resolve(parent.grant_id) == parent

    asyncio.run(scenario())


def test_revoking_parent_atomically_revokes_its_descendants():
    async def scenario():
        authority = InMemoryCapabilityAuthority()
        parent = await authority.issue(PRODUCT, tool_identities=(READ,), principal_id="seller")
        child = await authority.issue_child(
            parent.grant_id, CHILD,
            tool_identities=(READ,), capabilities=frozenset(),
            principal_id="seller", lifetime=timedelta(minutes=5),
        )
        grandchild = await authority.issue_child(
            child.grant_id,
            ScopePath.agent("tenant", "product", "grandchild", "grandchild-session"),
            tool_identities=(READ,), capabilities=frozenset(),
            principal_id="seller", lifetime=timedelta(minutes=2),
        )
        assert await authority.revoke(parent.grant_id)
        assert await authority.resolve(parent.grant_id) is None
        assert await authority.resolve(child.grant_id) is None
        assert await authority.resolve(grandchild.grant_id) is None
        assert not await authority.revoke(child.grant_id)

    asyncio.run(scenario())


def test_child_resolution_fails_when_parent_expires(monkeypatch):
    async def scenario():
        authority = InMemoryCapabilityAuthority()
        start = datetime(2026, 9, 15, tzinfo=UTC)
        monkeypatch.setattr(in_memory, "_now", lambda: start)
        parent = await authority.issue(
            PRODUCT, tool_identities=(READ,), principal_id="seller",
            lifetime=timedelta(seconds=30), now=start,
        )
        child = await authority.issue_child(
            parent.grant_id, CHILD,
            tool_identities=(READ,), capabilities=frozenset(),
            principal_id="seller", lifetime=timedelta(seconds=5),
        )
        monkeypatch.setattr(in_memory, "_now", lambda: start + timedelta(seconds=31))
        assert await authority.resolve(child.grant_id) is None

    asyncio.run(scenario())


def test_child_issue_checks_expiry_when_lock_is_acquired(monkeypatch):
    async def scenario():
        authority = InMemoryCapabilityAuthority()
        start = datetime(2026, 9, 15, tzinfo=UTC)
        clock = [start]

        def now():
            return clock[0]

        monkeypatch.setattr(in_memory, "_now", now)
        parent = await authority.issue(
            PRODUCT, tool_identities=(READ,), principal_id="seller",
            lifetime=timedelta(seconds=30), now=start,
        )
        async with authority._lock:
            pending = asyncio.create_task(authority.issue_child(
                parent.grant_id, CHILD,
                tool_identities=(READ,), capabilities=frozenset(),
                principal_id="seller", lifetime=timedelta(seconds=5),
            ))
            await asyncio.sleep(0)
            clock[0] = start + timedelta(seconds=31)
        with pytest.raises(ValueError, match="not active"):
            await pending

    asyncio.run(scenario())


def test_child_resolution_fails_after_activation_release():
    async def scenario():
        authority = InMemoryCapabilityAuthority()
        await authority.bind_activation("tenant", ("product",), "owner")
        parent = await authority.issue(PRODUCT, tool_identities=(READ,), principal_id="seller")
        child = await authority.issue_child(
            parent.grant_id, CHILD,
            tool_identities=(READ,), capabilities=frozenset(),
            principal_id="seller", lifetime=timedelta(minutes=5),
        )
        await authority.release_activation("owner")
        assert await authority.resolve(child.grant_id) is None

    asyncio.run(scenario())
