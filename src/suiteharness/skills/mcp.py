"""MCP transport adapter for an explicitly versioned skill application contract.

MCP has tools/resources/prompts, not a native universal Skill method. This
adapter uses standard tools/call to two deployment-selected tool names; it never
accepts a server URL, credentials, package or identity from model arguments.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from suiteharness.mcp.client import McpClient
from suiteharness.mcp.manager import McpClientManager
from suiteharness.mcp.models import CallToolResult
from suiteharness.runtime.scopes import RequestScope, ScopePath

from .models import (
    SkillAccess,
    SkillDescriptor,
    SkillDocument,
    SkillError,
    SkillLimits,
    content_sha256,
    skill_identifier,
)
from .protocols import SkillAccessPolicy, SkillProvider
from .registry import SkillRegistry

_HASH = re.compile(r"^[a-f0-9]{64}$")
_TOOL = re.compile(r"^[A-Za-z][A-Za-z0-9_./-]{0,255}$")
_DESCRIPTOR_FIELDS = {
    "skill_id", "name", "description", "version", "source_sha256", "display_name",
    "model_invocable", "user_invocable", "includes",
}


def _descriptor(raw: object) -> SkillDescriptor:
    if not isinstance(raw, dict) or set(raw) != _DESCRIPTOR_FIELDS:
        raise SkillError("skill_invalid", "Remote skill descriptor does not match its contract")
    if not isinstance(raw["includes"], list):
        raise SkillError("skill_invalid", "Remote skill dependencies must be an array")
    try:
        return SkillDescriptor(**{**raw, "includes": tuple(raw["includes"])})
    except (TypeError, ValueError) as exc:
        raise SkillError("skill_invalid", "Remote skill descriptor is invalid") from exc


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SkillError("skill_invalid", "Duplicate remote skill JSON field")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class BoundMcpSkillClient:
    """Borrow one configured manager connection; never expose its internals.

    The manager retains connection lifecycle, credentials, endpoint checks and
    tenant/product ownership. This object only binds the deployment's server_id
    to the small call_tool shape consumed by McpSkillProvider.
    """

    manager: McpClientManager
    tenant_id: str
    product_id: str
    server_id: str

    def __post_init__(self) -> None:
        # Use existing RequestScope identity rules rather than a second identity
        # format. No request or synthetic employee is constructed here.
        ScopePath.product(self.tenant_id, self.product_id)
        skill_identifier(self.server_id)

    async def call_tool(
        self, scope: RequestScope, name: str, arguments: dict | None = None
    ) -> CallToolResult:
        if scope.tenant_id != self.tenant_id or scope.product_id != self.product_id:
            raise SkillError("skill_not_available", "Skill is not available")
        # Keep the actual manager signature explicit. Do not bypass its public
        # lookup with manager._clients or resolve a client using a fake identity.
        return await self.manager.call_tool(scope, self.server_id, name, arguments or {})


class McpSkillProvider(SkillProvider):
    """Read-only access to one trusted package on an existing MCP connection.

    ``manifest`` and ``resource_hashes`` are a deployment-reviewed lockfile, not
    values supplied by the model or auto-approved from list_skills. This pins
    both raw metadata sources and the complete dependency-expanded resources.
    A remote update requires explicit re-registration; no stale-cache fallback.
    The provider borrows the client and never closes that shared connection.
    """

    def __init__(
        self,
        client: McpClient | BoundMcpSkillClient,
        *,
        namespace: str,
        package_id: str,
        manifest: tuple[SkillDescriptor, ...],
        resource_hashes: Mapping[str, Mapping[str, str]],
        list_tool: str = "skills.list",
        read_tool: str = "skills.read",
        timeout_seconds: float = 10,
        access_policy: SkillAccessPolicy | None = None,
        limits: SkillLimits | None = None,
    ) -> None:
        self.namespace = skill_identifier(namespace)
        self.package_id = skill_identifier(package_id)
        self.limits = limits or SkillLimits()
        if (
            type(timeout_seconds) not in {int, float}
            or not 0 < timeout_seconds <= 120
        ):
            raise ValueError("timeout_seconds must be in (0, 120]")
        if not _TOOL.fullmatch(list_tool) or not _TOOL.fullmatch(read_tool) or list_tool == read_tool:
            raise ValueError("MCP skill tool names must be distinct registered tool names")
        if len(manifest) > self.limits.max_skills:
            raise SkillError("skill_budget_exceeded", "Remote skill registration limit exceeded")
        if getattr(client, "product_id", namespace) != namespace:
            raise SkillError("skill_invalid", "MCP connection belongs to another product")
        self._registry = SkillRegistry(
            namespace, manifest, max_dependency_depth=self.limits.max_dependency_depth
        )
        known = {descriptor.skill_id for descriptor in manifest}
        if set(resource_hashes) != known:
            raise SkillError("skill_invalid", "Remote resource lockfile must cover the registered manifest")
        locks = {}
        for skill_id, resources in resource_hashes.items():
            if (
                not isinstance(resources, Mapping)
                or "instructions" not in resources
                or set(resources) - {"instructions", "reply"}
                or any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in resources.values())
            ):
                raise SkillError("skill_invalid", "Remote resource lockfile has invalid hashes")
            locks[skill_id] = MappingProxyType(dict(resources))
        self._resource_hashes = MappingProxyType(locks)
        self._client = client
        self._list_tool = list_tool
        self._read_tool = read_tool
        self._timeout_seconds = timeout_seconds
        self._access_policy = access_policy

    @classmethod
    def from_manager(
        cls,
        manager: McpClientManager,
        *,
        tenant_id: str,
        product_id: str,
        server_id: str,
        package_id: str,
        manifest: tuple[SkillDescriptor, ...],
        resource_hashes: Mapping[str, Mapping[str, str]],
        list_tool: str = "skills.list",
        read_tool: str = "skills.read",
        timeout_seconds: float = 10,
        access_policy: SkillAccessPolicy | None = None,
        limits: SkillLimits | None = None,
    ) -> McpSkillProvider:
        """Bind an existing product connection without obtaining a private client."""
        return cls(
            BoundMcpSkillClient(manager, tenant_id, product_id, server_id),
            namespace=product_id, package_id=package_id, manifest=manifest,
            resource_hashes=resource_hashes, list_tool=list_tool, read_tool=read_tool,
            timeout_seconds=timeout_seconds, access_policy=access_policy, limits=limits,
        )

    def static_manifest(self) -> tuple[SkillDescriptor, ...]:
        return self._registry.manifest()

    def _allowed(self, access: SkillAccess, descriptor: SkillDescriptor, resource_id: str) -> bool:
        if access.scope.product_id != self.namespace:
            return False
        tenant_id = getattr(self._client, "tenant_id", access.scope.tenant_id)
        if access.scope.tenant_id != tenant_id:
            return False
        return self._access_policy is None or bool(self._access_policy(access, descriptor, resource_id))

    @staticmethod
    def _invocable(access: SkillAccess, descriptor: SkillDescriptor) -> bool:
        return not (
            (access.source == "model" and not descriptor.model_invocable)
            or (access.source == "command" and not descriptor.user_invocable)
        )

    async def list_skills(self, access: SkillAccess) -> tuple[SkillDescriptor, ...]:
        locally_visible = {
            d.skill_id: d for d in self.static_manifest()
            if self._invocable(access, d) and self._allowed(access, d, "instructions")
        }
        if not locally_visible:
            return ()
        raw = await self._call(access, self._list_tool, {"package_id": self.package_id})
        if set(raw) != {"package_id", "skills"} or raw["package_id"] != self.package_id:
            raise SkillError("skill_invalid", "Remote skill directory package does not match")
        if not isinstance(raw["skills"], list) or len(raw["skills"]) > self.limits.max_skills:
            raise SkillError("skill_invalid", "Remote skill directory is invalid or exceeds its limit")
        visible = []
        seen = set()
        for item in raw["skills"]:
            descriptor = _descriptor(item)
            if descriptor.skill_id in seen:
                raise SkillError("skill_invalid", "Remote skill directory contains duplicate identifiers")
            seen.add(descriptor.skill_id)
            expected = self._registry.descriptor(descriptor.skill_id)
            if descriptor != expected:
                raise SkillError("skill_content_changed", "Remote skill directory changed; reload the product")
            if descriptor.skill_id in locally_visible:
                visible.append(expected)
        return tuple(sorted(visible, key=lambda d: d.skill_id))

    async def read_skill(
        self, access: SkillAccess, skill_id: str, *, resource_id: str = "instructions"
    ) -> SkillDocument:
        descriptor = self._registry.descriptor(skill_id)
        if (
            resource_id not in {"instructions", "reply"}
            or (resource_id == "reply" and access.source != "workflow")
            or not self._invocable(access, descriptor)
            or resource_id not in self._resource_hashes[skill_id]
        ):
            raise SkillError("skill_not_available", "Skill is not available")
        expanded = self._registry.expanded_ids(skill_id) if resource_id == "instructions" else (skill_id,)
        for dependency in expanded:
            if not self._allowed(access, self._registry.descriptor(dependency), resource_id):
                raise SkillError("skill_not_available", "Skill is not available")
        raw = await self._call(access, self._read_tool, {
            "package_id": self.package_id, "skill_id": skill_id,
            "resource_id": resource_id, "version": descriptor.version,
            "source_sha256": descriptor.source_sha256,
        })
        fields = {
            "package_id", "descriptor", "resource_id", "instructions", "instructions_sha256", "dependencies"
        }
        if set(raw) != fields or raw["package_id"] != self.package_id or raw["resource_id"] != resource_id:
            raise SkillError("skill_invalid", "Remote skill document does not match its contract")
        if _descriptor(raw["descriptor"]) != descriptor:
            raise SkillError("skill_content_changed", "Remote skill version changed; reload the product")
        instructions = raw["instructions"]
        expected_dependencies = tuple(item for item in expanded if item != skill_id)
        if not isinstance(raw["dependencies"], list) or tuple(raw["dependencies"]) != expected_dependencies:
            raise SkillError("skill_invalid", "Remote skill dependency composition does not match")
        if not isinstance(instructions, str) or not instructions.strip():
            raise SkillError("skill_invalid", "Remote skill instructions are empty or invalid")
        computed = content_sha256(instructions)
        if len(instructions.encode("utf-8")) > self.limits.max_instruction_bytes:
            raise SkillError("skill_budget_exceeded", "Complete remote skill instructions exceed the byte limit")
        if computed != raw["instructions_sha256"] or computed != self._resource_hashes[skill_id][resource_id]:
            raise SkillError("skill_content_changed", "Remote skill resource hash does not match its lockfile")
        return SkillDocument(
            descriptor, resource_id, instructions, computed, expected_dependencies, source="mcp"
        )

    async def _call(self, access: SkillAccess, tool_name: str, arguments: dict) -> dict:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                result = await self._client.call_tool(access.scope, tool_name, arguments)
        except SkillError:
            raise
        except TimeoutError as exc:
            raise SkillError("skill_read_failed", "Remote skill request timed out", retryable=True) from exc
        except Exception:
            # Provider/network errors may contain endpoints or bearer material.
            # Suppress the original exception in automatic traceback logging too.
            raise SkillError(
                "skill_read_failed", "Remote skill service is unavailable", retryable=True
            ) from None
        if not isinstance(result, CallToolResult) or result.is_error:
            raise SkillError("skill_read_failed", "Remote skill service rejected the request")
        raw = result.structured_content
        maximum = max(self.limits.max_instruction_bytes * 4, self.limits.max_skills * 2048)
        if raw is None:
            if len(result.content) != 1 or result.content[0].type != "text":
                raise SkillError("skill_invalid", "Remote skill service must return structured JSON")
            text = result.content[0].text
            try:
                text_size = len(text.encode("utf-8"))
            except UnicodeError as exc:
                raise SkillError("skill_invalid", "Remote skill response must be valid UTF-8") from exc
            if text_size > maximum:
                raise SkillError("skill_budget_exceeded", "Remote skill response limit exceeded")
            try:
                raw = json.loads(text, object_pairs_hook=_unique_object)
            except (ValueError, RecursionError) as exc:
                raise SkillError("skill_invalid", "Remote skill service returned invalid JSON") from exc
        elif len(json.dumps(raw, ensure_ascii=True).encode("utf-8")) > maximum:
            raise SkillError("skill_budget_exceeded", "Remote skill response limit exceeded")
        if not isinstance(raw, dict):
            raise SkillError("skill_invalid", "Remote skill response must be a JSON object")
        return raw


def mcp_skill_provider_from_config(
    manager: McpClientManager,
    *,
    tenant_id: str,
    product_id: str,
    config: Mapping[str, object],
    access_policy: SkillAccessPolicy | None = None,
    limits: SkillLimits | None = None,
) -> McpSkillProvider:
    """Product-activation factory for a credential-free, reviewed mapping.

    This is an explicit factory, not an implicit global YAML loader. A product
    chooses whether to call it and bind the resulting SKILL_PROVIDER; transports
    and authentication continue to be configured through existing MCP services.
    """
    required = {"server_id", "package_id", "manifest", "resource_hashes"}
    allowed = required | {"list_tool", "read_tool", "timeout_seconds"}
    if not isinstance(config, Mapping) or set(config) - allowed or not required <= set(config):
        raise SkillError("skill_invalid", "Unknown or missing MCP skill provider configuration")
    if not isinstance(config["manifest"], list):
        raise SkillError("skill_invalid", "MCP skill manifest must be an array")
    if not isinstance(config["resource_hashes"], Mapping):
        raise SkillError("skill_invalid", "MCP skill resource lockfile must be an object")
    for field in ("server_id", "package_id", "list_tool", "read_tool"):
        if field in config and not isinstance(config[field], str):
            raise SkillError("skill_invalid", "MCP skill provider names must be strings")
    timeout = config.get("timeout_seconds", 10)
    if type(timeout) not in {int, float} or not 0 < timeout <= 120:
        raise SkillError("skill_invalid", "MCP skill timeout must be in (0, 120]")
    return McpSkillProvider.from_manager(
        manager, tenant_id=tenant_id, product_id=product_id,
        server_id=config["server_id"], package_id=config["package_id"],
        manifest=tuple(_descriptor(item) for item in config["manifest"]),
        resource_hashes=config["resource_hashes"],
        list_tool=config.get("list_tool", "skills.list"),
        read_tool=config.get("read_tool", "skills.read"),
        timeout_seconds=config.get("timeout_seconds", 10),
        access_policy=access_policy, limits=limits,
    )


__all__ = ["BoundMcpSkillClient", "McpSkillProvider", "mcp_skill_provider_from_config"]
