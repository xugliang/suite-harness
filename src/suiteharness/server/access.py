"""Trusted control-plane issuance of short-lived, exact tool grants."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from types import MappingProxyType
from typing import Protocol

from suiteharness.execution import (
    CapabilityGrant,
    ToolEffect,
    ToolIdentity,
    ToolRegistry,
)
from suiteharness.runtime import RequestScope, ScopePath

_TOOL_NAME = re.compile(r"^[a-z][a-z0-9]*(?:[._:/-][a-z0-9]+)*$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MCP_SERVER_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_FEISHU_FILE_WRITES = frozenset({"suiteharness.fs.write", "suiteharness.fs.edit"})


class ToolAccessError(PermissionError):
    """A configured access template cannot safely authorize this request."""


class GrantAuthority(Protocol):
    """Capability authority operations needed by the server control plane."""

    async def issue(
        self,
        scope: ScopePath,
        *,
        tool_identities: Iterable[ToolIdentity],
        capabilities: Iterable[str] = (),
        principal_id: str | None = None,
        lifetime: timedelta = timedelta(hours=1),
    ) -> CapabilityGrant: ...

    async def revoke(self, grant_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class ToolAccessTemplate:
    """Exact aliases that one product may expose on one company channel.

    The three alias sets deliberately encode effects separately.  Moving a tool
    from read to write in its registration therefore makes an old template fail
    closed instead of silently gaining authority.
    """

    channel_id: str
    product_id: str
    read_aliases: tuple[str, ...] = ()
    write_aliases: tuple[str, ...] = ()
    destructive_aliases: tuple[str, ...] = ()
    lifetime: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.channel_id):
            raise ValueError("invalid access-template channel_id")
        if not _IDENTIFIER.fullmatch(self.product_id):
            raise ValueError("invalid access-template product_id")
        aliases = (*self.read_aliases, *self.write_aliases, *self.destructive_aliases)
        if any(not _TOOL_NAME.fullmatch(alias) for alias in aliases):
            raise ValueError("access-template aliases must be valid tool names")
        if len(aliases) != len(set(aliases)):
            raise ValueError("an alias may occur in only one access category")
        if self.lifetime <= timedelta(0) or self.lifetime > timedelta(hours=1):
            raise ValueError("tool grants must live for more than zero and at most one hour")
        if self.channel_id == "feishu":
            if self.destructive_aliases:
                raise ValueError("Feishu templates cannot enable destructive tools")
            if any(alias not in _FEISHU_FILE_WRITES for alias in self.write_aliases):
                raise ValueError(
                    "Feishu may enable only the built-in file write/edit aliases; "
                    "path policy is enforced separately"
                )

    @property
    def aliases(self) -> tuple[str, ...]:
        return (*self.read_aliases, *self.write_aliases, *self.destructive_aliases)

    @property
    def read_only(self) -> bool:
        return not self.write_aliases and not self.destructive_aliases


@dataclass(frozen=True, slots=True)
class IssuedToolGrant:
    """A grant plus the runner's independent coarse read-only guard."""

    grant: CapabilityGrant
    read_only: bool

    @property
    def grant_id(self) -> str:
        return self.grant.grant_id


@dataclass(frozen=True, slots=True)
class McpChannelAccessRule:
    """Server- or remote-tool-level MCP allowlist for one exact route."""

    server_ids: frozenset[str] = frozenset()
    tools_by_server: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        servers = frozenset(self.server_ids)
        tools = {
            server_id: frozenset(names)
            for server_id, names in self.tools_by_server.items()
        }
        if any(
            not _MCP_SERVER_ID.fullmatch(server_id)
            for server_id in servers | set(tools)
        ):
            raise ValueError("MCP access server ids must be valid names")
        if overlap := servers & set(tools):
            raise ValueError(
                f"MCP server and tool access rules must not overlap: {sorted(overlap)!r}"
            )
        for names in tools.values():
            if not names:
                raise ValueError("MCP per-server tool access must not be empty")
            if any(
                not name.strip() or len(name) > 256 or "\x00" in name
                for name in names
            ):
                raise ValueError("MCP remote tool names must be bounded and non-blank")
        object.__setattr__(self, "server_ids", servers)
        object.__setattr__(self, "tools_by_server", MappingProxyType(tools))


