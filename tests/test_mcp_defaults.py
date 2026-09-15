import asyncio
import json
import sys

import pytest

from suiteharness.sandbox import SandboxLimits, SandboxMount, SandboxRequest
from suiteharness.sandbox.docker import DockerBackendConfig, DockerSandboxBackend
from suiteharness.sandbox.models import ProcessResult, SandboxUnavailable
from suiteharness.sandbox.process import AsyncioProcessTransport


def test_persistent_process_roundtrip_and_output_bound():
    async def check():
        transport = AsyncioProcessTransport()
        assert hasattr(transport, 'open_session'), 'persistent process port is missing'
        session = await transport.open_session(
            (sys.executable, '-u', '-c', 'import sys; [print(s.strip(),flush=True) for s in sys.stdin]'),
            environment={}, timeout_seconds=10, output_bytes=1024,
        )
        try:
            await session.send_line(b'first')
            assert await session.receive_line() == b'first'
            await session.send_line(b'second')
            assert await session.receive_line() == b'second'
            with pytest.raises(SandboxUnavailable):
                await session.send_line(b'x' * 1025)
        finally:
            await session.close()
        with pytest.raises(SandboxUnavailable):
            await session.send_line(b'after close')
    asyncio.run(check())


def test_http_policy_denies_private_mixed_dns_and_other_authority():
    async def check():
        from suiteharness.mcp import defaults
        assert hasattr(defaults, 'ConfiguredMcpEgressPolicy'), 'default HTTP policy missing'
        from suiteharness.mcp.models import McpServerConfig
        class DNS:
            addresses = frozenset({'8.8.8.8'})
            async def resolve(self, hostname, port):
                return self.addresses
        dns = DNS()
        cfg = McpServerConfig(server_id='test', transport='streamable_http', endpoint='https://mcp.example/rpc')
        policy = defaults.ConfiguredMcpEgressPolicy('tenant', {'product': (cfg,)}, resolver=dns)
        assert await policy.authorize(cfg.endpoint, tenant_id='tenant', product_id='product') == cfg.endpoint
        for addresses in ({'127.0.0.1'}, {'8.8.8.8', '10.1.2.3'}, {'::ffff:127.0.0.1'}):
            dns.addresses = frozenset(addresses)
            with pytest.raises(PermissionError):
                await policy.authorize(cfg.endpoint, tenant_id='tenant', product_id='product')
        with pytest.raises(PermissionError):
            await policy.authorize('https://other.example/rpc', tenant_id='tenant', product_id='product')
        with pytest.raises(PermissionError):
            await policy.authorize(cfg.endpoint, tenant_id='other', product_id='product')
        private = cfg.model_copy(update={'allowed_private_networks': ('10.1.2.0/24',)})
        policy = defaults.ConfiguredMcpEgressPolicy('tenant', {'product': (private,)}, resolver=dns)
        dns.addresses = frozenset({'10.1.2.3'})
        assert await policy.authorize(cfg.endpoint, tenant_id='tenant', product_id='product') == cfg.endpoint
        dns.addresses = frozenset({'10.1.3.3'})
        with pytest.raises(PermissionError):
            await policy.authorize(cfg.endpoint, tenant_id='tenant', product_id='product')
    asyncio.run(check())


def test_http_exchange_pins_dns_and_does_not_redirect_or_leak_credentials():
    async def check():
        import httpx
        from suiteharness.mcp import defaults
        from suiteharness.mcp.models import McpServerConfig
        from suiteharness.mcp.protocols import HttpRequest
        class DNS:
            async def resolve(self, hostname, port):
                return frozenset({'8.8.8.8'})
        cfg = McpServerConfig(server_id='test', transport='streamable_http', endpoint='https://mcp.example/rpc')
        policy = defaults.ConfiguredMcpEgressPolicy('tenant', {'product': (cfg,)}, resolver=DNS())
        seen = []
        async def handle(request):
            seen.append(request)
            return httpx.Response(302, headers={'location': 'https://evil.example/'})
        exchange = defaults.GuardedMcpHttpExchange(policy, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        exchange = exchange.bind(tenant_id='tenant', product_id='product', config=cfg)
        response = await exchange.send(HttpRequest('POST', cfg.endpoint, {'Authorization': 'Bearer test'}, b'{}', 3))
        assert response.status == 302
        assert len(seen) == 1
        assert str(seen[0].url) == 'https://8.8.8.8/rpc'
        assert seen[0].headers['host'] == 'mcp.example'
        assert seen[0].extensions['sni_hostname'] == 'mcp.example'
        with pytest.raises(PermissionError):
            await exchange.send(HttpRequest('POST', 'https://evil.example/', {'Authorization': 'Bearer test'}, b'{}', 3))
        assert len(seen) == 1
    asyncio.run(check())


class Engine:
    def __init__(self):
        self.commands = []
        self.removals = []
        self.closed = False
        self.finished = asyncio.Event()

    async def probe(self, argv, **kwargs):
        return ProcessResult(0, b'["name=rootless"]', b'')

    async def execute(self, argv, **kwargs):
        if 'rm' in argv:
            self.removals.append(argv[-1])
        return ProcessResult(0, b'', b'')

    async def open_session(self, argv, **kwargs):
        self.commands.append(argv)
        return self

    async def send_line(self, payload):
        pass

    async def receive_line(self):
        return None

    async def close(self):
        self.closed = True
        self.finished.set()

    async def wait_closed(self):
        await self.finished.wait()


def test_persistent_docker_preserves_isolation_and_exact_cleanup(tmp_path):
    async def check():
        engine = Engine()
        sandbox = DockerSandboxBackend(DockerBackendConfig('test-image', (tmp_path,), production=False), engine)
        assert hasattr(sandbox, 'open_session'), 'sandbox persistent port is missing'
        session = await sandbox.open_session(SandboxRequest(
            'mcp-test', ('python', '/workspace/server.py'),
            (SandboxMount(tmp_path, '/workspace'),),
            limits=SandboxLimits(cpu_count=0.5, memory_mb=256, pids=32),
        ))
        argv = engine.commands[0]
        for flag in ('--interactive', '--cpus=0.5', '--memory=256m', '--pids-limit=32',
                     '--network=none', '--read-only', '--cap-drop=ALL',
                     '--security-opt=no-new-privileges=true', '--user=65532:65532'):
            assert flag in argv
        assert any('dst=/workspace,readonly' in item for item in argv)
        assert argv[-3:] == ('test-image', 'python', '/workspace/server.py')
        await session.close()
        await session.close()
        assert engine.closed
        assert engine.removals == [next(x[7:] for x in argv if x.startswith('--name='))]
    asyncio.run(check())
