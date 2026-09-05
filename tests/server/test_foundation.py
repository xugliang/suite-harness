from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from suiteharness.agents import (
    DefaultPromptStrategy,
    NoOpReflectionStrategy,
    ProductRoutedReActWorkflow,
)
from suiteharness.config import (
    LoadedSuiteHarnessConfig,
    SuiteHarnessConfig,
    SuiteHarnessSecrets,
    WorkspaceConfig,
)
from suiteharness.execution import (
    PROMPT_STRATEGY,
    REFLECTION_STRATEGY,
    WORKFLOW,
    ToolCallContext,
)
from suiteharness.runtime import RequestScope, ScopePath
from suiteharness.sandbox import ProcessResult
from suiteharness.server import FoundationRuntime, FoundationStartupError
from suiteharness.workspace import (
    WorkspaceAccessDenied,
    WorkspaceOperation,
    secure_workspace_supported,
)


class ModelTransport:
    async def send(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError("model transport should not be called")

    def stream(self, request) -> AsyncIterator[bytes]:  # type: ignore[no-untyped-def]
        raise AssertionError("model transport should not be called")


class ProcessTransport:
    async def probe(self, argv, *, timeout_seconds):  # type: ignore[no-untyped-def]
        return ProcessResult(0, b"ok", b"")

    async def execute(
        self,
        argv: tuple[str, ...],
        *,
        working_directory: Path | None,
        environment: Mapping[str, str],
        stdin: bytes | None,
        timeout_seconds: float,
        output_bytes: int,
    ) -> ProcessResult:
        return ProcessResult(0, b"ok", b"")


def _loaded(tmp_path: Path) -> LoadedSuiteHarnessConfig:
    config = SuiteHarnessConfig.model_validate(
        {
            "config_version": 1,
            "deployment": {
                "mode": "server",
                "environment": "development",
                "instance_id": "acme-main",
                "tenant_id": "acme",
            },
            "customer_bundle": {
                "customer_bundle_id": "acme-products",
                "version": "1.0.0",
                "harness_api": ">=0.1,<0.2",
                "products": [
                    {"product_id": "sales", "version": "==1.0.0", "config": {}}
                ],
            },
            "workspace": {"root": tmp_path / "workspace"},
            "storage": {"root": tmp_path / "state"},
            "sandbox": {
                "backend": "docker",
                "required": True,
                "image": "registry.example/suiteharness-sandbox:0.1.0",
            },
            "models": {
                "profiles": [
                    {
                        "profile_id": "local-qwen",
                        "provider_id": "ollama",
                        "model": "qwen3",
                        "base_url": "http://ollama:11434/v1",
                        "allow_plain_http": True,
                    }
                ],
                "routes": [{"route_id": "default", "primary_profile": "local-qwen"}],
                "default_route": "default",
            },
            "web_tools": {
                "search": {
                    "default_provider": "baidu-qianfan",
                    "providers": [
                        {
                            "kind": "baidu_qianfan",
                            "credentials_ref": "search/baidu-qianfan",
                        }
                    ],
                },
                "fetch": {
                    "default_route": "direct",
                    "routes": [{"kind": "direct", "name": "direct"}],
                },
            },
            "channels": {
                "web": {"enabled": True, "credentials_ref": "web/default"},
                "feishu": {
                    "enabled": True,
                    "app_id": "cli-example",
                    "credentials_ref": "feishu/default",
                    "writable_roots": [{"space": "product", "path": "exports"}],
                },
            },
        }
    )
    secrets = SuiteHarnessSecrets.model_validate(
        {
            "config_version": 1,
            "web": {"web/default": {"session_signing_key": "x" * 32}},
            "feishu": {"feishu/default": {"app_secret": "secret"}},
            "services": {"search/baidu-qianfan": {"token": "token"}},
        }
    )
    return LoadedSuiteHarnessConfig(
        config=config,
        secrets=secrets,
        config_path=tmp_path / "suiteharness.yaml",
        secrets_path=tmp_path / "suiteharness.secrets.yaml",
    )


def _context(runtime: FoundationRuntime, channel: str) -> ToolCallContext:
    scope = RequestScope(
        ScopePath.agent("acme", "sales", "default", "session-1"),
        "user-1",
        channel_id=channel,
    )
    registration = runtime.tools.resolve(scope, "suiteharness.fs.write")
    assert registration is not None
    return ToolCallContext(
        scope=scope,
        run_id="run-1",
        call_id="call-1",
        tool_identity=registration.identity,
        tool=registration.spec,
        remaining_seconds=10,
    )


@pytest.mark.skipif(
    secure_workspace_supported(),
    reason="this regression exercises a host without the Linux secure workspace backend",
)
def test_production_start_fails_closed_without_secure_workspace_backend(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        loaded = _loaded(tmp_path)
        deployment = loaded.config.deployment.model_copy(
            update={"environment": "production"}
        )
        sandbox = loaded.config.sandbox.model_copy(
            update={
                "image": "registry.example/suiteharness-sandbox@sha256:" + "0" * 64,
            }
        )
        runtime = FoundationRuntime(
            replace(
                loaded,
                config=loaded.config.model_copy(
                    update={"deployment": deployment, "sandbox": sandbox}
                ),
            ),
            model_transport=ModelTransport(),  # type: ignore[arg-type]
            sandbox_transport=ProcessTransport(),
        )
        try:
            with pytest.raises(FoundationStartupError, match="secure workspace backend"):
                await runtime.start()
        finally:
            await runtime.close()

    asyncio.run(exercise())


def test_foundation_builds_shared_services_and_channel_specific_workspace_policy(
    tmp_path: Path,
) -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        runtime = FoundationRuntime(
            _loaded(tmp_path),
            model_transport=ModelTransport(),  # type: ignore[arg-type]
            sandbox_transport=ProcessTransport(),
        )
        with pytest.raises(ValueError, match="outside the configured customer bundle"):
            runtime.prepare_product_workspace("unselected")
        product_root = runtime.prepare_product_workspace("sales")
        web = runtime.workspace_bindings.resolve(_context(runtime, "web"))
        feishu = runtime.workspace_bindings.resolve(_context(runtime, "feishu"))
        web.access_policy.authorize(product_root / "private.txt", WorkspaceOperation.WRITE)
        with pytest.raises(WorkspaceAccessDenied):
            web.access_policy.authorize(
                runtime.workspaces.paths_for("acme", "sales").shared_root / "shared.txt",
                WorkspaceOperation.READ,
            )
        with pytest.raises(WorkspaceAccessDenied):
            feishu.access_policy.authorize(product_root / "private.txt", WorkspaceOperation.WRITE)
        allowed = feishu.access_policy.authorize(
            product_root / "exports" / "report.txt", WorkspaceOperation.WRITE
        )
        assert runtime.models.route_for("sales") == "default"
        with pytest.raises(TypeError):
            runtime.models.product_routes["sales"] = "changed"  # type: ignore[index]
        assert len(runtime.builtins.identities) == 9
        assert isinstance(runtime.kernel.root.resolve(WORKFLOW), ProductRoutedReActWorkflow)
        assert isinstance(runtime.kernel.root.resolve(PROMPT_STRATEGY), DefaultPromptStrategy)
        assert isinstance(runtime.kernel.root.resolve(REFLECTION_STRATEGY), NoOpReflectionStrategy)
        await runtime.close()
        await runtime.close()
        return product_root, allowed

    product_root, allowed = asyncio.run(exercise())
    assert product_root.is_dir()
    assert allowed == (product_root / "exports" / "report.txt").resolve()
    assert (tmp_path / "state" / "sessions.sqlite3").is_file()
    assert (tmp_path / "state" / "audit.sqlite3").is_file()
    assert (tmp_path / "state" / "runtime.sqlite3").is_file()


@pytest.mark.parametrize(
    ("access", "write_allowed"),
    [("read_only", False), ("read_write", True)],
)
def test_shared_workspace_requires_explicit_per_product_access(
    tmp_path: Path,
    access: str,
    write_allowed: bool,
) -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        loaded = _loaded(tmp_path)
        workspace = WorkspaceConfig(
            root=loaded.config.workspace.root,
            shared_enabled=True,
            shared_access_by_product={"sales": access},  # type: ignore[arg-type]
        )
        loaded = replace(
            loaded,
            config=loaded.config.model_copy(update={"workspace": workspace}),
        )
        runtime = FoundationRuntime(
            loaded,
            model_transport=ModelTransport(),  # type: ignore[arg-type]
            sandbox_transport=ProcessTransport(),
        )
        runtime.prepare_product_workspace("sales")
        binding = runtime.workspace_bindings.resolve(_context(runtime, "web"))
        shared = runtime.workspaces.paths_for("acme", "sales").shared_root
        readable = binding.access_policy.authorize(shared / "note.txt", WorkspaceOperation.READ)
        if write_allowed:
            writable = binding.access_policy.authorize(
                shared / "note.txt", WorkspaceOperation.WRITE
            )
        else:
            with pytest.raises(WorkspaceAccessDenied):
                binding.access_policy.authorize(
                    shared / "note.txt", WorkspaceOperation.WRITE
                )
            writable = None
        await runtime.close()
        return shared, readable, writable

    shared, readable, writable = asyncio.run(exercise())
    assert readable == (shared / "note.txt").resolve()
    assert (writable is not None) is write_allowed
