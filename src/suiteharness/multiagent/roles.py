"""Lookup of deployment-configured child roles."""

from __future__ import annotations

from suiteharness.config.models import (
    AgentRoleDefinition,
    MultiAgentConfig,
    MultiAgentProductConfig,
)


class ConfiguredAgentRoleRegistry:
    """Resolve only explicitly enabled, same-product child roles."""

    def __init__(self, config: MultiAgentConfig) -> None:
        self._config = config

    def policy(self, product_id: str) -> MultiAgentProductConfig | None:
        policy = self._config.products.get(product_id)
        return policy if policy is not None and policy.enabled else None

    def resolve(self, product_id: str, role_id: str) -> AgentRoleDefinition | None:
        policy = self.policy(product_id)
        if policy is None:
            return None
        return next((role for role in policy.roles if role.role_id == role_id), None)


__all__ = ["ConfiguredAgentRoleRegistry"]
