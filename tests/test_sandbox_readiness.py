import asyncio

import pytest

from suiteharness.persistence.database import SQLiteDatabase
from suiteharness.persistence.sandbox import SQLiteSandboxQuarantineStore
from suiteharness.sandbox.docker import DockerBackendConfig, DockerSandboxBackend
from suiteharness.sandbox.models import ProcessResult, SandboxLimits, SandboxMount, SandboxRequest


class Engine:
    def __init__(self, result=ProcessResult(0, b'', b'')):
        self.result = result
        self.executions = []
        self.removals = []
        self.cleanup_fails = False

    async def probe(self, argv, **kwargs):
        return ProcessResult(0, b'["name=rootless"]', b'')

    async def execute(self, argv, **kwargs):
        if 'rm' in argv:
            self.removals.append(argv[-1])
            return ProcessResult(1 if self.cleanup_fails else 0, b'', b'')
        self.executions.append((argv, kwargs))
        return self.result


def backend(root, engine, **kwargs):
    return DockerSandboxBackend(DockerBackendConfig('test-image', (root,), require_rootless=True,
                                                   production=False), engine, **kwargs)


def test_readiness_rejects_execution_failure_with_healthy_rootless_daemon(tmp_path):
    engine = Engine(ProcessResult(125, b'', b'CPU quota is unsupported'))
    state = asyncio.run(backend(tmp_path, engine).availability())
    assert not state.available
    argv, _ = engine.executions[0]
    assert engine.removals == [next(arg.removeprefix('--name=') for arg in argv if arg.startswith('--name='))]


def test_readiness_executes_constrained_without_customer_mounts_and_caches(tmp_path):
    async def check():
        engine = Engine()
        limits = SandboxLimits(cpu_count=0.5, memory_mb=256, pids=32, tmpfs_mb=32,
                               timeout_seconds=2, output_bytes=2048)
        sandbox = backend(tmp_path, engine, probe_limits=limits)
        states = await asyncio.gather(*(sandbox.availability() for _ in range(8)))
        assert all(state.available for state in states)
        assert len(engine.executions) == 1
        argv, options = engine.executions[0]
        for flag in ('--cpus=0.5', '--memory=256m', '--pids-limit=32',
                     '--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=32m', '--network=none',
                     '--user=65532:65532', '--read-only', '--cap-drop=ALL',
                     '--security-opt=no-new-privileges=true', '--workdir=/tmp'):
            assert flag in argv
        assert '--mount' not in argv
        assert not any(arg.startswith('--env=') for arg in argv)
        assert options['environment'] == {}
        assert options['stdin'] is None
        assert options['timeout_seconds'] <= 2
        assert options['output_bytes'] <= 2048
    asyncio.run(check())


def test_readiness_timeout_cleans_exact_container_and_fails_closed(tmp_path):
    async def check():
        engine = Engine(ProcessResult(None, b'', b'', timed_out=True))
        sandbox = backend(tmp_path, engine)
        assert not (await sandbox.availability()).available
        argv, _ = engine.executions[0]
        assert engine.removals == [next(arg.removeprefix('--name=') for arg in argv if arg.startswith('--name='))]
    asyncio.run(check())


def test_readiness_uncertain_cleanup_quarantines(tmp_path):
    async def check():
        engine = Engine(ProcessResult(None, b'', b'', timed_out=True))
        engine.cleanup_fails = True
        sandbox = backend(tmp_path, engine)
        assert not (await sandbox.availability()).available
        assert sandbox.quarantined_containers
        engine.result = ProcessResult(0, b'', b'')
        assert not (await sandbox.availability()).available
        assert len(engine.executions) == 1
    asyncio.run(check())


def test_readiness_rechecks_execution_after_cache_expiry(tmp_path):
    async def check():
        now = [0.0]
        engine = Engine()
        sandbox = backend(tmp_path, engine, probe_clock=lambda: now[0])
        assert (await sandbox.availability()).available
        engine.result = ProcessResult(125, b'', b'')
        now[0] = 60.0
        assert not (await sandbox.availability()).available
    asyncio.run(check())


def test_failed_run_invalidates_cached_readiness(tmp_path):
    async def check():
        engine = Engine()
        sandbox = backend(tmp_path, engine)
        assert (await sandbox.availability()).available
        engine.result = ProcessResult(125, b'', b'')
        result = await sandbox.run(SandboxRequest('customer', ('true',), (SandboxMount(tmp_path, '/workspace'),)))
        assert result.exit_code == 125
        assert not (await sandbox.availability()).available
    asyncio.run(check())


def test_production_probe_persists_lease_and_cancellation_cleans_it(tmp_path):
    async def check():
        database = SQLiteDatabase(tmp_path / 'runtime.sqlite3')
        store = SQLiteSandboxQuarantineStore(database, deployment_id='readiness-test')
        started = asyncio.Event()
        class BlockingEngine(Engine):
            async def execute(self, argv, **kwargs):
                if 'run' in argv:
                    name = next(arg.removeprefix('--name=') for arg in argv if arg.startswith('--name='))
                    assert name in await store.load()
                    started.set()
                    await asyncio.Event().wait()
                return await super().execute(argv, **kwargs)
        engine = BlockingEngine()
        config = DockerBackendConfig('example@sha256:' + 'a' * 64, (tmp_path,), require_rootless=True)
        sandbox = DockerSandboxBackend(config, engine, quarantine_store=store)
        task = asyncio.create_task(sandbox.availability())
        try:
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(engine.removals) == 1
            assert not await store.load()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await database.close()
    asyncio.run(check())


def test_production_probe_quarantine_survives_backend_restart(tmp_path):
    async def check():
        database = SQLiteDatabase(tmp_path / 'runtime.sqlite3')
        store = SQLiteSandboxQuarantineStore(database, deployment_id='readiness-test')
        config = DockerBackendConfig('example@sha256:' + 'a' * 64, (tmp_path,), require_rootless=True)
        engine = Engine(ProcessResult(None, b'', b'', timed_out=True))
        engine.cleanup_fails = True
        try:
            assert not (await DockerSandboxBackend(config, engine, quarantine_store=store).availability()).available
            assert await store.load()
            recovered_engine = Engine()
            restarted = DockerSandboxBackend(config, recovered_engine, quarantine_store=store)
            assert not (await restarted.availability()).available
            assert not recovered_engine.executions
        finally:
            await database.close()
    asyncio.run(check())