@dataclass(frozen=True, slots=True)
class AdditionalGrantTool:
    """One discovered MCP tool with its stable remote identity and grant needs."""

    server_id: str
    remote_name: str
    identity: ToolIdentity
    capabilities: frozenset[str]

    def __post_init__(self) -> None:
        if not _MCP_SERVER_ID.fullmatch(self.server_id):
            raise ValueError("invalid MCP grant server_id")
        if (
            not self.remote_name.strip()
            or len(self.remote_name) > 256
            or "\x00" in self.remote_name
        ):
            raise ValueError("invalid MCP grant remote tool name")
        if not isinstance(self.identity, ToolIdentity):
            raise TypeError("MCP grant identity must be a ToolIdentity")
        capabilities = frozenset(self.capabilities)
        if any(not _TOOL_NAME.fullmatch(item) for item in capabilities):
            raise ValueError("MCP grant capabilities must be valid names")
        if f"mcp.{self.server_id}.call" not in capabilities:
            raise ValueError("MCP grant tool is missing its server call capability")
        if not self.identity.name.startswith(f"mcp.{self.server_id}."):
            raise ValueError("MCP grant identity does not match its server_id")
        object.__setattr__(self, "capabilities", capabilities)


@dataclass(frozen=True, slots=True)
class AdditionalGrantMaterial:
    """Structured MCP inventory discovered by the trusted extension host."""

    tools: tuple[AdditionalGrantTool, ...] = ()

    def __post_init__(self) -> None:
        tools = tuple(self.tools)
        if any(not isinstance(item, AdditionalGrantTool) for item in tools):
            raise TypeError("additional material must contain AdditionalGrantTool values")
        identities = [item.identity for item in tools]
        remote_keys = [(item.server_id, item.remote_name) for item in tools]
        if len(identities) != len(set(identities)):
            raise ValueError("additional MCP material contains duplicate identities")
        if len(remote_keys) != len(set(remote_keys)):
            raise ValueError("additional MCP material contains duplicate remote tools")
        object.__setattr__(self, "tools", tools)


class ChannelGrantIssuer(Protocol):
    """Injectable application contract for a revocable grant lease."""

    def lease(self, scope: RequestScope) -> AbstractAsyncContextManager[IssuedToolGrant]: ...


