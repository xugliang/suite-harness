"""Skill packages are separately owned by each product, never tenant-inherited."""

from suiteharness.runtime.scopes import ScopeKind, ServiceKey

from .protocols import SkillProvider

SKILL_PROVIDER = ServiceKey(
    name="skills.provider", contract=SkillProvider, declared_at=ScopeKind.PRODUCT
)

__all__ = ["SKILL_PROVIDER"]
