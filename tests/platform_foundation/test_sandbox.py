from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from suiteharness.persistence import SQLiteDatabase, SQLiteSandboxQuarantineStore
from suiteharness.sandbox import (
    DockerBackendConfig,
    DockerCommandBuilder,
    DockerSandboxBackend,
    LocalDevelopmentConfig,
    ProcessResult,
    SandboxCleanupError,
    SandboxConfigurationError,
    SandboxMount,
    SandboxNetworkMode,
    SandboxNetworkPolicy,
    SandboxRequest,
    SandboxUnavailable,
)


class FakeTransport:
    def __init__(
        self,
        *,
        probe_exit_code: int | None = 0,
        probe_stdout: bytes = b"26.1",
        execution_result: ProcessResult | None = None,
        cleanup_result: ProcessResult | None = None,
    ) -> None:
        self.probe_exit_code = probe_exit_code
        self.probe_stdout = probe_stdout
        self.execution_result = execution_result or ProcessResult(0, b"ok", b"")
        self.cleanup_result = cleanup_result or ProcessResult(0, b"", b"")
        self.executions: list[tuple[tuple[str, ...], Mapping[str, str]]] = []
        self.probe_commands: list[tuple[str, ...]] = []
        self.probes = 0

    async def probe(self, argv: tuple[str, ...], *, timeout_seconds: float) -> ProcessResult:
        self.probes += 1
        self.probe_commands.append(argv)
        return ProcessResult(self.probe_exit_code, self.probe_stdout, b"")

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
        self.executions.append((argv, environment))
        command_index = 3 if len(argv) > 3 and argv[1] == "--context" else 1
        if argv[command_index : command_index + 1] == ("rm",):
            return self.cleanup_result
        if argv[command_index : command_index + 2] == ("container", "ls"):
            return ProcessResult(0, b"", b"")
        return self.execution_result


class MemoryQuarantineStore:
    def __init__(self) -> None:
        self.items: dict[str, str] = {}
        self.owners: dict[str, str] = {}

    async def load(self):  # type: ignore[no-untyped-def]
        return dict(self.items)

    async def mark(self, container_name: str, reason: str) -> None:
        self.items[container_name] = reason
        self.owners.pop(container_name, None)

    async def claim(
        self, container_name: str, reason: str, *, owner_token: str
    ) -> bool:
        if container_name in self.items:
            return False
        self.items[container_name] = reason
        self.owners[container_name] = owner_token
        return True

    async def clear(
        self, container_name: str, *, owner_token: str | None = None
    ) -> bool:
        if container_name not in self.items:
            return False
        if owner_token is not None and self.owners.get(container_name) != owner_token:
            return False
        self.items.pop(container_name, None)
        self.owners.pop(container_name, None)
        return True


def _backend(
    workspace: Path,
    transport: FakeTransport,
    *,
    quarantine_store: MemoryQuarantineStore | None = None,
) -> DockerSandboxBackend:
    return DockerSandboxBackend(
        _docker_config(workspace),
        transport,
        quarantine_store=quarantine_store or MemoryQuarantineStore(),
    )


def _request(workspace: Path) -> SandboxRequest:
    return SandboxRequest(
        request_id="run-1",
        argv=("bash", "-lc", "printf ok"),
        mounts=(SandboxMount(workspace, "/workspace", read_only=False),),
        environment={"PUBLIC_SETTING": "visible", "API_TOKEN": "not-in-argv"},
    )


def _docker_config(workspace_root: Path) -> DockerBackendConfig:
    return DockerBackendConfig(
        image="registry.example/suiteharness@sha256:" + "a" * 64,
        allowed_host_roots=(workspace_root,),
    )


