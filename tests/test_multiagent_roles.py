"""Configured child roles are opt-in and never recursively delegate."""

import pytest
import yaml

from suiteharness.config import (
    AgentRoleDefinition,
    MultiAgentConfig,
    MultiAgentProductConfig,
)
from suiteharness.config.models import SuiteHarnessConfig
from suiteharness.multiagent.roles import ConfiguredAgentRoleRegistry


def search_role(**changes: object) -> AgentRoleDefinition:
    values: dict[str, object] = {
        "role_id": "search",
        "instruction": "Find relevant account evidence.",
        "allowed_tools": ("web.search",),
        "model_route": "research",
    }
    values.update(changes)
    return AgentRoleDefinition(**values)


def test_absent_multiagent_config_and_disabled_product_resolve_no_role():
    registry = ConfiguredAgentRoleRegistry(MultiAgentConfig())
    assert registry.resolve("sales-profile", "search") is None
    assert registry.policy("sales-profile") is None

    disabled = MultiAgentProductConfig(enabled=False, roles=(search_role(),))
    registry = ConfiguredAgentRoleRegistry(
        MultiAgentConfig(products={"sales-profile": disabled})
    )
    assert registry.resolve("sales-profile", "search") is None
    assert registry.policy("sales-profile") is None


def test_enabled_role_registry_returns_only_configured_role_and_policy():
    product = MultiAgentProductConfig(enabled=True, roles=(search_role(),))
    registry = ConfiguredAgentRoleRegistry(
        MultiAgentConfig(products={"sales-profile": product})
    )

    assert registry.resolve("sales-profile", "search") == search_role()
    assert registry.resolve("sales-profile", "unknown") is None
    assert registry.resolve("other-product", "search") is None
    assert registry.policy("sales-profile") == product


def test_duplicate_role_ids_are_rejected():
    with pytest.raises(ValueError, match="role_id"):
        MultiAgentProductConfig(enabled=True, roles=(search_role(), search_role()))


def test_child_role_cannot_receive_delegation_tool():
    with pytest.raises(ValueError, match="agent.delegate"):
        search_role(allowed_tools=("web.search", "agent.delegate"))


def deployment_config_data(tmp_path):
    with open("config/suiteharness.example.yaml", encoding="utf-8") as source:
        config_data = yaml.safe_load(source)
    config_data["workspace"]["root"] = str(tmp_path / "workspace")
    config_data["storage"]["root"] = str(tmp_path / "state")
    config_data["plugins"]["allowed_roots"] = [str(tmp_path / "plugins")]
    return config_data


def test_old_deployment_config_defaults_delegation_to_disabled(tmp_path):
    old_config = deployment_config_data(tmp_path)

    config = SuiteHarnessConfig.model_validate(old_config)

    assert config.multi_agent == MultiAgentConfig()


@pytest.mark.parametrize(
    ("product_id", "model_route", "message"),
    [
        ("unselected", "default", "outside customer_bundle"),
        ("product-a", "missing-route", "unknown model routes"),
    ],
)
def test_deployment_rejects_unselected_roles_and_unknown_role_routes(
    tmp_path, product_id: str, model_route: str, message: str
):
    config_data = deployment_config_data(tmp_path)
    config_data["multi_agent"] = {
        "products": {
            product_id: {
                "enabled": True,
                "roles": [
                    {
                        "role_id": "search",
                        "instruction": "Find relevant account evidence.",
                        "allowed_tools": ["web.search"],
                        "model_route": model_route,
                    }
                ],
            }
        }
    }

    with pytest.raises(ValueError, match=message):
        SuiteHarnessConfig.model_validate(config_data)
