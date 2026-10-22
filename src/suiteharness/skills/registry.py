"""Package-local dependency validation without inventing an administrator scope."""

from __future__ import annotations

from collections.abc import Iterable
from types import MappingProxyType

from .models import SkillDescriptor, SkillError, skill_identifier


class SkillRegistry:
    def __init__(
        self, namespace: str, descriptors: Iterable[SkillDescriptor], *, max_dependency_depth: int = 4
    ) -> None:
        self.namespace = skill_identifier(namespace)
        self.max_dependency_depth = max_dependency_depth
        items: dict[str, SkillDescriptor] = {}
        for descriptor in descriptors:
            if descriptor.skill_id != f"{namespace}.{descriptor.name}":
                raise SkillError("skill_invalid", "Skill does not belong to the registered package")
            if descriptor.skill_id in items:
                raise SkillError("skill_invalid", "Duplicate registered skill identifier")
            items[descriptor.skill_id] = descriptor
        self._items = MappingProxyType(items)
        for descriptor in items.values():
            self.expanded_ids(descriptor.skill_id)

    def manifest(self) -> tuple[SkillDescriptor, ...]:
        return tuple(self._items[key] for key in sorted(self._items))

    def descriptor(self, skill_id: str) -> SkillDescriptor:
        descriptor = self._items.get(skill_id)
        if descriptor is None:
            raise SkillError("skill_not_available", "Skill is not available")
        return descriptor

    def expanded_ids(self, skill_id: str) -> tuple[str, ...]:
        """Stable postorder, de-duplicated across a dependency diamond."""
        result: list[str] = []
        seen: set[str] = set()
        heights: dict[str, int] = {}

        def visit(current: str, path: tuple[str, ...]) -> int:
            if current in path:
                raise SkillError("skill_invalid", "Skill dependencies contain a cycle")
            if len(path) >= self.max_dependency_depth:
                raise SkillError("skill_budget_exceeded", "Skill dependency depth exceeded")
            if current in seen:
                if len(path) + heights[current] > self.max_dependency_depth:
                    raise SkillError("skill_budget_exceeded", "Skill dependency depth exceeded")
                return heights[current]
            try:
                descriptor = self.descriptor(current)
            except SkillError as exc:
                raise SkillError("skill_invalid", "Skill dependency is not registered") from exc
            height = 1
            for dependency in descriptor.includes:
                height = max(height, 1 + visit(dependency, (*path, current)))
            if current not in seen:
                result.append(current)
                seen.add(current)
                heights[current] = height
            return height

        self.descriptor(skill_id)
        visit(skill_id, ())
        return tuple(result)


__all__ = ["SkillRegistry"]
