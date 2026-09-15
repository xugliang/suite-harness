"""Fail-closed Docker sandbox adapter and deterministic command builder."""

from __future__ import annotations

from suiteharness.sandbox.protocols import SandboxBackend

import asyncio
import json
import logging
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from time import monotonic
from types import MappingProxyType

from .models import (
    SandboxAvailability,
    SandboxCleanupError,
    SandboxConfigurationError,
    SandboxLimits,
    SandboxNetworkMode,
    SandboxRequest,
    SandboxResult,
    SandboxUnavailable,
)
from .protocols import SandboxProcessTransport, SandboxQuarantineStore, SandboxSession

_DIGEST_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_DOCKER_RESOURCE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
_DOCKER_CONTAINER = re.compile(r"^suiteharness-sandbox-[0-9a-f]{24}$")
_HEALTHY_PROBE_TTL_SECONDS = 3.0
_FAILED_PROBE_TTL_SECONDS = 0.25
_LOGGER = logging.getLogger(__name__)


def _within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


@dataclass(frozen=True)
class DockerBackendConfig:
    image: str
    allowed_host_roots: tuple[Path, ...]
    binary: str = "docker"
    context: str | None = None
    require_rootless: bool = False
    production: bool = True
    egress_networks: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.binary or Path(self.binary).name != self.binary:
            raise SandboxConfigurationError("Docker binary must be a bare executable name")
        if not isinstance(self.require_rootless, bool):
            raise SandboxConfigurationError("require_rootless must be a boolean")
        if self.context is not None and (
            len(self.context) > 128 or not _DOCKER_RESOURCE.fullmatch(self.context)
        ):
            raise SandboxConfigurationError("invalid Docker context name")
        if self.production and not _DIGEST_IMAGE.fullmatch(self.image):
            raise SandboxConfigurationError("production Docker image must be pinned by sha256 digest")
        if (
            not self.image
            or self.image.startswith("-")
            or "\x00" in self.image
            or any(char.isspace() for char in self.image)
        ):
            raise SandboxConfigurationError("invalid Docker image reference")
        roots = tuple(Path(item).resolve(strict=True) for item in self.allowed_host_roots)
        if not roots or any(not item.is_dir() for item in roots):
            raise SandboxConfigurationError("Docker requires existing allowed_host_roots")
        networks = dict(self.egress_networks or {})
        for profile, network in networks.items():
            if not _DOCKER_RESOURCE.fullmatch(profile) or not _DOCKER_RESOURCE.fullmatch(network):
                raise SandboxConfigurationError("invalid Docker egress profile or network name")
        object.__setattr__(self, "allowed_host_roots", roots)
        object.__setattr__(self, "egress_networks", MappingProxyType(networks))

    @property
    def command_prefix(self) -> tuple[str, ...]:
        if self.context is None:
            return (self.binary,)
        return (self.binary, "--context", self.context)


class DockerCommandBuilder:
    def __init__(self, config: DockerBackendConfig) -> None:
        self._config = config

    def build(self, request: SandboxRequest) -> tuple[str, ...]:
        command = self._run_prefix(request.request_id, request.limits)
        if request.network.mode is SandboxNetworkMode.NONE:
            command.append("--network=none")
        else:
            profile = request.network.egress_profile or ""
            network = self._config.egress_networks.get(profile)
            if network is None:
                raise SandboxConfigurationError("requested egress profile is not configured")
            command.extend((f"--network={network}", f"--env=SUITEHARNESS_EGRESS_PROFILE={profile}"))
        for mount in request.mounts:
            source = mount.host_path.resolve(strict=True)
            if not any(_within(source, root) for root in self._config.allowed_host_roots):
                raise SandboxConfigurationError("sandbox mount escapes allowed host roots")
            option = f"type=bind,src={source},dst={mount.container_path}"
            if mount.read_only:
                option += ",readonly"
            command.extend(("--mount", option))
        # Values stay outside argv, where process listings could expose them.
        for key in sorted(request.environment):
            command.append(f"--env={key}")
        command.extend((f"--workdir={request.working_directory}", self._config.image))
        command.extend(request.argv)
        return tuple(command)

    def build_probe(self, request_id: str, limits: SandboxLimits) -> tuple[str, ...]:
        """Trusted smoke command with the same isolation and no host mounts."""
        return (*self._run_prefix(request_id, limits), "--network=none", "--workdir=/tmp",
                self._config.image, "/bin/sh", "-c", "exit 0")

    def _run_prefix(self, request_id: str, limits: SandboxLimits) -> list[str]:
        return [
            *self._config.command_prefix,
            "run",
            "--rm",
            f"--name={self.container_name(request_id)}",
            "--init",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges=true",
            "--user=65532:65532",
            f"--cpus={limits.cpu_count:g}",
            f"--memory={limits.memory_mb}m",
            f"--pids-limit={limits.pids}",
            f"--tmpfs=/tmp:rw,noexec,nosuid,nodev,size={limits.tmpfs_mb}m",
        ]

    @staticmethod
    def container_name(request_id: str) -> str:
        digest = sha256(request_id.encode("utf-8")).hexdigest()[:24]
        return f"suiteharness-sandbox-{digest}"