def test_docker_builder_applies_isolation_and_resource_limits(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command = DockerCommandBuilder(_docker_config(workspace)).build(_request(workspace))

    assert command[:2] == ("docker", "run")
    assert "--network=none" in command
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "--security-opt=no-new-privileges=true" in command
    assert "--pids-limit=128" in command
    assert "--memory=512m" in command
    assert "--env=API_TOKEN" in command
    assert all("not-in-argv" not in argument for argument in command)
    assert command[-3:] == ("bash", "-lc", "printf ok")


def test_docker_context_is_applied_to_run_probe_and_exact_cleanup(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    transport = FakeTransport(
        execution_result=ProcessResult(None, b"", b"", timed_out=True)
    )
    config = replace(_docker_config(workspace), context="suiteharness-rootless")
    backend = DockerSandboxBackend(
        config,
        transport,
        quarantine_store=MemoryQuarantineStore(),
    )

    result = asyncio.run(backend.run(_request(workspace)))

    assert result.timed_out
    assert transport.probe_commands == [
        (
            "docker",
            "--context",
            "suiteharness-rootless",
            "version",
            "--format",
            "{{.Server.Version}}",
        ),
        (
            "docker",
            "--context",
            "suiteharness-rootless",
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            config.image,
        ),
    ]
    run_command, cleanup_command = (item[0] for item in transport.executions)
    assert run_command[:4] == (
        "docker",
        "--context",
        "suiteharness-rootless",
        "run",
    )
    assert cleanup_command[:4] == (
        "docker",
        "--context",
        "suiteharness-rootless",
        "rm",
    )


@pytest.mark.parametrize(
    "context",
    ("", "bad context", "--host", "bad/context", "x" * 129),
)
def test_docker_context_rejects_unsafe_names(tmp_path: Path, context: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(SandboxConfigurationError, match="context"):
        replace(_docker_config(workspace), context=context)


def test_docker_image_cannot_be_parsed_as_a_cli_option(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(SandboxConfigurationError, match="image"):
        replace(_docker_config(workspace), image="--help@sha256:" + "a" * 64)


def test_required_rootless_daemon_is_verified_before_any_run(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = replace(
        _docker_config(workspace),
        context="suiteharness-rootless",
        require_rootless=True,
    )
    rootless = FakeTransport(
        probe_stdout=b'["name=seccomp,profile=builtin","name=rootless"]'
    )
    backend = DockerSandboxBackend(
        config,
        rootless,
        quarantine_store=MemoryQuarantineStore(),
    )
    assert asyncio.run(backend.run(_request(workspace))).stdout == b"ok"
    assert rootless.probe_commands == [
        (
            "docker",
            "--context",
            "suiteharness-rootless",
            "info",
            "--format",
            "{{json .SecurityOptions}}",
        ),
        (
            "docker",
            "--context",
            "suiteharness-rootless",
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            config.image,
        ),
    ]

    for unsafe_output in (b'["name=seccomp"]', b"not-json", b"{}"):
        rootful = FakeTransport(probe_stdout=unsafe_output)
        denied = DockerSandboxBackend(
            config,
            rootful,
            quarantine_store=MemoryQuarantineStore(),
        )
        with pytest.raises(SandboxUnavailable, match="rootless"):
            asyncio.run(denied.run(_request(workspace)))
        assert rootful.executions == []


def test_docker_backend_fails_closed_when_daemon_is_unavailable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    transport = FakeTransport(probe_exit_code=1)
    backend = _backend(workspace, transport)

    with pytest.raises(SandboxUnavailable, match="unavailable"):
        asyncio.run(backend.run(_request(workspace)))
    assert transport.executions == []


def test_production_docker_requires_the_pinned_image_to_exist_locally(
    tmp_path: Path,
) -> None:
    class MissingImageTransport(FakeTransport):
        async def probe(
            self, argv: tuple[str, ...], *, timeout_seconds: float
        ) -> ProcessResult:
            del timeout_seconds
            self.probes += 1
            self.probe_commands.append(argv)
            if "inspect" in argv:
                return ProcessResult(1, b"", b"image not found")
            return ProcessResult(0, b"26.1", b"")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    transport = MissingImageTransport()
    backend = _backend(workspace, transport)

    with pytest.raises(SandboxUnavailable, match="image is unavailable"):
        asyncio.run(backend.run(_request(workspace)))
    assert transport.executions == []


def test_docker_availability_coalesces_concurrent_daemon_probes(tmp_path: Path) -> None:
    class BlockingProbeTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.probe_started = asyncio.Event()
            self.release_probe = asyncio.Event()

        async def probe(
            self, argv: tuple[str, ...], *, timeout_seconds: float
        ) -> ProcessResult:
            self.probes += 1
            self.probe_started.set()
            await self.release_probe.wait()
            return ProcessResult(0, b"26.1", b"")

    async def exercise() -> tuple[tuple[bool, ...], int]:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        transport = BlockingProbeTransport()
        backend = _backend(workspace, transport)
        requests = [asyncio.create_task(backend.availability()) for _ in range(100)]
        await transport.probe_started.wait()
        await asyncio.sleep(0)
        transport.release_probe.set()
        results = await asyncio.gather(*requests)
        return tuple(item.available for item in results), transport.probes

    available, probes = asyncio.run(exercise())
    assert all(available)
    assert probes == 2


def test_docker_availability_reprobes_after_healthy_cache_ttl(tmp_path: Path) -> None:
    async def exercise() -> tuple[int, int, int]:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        transport = FakeTransport()
        clock = [100.0]
        backend = DockerSandboxBackend(
            _docker_config(workspace),
            transport,
            quarantine_store=MemoryQuarantineStore(),
            probe_clock=lambda: clock[0],
        )
        await backend.availability()
        first = transport.probes
        clock[0] += 2.999
        await backend.availability()
        cached = transport.probes
        clock[0] += 0.001
        await backend.availability()
        return first, cached, transport.probes

    first, cached, expired = asyncio.run(exercise())
    assert (first, cached, expired) == (2, 2, 4)


def test_docker_quarantine_immediately_overrides_cached_health(tmp_path: Path) -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        transport = FakeTransport(
            execution_result=ProcessResult(None, b"", b"", timed_out=True),
            cleanup_result=ProcessResult(1, b"", b"cleanup failed"),
        )
        backend = _backend(workspace, transport)
        healthy = await backend.availability()
        with pytest.raises(SandboxCleanupError):
            await backend.run(_request(workspace))
        quarantined = await backend.availability()
        return healthy, quarantined, transport.probes

    healthy, quarantined, probes = asyncio.run(exercise())
    assert healthy.available is True
    assert quarantined.available is False
    assert "quarantined" in quarantined.detail
    assert probes == 2


def test_docker_backend_passes_limits_and_environment_to_transport(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    transport = FakeTransport()
    backend = _backend(workspace, transport)

    result = asyncio.run(backend.run(_request(workspace)))

    assert result.stdout == b"ok"
    assert transport.executions[0][1]["API_TOKEN"] == "not-in-argv"


def test_docker_backend_force_removes_exact_container_after_timeout(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    transport = FakeTransport(execution_result=ProcessResult(None, b"", b"", timed_out=True))
    backend = _backend(workspace, transport)

    result = asyncio.run(backend.run(_request(workspace)))

    assert result.timed_out
    run_command = transport.executions[0][0]
    name_option = next(item for item in run_command if item.startswith("--name="))
    assert transport.executions[1][0] == (
        "docker",
        "rm",
        "--force",
        name_option.removeprefix("--name="),
    )


def test_docker_backend_force_removes_exact_container_when_cancelled(tmp_path: Path) -> None:
    class CancellingTransport(FakeTransport):
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
            self.executions.append((argv, environment))
            if argv[:2] == ("docker", "run"):
                raise asyncio.CancelledError
            return ProcessResult(0, b"", b"")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    transport = CancellingTransport()
    backend = _backend(workspace, transport)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(backend.run(_request(workspace)))

    run_command = transport.executions[0][0]
    name_option = next(item for item in run_command if item.startswith("--name="))
    assert transport.executions[1][0] == (
        "docker",
        "rm",
        "--force",
        name_option.removeprefix("--name="),
    )


@pytest.mark.parametrize(
    "cleanup_result",
    [
        ProcessResult(17, b"", b"failed"),
        ProcessResult(None, b"", b"", timed_out=True),
    ],
    ids=("nonzero", "timeout"),
)
def test_cleanup_failure_quarantines_backend_and_blocks_future_runs(
    tmp_path: Path,
    cleanup_result: ProcessResult,
) -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        transport = FakeTransport(
            execution_result=ProcessResult(None, b"", b"", timed_out=True),
            cleanup_result=cleanup_result,
        )
        backend = _backend(workspace, transport)
        with pytest.raises(SandboxCleanupError, match="quarantined"):
            await backend.run(_request(workspace))
        probes_after_failure = transport.probes
        availability = await backend.availability()
        with pytest.raises(SandboxUnavailable, match="quarantined"):
            await backend.run(_request(workspace))
        return backend, transport, availability, probes_after_failure

    backend, transport, availability, probes_after_failure = asyncio.run(exercise())
    assert availability.available is False
    assert backend.quarantined_containers
    assert transport.probes == probes_after_failure
    assert len(transport.executions) == 2


def test_cleanup_exception_is_observable_and_explicit_reconciliation_recovers(
    tmp_path: Path,
) -> None:
    class RaisesCleanupOnce(FakeTransport):
        def __init__(self) -> None:
            super().__init__(
                execution_result=ProcessResult(None, b"", b"", timed_out=True)
            )
            self.raise_cleanup = True

        async def execute(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            self.executions.append((argv, kwargs["environment"]))
            if argv[:2] == ("docker", "rm") and self.raise_cleanup:
                self.raise_cleanup = False
                raise OSError("docker transport failed")
            if argv[:3] == ("docker", "container", "ls"):
                return ProcessResult(0, b"", b"")
            return self.execution_result

    async def exercise():  # type: ignore[no-untyped-def]
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        transport = RaisesCleanupOnce()
        backend = _backend(workspace, transport)
        with pytest.raises(SandboxCleanupError) as captured:
            await backend.run(_request(workspace))
        assert isinstance(captured.value.__cause__, OSError)
        reconciled = await backend.reconcile_cleanup()
        return backend, reconciled

    backend, reconciled = asyncio.run(exercise())
    assert reconciled.available is True
    assert backend.quarantined_containers == ()


def test_quarantine_survives_process_restart_until_exact_name_reconciliation(
    tmp_path: Path,
) -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        database = SQLiteDatabase(tmp_path / "runtime.sqlite3")
        store = SQLiteSandboxQuarantineStore(database, deployment_id="acme-main")
        failing = FakeTransport(
            execution_result=ProcessResult(None, b"", b"", timed_out=True),
            cleanup_result=ProcessResult(1, b"", b"failed"),
        )
        first = DockerSandboxBackend(
            _docker_config(workspace),
            failing,
            quarantine_store=store,
        )
        with pytest.raises(SandboxCleanupError):
            await first.run(_request(workspace))

        after_restart_transport = FakeTransport()
        after_restart = DockerSandboxBackend(
            _docker_config(workspace),
            after_restart_transport,
            quarantine_store=store,
        )
        unavailable = await after_restart.availability()
        assert after_restart_transport.probes == 0
        reconciled = await after_restart.reconcile_cleanup()
        persisted = await store.load()
        await database.close()
        return unavailable, reconciled, persisted

    unavailable, reconciled, persisted = asyncio.run(exercise())
    assert unavailable.available is False
    assert "quarantined" in unavailable.detail
    assert reconciled.available is True
    assert persisted == {}


def test_active_lease_is_persisted_before_docker_run_and_cleared_after_exit(
    tmp_path: Path,
) -> None:
    class ObservingTransport(FakeTransport):
        def __init__(self, store: MemoryQuarantineStore) -> None:
            super().__init__()
            self.store = store
            self.saw_lease = False

        async def execute(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            if argv[:2] == ("docker", "run"):
                expected = next(
                    item.removeprefix("--name=")
                    for item in argv
                    if item.startswith("--name=")
                )
                self.saw_lease = expected in self.store.items
            return await super().execute(argv, **kwargs)

    async def exercise():  # type: ignore[no-untyped-def]
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = MemoryQuarantineStore()
        transport = ObservingTransport(store)
        backend = _backend(workspace, transport, quarantine_store=store)
        await backend.run(_request(workspace))
        return transport.saw_lease, store.items

    saw_lease, remaining = asyncio.run(exercise())
    assert saw_lease is True
    assert remaining == {}


def test_active_lease_compare_delete_rejects_delayed_previous_owner(
    tmp_path: Path,
) -> None:
    async def exercise() -> tuple[bool, bool, dict[str, str]]:
        database = SQLiteDatabase(tmp_path / "runtime.sqlite3")
        store = SQLiteSandboxQuarantineStore(database, deployment_id="acme-main")
        name = "suiteharness-sandbox-" + "a" * 24
        first_token = "a" * 32
        second_token = "b" * 32
        assert await store.claim(
            name, "active container lease", owner_token=first_token
        )
        assert await store.clear(name, owner_token=first_token)
        assert await store.claim(
            name, "active container lease", owner_token=second_token
        )
        delayed_clear = await store.clear(name, owner_token=first_token)
        duplicate_claim = await store.claim(
            name, "active container lease", owner_token="c" * 32
        )
        persisted = dict(await store.load())
        await database.close()
        return delayed_clear, duplicate_claim, persisted

    delayed_clear, duplicate_claim, persisted = asyncio.run(exercise())
    assert delayed_clear is False
    assert duplicate_claim is False
    assert persisted == {"suiteharness-sandbox-" + "a" * 24: "active container lease"}


def test_concurrent_backends_cannot_execute_the_same_container_lease(
    tmp_path: Path,
) -> None:
    class BlockingRunTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.run_started = asyncio.Event()
            self.release_run = asyncio.Event()

        async def execute(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            self.executions.append((argv, kwargs["environment"]))
            if argv[:2] == ("docker", "run"):
                self.run_started.set()
                await self.release_run.wait()
            return ProcessResult(0, b"ok", b"")

    async def exercise() -> tuple[int, dict[str, str]]:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        path = tmp_path / "runtime.sqlite3"
        first_database = SQLiteDatabase(path)
        second_database = SQLiteDatabase(path)
        first_store = SQLiteSandboxQuarantineStore(
            first_database, deployment_id="acme-main"
        )
        second_store = SQLiteSandboxQuarantineStore(
            second_database, deployment_id="acme-main"
        )
        transport = BlockingRunTransport()
        first = DockerSandboxBackend(
            _docker_config(workspace), transport, quarantine_store=first_store
        )
        second = DockerSandboxBackend(
            _docker_config(workspace), transport, quarantine_store=second_store
        )
        # Load both views before either lease exists, as two ready server
        # processes would. Atomic store.claim(), not startup loading, must win.
        assert (await first.availability()).available
        assert (await second.availability()).available
        first_run = asyncio.create_task(first.run(_request(workspace)))
        await transport.run_started.wait()
        with pytest.raises(SandboxUnavailable, match="already has"):
            await second.run(_request(workspace))
        transport.release_run.set()
        await first_run
        persisted = dict(await first_store.load())
        await first_database.close()
        await second_database.close()
        docker_runs = sum(
            argv[:2] == ("docker", "run") for argv, _ in transport.executions
        )
        return docker_runs, persisted

    docker_runs, persisted = asyncio.run(exercise())
    assert docker_runs == 1
    assert persisted == {}


def test_stale_active_lease_from_hard_crash_blocks_restarted_backend(
    tmp_path: Path,
) -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        database = SQLiteDatabase(tmp_path / "runtime.sqlite3")
        store = SQLiteSandboxQuarantineStore(database, deployment_id="acme-main")
        name = DockerCommandBuilder(_docker_config(workspace)).container_name("run-1")
        await store.mark(name, "active container lease")
        transport = FakeTransport()
        restarted = DockerSandboxBackend(
            _docker_config(workspace),
            transport,
            quarantine_store=store,
        )
        availability = await restarted.availability()
        await database.close()
        return availability, transport.probes

    availability, probes = asyncio.run(exercise())
    assert availability.available is False
    assert "quarantined" in availability.detail
    assert probes == 0


def test_mount_must_remain_in_backend_allowed_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    builder = DockerCommandBuilder(_docker_config(allowed))

    with pytest.raises(SandboxConfigurationError, match="escapes"):
        builder.build(_request(outside))


def test_network_requires_an_explicit_configured_egress_profile(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = SandboxRequest(
        request_id="run-network",
        argv=("curl", "https://example.com"),
        mounts=(SandboxMount(workspace, "/workspace"),),
        network=SandboxNetworkPolicy(SandboxNetworkMode.EGRESS_PROFILE, "domestic-web"),
    )

    with pytest.raises(SandboxConfigurationError, match="not configured"):
        DockerCommandBuilder(_docker_config(workspace)).build(request)


def test_container_paths_cannot_mount_host_content_over_system_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(SandboxConfigurationError, match="restricted to /workspace"):
        SandboxMount(workspace, "/etc")


def test_local_backend_can_only_be_acknowledged_for_development() -> None:
    with pytest.raises(ValueError, match="development"):
        LocalDevelopmentConfig(environment="production", acknowledge_unsafe=True)
    with pytest.raises(ValueError, match="acknowledgement"):
        LocalDevelopmentConfig(environment="development", acknowledge_unsafe=False)
