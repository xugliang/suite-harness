from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from suiteharness.mcp import (
    ClientCapabilities,
    HttpResumeState,
    McpClientManager,
    McpConnectionState,
    McpHttpResumeCheckpoint,
    McpServerConfig,
    McpTransportError,
    McpTransportKind,
    ProductMcpConfig,
)
from suiteharness.runtime import RequestScope, ScopePath


class MemoryResumeStore:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str, str, str], McpHttpResumeCheckpoint] = {}
        self.loads: list[tuple[str, str, str, str]] = []
        self.saves: list[tuple[tuple[str, str, str, str], int, HttpResumeState, str]] = []
        self.fail_load = False
        self.fail_save_after: int | None = None

    async def load(
        self,
        *,
        tenant_id: str,
        product_id: str,
        server_id: str,
        endpoint: str,
    ) -> McpHttpResumeCheckpoint | None:
        if self.fail_load:
            raise RuntimeError("resume database unavailable")
        key = tenant_id, product_id, server_id, endpoint
        self.loads.append(key)
        return self.records.get(key)

    async def save(
        self,
        *,
        tenant_id: str,
        product_id: str,
        server_id: str,
        endpoint: str,
        state: HttpResumeState,
        expected_revision: int,
        idempotency_key: str,
    ) -> McpHttpResumeCheckpoint:
        key = tenant_id, product_id, server_id, endpoint
        self.saves.append((key, expected_revision, state, idempotency_key))
        if self.fail_save_after is not None and len(self.saves) > self.fail_save_after:
            raise RuntimeError("resume CAS failed")
        current = self.records.get(key)
        revision = 0 if current is None else current.revision
        if revision != expected_revision:
            raise RuntimeError("stale resume revision")
        checkpoint = McpHttpResumeCheckpoint(
            state=state,
            revision=revision + 1,
            updated_at=datetime.now(UTC),
        )
        self.records[key] = checkpoint
        return checkpoint


class ResumableTransport:
    def __init__(self, resume: HttpResumeState | None, *, fail_ping: bool = False) -> None:
        self._resume = resume or HttpResumeState()
        self.fail_ping = fail_ping
        self.requests: list[str] = []
        self.notifications: list[str] = []
        self.normal_close = False
        self.resume_close = False
        self.active_requests = 0
        self.max_active_requests = 0

    @property
    def resume_state(self) -> HttpResumeState:
        return self._resume

    async def start(self, request_handler, notification_handler) -> None:  # type: ignore[no-untyped-def]
        del request_handler, notification_handler

    def set_protocol_version(self, version: str) -> None:
        assert version == "2025-11-25"

    async def request(  # type: ignore[no-untyped-def]
        self, method, params, *, context, timeout_seconds
    ):
        del params, context, timeout_seconds
        self.requests.append(method)
        self.active_requests += 1
        self.max_active_requests = max(self.max_active_requests, self.active_requests)
        try:
            await asyncio.sleep(0)
            if method == "initialize":
                if self._resume.session_id is None:
                    self._resume = HttpResumeState(session_id="remote-session")
                return {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "serverInfo": {"name": "test", "version": "1"},
                }
            if method == "ping":
                event_number = sum(item == "ping" for item in self.requests)
                self._resume = self._resume.model_copy(
                    update={"last_event_id": f"event-{event_number}"}
                )
                if self.fail_ping:
                    raise RuntimeError("remote failed after advancing cursor")
                return {}
            raise AssertionError(f"unexpected method: {method}")
        finally:
            self.active_requests -= 1

    async def notify(  # type: ignore[no-untyped-def]
        self, method, params=None, *, context=None
    ) -> None:
        del params, context
        self.notifications.append(method)

    async def close(self) -> None:
        self.normal_close = True

    async def close_for_resume(self) -> None:
        self.resume_close = True


class ResumeFactory:
    def __init__(self, *, fail_ping: bool = False, ignore_resume: bool = False) -> None:
        self.fail_ping = fail_ping
        self.ignore_resume = ignore_resume
        self.created: list[tuple[str, str, str, HttpResumeState | None, ResumableTransport]] = []

    def create(  # type: ignore[no-untyped-def]
        self,
        config,
        *,
        tenant_id,
        product_id,
        product_workspace,
        resume=None,
    ):
        del product_workspace
        accepted = None if self.ignore_resume else resume
        transport = ResumableTransport(accepted, fail_ping=self.fail_ping)
        self.created.append(
            (tenant_id, product_id, config.server_id, resume, transport)
        )
        return transport


def _server(
    *, endpoint: str = "https://mcp.example.test/rpc", resume_sessions: bool = True
) -> McpServerConfig:
    return McpServerConfig(
        server_id="knowledge",
        transport=McpTransportKind.STREAMABLE_HTTP,
        endpoint=endpoint,
        resume_sessions=resume_sessions,
    )


