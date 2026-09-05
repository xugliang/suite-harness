from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from suiteharness.execution import (
    ExecutionStrategies,
    InMemoryApprovalStore,
    InMemoryAuditJournal,
    InMemoryCapabilityAuthority,
    InMemoryToolRegistry,
    RunRequest,
    ToolCallContext,
    ToolEffect,
    ToolIdentity,
    ToolIntent,
    ToolSpec,
    WorkflowDecision,
)
from suiteharness.execution.runner import ExecutionRunner
from suiteharness.runtime.scopes import RequestScope, ScopePath
from suiteharness.tools import (
    BuiltinToolInstaller,
    MappingWorkspaceBindingResolver,
    WorkspaceFileTools,
    WorkspaceToolBinding,
    builtin_specs,
)
from suiteharness.web import SearchResponse
from suiteharness.workspace import (
    SecurePosixWorkspaceFileSystem,
    WorkspaceAccessDenied,
    WorkspaceAccessPolicy,
    WorkspaceLayout,
    WorkspaceSpace,
    WritableWorkspaceRoot,
    secure_workspace_supported,
)


class UnusedSandbox:
    backend_id = "unused"
    production_safe = True

    async def availability(self):  # type: ignore[no-untyped-def]
        raise AssertionError("sandbox should not be called")

    async def run(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError("sandbox should not be called")


class UnusedSearch:
    async def search(self, request, *, provider_id=None):  # type: ignore[no-untyped-def]
        return SearchResponse("unused", request.query, ())


class UnusedFetch:
    async def fetch(self, url, *, egress_profile=None):  # type: ignore[no-untyped-def]
        raise AssertionError("fetch should not be called")


def _scope() -> RequestScope:
    return RequestScope(
        path=ScopePath.agent("acme", "sales", "agent-1", "session-1"),
        principal_id="user-1",
        request_id="request-1",
        correlation_id="correlation-1",
    )


def _context(name: str = "suiteharness.fs.read") -> ToolCallContext:
    spec = next(item for item in builtin_specs() if item.name == name)
    return ToolCallContext(
        scope=_scope(),
        run_id="run-1",
        call_id="call-1",
        tool_identity=ToolIdentity(
            namespace="suiteharness",
            name=name,
            origin="suiteharness.builtin.fs",
            version="1",
        ),
        tool=spec,
        remaining_seconds=10,
    )


def _binding(tmp_path: Path, *, policy: str = "read-write"):  # type: ignore[no-untyped-def]
    paths = WorkspaceLayout(tmp_path / "workspace").prepare_product("acme", "sales")
    exports = paths.product_root / "exports"
    exports.mkdir()
    if policy == "feishu":
        access = WorkspaceAccessPolicy.for_feishu(
            paths,
            [WritableWorkspaceRoot(WorkspaceSpace.PRODUCT, "exports")],
        )
    elif policy == "read-only":
        access = WorkspaceAccessPolicy.read_only(paths)
    else:
        access = WorkspaceAccessPolicy(
            readable_roots=(paths.product_root, paths.shared_root),
            writable_roots=(paths.product_root, paths.shared_root),
            allow_delete=True,
        )
    resolver = MappingWorkspaceBindingResolver(
        {("acme", "sales"): WorkspaceToolBinding(paths, access)}
    )
    return paths, resolver


def test_feishu_can_write_only_the_explicit_directory_and_has_no_delete_tool(
    tmp_path: Path,
) -> None:
    paths, resolver = _binding(tmp_path, policy="feishu")
    tools = WorkspaceFileTools(resolver)

    result = asyncio.run(
        tools.write(
            _context("suiteharness.fs.write"),
            {"path": "exports/report.md", "content": "安全输出"},
        )
    )
    assert result["bytes"] == len("安全输出".encode())
    assert (paths.product_root / "exports" / "report.md").read_text(encoding="utf-8") == "安全输出"
    with pytest.raises(WorkspaceAccessDenied):
        asyncio.run(
            tools.write(
                _context("suiteharness.fs.write"),
                {"path": "private.md", "content": "forbidden"},
            )
        )
    assert "suiteharness.fs.delete" not in {spec.name for spec in builtin_specs()}


def test_read_only_policy_denies_all_file_writes(tmp_path: Path) -> None:
    _, resolver = _binding(tmp_path, policy="read-only")
    with pytest.raises(WorkspaceAccessDenied):
        asyncio.run(
            WorkspaceFileTools(resolver).write(
                _context("suiteharness.fs.write"),
                {"path": "new.txt", "content": "no"},
            )
        )


def test_workspace_policy_can_be_narrower_for_feishu_than_web(tmp_path: Path) -> None:
    paths = WorkspaceLayout(tmp_path / "workspace").prepare_product("acme", "sales")
    exports = paths.product_root / "exports"
    exports.mkdir()
    web_policy = WorkspaceAccessPolicy(
        readable_roots=(paths.product_root, paths.shared_root),
        writable_roots=(paths.product_root, paths.shared_root),
        allow_delete=True,
    )
    feishu_policy = WorkspaceAccessPolicy.for_feishu(
        paths,
        [WritableWorkspaceRoot(WorkspaceSpace.PRODUCT, "exports")],
    )
    resolver = MappingWorkspaceBindingResolver()
    resolver.register(
        "acme", "sales", WorkspaceToolBinding(paths, web_policy), channel_id="web"
    )
    resolver.register(
        "acme", "sales", WorkspaceToolBinding(paths, feishu_policy), channel_id="feishu"
    )
    tools = WorkspaceFileTools(resolver)
    web = _context("suiteharness.fs.write")
    web = ToolCallContext(
        scope=RequestScope(
            web.scope.path,
            web.scope.principal_id,
            channel_id="web",
            request_id=web.scope.request_id,
            correlation_id=web.scope.correlation_id,
        ),
        run_id=web.run_id,
        call_id=web.call_id,
        tool_identity=web.tool_identity,
        tool=web.tool,
        remaining_seconds=web.remaining_seconds,
    )
    feishu = ToolCallContext(
        scope=RequestScope(
            web.scope.path,
            web.scope.principal_id,
            channel_id="feishu",
            request_id=web.scope.request_id,
            correlation_id=web.scope.correlation_id,
        ),
        run_id=web.run_id,
        call_id="call-2",
        tool_identity=web.tool_identity,
        tool=web.tool,
        remaining_seconds=web.remaining_seconds,
    )

    asyncio.run(tools.write(web, {"path": "private.txt", "content": "web"}))
    with pytest.raises(WorkspaceAccessDenied):
        asyncio.run(tools.write(feishu, {"path": "private.txt", "content": "feishu"}))
    asyncio.run(tools.write(feishu, {"path": "exports/report.txt", "content": "ok"}))


def test_paths_and_symlinks_cannot_escape_the_selected_workspace(tmp_path: Path) -> None:
    paths, resolver = _binding(tmp_path)
    tools = WorkspaceFileTools(resolver)
    with pytest.raises((ValueError, WorkspaceAccessDenied)):
        asyncio.run(tools.read(_context(), {"path": "../shared/secret.txt"}))

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = paths.product_root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks is not permitted on this host")
    with pytest.raises(ValueError, match="escapes"):
        asyncio.run(tools.read(_context(), {"path": "escape/secret.txt"}))


def test_write_rejects_symlink_components_even_when_target_stays_inside(tmp_path: Path) -> None:
    paths, resolver = _binding(tmp_path)
    actual = paths.product_root / "actual"
    actual.mkdir()
    link = paths.product_root / "alias"
    try:
        link.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks is not permitted on this host")

    with pytest.raises(ValueError, match="symbolic links"):
        asyncio.run(
            WorkspaceFileTools(resolver).write(
                _context("suiteharness.fs.write"),
                {"path": "alias/new.txt", "content": "refused"},
            )
        )
    assert not (actual / "new.txt").exists()


def test_edit_requires_the_exact_expected_match_count_and_is_atomic(tmp_path: Path) -> None:
    paths, resolver = _binding(tmp_path)
    target = paths.product_root / "note.txt"
    target.write_text("old old", encoding="utf-8")
    tools = WorkspaceFileTools(resolver)

    with pytest.raises(ValueError, match="expected 1 matches but found 2"):
        asyncio.run(
            tools.edit(
                _context("suiteharness.fs.edit"),
                {"path": "note.txt", "old_text": "old", "new_text": "new"},
            )
        )
    assert target.read_text(encoding="utf-8") == "old old"

    result = asyncio.run(
        tools.edit(
            _context("suiteharness.fs.edit"),
            {
                "path": "note.txt",
                "old_text": "old",
                "new_text": "new",
                "expected_replacements": 2,
            },
        )
    )
    assert result["replacements"] == 2
    assert target.read_text(encoding="utf-8") == "new new"
    assert not any(item.name.startswith(".suiteharness-write-") for item in target.parent.iterdir())


def test_glob_and_grep_are_bounded_and_do_not_return_binary_files(tmp_path: Path) -> None:
    paths, resolver = _binding(tmp_path)
    (paths.product_root / "a.txt").write_text("needle one\nother", encoding="utf-8")
    (paths.product_root / "b.txt").write_text("needle two", encoding="utf-8")
    (paths.product_root / "binary.dat").write_bytes(b"\xff\xfe\x00")
    tools = WorkspaceFileTools(resolver)

    globbed = asyncio.run(
        tools.glob(_context("suiteharness.fs.glob"), {"pattern": "*.txt", "limit": 1})
    )
    assert len(globbed["matches"]) == 1
    assert globbed["truncated"] is True
    found = asyncio.run(
        tools.grep(
            _context("suiteharness.fs.grep"),
            {"query": "needle", "file_pattern": "*.txt", "limit": 1},
        )
    )
    assert len(found["matches"]) == 1
    assert found["truncated"] is True


def test_grep_rejects_common_catastrophic_regex_constructs(tmp_path: Path) -> None:
    _, resolver = _binding(tmp_path)
    with pytest.raises(ValueError, match="nested repetition"):
        asyncio.run(
            WorkspaceFileTools(resolver).grep(
                _context("suiteharness.fs.grep"),
                {"query": "(a+)+$", "regex": True},
            )
        )


def test_grep_times_out_catastrophic_pattern_not_caught_by_shape_filter(
    tmp_path: Path,
) -> None:
    paths, resolver = _binding(tmp_path)
    (paths.product_root / "attack.txt").write_text("a" * 100_000 + "!", encoding="utf-8")
    with pytest.raises(ValueError, match="safe execution time"):
        asyncio.run(
            WorkspaceFileTools(resolver).grep(
                _context("suiteharness.fs.grep"),
                {"query": "^(a|aa)+$", "regex": True},
            )
        )


def test_installer_registers_protected_root_identities_and_rolls_back(tmp_path: Path) -> None:
    _, resolver = _binding(tmp_path)
    registry = InMemoryToolRegistry()
    installed = BuiltinToolInstaller(
        registry,
        workspaces=resolver,
        sandbox=UnusedSandbox(),
        web_search=UnusedSearch(),  # type: ignore[arg-type]
        web_fetch=UnusedFetch(),  # type: ignore[arg-type]
    ).install()

    assert len(installed.identities) == 9
    assert all(identity.namespace == "suiteharness" for identity in installed.identities)
    with pytest.raises(ValueError, match="cannot be shadowed"):
        registry.register(
            ScopePath.product("acme", "sales"),
            ToolSpec(name="suiteharness.fs.read", effects=frozenset({ToolEffect.READ})),
            lambda context, arguments: None,  # type: ignore[arg-type,return-value]
        )
    installed.close()
    assert all(handle.closed for handle in installed.handles)


def test_builtin_file_read_executes_through_the_root_runner(tmp_path: Path) -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        paths, resolver = _binding(tmp_path)
        (paths.product_root / "hello.txt").write_text("hello", encoding="utf-8")
        registry = InMemoryToolRegistry()
        installed = BuiltinToolInstaller(
            registry,
            workspaces=resolver,
            sandbox=UnusedSandbox(),
            web_search=UnusedSearch(),  # type: ignore[arg-type]
            web_fetch=UnusedFetch(),  # type: ignore[arg-type]
        ).install()
        identity = next(item for item in installed.identities if item.name == "suiteharness.fs.read")
        authority = InMemoryCapabilityAuthority()
        scope = _scope()
        grant = await authority.issue(
            scope.path,
            tool_identities=(identity,),
            capabilities=("workspace.read",),
            principal_id=scope.principal_id,
        )
        runner = ExecutionRunner(
            tools=registry,
            capabilities=authority,
            approvals=InMemoryApprovalStore(),
            journal=InMemoryAuditJournal(),
        )

        class Workflow:
            calls = 0

            async def next(self, request, frame, prompt):  # type: ignore[no-untyped-def]
                self.calls += 1
                if self.calls == 1:
                    return WorkflowDecision.tools(
                        ToolIntent(
                            call_id="read-1",
                            tool_name="suiteharness.fs.read",
                            arguments={"path": "hello.txt"},
                        )
                    )
                return WorkflowDecision.final(frame.observations[0].result)

        return await runner.run(
            RunRequest(
                run_id="run-1",
                scope=scope,
                grant_id=grant.grant_id,
                input="read",
            ),
            ExecutionStrategies(workflow=Workflow()),
        )

    result = asyncio.run(exercise())
    assert result.output["content"] == "hello"
    assert result.observations[0].tool_name == "suiteharness.fs.read"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics are required")
def test_write_does_not_require_a_process_global_current_directory(tmp_path: Path) -> None:
    paths, resolver = _binding(tmp_path)
    original = Path.cwd()
    asyncio.run(
        WorkspaceFileTools(resolver).write(
            _context("suiteharness.fs.write"), {"path": "cwd-independent.txt", "content": "ok"}
        )
    )
    assert Path.cwd() == original
    assert (paths.product_root / "cwd-independent.txt").is_file()


@pytest.mark.skipif(
    not secure_workspace_supported(),
    reason="Linux descriptor-relative filesystem operations are required",
)
def test_secure_file_tools_reject_final_and_intermediate_symlinks(tmp_path: Path) -> None:
    paths, resolver = _binding(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside-secret", encoding="utf-8")
    (outside / "target.txt").write_text("outside-target", encoding="utf-8")
    (paths.product_root / "file-link").symlink_to(outside / "secret.txt")
    (paths.product_root / "dir-link").symlink_to(outside, target_is_directory=True)
    tools = WorkspaceFileTools(
        resolver, secure_filesystem=SecurePosixWorkspaceFileSystem()
    )

    with pytest.raises(ValueError):
        asyncio.run(tools.read(_context(), {"path": "file-link"}))
    with pytest.raises(ValueError):
        asyncio.run(
            tools.edit(
                _context("suiteharness.fs.edit"),
                {"path": "file-link", "old_text": "outside", "new_text": "changed"},
            )
        )
    with pytest.raises(ValueError):
        asyncio.run(
            tools.write(
                _context("suiteharness.fs.write"),
                {"path": "file-link", "content": "changed", "overwrite": True},
            )
        )
    with pytest.raises(ValueError):
        asyncio.run(tools.list(_context("suiteharness.fs.list"), {"path": "dir-link"}))
    with pytest.raises(ValueError):
        asyncio.run(
            tools.read(_context(), {"path": "dir-link/secret.txt"})
        )
    with pytest.raises(ValueError):
        asyncio.run(
            tools.write(
                _context("suiteharness.fs.write"),
                {"path": "dir-link/new.txt", "content": "changed"},
            )
        )

    globbed = asyncio.run(
        tools.glob(
            _context("suiteharness.fs.glob"), {"pattern": "dir-link/**/secret.txt"}
        )
    )
    assert globbed["matches"] == []
    with pytest.raises(ValueError):
        asyncio.run(
            tools.grep(
                _context("suiteharness.fs.grep"),
                {"path": "dir-link", "query": "outside-secret"},
            )
        )
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "outside-secret"
    assert (outside / "target.txt").read_text(encoding="utf-8") == "outside-target"
    assert not (outside / "new.txt").exists()


@pytest.mark.skipif(
    not secure_workspace_supported(),
    reason="Linux descriptor-relative filesystem operations are required",
)
def test_secure_read_stays_on_open_parent_during_directory_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, resolver = _binding(tmp_path)
    inside = paths.product_root / "safe"
    inside.mkdir()
    (inside / "secret.txt").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    anchored = paths.product_root / "anchored"
    filesystem = SecurePosixWorkspaceFileSystem()
    original = filesystem._open_child_directory
    swapped = False

    def swap_after_open(parent_fd: int, name: str, relative: str) -> int:
        nonlocal swapped
        descriptor = original(parent_fd, name, relative)
        if relative == "safe" and not swapped:
            swapped = True
            inside.rename(anchored)
            inside.symlink_to(outside, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(filesystem, "_open_child_directory", swap_after_open)
    result = asyncio.run(
        WorkspaceFileTools(resolver, secure_filesystem=filesystem).read(
            _context(), {"path": "safe/secret.txt"}
        )
    )
    assert result["content"] == "inside"
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "outside"


@pytest.mark.skipif(
    not secure_workspace_supported(),
    reason="Linux descriptor-relative filesystem operations are required",
)
def test_feishu_write_stays_in_authorized_directory_during_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, resolver = _binding(tmp_path, policy="feishu")
    exports = paths.product_root / "exports"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "report.txt"
    sentinel.write_text("outside", encoding="utf-8")
    anchored = paths.product_root / "anchored-exports"
    filesystem = SecurePosixWorkspaceFileSystem()
    original = filesystem._open_child_directory
    swapped = False

    def swap_after_open(parent_fd: int, name: str, relative: str) -> int:
        nonlocal swapped
        descriptor = original(parent_fd, name, relative)
        if relative == "exports" and not swapped:
            swapped = True
            exports.rename(anchored)
            exports.symlink_to(outside, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(filesystem, "_open_child_directory", swap_after_open)
    asyncio.run(
        WorkspaceFileTools(resolver, secure_filesystem=filesystem).write(
            _context("suiteharness.fs.write"),
            {"path": "exports/report.txt", "content": "inside", "overwrite": True},
        )
    )
    assert sentinel.read_text(encoding="utf-8") == "outside"
    assert (anchored / "report.txt").read_text(encoding="utf-8") == "inside"


@pytest.mark.skipif(
    not secure_workspace_supported(),
    reason="Linux descriptor-relative filesystem operations are required",
)
def test_secure_no_overwrite_publication_is_atomic(tmp_path: Path) -> None:
    paths, resolver = _binding(tmp_path)
    tools = WorkspaceFileTools(
        resolver, secure_filesystem=SecurePosixWorkspaceFileSystem()
    )

    async def race() -> list[object]:
        return await asyncio.gather(
            tools.write(
                _context("suiteharness.fs.write"), {"path": "winner.txt", "content": "one"}
            ),
            tools.write(
                _context("suiteharness.fs.write"), {"path": "winner.txt", "content": "two"}
            ),
            return_exceptions=True,
        )

    results = asyncio.run(race())
    assert sum(isinstance(item, FileExistsError) for item in results) == 1
    assert (paths.product_root / "winner.txt").read_text(encoding="utf-8") in {
        "one",
        "two",
    }
