"""Reference atomic contribution registry with stale-handle protection."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import uuid4

from suiteharness.plugins.errors import (
    PluginErrorCode,
    PluginLifecycleError,
)
from suiteharness.plugins.models import ContributionKind
from suiteharness.plugins.protocols import (
    ContributionHandle,
    ContributionRegistration,
    ContributionRegistry,
)


@dataclass(frozen=True, slots=True)
class VisibleContribution:
    kind: ContributionKind
    contribution_id: str
    plugin_id: str
    plugin_version: str
    activation_id: str
    value: object


@dataclass(frozen=True, slots=True)
class _Record:
    token: str
    registration: ContributionRegistration


class _RegistryHandle:
    def __init__(
        self,
        registry: InMemoryContributionRegistry,
        key: tuple[ContributionKind, str],
        token: str,
    ) -> None:
        self._registry = registry
        self._key = key
        self._token = token
        self._lock = asyncio.Lock()
        self._revoked = False

    async def revoke(self) -> None:
        async with self._lock:
            if self._revoked:
                return
            self._revoked = True
        await self._registry._revoke(self._key, self._token)


class InMemoryContributionRegistry(ContributionRegistry):
    """Atomic replacement stack intended as a host-side reference implementation.

    Old and replacement records may coexist briefly during reload.  Lookup always
    returns the newest record.  A handle removes only its random registration
    token, so an old fiber can never unregister its replacement.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._records: dict[tuple[ContributionKind, str], list[_Record]] = {}

    async def publish(
        self,
        registrations: tuple[ContributionRegistration, ...],
        *,
        replaces: frozenset[str],
    ) -> tuple[ContributionHandle, ...]:
        keys = [item.key for item in registrations]
        if len(keys) != len(set(keys)):
            raise PluginLifecycleError(
                PluginErrorCode.CONTRIBUTION_CONFLICT,
                "one publication batch repeats a contribution key",
            )

        async with self._lock:
            for registration in registrations:
                stack = self._records.get(registration.key, [])
                if stack and stack[-1].registration.owner.activation_id not in replaces:
                    owner = stack[-1].registration.owner
                    raise PluginLifecycleError(
                        PluginErrorCode.CONTRIBUTION_CONFLICT,
                        "contribution slot is owned by an unrelated activation",
                        kind=registration.kind.value,
                        contribution_id=registration.declaration.contribution_id,
                        owner_plugin_id=owner.plugin_id,
                    )

            handles: list[ContributionHandle] = []
            for registration in registrations:
                token = uuid4().hex
                self._records.setdefault(registration.key, []).append(
                    _Record(token=token, registration=registration)
                )
                handles.append(_RegistryHandle(self, registration.key, token))
            return tuple(handles)

    async def _revoke(self, key: tuple[ContributionKind, str], token: str) -> None:
        async with self._lock:
            stack = self._records.get(key)
            if not stack:
                return
            remaining = [record for record in stack if record.token != token]
            if remaining:
                self._records[key] = remaining
            else:
                self._records.pop(key, None)

    async def get(
        self, kind: ContributionKind, contribution_id: str
    ) -> VisibleContribution | None:
        async with self._lock:
            stack = self._records.get((kind, contribution_id))
            if not stack:
                return None
            registration = stack[-1].registration
            owner = registration.owner
            return VisibleContribution(
                kind=kind,
                contribution_id=contribution_id,
                plugin_id=owner.plugin_id,
                plugin_version=owner.version,
                activation_id=owner.activation_id,
                value=registration.value,
            )

    async def snapshot(self) -> tuple[VisibleContribution, ...]:
        async with self._lock:
            visible: list[VisibleContribution] = []
            for (kind, contribution_id), stack in sorted(
                self._records.items(), key=lambda item: (item[0][0].value, item[0][1])
            ):
                registration = stack[-1].registration
                owner = registration.owner
                visible.append(
                    VisibleContribution(
                        kind=kind,
                        contribution_id=contribution_id,
                        plugin_id=owner.plugin_id,
                        plugin_version=owner.version,
                        activation_id=owner.activation_id,
                        value=registration.value,
                    )
                )
            return tuple(visible)


__all__ = ["InMemoryContributionRegistry", "VisibleContribution"]