def _product(tmp_path: Path, product_id: str, server: McpServerConfig) -> ProductMcpConfig:
    workspace = tmp_path / product_id
    workspace.mkdir(exist_ok=True)
    return ProductMcpConfig(
        tenant_id="tenant-a",
        product_id=product_id,
        product_workspace=workspace,
        servers=(server,),
    )


def _scope(product_id: str = "product-a") -> RequestScope:
    return RequestScope(
        ScopePath.product("tenant-a", product_id),
        "user-a",
        channel_id="web",
    )


def test_resume_is_loaded_after_restart_and_orderly_close_preserves_remote_session(
    tmp_path: Path,
) -> None:
    async def exercise() -> tuple[MemoryResumeStore, ResumeFactory, ResumeFactory]:
        store = MemoryResumeStore()
        first_factory = ResumeFactory()
        first = McpClientManager(
            first_factory,
            client_capabilities=ClientCapabilities(),
            resume_store=store,
        )
        first.configure(_product(tmp_path, "product-a", _server()))
        (client,) = await first.connect_product("tenant-a", "product-a")
        await client.ping(_scope())
        await first.close()

        second_factory = ResumeFactory()
        second = McpClientManager(
            second_factory,
            client_capabilities=ClientCapabilities(),
            resume_store=store,
        )
        second.configure(_product(tmp_path, "product-a", _server()))
        await second.connect_product("tenant-a", "product-a")
        await second.close()
        return store, first_factory, second_factory

    store, first_factory, second_factory = asyncio.run(exercise())
    first_transport = first_factory.created[0][4]
    assert first_transport.resume_close
    assert not first_transport.normal_close
    assert second_factory.created[0][3] == HttpResumeState(
        session_id="remote-session", last_event_id="event-1"
    )
    assert store.loads == [
        ("tenant-a", "product-a", "knowledge", "https://mcp.example.test/rpc"),
        ("tenant-a", "product-a", "knowledge", "https://mcp.example.test/rpc"),
    ]
    # initialize, initialized notification, ping, then the restarted handshake.
    assert [item[1] for item in store.saves] == list(range(len(store.saves)))


def test_resume_scope_includes_tenant_product_server_and_endpoint(tmp_path: Path) -> None:
    async def exercise() -> tuple[MemoryResumeStore, ResumeFactory]:
        store = MemoryResumeStore()
        factory = ResumeFactory()
        manager = McpClientManager(
            factory,
            client_capabilities=ClientCapabilities(),
            resume_store=store,
        )
        manager.configure(
            _product(
                tmp_path,
                "product-a",
                _server(endpoint="https://a.example.test/rpc"),
            )
        )
        manager.configure(
            _product(
                tmp_path,
                "product-b",
                _server(endpoint="https://b.example.test/rpc"),
            )
        )
        await manager.connect_product("tenant-a", "product-a")
        await manager.connect_product("tenant-a", "product-b")
        await manager.close()
        return store, factory

    store, factory = asyncio.run(exercise())
    assert set(store.loads) == {
        ("tenant-a", "product-a", "knowledge", "https://a.example.test/rpc"),
        ("tenant-a", "product-b", "knowledge", "https://b.example.test/rpc"),
    }
    assert all(created[3] is None for created in factory.created)


def test_resume_disabled_skips_store_and_terminates_remote_session(tmp_path: Path) -> None:
    async def exercise() -> tuple[ResumeFactory, MemoryResumeStore]:
        factory = ResumeFactory()
        store = MemoryResumeStore()
        manager = McpClientManager(
            factory,
            client_capabilities=ClientCapabilities(),
            resume_store=store,
        )
        manager.configure(
            _product(tmp_path, "product-a", _server(resume_sessions=False))
        )
        await manager.connect_product("tenant-a", "product-a")
        await manager.close()
        return factory, store

    factory, store = asyncio.run(exercise())
    transport = factory.created[0][4]
    assert transport.normal_close
    assert not transport.resume_close
    assert store.loads == []
    assert store.saves == []


def test_exception_path_persists_advanced_cursor(tmp_path: Path) -> None:
    async def exercise() -> MemoryResumeStore:
        store = MemoryResumeStore()
        manager = McpClientManager(
            ResumeFactory(fail_ping=True),
            client_capabilities=ClientCapabilities(),
            resume_store=store,
        )
        manager.configure(_product(tmp_path, "product-a", _server()))
        (client,) = await manager.connect_product("tenant-a", "product-a")
        with pytest.raises(RuntimeError, match="advancing cursor"):
            await client.ping(_scope())
        await manager.close()
        return store

    store = asyncio.run(exercise())
    assert store.saves[-1][2].last_event_id == "event-1"


