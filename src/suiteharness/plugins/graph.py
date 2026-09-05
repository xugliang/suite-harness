"""Deterministic provider/consumer planning for verified plugins."""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from packaging.version import InvalidVersion, Version

from suiteharness.plugins.discovery import VerifiedPluginSource
from suiteharness.plugins.errors import PluginErrorCode, PluginPlanError
from suiteharness.plugins.models import ContributionKind, RequiredService


@dataclass(frozen=True, slots=True)
class ResolvedRequirement:
    requirement: RequiredService
    provider_plugin_id: str | None


@dataclass(frozen=True, slots=True)
class PluginPlan:
    """A stable topological order and explicit injection bindings."""

    sources: tuple[VerifiedPluginSource, ...]
    bindings: Mapping[str, tuple[ResolvedRequirement, ...]]
    _attestation: object = field(repr=False, compare=False)

    def is_attested_by(self, token: object) -> bool:
        return self._attestation is token

    @property
    def plugin_ids(self) -> tuple[str, ...]:
        return tuple(source.manifest.plugin_id for source in self.sources)


class PluginPlanner:
    """Resolve service providers, conflicts and cycles before code execution."""

    def __init__(self, *, harness_version: str, attestation: object) -> None:
        try:
            self._harness_version = Version(harness_version)
        except InvalidVersion as exc:
            raise ValueError("harness_version must be a valid PEP 440 version") from exc
        self._attestation = attestation

    def plan(self, sources: Iterable[VerifiedPluginSource]) -> PluginPlan:
        candidates = tuple(sources)
        for source in candidates:
            if not source.is_attested_by(self._attestation):
                raise PluginPlanError(
                    PluginErrorCode.TRUST_REJECTED,
                    "plugin source was not produced by the configured discovery",
                )
            if not source.manifest.supports_harness(self._harness_version):
                raise PluginPlanError(
                    PluginErrorCode.HARNESS_API_MISMATCH,
                    "plugin does not support this harness API",
                    plugin_id=source.manifest.plugin_id,
                    plugin_harness_api=source.manifest.harness_api,
                    harness_version=str(self._harness_version),
                )

        by_id: dict[str, VerifiedPluginSource] = {}
        for source in sorted(candidates, key=self._source_key):
            plugin_id = source.manifest.plugin_id
            if plugin_id in by_id:
                raise PluginPlanError(
                    PluginErrorCode.DUPLICATE_PLUGIN,
                    "a plan may contain only one artifact for each plugin_id",
                    plugin_id=plugin_id,
                )
            by_id[plugin_id] = source

        providers: dict[str, tuple[VerifiedPluginSource, str]] = {}
        for source in by_id.values():
            for provided in source.manifest.provides:
                previous = providers.get(provided.service)
                if previous is not None:
                    raise PluginPlanError(
                        PluginErrorCode.DUPLICATE_PROVIDER,
                        "a service must have exactly one provider",
                        service=provided.service,
                        providers=sorted(
                            [previous[0].manifest.plugin_id, source.manifest.plugin_id]
                        ),
                    )
                providers[provided.service] = (source, provided.version)

        seen_contributions: dict[tuple[ContributionKind, str], str] = {}
        for source in by_id.values():
            for key in source.manifest.contribution_map():
                previous = seen_contributions.get(key)
                if previous is not None:
                    kind, contribution_id = key
                    raise PluginPlanError(
                        PluginErrorCode.CONTRIBUTION_CONFLICT,
                        "two plugins declare the same contribution slot",
                        kind=kind.value,
                        contribution_id=contribution_id,
                        plugins=sorted([previous, source.manifest.plugin_id]),
                    )
                seen_contributions[key] = source.manifest.plugin_id

        outgoing: dict[str, set[str]] = {plugin_id: set() for plugin_id in by_id}
        incoming_count: dict[str, int] = {plugin_id: 0 for plugin_id in by_id}
        bindings: dict[str, tuple[ResolvedRequirement, ...]] = {}
        for plugin_id, source in by_id.items():
            resolved: list[ResolvedRequirement] = []
            provider_dependencies: set[str] = set()
            for requirement in source.manifest.requires:
                provider = providers.get(requirement.service)
                if provider is None:
                    if requirement.optional:
                        resolved.append(ResolvedRequirement(requirement, None))
                        continue
                    raise PluginPlanError(
                        PluginErrorCode.MISSING_PROVIDER,
                        "required plugin service has no provider",
                        plugin_id=plugin_id,
                        service=requirement.service,
                    )
                provider_source, provider_version = provider
                if not requirement.accepts(provider_version):
                    raise PluginPlanError(
                        PluginErrorCode.PROVIDER_VERSION_MISMATCH,
                        "provided service version does not satisfy the consumer",
                        plugin_id=plugin_id,
                        service=requirement.service,
                        required=requirement.version,
                        provided=provider_version,
                        provider=provider_source.manifest.plugin_id,
                    )
                provider_id = provider_source.manifest.plugin_id
                resolved.append(ResolvedRequirement(requirement, provider_id))
                provider_dependencies.add(provider_id)
            bindings[plugin_id] = tuple(resolved)
            for provider_id in provider_dependencies:
                outgoing[provider_id].add(plugin_id)
                incoming_count[plugin_id] += 1

        ready = [plugin_id for plugin_id, count in incoming_count.items() if count == 0]
        heapq.heapify(ready)
        order: list[str] = []
        while ready:
            plugin_id = heapq.heappop(ready)
            order.append(plugin_id)
            for consumer in sorted(outgoing[plugin_id]):
                incoming_count[consumer] -= 1
                if incoming_count[consumer] == 0:
                    heapq.heappush(ready, consumer)

        if len(order) != len(by_id):
            members = sorted(plugin_id for plugin_id, count in incoming_count.items() if count)
            raise PluginPlanError(
                PluginErrorCode.DEPENDENCY_CYCLE,
                "plugin service dependencies contain a cycle",
                plugins=members,
            )

        return PluginPlan(
            sources=tuple(by_id[plugin_id] for plugin_id in order),
            bindings=MappingProxyType(dict(bindings)),
            _attestation=self._attestation,
        )

    @staticmethod
    def _source_key(source: VerifiedPluginSource) -> tuple[str, Version, str]:
        return (
            source.manifest.plugin_id,
            source.manifest.parsed_version,
            source.digest,
        )


__all__ = ["PluginPlan", "PluginPlanner", "ResolvedRequirement"]
