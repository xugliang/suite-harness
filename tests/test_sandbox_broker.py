import asyncio
import importlib.util
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from suiteharness.sandbox.models import SandboxLimits, SandboxMount, SandboxRequest, SandboxResult, SandboxConfigurationError, SandboxUnavailable


def modules():
    assert importlib.util.find_spec('suiteharness.sandbox.remote'), 'remote protocol is missing'
    from suiteharness.sandbox import remote, broker
    return remote, broker


def request(root):
    return SandboxRequest('test-1', ('printf', 'hi'), (SandboxMount(root, '/workspace/product'),), working_directory='/workspace/product', stdin=b'\x00\xff')


def test_request_roundtrip_and_policy_rejects_cross_product(tmp_path):
    remote, broker = modules()
    root = tmp_path / 'product-a'
    root.mkdir()
    other = tmp_path / 'product-b'
    other.mkdir()
    policy = broker.BrokerPolicy({'/workspace/product': root}, SandboxLimits(), frozenset())
    encoded = remote.encode_request(request(root))
    decoded = policy.decode(encoded)
    assert decoded.stdin == b'\x00\xff'
    assert decoded.argv == ('printf', 'hi')
    encoded['mounts'][0]['host_path'] = str(other)
    with pytest.raises(SandboxConfigurationError):
        policy.decode(encoded)


@pytest.mark.parametrize('change', ['unknown', 'boolean_limit', 'large_limit', 'network', 'base64', 'relative', 'target', 'host_environment'])
def test_rejects_invalid_requests(tmp_path, change):
    remote, broker = modules()
    data = remote.encode_request(request(tmp_path))
    if change == 'unknown': data['docker_args'] = ['--privileged']
    if change == 'boolean_limit': data['limits']['cpu_count'] = True
    if change == 'large_limit': data['limits']['memory_mb'] = 1024
    if change == 'network': data['network'] = {'mode': 'egress_profile', 'egress_profile': 'other'}
    if change == 'base64': data['stdin'] = '!!!!'
    if change == 'relative': data['mounts'][0]['host_path'] = '../outside'
    if change == 'target': data['mounts'][0]['container_path'] = '/workspace/shared'
    if change == 'host_environment': data['environment'] = {'DOCKER_HOST': 'tcp://other-engine:2375'}
    with pytest.raises(SandboxConfigurationError):
        broker.BrokerPolicy({'/workspace/product': tmp_path}, SandboxLimits(), frozenset()).decode(data)


def test_symlink_mount_rejected(tmp_path):
    remote, broker = modules()
    root = tmp_path / 'root'
    root.mkdir()
    link = root / 'link'
    try:
        link.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip('symlink creation unavailable')
    with pytest.raises(SandboxConfigurationError):
        broker.BrokerPolicy({'/workspace/product': root}, SandboxLimits(), frozenset()).decode(remote.encode_request(request(link)))


def test_result_preserves_binary_and_status():
    remote, _ = modules()
    result = SandboxResult('test-1', None, b'\x00\xff', b'\xfe', True, True)
    assert remote.decode_result(remote.encode_result(result), 'test-1', 1024) == result
    with pytest.raises(SandboxConfigurationError):
        remote.decode_result(remote.encode_result(result), 'wrong-id', 1024)


def test_unavailable_broker_fails_closed(tmp_path):
    remote, _ = modules()
    async def check():
        backend = remote.RemoteSandboxBackend(tmp_path / 'absent.sock')
        assert not (await backend.availability()).available
        with pytest.raises(SandboxUnavailable):
            await backend.run(request(tmp_path))
    asyncio.run(check())


def test_http_invalid_path_never_reaches_backend_and_binary_roundtrip(tmp_path):
    pytest.importorskip('starlette')
    import httpx
    remote, broker = modules()
    class Backend:
        calls = 0
        async def run(self, command):
            self.calls += 1
            assert command.mounts[0].host_path == tmp_path.resolve()
            return SandboxResult(command.request_id, 0, command.stdin, b'error', False, True)
    async def check():
        backend = Backend()
        app = broker.create_app(broker.BrokerPolicy({'/workspace/product': tmp_path}, SandboxLimits(), frozenset()), backend)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            data = remote.encode_request(request(tmp_path))
            data['mounts'][0]['host_path'] = str(tmp_path.parent)
            assert (await client.post('/run', json=data)).status_code == 400
            assert backend.calls == 0
            response = await client.post('/run', json=remote.encode_request(request(tmp_path)))
            assert response.status_code == 200
            result = remote.decode_result(response.json(), 'test-1', 1024)
            assert result.stdout == b'\x00\xff'
            assert result.stderr == b'error'
            assert result.output_truncated
            assert backend.calls == 1
    asyncio.run(check())


def test_http_disconnect_awaits_backend_cleanup(tmp_path):
    pytest.importorskip('starlette')
    remote, broker = modules()
    async def check():
        started, cleaned = asyncio.Event(), asyncio.Event()
        class Backend:
            async def run(self, command):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0)
                    cleaned.set()
        app = broker.create_app(broker.BrokerPolicy({'/workspace/product': tmp_path}, SandboxLimits(), frozenset()), Backend())
        first = True
        async def receive():
            nonlocal first
            if first:
                first = False
                return {'type': 'http.request', 'body': remote.bounded_json(remote.encode_request(request(tmp_path)), remote.MAX_REQUEST_BYTES), 'more_body': False}
            await started.wait()
            return {'type': 'http.disconnect'}
        messages = []
        async def send(message):
            messages.append(message)
        await asyncio.wait_for(app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': 'POST',
                                    'scheme': 'http', 'path': '/run', 'raw_path': b'/run', 'query_string': b'', 'headers': [],
                                    'client': None, 'server': None}, receive, send), 3)
        assert cleaned.is_set()
        assert messages[0]['status'] == 499
    asyncio.run(check())