def test_persistence_failure_disables_connection_before_next_remote_call(
    tmp_path: Path,
) -> None:
    async def exercise() -> tuple[ResumableTransport, MemoryResumeStore]:
        store = MemoryResumeStore()
        # The connect handshake performs two durable commits.
        store.fail_save_after = 2
        factory = ResumeFactory()
        manager = McpClientManager(
            factory,
            client_capabilities=ClientCapabilities(),
            resume_store=store,
        )
        manager.configure(_product(tmp_path, "product-a", _server()))
        (client,) = await manager.connect_product("tenant-a", "product-a")
        transport = factory.created[0][4]
        with pytest.raises(McpTransportError, match="connection disabled"):
            await client.ping(_scope())
        assert client.health.state is McpConnectionState.DEGRADED
        request_count = len(transport.requests)
        with pytest.raises(McpTransportError, match="previously failed"):
            await client.ping(_scope())
        assert len(transport.requests) == request_count
        await manager.close()
        return transport, store

    transport, store = asyncio.run(exercise())
    assert transport.resume_close
    assert len(store.saves) == 3


def test_load_failure_and_ignored_checkpoint_fail_before_remote_io(tmp_path: Path) -> None:
    async def exercise() -> None:
        unavailable = MemoryResumeStore()
        unavailable.fail_load = True
        factory = ResumeFactory()
        manager = McpClientManager(
            factory,
            client_capabilities=ClientCapabilities(),
            resume_store=unavailable,
        )
        manager.configure(_product(tmp_path, "product-a", _server()))
        with pytest.raises(McpTransportError, match="failed to load"):
            await manager.connect_product("tenant-a", "product-a")
        assert factory.created == []

        store = MemoryResumeStore()
        key = (
            "tenant-a",
            "product-b",
            "knowledge",
            "https://mcp.example.test/rpc",
        )
        store.records[key] = McpHttpResumeCheckpoint(
            state=HttpResumeState(session_id="existing-session"),
            revision=7,
            updated_at=datetime.now(UTC),
        )
        ignoring_factory = ResumeFactory(ignore_resume=True)
        ignoring = McpClientManager(
            ignoring_factory,
            client_capabilities=ClientCapabilities(),
            resume_store=store,
        )
        ignoring.configure(_product(tmp_path, "product-b", _server()))
        with pytest.raises(McpTransportError, match="did not accept"):
            await ignoring.connect_product("tenant-a", "product-b")
        assert ignoring_factory.created[0][4].requests == []

    asyncio.run(exercise())


def test_concurrent_calls_are_serialized_with_distinct_cas_commands(tmp_path: Path) -> None:
    async def exercise() -> tuple[ResumableTransport, list[str], list[int]]:
        store = MemoryResumeStore()
        factory = ResumeFactory()
        manager = McpClientManager(
            factory,
            client_capabilities=ClientCapabilities(),
            resume_store=store,
        )
        manager.configure(_product(tmp_path, "product-a", _server()))
        (client,) = await manager.connect_product("tenant-a", "product-a")
        await asyncio.gather(client.ping(_scope()), client.ping(_scope()))
        await manager.close()
        transport = factory.created[0][4]
        return (
            transport,
            [item[3] for item in store.saves],
            [item[1] for item in store.saves],
        )

    transport, idempotency_keys, revisions = asyncio.run(exercise())
    assert transport.max_active_requests == 1
    assert len(idempotency_keys) == len(set(idempotency_keys))
    assert revisions == list(range(len(revisions)))


def test_two_runtime_owners_racing_same_checkpoint_fail_closed(tmp_path: Path) -> None:
    async def exercise() -> tuple[list[object], list[ResumableTransport]]:
        store = MemoryResumeStore()
        factories = [ResumeFactory(), ResumeFactory()]
        managers = [
            McpClientManager(
                factory,
                client_capabilities=ClientCapabilities(),
                resume_store=store,
            )
            for factory in factories
        ]
        for manager in managers:
            manager.configure(_product(tmp_path, "product-a", _server()))
        outcomes = await asyncio.gather(
            *(manager.connect_product("tenant-a", "product-a") for manager in managers),
            return_exceptions=True,
        )
        await asyncio.gather(*(manager.close() for manager in managers))
        return outcomes, [factory.created[0][4] for factory in factories]

    outcomes, transports = asyncio.run(exercise())
    assert sum(isinstance(item, tuple) for item in outcomes) == 1
    failures = [item for item in outcomes if isinstance(item, BaseException)]
    assert len(failures) == 1
    assert isinstance(failures[0], McpTransportError)
    assert "connection disabled" in str(failures[0])
    assert all(transport.resume_close for transport in transports)
