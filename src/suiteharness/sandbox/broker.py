"""Product-scoped rootless Docker broker; no TCP listener or Docker API proxy."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import errno
import hashlib
import math
import os
import signal
import socket
import stat
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath

from .models import SandboxConfigurationError, SandboxLimits, SandboxMount, SandboxNetworkMode, SandboxNetworkPolicy, SandboxRequest, SandboxUnavailable
from .remote import MAX_OUTPUT_BYTES, MAX_REQUEST_BYTES, MAX_STDIN_BYTES, boolean, bounded_json, encode_result, fields, parse_json, string, unbytes


def safe_directory(path):
    if not path.is_absolute() or '..' in path.parts:
        raise SandboxConfigurationError('workspace path must be normalized and absolute')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise SandboxConfigurationError('symlink workspace paths are forbidden')
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SandboxConfigurationError('workspace directory unavailable') from exc
    if not resolved.is_dir():
        raise SandboxConfigurationError('workspace path must be a directory')
    return resolved


@dataclass(frozen=True)
class BrokerPolicy:
    roots: dict[str, Path]
    limits: SandboxLimits
    allowed_profiles: frozenset[str]
    namespace: str = 'broker'

    def decode(self, data, *, persistent=False):
        fields(data, 'request_id argv mounts working_directory environment stdin limits network')
        bounded_json(data, MAX_REQUEST_BYTES)
        argv = data['argv']
        if type(argv) is not list or not 1 <= len(argv) <= 256:
            raise SandboxConfigurationError('invalid argv')
        argv = tuple(string(value) for value in argv)
        environment = data['environment']
        if type(environment) is not dict or len(environment) > 128:
            raise SandboxConfigurationError('invalid environment')
        environment = {string(k, 128): string(v) for k, v in environment.items()}
        # Existing Bash requests have no environment overlay. Docker's transport
        # would apply it to the host CLI too (DOCKER_HOST, PATH, LD_PRELOAD, ...).
        # Callers may set command-local variables inside their sandbox argv.
        if environment:
            raise SandboxConfigurationError('broker does not accept host environment overlays')
        limits = fields(data['limits'], 'cpu_count memory_mb pids timeout_seconds output_bytes tmpfs_mb')
        for key, maximum in asdict(self.limits).items():
            if persistent and key == 'timeout_seconds':
                maximum = 86400.0  # Persistent process lifetime, not one command deadline.
            value = limits[key]
            expected = (int, float) if key in ('cpu_count', 'timeout_seconds') else (int,)
            if type(value) not in expected or not math.isfinite(value) or value > maximum:
                raise SandboxConfigurationError('sandbox resource ceiling exceeded or malformed')
        limits = SandboxLimits(**limits)
        if limits.output_bytes > MAX_OUTPUT_BYTES:
            raise SandboxConfigurationError('sandbox output exceeds protocol ceiling')
        network = fields(data['network'], 'mode egress_profile')
        if network['mode'] == 'none' and network['egress_profile'] is None:
            network = SandboxNetworkPolicy()
        elif network['mode'] == 'egress_profile' and type(network['egress_profile']) is str and network['egress_profile'] in self.allowed_profiles:
            network = SandboxNetworkPolicy(SandboxNetworkMode.EGRESS_PROFILE, network['egress_profile'])
        else:
            raise SandboxConfigurationError('network policy denied')
        if type(data['mounts']) is not list or not 1 <= len(data['mounts']) <= 64:
            raise SandboxConfigurationError('invalid mount count')
        mounts = []
        for mount in data['mounts']:
            fields(mount, 'host_path container_path read_only')
            host = safe_directory(Path(string(mount['host_path'], 4096)))
            target = PurePosixPath(string(mount['container_path'], 4096))
            if '..' in target.parts or str(target) != mount['container_path']:
                raise SandboxConfigurationError('invalid container path')
            matched = False
            for container_root, host_root in self.roots.items():
                root = safe_directory(host_root)
                base = PurePosixPath(container_root)
                if persistent and container_root == '/workspace/product' and str(target) == '/workspace':
                    matched = host == root and mount['read_only'] is True
                    if matched:
                        break
                if target.is_relative_to(base) and host.is_relative_to(root):
                    matched = host.relative_to(root).as_posix() == target.relative_to(base).as_posix()
                    if matched:
                        break
            if not matched:
                raise SandboxConfigurationError('mount is outside this product workspace')
            mounts.append(SandboxMount(host, str(target), boolean(mount['read_only'])))
        if persistent and (len(mounts) != 1 or mounts[0].container_path != '/workspace'
                           or not mounts[0].read_only or data['stdin'] is not None):
            raise SandboxConfigurationError('persistent MCP sessions require one read-only product mount and no initial stdin')
        return SandboxRequest(string(data['request_id'], 128), argv, tuple(mounts),
                              string(data['working_directory'], 4096), environment,
                              None if data['stdin'] is None else unbytes(data['stdin'], MAX_STDIN_BYTES), limits, network)


def create_app(policy, backend, database=None):
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    active = set()
    busy = 0
    from .session_broker import SessionBroker
    sessions = SessionBroker(policy, backend, lambda: busy)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            tasks = tuple(active)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await sessions.close()
            if database is not None:
                await database.close()

    async def availability(request):
        try:
            state = await backend.availability()
            return JSONResponse({'available': state.available, 'detail': 'ready' if state.available else 'sandbox unavailable'})
        except Exception:
            return JSONResponse({'available': False, 'detail': 'sandbox unavailable'})

    async def run(request):
        nonlocal busy
        if busy + sessions.count >= 4:
            return JSONResponse({'error': 'sandbox busy'}, status_code=429)
        busy += 1
        execution = disconnect = None
        try:
            data = bytearray()
            async with asyncio.timeout(10):
                async for chunk in request.stream():
                    data.extend(chunk)
                    if len(data) > MAX_REQUEST_BYTES:
                        raise SandboxConfigurationError('request exceeds limit')
            command = policy.decode(parse_json(data))
            # Distinct product namespaces prevent cross-broker name collisions.
            internal_id = hashlib.sha256((policy.namespace + ':' + command.request_id).encode()).hexdigest()
            execution = asyncio.create_task(backend.run(replace(command, request_id=internal_id)))
            active.add(execution)

            async def disconnected():
                while True:
                    if (await request.receive())['type'] == 'http.disconnect':
                        return

            disconnect = asyncio.create_task(disconnected())
            done, _ = await asyncio.wait((execution, disconnect), return_when=asyncio.FIRST_COMPLETED)
            if execution not in done:
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
                return Response(status_code=499)
            result = replace(await execution, request_id=command.request_id)
            # Validate backend output before serializing to the caller.
            from .remote import decode_result
            encoded = encode_result(result)
            decode_result(encoded, command.request_id, command.limits.output_bytes)
            return Response(bounded_json(encoded, MAX_OUTPUT_BYTES * 2), media_type='application/json')
        except (SandboxConfigurationError, ValueError, TypeError, KeyError, TimeoutError):
            return JSONResponse({'error': 'invalid sandbox request'}, status_code=400)
        except SandboxUnavailable:
            return JSONResponse({'error': 'sandbox unavailable'}, status_code=503)
        except Exception:
            return JSONResponse({'error': 'sandbox failed closed'}, status_code=503)
        finally:
            if disconnect is not None:
                disconnect.cancel()
                await asyncio.gather(disconnect, return_exceptions=True)
            if execution is not None:
                if not execution.done():
                    execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
                active.discard(execution)
            busy -= 1

    routes = [Route('/availability', availability), Route('/run', run, methods=['POST'])]
    routes.extend(Route('/session/' + action, sessions.dispatch, methods=['POST'])
                  for action in ('open', 'send', 'receive', 'close'))
    return Starlette(routes=routes, lifespan=lifespan)


def bind_socket(path):
    """Never unlink an existing file, even an apparently stale socket."""
    path = Path(path)
    parent = safe_directory(path.parent)
    info = parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise SandboxConfigurationError('broker socket parent must be owner-controlled and not group/world writable')
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise SandboxConfigurationError('broker socket path must be absolute and unused')
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        old_umask = os.umask(0o177)
        try:
            listener.bind(str(path))
        finally:
            os.umask(old_umask)
        listener.listen(16)
        return listener
    except BaseException:
        listener.close()
        raise


def _unlink_owned_socket(path, identity):
    with contextlib.suppress(FileNotFoundError):
        current = path.lstat()
        if (stat.S_ISSOCK(current.st_mode) and current.st_uid == os.getuid()
                and (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino)):
            path.unlink()


@contextlib.contextmanager
def _owned_socket(path):
    """Serialize brokers through a persistent private lock, including stale recovery."""
    import fcntl

    path = Path(path)
    parent = safe_directory(path.parent)
    info = parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise SandboxConfigurationError('broker socket parent must be owner-controlled and not group/world writable')
    lock_path = path.with_name(path.name + '.lock')
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    listener = identity = None
    try:
        lock_info = os.fstat(lock_fd)
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid()
                or lock_info.st_mode & 0o077 or lock_info.st_nlink != 1):
            raise SandboxConfigurationError('broker socket lock must be a private owned regular file')
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SandboxConfigurationError('another broker owns this socket lock') from exc
        try:
            stale = path.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(stale.st_mode) or stale.st_uid != os.getuid():
                raise SandboxConfigurationError('existing socket path is not an owned socket')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as check:
                check.settimeout(0.25)
                try:
                    check.connect(str(path))
                except OSError as exc:
                    if exc.errno != errno.ECONNREFUSED:
                        raise SandboxConfigurationError('existing socket liveness is uncertain') from exc
                else:
                    raise SandboxConfigurationError('another listener owns this socket')
            _unlink_owned_socket(path, stale)
        listener = bind_socket(path)
        identity = path.lstat()
        yield listener
    finally:
        if listener is not None:
            listener.close()
        try:
            if identity is not None:
                _unlink_owned_socket(path, identity)
        finally:
            # Never unlink the lock: waiters must always share the same inode.
            os.close(lock_fd)


@contextlib.contextmanager
def _capture_shutdown_signals(server):
    """Let outer cleanup finish instead of Uvicorn re-raising SIGTERM."""
    previous = {}
    def shutdown(signum, frame):
        server.should_exit = True
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, shutdown)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


async def _serve_app(app, listener):
    import uvicorn

    class BrokerServer(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):
            # The broker owns signal handling through its outer cleanup scope.
            yield

        def install_signal_handlers(self):
            # Compatibility with Uvicorn versions predating capture_signals.
            pass

    server = BrokerServer(uvicorn.Config(app, access_log=False, log_level='warning',
                                        limit_concurrency=16, timeout_graceful_shutdown=30))
    with _capture_shutdown_signals(server):
        await server.serve(sockets=[listener])


async def serve(config_path, socket_path):
    from suiteharness.config.loader import _default_yaml_decoder, _read_document
    from suiteharness.config.models import DockerSandboxConfig, SuiteHarnessConfig
    from suiteharness.persistence.database import SQLiteDatabase
    from suiteharness.persistence.sandbox import SQLiteSandboxQuarantineStore
    from .docker import DockerBackendConfig, DockerSandboxBackend
    from .process import AsyncioProcessTransport

    if os.name != 'posix' or os.getuid() == 0:
        raise SandboxConfigurationError('broker must run as a nonroot Unix user')
    document = _read_document(Path(config_path), label='broker configuration', maximum_bytes=2 * 1024 * 1024,
                              decoder=_default_yaml_decoder, check_private_permissions=False)
    config = SuiteHarnessConfig.model_validate(document.value)
    sandbox = config.sandbox
    if (not isinstance(sandbox, DockerSandboxConfig)
            or not sandbox.require_rootless or sandbox.context is None
            or getattr(sandbox, 'broker_socket', None) is not None or len(config.customer_bundle.products) != 1):
        raise SandboxConfigurationError('broker requires original single-product rootless Docker configuration with an explicit context')
    product_id = config.customer_bundle.products[0].product_id
    root = safe_directory(config.workspace.root)
    tenant = root / 'tenants' / config.deployment.tenant_id
    policy = BrokerPolicy({'/workspace/product': tenant / 'products' / product_id, '/workspace/shared': tenant / 'shared'},
                          SandboxLimits(**sandbox.limits.model_dump()), sandbox.network.allowed_profiles_by_product.get(product_id, frozenset()),
                          config.deployment.instance_id + ':' + product_id)
    if policy.limits.output_bytes > MAX_OUTPUT_BYTES:
        raise SandboxConfigurationError('configured output ceiling exceeds broker protocol')
    database = SQLiteDatabase(config.storage.runtime_path())
    try:
        backend = DockerSandboxBackend(DockerBackendConfig(image=sandbox.image, allowed_host_roots=(root,), binary=sandbox.binary,
                                       context=sandbox.context, require_rootless=True, production=True, egress_networks=sandbox.network.egress_profiles),
                                       AsyncioProcessTransport(), quarantine_store=SQLiteSandboxQuarantineStore(database, deployment_id=config.deployment.instance_id),
                                       probe_limits=policy.limits)
        with _owned_socket(socket_path) as listener:
            await _serve_app(create_app(policy, backend, database), listener)
    finally:
        await database.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--socket', required=True, type=Path)
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.config, args.socket))
    except Exception:
        parser.exit(1, 'sandbox broker startup failed; verify public configuration, socket directory and rootless runtime\n')


if __name__ == '__main__':
    main()
