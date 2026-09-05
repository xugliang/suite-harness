"""Sandbox-only Bash built-in."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path

from pydantic import JsonValue

from suiteharness.execution import ToolCallContext
from suiteharness.sandbox import (
    SandboxBackend,
    SandboxLimits,
    SandboxMount,
    SandboxNetworkMode,
    SandboxNetworkPolicy,
    SandboxRequest,
    SandboxUnavailable,
)
from suiteharness.workspace import WorkspaceOperation, WorkspaceSpace, resolve_beneath

from .egress import ProductEgressArgumentPolicy
from .workspace import WorkspaceBindingResolver, WorkspaceToolBinding


@dataclass(frozen=True, slots=True)
class BashToolConfig:
    production: bool = True
    command_characters: int = 32_768
    limits: SandboxLimits = SandboxLimits()
    allowed_egress_profiles: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not 1 <= self.command_characters <= 1_000_000:
            raise ValueError("command_characters must be in [1, 1000000]")
        if any(not item or any(char.isspace() for char in item) for item in self.allowed_egress_profiles):
            raise ValueError("invalid Bash egress profile")


def _request_id(context: ToolCallContext) -> str:
    raw = f"{context.run_id}-{context.call_id}"
    if len(raw) <= 128:
        return raw
    return f"shell-{hashlib.sha256(raw.encode()).hexdigest()[:32]}"


def _container_location(
    binding: WorkspaceToolBinding,
    space: WorkspaceSpace,
    relative: str,
) -> tuple[Path, str]:
    root = binding.paths.product_root if space is WorkspaceSpace.PRODUCT else binding.paths.shared_root
    host = resolve_beneath(root, relative, require_exists=True)
    binding.access_policy.authorize(host, WorkspaceOperation.READ)
    suffix = host.relative_to(root.resolve(strict=True)).as_posix()
    base = f"/workspace/{space.value}"
    return host, base if suffix == "." else f"{base}/{suffix}"


def _mounts(binding: WorkspaceToolBinding) -> tuple[SandboxMount, ...]:
    roots = (
        (WorkspaceSpace.PRODUCT, binding.paths.product_root.resolve(strict=True)),
        (WorkspaceSpace.SHARED, binding.paths.shared_root.resolve(strict=True)),
    )
    mounts: dict[str, SandboxMount] = {}
    for space, root in roots:
        try:
            binding.access_policy.authorize(root, WorkspaceOperation.READ)
        except PermissionError:
            continue
        container_root = f"/workspace/{space.value}"
        whole_root_writable = any(root == writable for writable in binding.access_policy.writable_roots)
        mounts[container_root] = SandboxMount(
            root,
            container_root,
            read_only=not whole_root_writable,
        )
        for writable in binding.access_policy.writable_roots:
            if writable == root or not writable.is_relative_to(root):
                continue
            suffix = writable.relative_to(root).as_posix()
            container_path = f"{container_root}/{suffix}"
            mounts[container_path] = SandboxMount(writable, container_path, read_only=False)
    if not mounts:
        raise PermissionError("no workspace roots are readable for Bash")
    return tuple(mounts[key] for key in sorted(mounts, key=lambda item: (item.count("/"), item)))


class BashTool:
    """Execute Bash only through the configured sandbox backend.

    Policies with deletion disabled (including Feishu's default policy) cannot
    invoke an arbitrary shell, because a shell could otherwise bypass the
    absence of an exposed delete tool.
    """

    def __init__(
        self,
        workspaces: WorkspaceBindingResolver,
        sandbox: SandboxBackend,
        config: BashToolConfig | None = None,
        egress_policy: ProductEgressArgumentPolicy | None = None,
    ) -> None:
        self._workspaces = workspaces
        self._sandbox = sandbox
        self._config = config or BashToolConfig()
        self._egress_policy = egress_policy

    async def __call__(
        self,
        context: ToolCallContext,
        arguments: dict[str, JsonValue],
    ) -> JsonValue:
        if self._config.production and not self._sandbox.production_safe:
            raise SandboxUnavailable("production Bash requires a production-safe sandbox")
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        if "\x00" in command or len(command) > self._config.command_characters:
            raise ValueError("command exceeds its configured limit or contains NUL")
        binding = self._workspaces.resolve(context)
        if not binding.access_policy.allow_delete:
            raise PermissionError("Bash is forbidden when workspace deletion is disabled")
        try:
            space = WorkspaceSpace(str(arguments.get("space", "product")))
        except ValueError as exc:
            raise ValueError("space must be 'product' or 'shared'") from exc
        relative = str(arguments.get("working_directory", ".")).replace("\\", "/")
        if relative != "." and (not relative or ".." in relative.split("/") or relative.startswith("/")):
            raise ValueError("working_directory must remain inside the selected workspace")
        host_working, container_working = _container_location(binding, space, relative)
        if not host_working.is_dir():
            raise ValueError("Bash working_directory must be a directory")
        profile_value = arguments.get("egress_profile")
        if profile_value is not None and not isinstance(profile_value, str):
            raise ValueError("egress_profile must be a string")
        if self._egress_policy is not None:
            profile_value = self._egress_policy.authorize_bash(context, profile_value)
        if profile_value is None:
            network = SandboxNetworkPolicy()
        else:
            if (
                self._egress_policy is None
                and profile_value not in self._config.allowed_egress_profiles
            ):
                raise PermissionError("requested Bash egress profile is not allowed")
            network = SandboxNetworkPolicy(SandboxNetworkMode.EGRESS_PROFILE, profile_value)
        remaining = min(self._config.limits.timeout_seconds, context.remaining_seconds)
        if remaining <= 0:
            raise TimeoutError("no execution time remains for Bash")
        limits = replace(
            self._config.limits,
            timeout_seconds=remaining,
        )
        result = await self._sandbox.run(
            SandboxRequest(
                request_id=_request_id(context),
                argv=("bash", "-lc", command),
                mounts=_mounts(binding),
                working_directory=container_working,
                limits=limits,
                network=network,
            )
        )
        return {
            "exit_code": result.exit_code,
            "stdout": result.stdout.decode("utf-8", errors="replace"),
            "stderr": result.stderr.decode("utf-8", errors="replace"),
            "timed_out": result.timed_out,
            "output_truncated": result.output_truncated,
            "sandbox": self._sandbox.backend_id,
            "network": network.mode.value,
        }


__all__ = ["BashTool", "BashToolConfig"]