class DockerSandboxBackend(SandboxBackend):
    """Production backend; every execution first verifies Docker availability."""

    def __init__(
        self,
        config: DockerBackendConfig,
        transport: SandboxProcessTransport,
        *,
        quarantine_store: SandboxQuarantineStore | None = None,
        probe_clock: Callable[[], float] | None = None,
        probe_limits: SandboxLimits | None = None,
    ) -> None:
        if config.production and quarantine_store is None:
            raise SandboxConfigurationError(
                "production Docker requires a durable quarantine_store"
            )
        self._config = config
        self._transport = transport
        self._builder = DockerCommandBuilder(config)
        self._quarantine_store = quarantine_store
        self._quarantine_lock = asyncio.Lock()
        self._quarantined: dict[str, str] = {}
        self._quarantine_loaded = quarantine_store is None
        self._quarantine_load_error: str | None = None
        self._probe_clock = monotonic if probe_clock is None else probe_clock
        self._probe_lock = asyncio.Lock()
        self._cached_probe: SandboxAvailability | None = None
        self._cached_probe_expires_at = 0.0
        limits = probe_limits or SandboxLimits()
        self._probe_limits = replace(limits, timeout_seconds=min(limits.timeout_seconds, 5.0),
                                     output_bytes=min(limits.output_bytes, 4096))

    @property
    def backend_id(self) -> str:
        return "docker"

    @property
    def production_safe(self) -> bool:
        return self._config.production

    async def availability(self) -> SandboxAvailability:
        await self._ensure_quarantine_loaded()
        quarantine = await self._quarantine_availability()
        if quarantine is not None:
            return quarantine
        daemon = await self._probe_readiness_cached()
        # A cleanup may become uncertain while a daemon probe is in flight.
        # Recheck the fail-closed state so a cached healthy result can never
        # mask newly recorded quarantine state.
        quarantine = await self._quarantine_availability()
        return quarantine or daemon

    async def _quarantine_availability(self) -> SandboxAvailability | None:
        async with self._quarantine_lock:
            quarantined = tuple(sorted(self._quarantined))
            load_error = self._quarantine_load_error
        if load_error is not None:
            return SandboxAvailability(False, load_error)
        if quarantined:
            return SandboxAvailability(
                False,
                "Docker sandbox is quarantined after an uncertain cleanup: "
                + ", ".join(quarantined),
            )
        return None

    async def _probe_readiness_cached(self) -> SandboxAvailability:
        """Coalesce full execution probes and briefly reuse their result."""

        async with self._probe_lock:
            now = self._probe_clock()
            if (
                self._cached_probe is not None
                and now < self._cached_probe_expires_at
            ):
                return self._cached_probe
            result = await self._probe_daemon()
            if result.available:
                result = await self._probe_execution()
            ttl = (
                _HEALTHY_PROBE_TTL_SECONDS
                if result.available
                else _FAILED_PROBE_TTL_SECONDS
            )
            self._cached_probe = result
            self._cached_probe_expires_at = self._probe_clock() + ttl
            return result

    async def _probe_daemon(self) -> SandboxAvailability:
        command = (
            (
                *self._config.command_prefix,
                "info",
                "--format",
                "{{json .SecurityOptions}}",
            )
            if self._config.require_rootless
            else (
                *self._config.command_prefix,
                "version",
                "--format",
                "{{.Server.Version}}",
            )
        )
        try:
            result = await self._transport.probe(
                command,
                timeout_seconds=5.0,
            )
        except Exception:
            return SandboxAvailability(False, "Docker availability probe failed")
        if result.timed_out:
            return SandboxAvailability(False, "Docker availability probe timed out")
        if result.exit_code != 0:
            return SandboxAvailability(False, "Docker daemon is unavailable")
        if self._config.require_rootless:
            try:
                options = json.loads(result.stdout.decode("utf-8", errors="strict"))
            except (UnicodeError, json.JSONDecodeError):
                return SandboxAvailability(
                    False,
                    "Docker daemon rootless status could not be verified",
                )
            if (
                not isinstance(options, list)
                or len(options) > 128
                or any(not isinstance(option, str) for option in options)
                or "name=rootless" not in options
            ):
                return SandboxAvailability(
                    False,
                    "Docker daemon is not verified rootless",
                )
        if self._config.production:
            try:
                image = await self._transport.probe(
                    (
                        *self._config.command_prefix,
                        "image",
                        "inspect",
                        "--format",
                        "{{.Id}}",
                        self._config.image,
                    ),
                    timeout_seconds=5.0,
                )
            except Exception:
                return SandboxAvailability(
                    False,
                    "Pinned Docker sandbox image inspection failed",
                )
            if image.timed_out:
                return SandboxAvailability(
                    False,
                    "Pinned Docker sandbox image inspection timed out",
                )
            if image.exit_code != 0 or not image.stdout.strip():
                return SandboxAvailability(
                    False,
                    "Pinned Docker sandbox image is unavailable",
                )
        return SandboxAvailability(True)

    async def _probe_execution(self) -> SandboxAvailability:
        request_id = "readiness-" + secrets.token_hex(16)
        try:
            result = await self._execute(
                request_id, self._builder.build_probe(request_id, self._probe_limits),
                limits=self._probe_limits, environment={}, stdin=None,
            )
        except Exception:
            return SandboxAvailability(False, "Docker constrained execution probe failed")
        if result.timed_out:
            return SandboxAvailability(False, "Docker constrained execution probe timed out")
        if result.exit_code != 0:
            return SandboxAvailability(False, "Docker constrained execution is unavailable")
        return SandboxAvailability(True)

    async def run(self, request: SandboxRequest) -> SandboxResult:
        available = await self.availability()
        if not available.available:
            raise SandboxUnavailable(available.detail)
        command = self._builder.build(request)
        return await self._execute(request.request_id, command, limits=request.limits,
                                   environment=request.environment, stdin=request.stdin)

    async def open_session(self, request: SandboxRequest) -> SandboxSession:
        available = await self.availability()
        if not available.available:
            raise SandboxUnavailable(available.detail)
        command = list(self._builder.build(request))
        command.insert(command.index("run") + 1, "--interactive")
        lease = await self._record_active_lease(request.request_id)
        try:
            session = await self._transport.open_session(
                tuple(command), environment=request.environment,
                timeout_seconds=request.limits.timeout_seconds,
                output_bytes=request.limits.output_bytes,
            )
        except BaseException:
            await asyncio.shield(self._force_remove(request.request_id, owner_token=lease))
            raise
        return _DockerSession(session, lambda: self._force_remove(request.request_id, owner_token=lease))

    async def _execute(self, request_id: str, command: tuple[str, ...], *,
                       limits: SandboxLimits, environment: Mapping[str, str],
                       stdin: bytes | None) -> SandboxResult:
        lease_token = await self._record_active_lease(request_id)
        try:
            result = await self._transport.execute(
                command,
                working_directory=None,
                environment=environment,
                stdin=stdin,
                timeout_seconds=limits.timeout_seconds,
                output_bytes=limits.output_bytes,
            )
        except BaseException as cause:
            self._cached_probe = None
            # Cancelling the Docker CLI does not guarantee that the daemon also
            # stopped the already-created container. Always target the one
            # deterministic name before propagating cancellation/failure.
            try:
                await asyncio.shield(
                    self._force_remove(request_id, owner_token=lease_token)
                )
            except SandboxCleanupError as cleanup_error:
                cause.add_note(str(cleanup_error))
            raise
        if result.exit_code != 0 or result.timed_out:
            self._cached_probe = None
        if result.timed_out or result.exit_code != 0:
            # Killing ``docker run`` does not reliably stop the container. The
            # CLI may also fail after creation. Remove only this exact name
            # before clearing its lease in either failure case.
            await self._force_remove(request_id, owner_token=lease_token)
        else:
            await self._clear_persisted_lease(
                self._builder.container_name(request_id),
                owner_token=lease_token,
            )
        return SandboxResult(
            request_id=request_id,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
            output_truncated=result.output_truncated,
        )

    async def _force_remove(
        self,
        request_id: str,
        *,
        owner_token: str | None,
    ) -> None:
        container_name = self._builder.container_name(request_id)
        try:
            result = await self._transport.execute(
                (
                    *self._config.command_prefix,
                    "rm",
                    "--force",
                    container_name,
                ),
                working_directory=None,
                environment={},
                stdin=None,
                timeout_seconds=10.0,
                output_bytes=64 * 1024,
            )
        except BaseException as exc:
            await self._quarantine(container_name, "cleanup command raised an exception")
            raise SandboxCleanupError(
                f"Docker cleanup failed; backend quarantined for {container_name}"
            ) from exc
        if result.timed_out or result.exit_code != 0:
            reason = (
                "cleanup command timed out"
                if result.timed_out
                else f"cleanup command exited with code {result.exit_code}"
            )
            await self._quarantine(container_name, reason)
            raise SandboxCleanupError(
                f"Docker cleanup failed; backend quarantined for {container_name}"
            )
        await self._clear_persisted_lease(container_name, owner_token=owner_token)

    async def _record_active_lease(self, request_id: str) -> str | None:
        store = self._quarantine_store
        if store is None:
            return None
        container_name = self._builder.container_name(request_id)
        owner_token = secrets.token_urlsafe(32)
        try:
            claimed = await store.claim(
                container_name,
                "active container lease",
                owner_token=owner_token,
            )
        except BaseException as exc:
            await self._quarantine(
                container_name,
                "active container lease could not be persisted",
            )
            raise SandboxUnavailable(
                "Docker refused to start because its active lease was not persisted"
            ) from exc
        if not claimed:
            raise SandboxUnavailable(
                "Docker refused to start because the container name already has "
                "an active or quarantined lease"
            )
        return owner_token

    async def _clear_persisted_lease(
        self,
        container_name: str,
        *,
        owner_token: str | None,
    ) -> None:
        store = self._quarantine_store
        if store is None:
            return
        try:
            cleared = await store.clear(container_name, owner_token=owner_token)
        except BaseException as exc:
            async with self._quarantine_lock:
                self._quarantined[container_name] = (
                    "completed container lease could not be cleared"
                )
            _LOGGER.critical(
                "Docker active lease could not be cleared: container=%s",
                container_name,
                exc_info=True,
            )
            raise SandboxCleanupError(
                f"Docker lease cleanup failed; backend quarantined for {container_name}"
            ) from exc
        if not cleared:
            reason = "active container lease ownership changed before cleanup"
            await self._quarantine(container_name, reason)
            raise SandboxCleanupError(
                f"Docker lease cleanup failed; backend quarantined for {container_name}"
            )

    async def _quarantine(self, container_name: str, reason: str) -> None:
        async with self._quarantine_lock:
            self._quarantined[container_name] = reason
        persistence_failure: BaseException | None = None
        if self._quarantine_store is not None:
            try:
                await self._quarantine_store.mark(container_name, reason)
            except BaseException as exc:
                persistence_failure = exc
        _LOGGER.critical(
            "Docker sandbox quarantined after uncertain cleanup: container=%s reason=%s",
            container_name,
            reason,
        )
        if persistence_failure is not None:
            _LOGGER.critical(
                "Docker quarantine persistence failed: container=%s",
                container_name,
                exc_info=persistence_failure,
            )

    async def _ensure_quarantine_loaded(self) -> None:
        async with self._quarantine_lock:
            if self._quarantine_loaded:
                return
            store = self._quarantine_store
            assert store is not None
            try:
                persisted = dict(await store.load())
                for name, reason in persisted.items():
                    if (
                        not _DOCKER_CONTAINER.fullmatch(name)
                        or not reason
                        or len(reason) > 512
                        or any(char in reason for char in "\x00\r\n")
                    ):
                        raise ValueError("invalid persisted sandbox quarantine entry")
                self._quarantined.update(persisted)
            except BaseException:
                self._quarantine_load_error = (
                    "Docker sandbox quarantine state could not be loaded"
                )
                _LOGGER.critical(
                    "Docker sandbox quarantine state could not be loaded",
                    exc_info=True,
                )
            finally:
                self._quarantine_loaded = True

    @property
    def quarantined_containers(self) -> tuple[str, ...]:
        """Exact deterministic container names awaiting operator reconciliation."""

        return tuple(sorted(self._quarantined))

    async def reconcile_cleanup(self) -> SandboxAvailability:
        """Retry exact-name cleanup and clear quarantine only after verification.

        This is an explicit administrator operation. It never enumerates all
        containers and cannot clear an entry merely because the daemon probe is
        healthy.
        """

        await self._ensure_quarantine_loaded()
        async with self._quarantine_lock:
            if self._quarantine_load_error is not None:
                return SandboxAvailability(False, self._quarantine_load_error)
        # Reconciliation must not create a new container before old leases are cleared.
        self._cached_probe = None
        daemon = await self._probe_daemon()
        if not daemon.available:
            return daemon
        async with self._quarantine_lock:
            names = tuple(sorted(self._quarantined))
        for name in names:
            listing = await self._transport.execute(
                (
                    *self._config.command_prefix,
                    "container",
                    "ls",
                    "--all",
                    "--quiet",
                    f"--filter=name=^/{name}$",
                ),
                working_directory=None,
                environment={},
                stdin=None,
                timeout_seconds=10.0,
                output_bytes=64 * 1024,
            )
            if listing.timed_out or listing.exit_code != 0:
                return SandboxAvailability(
                    False, f"Docker cleanup reconciliation could not inspect {name}"
                )
            if listing.stdout.strip():
                removal = await self._transport.execute(
                    (*self._config.command_prefix, "rm", "--force", name),
                    working_directory=None,
                    environment={},
                    stdin=None,
                    timeout_seconds=10.0,
                    output_bytes=64 * 1024,
                )
                if removal.timed_out or removal.exit_code != 0:
                    return SandboxAvailability(
                        False, f"Docker cleanup reconciliation could not remove {name}"
                    )
                verified = await self._transport.execute(
                    (
                        *self._config.command_prefix,
                        "container",
                        "ls",
                        "--all",
                        "--quiet",
                        f"--filter=name=^/{name}$",
                    ),
                    working_directory=None,
                    environment={},
                    stdin=None,
                    timeout_seconds=10.0,
                    output_bytes=64 * 1024,
                )
                if (
                    verified.timed_out
                    or verified.exit_code != 0
                    or verified.stdout.strip()
                ):
                    return SandboxAvailability(
                        False, f"Docker cleanup reconciliation could not verify {name}"
                    )
            async with self._quarantine_lock:
                if self._quarantine_store is not None:
                    try:
                        await self._quarantine_store.clear(name)
                    except BaseException:
                        _LOGGER.critical(
                            "Docker quarantine reconciliation could not be persisted: "
                            "container=%s",
                            name,
                            exc_info=True,
                        )
                        return SandboxAvailability(
                            False,
                            f"Docker cleanup reconciliation could not persist {name}",
                        )
                self._quarantined.pop(name, None)
        return await self.availability()


class _DockerSession(SandboxSession):
    def __init__(self, session, cleanup):
        self._session = session
        self._cleanup = cleanup
        self._close_task = None
        self._watcher = asyncio.create_task(self._watch())

    async def _watch(self):
        await self._session.wait_closed()
        try:
            await self.close()
        except SandboxCleanupError:
            # Backend persists quarantine; subsequent availability fails closed.
            pass

    async def send_line(self, payload):
        await self._session.send_line(payload)

    async def receive_line(self):
        return await self._session.receive_line()

    async def _finish(self):
        try:
            await self._session.close()
        finally:
            await self._cleanup()

    async def close(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._finish())
        await asyncio.shield(self._close_task)

    async def wait_closed(self):
        await self._session.wait_closed()
        await self.close()


__all__ = ["DockerBackendConfig", "DockerCommandBuilder", "DockerSandboxBackend"]
