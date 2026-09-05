from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from suiteharness.execution import ToolCallContext, ToolIdentity
from suiteharness.sandbox import SandboxAvailability, SandboxResult, SandboxUnavailable
from suiteharness.tools import (
    BashTool,
    BashToolConfig,
    MappingWorkspaceBindingResolver,
    WorkspaceToolBinding,
    builtin_specs,
)
from suiteharness.workspace import WorkspaceAccessPolicy, WorkspaceLayout

from .test_workspace_tools import _scope


class RecordingSandbox:
    backend_id = "recording"

    def __init__(self, *, production_safe: bool = True) -> None:
        self.production_safe = production_safe
        self.requests = []

    async def availability(self) -> SandboxAvailability:
        return SandboxAvailability(True)

    async def run(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return SandboxResult(request.request_id, 0, b"stdout", b"", False, False)


def _tool_context() -> ToolCallContext:
    name = "suiteharness.shell.bash"
    return ToolCallContext(
        _scope(),
        "run-1",
        "call-1",
        ToolIdentity(
            namespace="suiteharness",
            name=name,
            origin="suiteharness.builtin.shell",
            version="1",
        ),
        next(item for item in builtin_specs() if item.name == name),
        30,
    )


def _resolver(tmp_path: Path, *, allow_delete: bool):  # type: ignore[no-untyped-def]
    paths = WorkspaceLayout(tmp_path / "workspace").prepare_product("acme", "sales")
    policy = WorkspaceAccessPolicy(
        readable_roots=(paths.product_root, paths.shared_root),
        writable_roots=(paths.product_root,),
        allow_delete=allow_delete,
    )
    return MappingWorkspaceBindingResolver(
        {("acme", "sales"): WorkspaceToolBinding(paths, policy)}
    )


def test_bash_always_uses_sandbox_and_disables_network_by_default(tmp_path: Path) -> None:
    sandbox = RecordingSandbox()
    tool = BashTool(_resolver(tmp_path, allow_delete=True), sandbox)

    result = asyncio.run(tool(_tool_context(), {"command": "printf ok"}))

    assert result["sandbox"] == "recording"
    request = sandbox.requests[0]
    assert request.argv == ("bash", "-lc", "printf ok")
    assert request.network.mode.value == "none"
    assert any(not mount.read_only for mount in request.mounts)


def test_production_bash_fails_closed_for_unsafe_local_backend(tmp_path: Path) -> None:
    sandbox = RecordingSandbox(production_safe=False)
    tool = BashTool(_resolver(tmp_path, allow_delete=True), sandbox)

    with pytest.raises(SandboxUnavailable, match="production-safe"):
        asyncio.run(tool(_tool_context(), {"command": "true"}))
    assert sandbox.requests == []


def test_non_deleting_feishu_style_policy_cannot_invoke_arbitrary_shell(tmp_path: Path) -> None:
    sandbox = RecordingSandbox()
    tool = BashTool(_resolver(tmp_path, allow_delete=False), sandbox)

    with pytest.raises(PermissionError, match="deletion is disabled"):
        asyncio.run(tool(_tool_context(), {"command": "rm -f file"}))
    assert sandbox.requests == []


def test_bash_network_needs_an_explicit_admin_profile(tmp_path: Path) -> None:
    sandbox = RecordingSandbox()
    tool = BashTool(
        _resolver(tmp_path, allow_delete=True),
        sandbox,
        BashToolConfig(allowed_egress_profiles=frozenset({"domestic-web"})),
    )
    asyncio.run(
        tool(
            _tool_context(),
            {"command": "curl https://example.cn", "egress_profile": "domestic-web"},
        )
    )
    assert sandbox.requests[0].network.egress_profile == "domestic-web"

    with pytest.raises(PermissionError, match="not allowed"):
        asyncio.run(
            tool(
                _tool_context(),
                {"command": "curl https://example.com", "egress_profile": "unknown"},
            )
        )