@pytest.mark.skipif(os.name != 'posix', reason='Unix socket permissions')
def test_socket_binding_preserves_existing_file_and_is_private(tmp_path):
    _, broker = modules()
    tmp_path.chmod(0o700)
    path = tmp_path / 'broker.sock'
    path.write_text('owned by somebody else')
    with pytest.raises(SandboxConfigurationError):
        broker.bind_socket(path)
    assert path.read_text() == 'owned by somebody else'
    fresh = tmp_path / 'fresh.sock'
    listener = broker.bind_socket(fresh)
    try:
        assert fresh.stat().st_mode & 0o777 == 0o600
    finally:
        listener.close()


def test_duplicate_json_fields_and_oversized_messages_rejected():
    remote, _ = modules()
    with pytest.raises(SandboxConfigurationError):
        remote.parse_json(b'{"argv":[],"argv":["dangerous"]}')
    with pytest.raises(SandboxConfigurationError):
        remote.bounded_json({'payload': 'x' * 100}, 32)


def test_shutdown_signal_capture_returns_and_restores_handlers():
    _, broker = modules()
    class Server:
        should_exit = False
    server = Server()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    with broker._capture_shutdown_signals(server):
        for sig in previous:
            signal.getsignal(sig)(sig, None)
            assert server.should_exit
    assert all(signal.getsignal(sig) == handler for sig, handler in previous.items())


@pytest.mark.skipif(os.name != 'posix', reason='Unix socket ownership and flock')
def test_owned_socket_preserves_live_listener_and_recovers_stale_socket(tmp_path):
    _, broker = modules()
    tmp_path.chmod(0o700)
    path = tmp_path / 'broker.sock'
    existing = broker.bind_socket(path)
    identity = path.stat()
    try:
        with pytest.raises(SandboxConfigurationError):
            with broker._owned_socket(path):
                pass
        assert path.stat().st_ino == identity.st_ino
    finally:
        existing.close()
    with broker._owned_socket(path):
        with pytest.raises(SandboxConfigurationError):
            with broker._owned_socket(path):
                pass
        assert path.exists()
    assert not path.exists()
    assert (tmp_path / 'broker.sock.lock').stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != 'posix', reason='Unix socket ownership and flock')
def test_owned_socket_preserves_replacement_and_unowned_paths(tmp_path, monkeypatch):
    _, broker = modules()
    tmp_path.chmod(0o700)
    path = tmp_path / 'broker.sock'
    with broker._owned_socket(path):
        path.unlink()
        path.write_text('replacement')
    assert path.read_text() == 'replacement'
    with pytest.raises(SandboxConfigurationError):
        with broker._owned_socket(path):
            pass
    path.unlink()
    listener = broker.bind_socket(path)
    listener.close()
    actual_lstat = Path.lstat
    def other_owner(item):
        result = actual_lstat(item)
        if item == path:
            fields = list(result)
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)
        return result
    monkeypatch.setattr(Path, 'lstat', other_owner)
    with pytest.raises(SandboxConfigurationError):
        with broker._owned_socket(path):
            pass
    assert path.exists()


@pytest.mark.skipif(os.name != 'posix', reason='real Unix signal shutdown')
@pytest.mark.parametrize('shutdown_signal', [signal.SIGTERM, signal.SIGINT])
def test_signal_shutdown_finishes_cleanup_then_removes_owned_socket(tmp_path, shutdown_signal):
    pytest.importorskip('uvicorn')
    pytest.importorskip('starlette')
    tmp_path.chmod(0o700)
    path = tmp_path / 'broker.sock'
    marker = tmp_path / 'cleaned'
    child = r'''
import asyncio, contextlib, sys
from pathlib import Path
from starlette.applications import Starlette
from suiteharness.sandbox.broker import _owned_socket, _serve_app
path, marker = map(Path, sys.argv[1:])
@contextlib.asynccontextmanager
async def lifespan(app):
    try:
        yield
    finally:
        await asyncio.sleep(0.05)
        assert path.exists(), 'socket removed before execution cleanup'
        marker.write_text('cleaned')
async def main():
    with _owned_socket(path) as listener:
        await _serve_app(Starlette(lifespan=lifespan), listener)
asyncio.run(main())
'''
    process = subprocess.Popen([sys.executable, '-c', child, str(path), str(marker)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        ready = False
        while process.poll() is None and time.monotonic() < deadline:
            if path.exists():
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(0.2)
                    try:
                        client.connect(str(path))
                        client.sendall(b'GET / HTTP/1.0\r\n\r\n')
                        ready = b'404' in client.recv(1024)
                    except OSError:
                        pass
                if ready:
                    break
            time.sleep(0.02)
        assert ready, 'broker did not start'
        process.send_signal(shutdown_signal)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, (stdout, stderr)
        assert marker.read_text() == 'cleaned'
        assert not path.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