class ConfiguredGrantIssuer:
    """Resolve aliases at request scope, then grant only concrete identities.

    Templates are selected by the exact ``(channel_id, product_id)`` pair.  A
    model, product plug-in, or inbound message cannot choose another template.
    Web writes and Bash may be listed explicitly, but the execution runner still
    performs its mandatory per-call approval policy.  Feishu is read-only unless
    its template explicitly lists ``suiteharness.fs.write`` and/or ``suiteharness.fs.edit``;
    their path whitelist remains enforced by the channel policy and file tool.
    """

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        authority: GrantAuthority,
        templates: Iterable[ToolAccessTemplate],
        additional_material_by_product: Mapping[str, AdditionalGrantMaterial] | None = None,
        mcp_access_by_route: Mapping[
            tuple[str, str], McpChannelAccessRule
        ] | None = None,
    ) -> None:
        self._registry = registry
        self._authority = authority
        indexed: dict[tuple[str, str], ToolAccessTemplate] = {}
        for template in templates:
            if not isinstance(template, ToolAccessTemplate):
                raise TypeError("templates must contain ToolAccessTemplate values")
            key = (template.channel_id, template.product_id)
            if key in indexed:
                raise ValueError("duplicate tool access template route")
            indexed[key] = template
        self._templates = indexed
        material = dict(additional_material_by_product or {})
        for product_id, item in material.items():
            if not _IDENTIFIER.fullmatch(product_id):
                raise ValueError("invalid additional grant material product_id")
            if not isinstance(item, AdditionalGrantMaterial):
                raise TypeError("additional material must contain AdditionalGrantMaterial values")
        self._additional_material = material
        access: dict[tuple[str, str], McpChannelAccessRule] = {}
        for key, rule in dict(mcp_access_by_route or {}).items():
            if (
                not isinstance(key, tuple)
                or len(key) != 2
                or not all(isinstance(item, str) for item in key)
                or not all(_IDENTIFIER.fullmatch(item) for item in key)
            ):
                raise ValueError("MCP access routes must be (channel_id, product_id) pairs")
            if key not in indexed:
                raise ValueError("MCP access route has no matching tool access template")
            if not isinstance(rule, McpChannelAccessRule):
                raise TypeError("MCP access routes must contain McpChannelAccessRule values")
            access[key] = rule
        self._mcp_access = access

    def lease(self, scope: RequestScope) -> AbstractAsyncContextManager[IssuedToolGrant]:
        if not isinstance(scope, RequestScope):
            raise TypeError("scope must be a RequestScope")
        return self._lease(scope)

    @asynccontextmanager
    async def _lease(self, scope: RequestScope) -> AsyncIterator[IssuedToolGrant]:
        template = self._templates.get((scope.channel_id, scope.product_id))
        if template is None:
            raise ToolAccessError("no tool access template matches this channel and product")

        identities: list[ToolIdentity] = []
        capabilities: set[str] = set()
        has_write = False
        for alias in template.read_aliases:
            identity, required = self._resolve(scope, alias, expected="read")
            identities.append(identity)
            capabilities.update(required)
        for alias in template.write_aliases:
            identity, required = self._resolve(scope, alias, expected="write")
            identities.append(identity)
            capabilities.update(required)
            has_write = True
        for alias in template.destructive_aliases:
            identity, required = self._resolve(scope, alias, expected="destructive")
            identities.append(identity)
            capabilities.update(required)
            has_write = True

        mcp_rule = self._mcp_access.get((scope.channel_id, scope.product_id))
        if mcp_rule is not None and (mcp_rule.server_ids or mcp_rule.tools_by_server):
            mcp_identities, mcp_capabilities, mcp_has_write = self._resolve_mcp(
                scope, mcp_rule
            )
            identities.extend(mcp_identities)
            capabilities.update(mcp_capabilities)
            has_write = has_write or mcp_has_write

        grant = await self._authority.issue(
            scope.path,
            tool_identities=identities,
            capabilities=capabilities,
            principal_id=scope.principal_id,
            lifetime=template.lifetime,
        )
        expected_identities = frozenset(identities)
        if (
            grant.tenant_id != scope.tenant_id
            or grant.product_id != scope.product_id
            or grant.principal_id != scope.principal_id
            or grant.tool_identities != expected_identities
            or grant.capabilities != frozenset(capabilities)
            or grant.expires_at - grant.issued_at > template.lifetime
        ):
            await self._authority.revoke(grant.grant_id)
            raise ToolAccessError("capability authority returned a mismatched grant")
        try:
            yield IssuedToolGrant(grant=grant, read_only=not has_write)
        finally:
            await self._authority.revoke(grant.grant_id)

    def _resolve(
        self,
        scope: RequestScope,
        alias: str,
        *,
        expected: str,
    ) -> tuple[ToolIdentity, frozenset[str]]:
        registered = self._registry.resolve(scope, alias)
        if registered is None:
            raise ToolAccessError(f"configured tool alias is unavailable: {alias}")
        if registered.spec.name != alias or registered.identity.name != alias:
            raise ToolAccessError("tool registry returned an alias/identity mismatch")

        effects = registered.spec.effects
        if expected == "read":
            matches = registered.spec.is_read
        elif expected == "write":
            matches = ToolEffect.WRITE in effects and ToolEffect.DESTRUCTIVE not in effects
        else:
            matches = ToolEffect.WRITE in effects and ToolEffect.DESTRUCTIVE in effects
        if not matches:
            raise ToolAccessError(
                f"configured {expected} alias has incompatible effects: {alias}"
            )
        if scope.channel_id == "feishu" and expected == "write":
            identity = registered.identity
            if not (
                alias in _FEISHU_FILE_WRITES
                and identity.namespace == "suiteharness"
                and identity.origin == "suiteharness.builtin.fs"
            ):
                raise ToolAccessError("Feishu write access requires an exact built-in file tool")
        return registered.identity, registered.spec.required_capabilities

    def _resolve_mcp(
        self,
        scope: RequestScope,
        rule: McpChannelAccessRule,
    ) -> tuple[list[ToolIdentity], set[str], bool]:
        material = self._additional_material.get(scope.product_id)
        if material is None:
            raise ToolAccessError("configured MCP access has no connected product inventory")
        requested_tools = {
            (server_id, remote_name)
            for server_id, names in rule.tools_by_server.items()
            for remote_name in names
        }
        found_tools: set[tuple[str, str]] = set()
        identities: list[ToolIdentity] = []
        capabilities: set[str] = set()
        has_write = False
        for item in sorted(
            material.tools,
            key=lambda tool: (tool.server_id, tool.remote_name, tool.identity.canonical_id),
        ):
            key = item.server_id, item.remote_name
            if item.server_id not in rule.server_ids and key not in requested_tools:
                continue
            registered = self._registry.resolve(scope, item.identity.name)
            if registered is None or registered.identity != item.identity:
                raise ToolAccessError("MCP grant material contains a stale tool identity")
            if registered.spec.required_capabilities != item.capabilities:
                raise ToolAccessError(
                    "MCP grant material capabilities do not match its exact tool"
                )
            if scope.channel_id == "feishu" and not registered.spec.is_read:
                raise ToolAccessError("Feishu MCP access permits read-only tools only")
            identities.append(item.identity)
            capabilities.update(item.capabilities)
            has_write = has_write or ToolEffect.WRITE in registered.spec.effects
            found_tools.add(key)
        if missing := requested_tools - found_tools:
            raise ToolAccessError(
                f"configured MCP tools were not discovered: {sorted(missing)!r}"
            )
        return identities, capabilities, has_write


__all__ = [
    "AdditionalGrantMaterial",
    "AdditionalGrantTool",
    "ChannelGrantIssuer",
    "ConfiguredGrantIssuer",
    "GrantAuthority",
    "IssuedToolGrant",
    "McpChannelAccessRule",
    "ToolAccessError",
    "ToolAccessTemplate",
]
