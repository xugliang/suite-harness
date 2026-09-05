"""Bridge remote MCP tools into SuiteHarness's single trusted ExecutionRunner path."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from pydantic import JsonValue

from suiteharness.execution import (
    InMemoryToolRegistry,
    ToolEffect,
    ToolIdentity,
    ToolRegistrationHandle,
    ToolSpec,
)
from suiteharness.execution.protocols import ToolCallContext
from suiteharness.runtime.effects import EffectScope
from suiteharness.runtime.scopes import RequestScope, ScopeKind, ScopePath
from suiteharness.security import UnsafeJsonSchemaError, harden_untrusted_json_schema

from .manager import McpClientManager
from .models import (
    McpProtocolError,
    McpTool,
    McpToolEffectOverride,
    McpToolSecurityOverride,
)

_UNSAFE_ALIAS = re.compile(r"[^a-z0-9]+")


def mcp_tool_alias(server_id: str, tool: McpTool) -> str:
    """Create a deterministic, valid and collision-resistant model-facing alias."""

    slug = _UNSAFE_ALIAS.sub("-", tool.name.lower()).strip("-") or "tool"
    slug = slug[:40].rstrip("-") or "tool"
    contract = json.dumps(
        {"name": tool.name, "inputSchema": tool.input_schema},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(contract).hexdigest()[:12]
    return f"mcp.{server_id}.{slug}-{digest}"


def mcp_tool_spec(
    server_id: str,
    tool: McpTool,
    override: McpToolSecurityOverride | None = None,
) -> ToolSpec:
    """Map MCP metadata without trusting remote effect annotations.

    The conservative default is WRITE+EXTERNAL. Only an explicit
    administrator override with a recorded rationale may lower it to READ.
    """

    try:
        safe_input_schema = harden_untrusted_json_schema(
            tool.input_schema,
            label="MCP tool inputSchema",
        )
    except UnsafeJsonSchemaError as exc:
        raise McpProtocolError(str(exc)) from exc

    effects = {ToolEffect.WRITE, ToolEffect.EXTERNAL}
    capabilities = {f"mcp.{server_id}.call"}
    if override is not None:
        capabilities.update(override.required_capabilities)
        if override.effect is McpToolEffectOverride.READ:
            effects = {ToolEffect.READ, ToolEffect.EXTERNAL}
        elif override.effect is McpToolEffectOverride.DESTRUCTIVE:
            effects = {ToolEffect.WRITE, ToolEffect.DESTRUCTIVE, ToolEffect.EXTERNAL}
    annotation_note = ""
    if tool.annotations is not None:
        annotation_note = " Remote MCP annotations are informational only."
    return ToolSpec(
        name=mcp_tool_alias(server_id, tool),
        description=(tool.description or tool.title or tool.name) + annotation_note,
        effects=frozenset(effects),
        required_capabilities=frozenset(capabilities),
        input_schema=safe_input_schema,
    )


@dataclass(frozen=True, slots=True)
class McpBridgedTool:
    server_id: str
    remote_name: str
    alias: str
    identity: ToolIdentity
    spec: ToolSpec


class McpToolBridgeHandle:
    """Own all registrations created by one bridge activation."""

    def __init__(
        self,
        registrations: tuple[ToolRegistrationHandle, ...],
        tools: tuple[McpBridgedTool, ...],
    ) -> None:
        self._registrations = registrations
        self.tools = tools
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        for registration in reversed(self._registrations):
            registration.close()
        self._closed = True


class McpToolBridge:
    """Discover and register MCP tools; it never invokes tools directly."""

    def __init__(
        self,
        manager: McpClientManager,
        registry: InMemoryToolRegistry,
    ) -> None:
        self._manager = manager
        self._registry = registry

    async def install(
        self,
        request_scope: RequestScope,
        server_id: str,
        *,
        registration_scope: ScopePath,
        overrides: dict[str, McpToolSecurityOverride] | None = None,
        effects: EffectScope | None = None,
    ) -> McpToolBridgeHandle:
        if registration_scope.kind is not ScopeKind.PRODUCT:
            raise ValueError("MCP tools must be registered at product scope")
        if (
            registration_scope.tenant_id != request_scope.tenant_id
            or registration_scope.product_id != request_scope.product_id
        ):
            raise ValueError("MCP registration scope does not match its authenticated request")
        configured_overrides = overrides or {}
        client = self._manager.client(request_scope, server_id)
        remote_tools = await client.all_tools(request_scope)
        names = [tool.name for tool in remote_tools]
        if len(names) != len(set(names)):
            raise ValueError("MCP server returned duplicate tool names")
        unknown = set(configured_overrides).difference(names)
        if unknown:
            raise ValueError(f"security override references unknown MCP tools: {sorted(unknown)!r}")

        registrations: list[ToolRegistrationHandle] = []
        bridged: list[McpBridgedTool] = []
        try:
            for remote in remote_tools:
                spec = mcp_tool_spec(server_id, remote, configured_overrides.get(remote.name))

                async def handler(
                    context: ToolCallContext,
                    arguments: dict[str, JsonValue],
                    *,
                    remote_name: str = remote.name,
                ) -> JsonValue:
                    result = await self._manager.call_tool(
                        context.scope,
                        server_id,
                        remote_name,
                        arguments,
                    )
                    return result.model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    )

                registration = self._registry.register(
                    registration_scope,
                    spec,
                    handler,
                )
                registrations.append(registration)
                bridged.append(
                    McpBridgedTool(
                        server_id=server_id,
                        remote_name=remote.name,
                        alias=spec.name,
                        identity=registration.identity,
                        spec=spec,
                    )
                )
        except BaseException:
            for registration in reversed(registrations):
                registration.close()
            raise
        handle = McpToolBridgeHandle(tuple(registrations), tuple(bridged))
        if effects is not None:
            try:
                effects.callback(f"mcp-tool-bridge:{server_id}", handle.close)
            except BaseException:
                handle.close()
                raise
        return handle


__all__ = [
    "McpBridgedTool",
    "McpToolBridge",
    "McpToolBridgeHandle",
    "mcp_tool_alias",
    "mcp_tool_spec",
]
