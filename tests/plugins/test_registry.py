from __future__ import annotations

import asyncio

from suiteharness.plugins import (
    ContributionDeclaration,
    ContributionKind,
    ContributionRegistration,
    InMemoryContributionRegistry,
    PluginActivationIdentity,
)


def _registration(activation: str, value: str) -> ContributionRegistration:
    return ContributionRegistration(
        owner=PluginActivationIdentity(
            plugin_id="acme.plugin",
            version="1.0.0",
            artifact_digest="sha256:" + "0" * 64,
            activation_id=activation,
        ),
        kind=ContributionKind.TOOL,
        declaration=ContributionDeclaration(contribution_id="lookup"),
        value=value,
    )


def test_stale_handle_cannot_remove_replacement() -> None:
    registry = InMemoryContributionRegistry()

    async def exercise():
        old_handle = (await registry.publish((_registration("old", "old"),), replaces=frozenset()))[0]
        new_handle = (
            await registry.publish(
                (_registration("new", "new"),), replaces=frozenset({"old"})
            )
        )[0]
        await old_handle.revoke()
        after_stale_revoke = await registry.get(ContributionKind.TOOL, "lookup")
        await old_handle.revoke()
        await new_handle.revoke()
        final = await registry.get(ContributionKind.TOOL, "lookup")
        return after_stale_revoke, final

    visible, final = asyncio.run(exercise())
    assert visible is not None and visible.value == "new"
    assert final is None
