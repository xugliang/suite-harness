"""A small ABC shared by file packages and remote MCP skill repositories."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from .models import SkillAccess, SkillDescriptor, SkillDocument, SkillError

SkillAccessPolicy = Callable[[SkillAccess, SkillDescriptor, str], bool]


class SkillProvider(ABC):
    @abstractmethod
    async def list_skills(self, access: SkillAccess) -> tuple[SkillDescriptor, ...]:
        """Return only discoverable skills for this actual request identity."""

    @abstractmethod
    async def read_skill(
        self, access: SkillAccess, skill_id: str, *, resource_id: str = "instructions"
    ) -> SkillDocument:
        """Read a registered resource, checking every expanded dependency."""

    def static_manifest(self) -> tuple[SkillDescriptor, ...]:
        """Trusted registration inventory, not an employee-visible catalogue.

        Override this when product tools declare ``required_skills``: activation
        validates those IDs here without impersonating an employee or connecting
        to a remote service. The empty default keeps non-Skill products valid.
        Runtime discovery/read authorization still uses the real SkillAccess.
        """
        return ()

    def close(self) -> None:
        return None


class EmptySkillProvider(SkillProvider):
    async def list_skills(self, access: SkillAccess) -> tuple[SkillDescriptor, ...]:
        return ()

    async def read_skill(
        self, access: SkillAccess, skill_id: str, *, resource_id: str = "instructions"
    ) -> SkillDocument:
        raise SkillError("skill_not_available", "Skill is not available")


__all__ = ["EmptySkillProvider", "SkillAccessPolicy", "SkillProvider"]
