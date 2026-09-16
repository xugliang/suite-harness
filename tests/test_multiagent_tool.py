"""The model-facing delegate tool accepts only a bounded role and task."""

import asyncio

import pytest
from jsonschema.exceptions import ValidationError

from suiteharness.execution import ToolEffect, ToolSpec
from suiteharness.execution.in_memory import InMemoryToolRegistry
from suiteharness.execution.protocols import ToolCallContext
from suiteharness.multiagent.service import DelegationService
from suiteharness.runtime import RequestScope, ScopePath


class _EchoDelegation(DelegationService):
    async def delegate(self, context, role_id, task):
        return {
            "principal": context.scope.principal_id,
            "product": context.scope.product_id,
            "role": role_id,
            "task": task,
        }


def _scope():
    return RequestScope(
        path=ScopePath.agent("tenant", "product", "parent", "session"),
        principal_id="seller-one",
        channel_id="web",
    )


def test_protected_delegate_accepts_only_role_and_task():
    from suiteharness.multiagent.tool import install_delegate_tool

    registry = InMemoryToolRegistry()
    handle = install_delegate_tool(registry, _EchoDelegation())
    item = registry.resolve(_scope(), "agent.delegate")
    assert item is not None
    assert item.spec.effects == frozenset({ToolEffect.READ})
    item.input_validator.validate({"role_id": "reader", "task": "Summarize"})
    with pytest.raises(ValidationError):
        item.input_validator.validate({
            "role_id": "reader", "task": "Summarize", "product_id": "other",
        })
    with pytest.raises(ValidationError):
        item.input_validator.validate({"role_id": "reader"})
    handle.close()
    assert registry.resolve(_scope(), "agent.delegate") is None


def test_protected_delegate_cannot_be_shadowed_by_product():
    from suiteharness.multiagent.tool import install_delegate_tool

    registry = InMemoryToolRegistry()
    handle = install_delegate_tool(registry, _EchoDelegation())

    async def replacement(context, arguments):
        return "wrong"

    with pytest.raises(ValueError, match="protected root tool"):
        registry.register(
            ScopePath.product("tenant", "product"),
            ToolSpec(name="agent.delegate", effects=frozenset({ToolEffect.READ})),
            replacement,
        )
    handle.close()


def test_delegate_handler_uses_runner_supplied_parent_context():
    from suiteharness.multiagent.tool import install_delegate_tool

    async def check():
        registry = InMemoryToolRegistry()
        handle = install_delegate_tool(registry, _EchoDelegation())
        item = registry.resolve(_scope(), "agent.delegate")
        assert item is not None
        context = ToolCallContext(
            scope=_scope(), run_id="run-one", call_id="call-one",
            tool_identity=item.identity, tool=item.spec, remaining_seconds=30.0,
        )
        result = await item.handler(
            context, {"role_id": "reader", "task": "Summarize"},
        )
        assert result == {
            "principal": "seller-one",
            "product": "product",
            "role": "reader",
            "task": "Summarize",
        }
        handle.close()

    asyncio.run(check())
