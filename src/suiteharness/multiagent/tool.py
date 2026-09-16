"""Protected model-facing entry for trusted, same-product child delegation."""

from __future__ import annotations

from pydantic import JsonValue

from suiteharness.execution import ToolEffect, ToolSpec
from suiteharness.execution.in_memory import InMemoryToolRegistry, ToolRegistrationHandle
from suiteharness.execution.protocols import ToolCallContext

from .service import DelegationDenied, DelegationService

_DELEGATE_SPEC = ToolSpec(
    name="agent.delegate",
    description=(
        "把一个有边界的任务交给当前产品已配置的子 Agent 角色执行。"
        "只能提供 role_id 和 task；子 Agent 权限与执行预算由框架控制。"
    ),
    effects=frozenset({ToolEffect.READ}),
    input_schema={
        "type": "object",
        "properties": {
            "role_id": {
                "type": "string",
                "pattern": "^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$",
                "maxLength": 128,
            },
            "task": {"type": "string", "minLength": 1, "maxLength": 4096},
        },
        "required": ["role_id", "task"],
        "additionalProperties": False,
    },
)


def install_delegate_tool(
    registry: InMemoryToolRegistry, service: DelegationService
) -> ToolRegistrationHandle:
    """Register one unshadowable root tool; grants remain a separate decision."""

    if not isinstance(registry, InMemoryToolRegistry):
        raise TypeError("delegation requires the protected framework tool registry")
    if not isinstance(service, DelegationService):
        raise TypeError("delegation requires a nominal service")

    async def delegate(
        context: ToolCallContext, arguments: dict[str, JsonValue]
    ) -> JsonValue:
        role_id = arguments.get("role_id")
        task = arguments.get("task")
        if not isinstance(role_id, str) or not isinstance(task, str):
            raise DelegationDenied("a configured role and bounded task are required")
        return await service.delegate(context, role_id, task)

    return registry.register_protected(_DELEGATE_SPEC, delegate)


__all__ = ["install_delegate_tool"]
