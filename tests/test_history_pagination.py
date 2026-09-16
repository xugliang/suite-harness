"""History pages must fit the wire and retain exclusive, authorized cursors."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from suiteharness.channels import AuthenticatedPrincipal
from suiteharness.channels.approvals import InteractiveApprovalCoordinator
from suiteharness.channels.gateway import EnterpriseChannelGateway, ProductRouter
from suiteharness.channels.websocket.models import WebSocketHandshake, parse_client_frame
from suiteharness.channels.websocket.server import WebSocketApprovalHub, WebSocketSession
from suiteharness.server.application import ChannelApplication
from suiteharness.sessions import InMemorySessionStore, SessionStore, TranscriptEventType


class Connection:
    def __init__(self):
        self.raw = []

    async def send_text(self, value):
        self.raw.append(value)


class Access:
    allowed = True

    async def authorize(self, principal, **selection):
        return self.allowed


async def setup(*, max_bytes=1_048_576, dispatcher=None, store=None):
    principal = AuthenticatedPrincipal(tenant_id="company", principal_id="seller")

    class Authenticator:
        async def authenticate(self, _):
            return principal

    store = store or InMemorySessionStore()
    app = ChannelApplication(
        activation=SimpleNamespace(tenant_id="company"), sessions=store, grants=None,
    )
    access = Access()
    gateway = EnterpriseChannelGateway(
        tenant_id="company", authenticator=Authenticator(), router=ProductRouter(("product",)),
        application=app, product_access_authorizer=access,
    )
    connection = Connection()
    hub = WebSocketApprovalHub()
    session = WebSocketSession(
        connection, connection_id="socket", principal=principal,
        handshake=WebSocketHandshake(origin="https://example.test"),
        authenticator=Authenticator(), revalidator=None, dispatcher=dispatcher or gateway,
        approvals=InteractiveApprovalCoordinator(hub.publish), approval_hub=hub,
        max_outbound_bytes=max_bytes,
    )
    message = session._control_message(
        control_id="history", conversation_id="conversation", product_id="product",
    )
    scope = await gateway._scope_for(message, principal)
    identity = app._session_identity(scope)
    await store.create(identity)
    return session, connection, store, app, scope, identity, access


async def append(store, identity, index, text="answer", event_type=TranscriptEventType.ASSISTANT_MESSAGE):
    return await store.append(
        identity, event_id=f"event-{index}", event_type=event_type,
        payload={"run_id": f"run-{index}", "output": text}, occurred_at=datetime.now(UTC),
    )


async def page(session, connection, *, limit=100, cursor=None):
    request = {"type": "history", "history_id": "history", "conversation_id": "conversation",
               "product_id": "product", "limit": limit}
    if cursor is not None:
        request["before_sequence"] = cursor
    await session._handle_frame(parse_client_frame(json.dumps(request)))
    return json.loads(connection.raw[-1])


def test_byte_bounded_history_does_not_drop_or_repeat_boundary_records():
    """An all-or-nothing frame or a cursor advanced past a dropped event loses history."""
    async def check():
        session, connection, store, _, _, identity, _ = await setup()
        for index in range(1, 101):
            await append(store, identity, index, "x" * 12000)
        seen = []
        cursor = None
        for _ in range(10):
            result = await page(session, connection, cursor=cursor)
            assert result["type"] == "history_result"
            assert len(connection.raw[-1].encode("utf-8")) <= 1_048_576
            seen.extend(item["run_id"] for item in result["events"])
            if not result["has_more"]:
                break
            assert result["next_cursor"] is not None
            cursor = result["next_cursor"]
        assert len(seen) == len(set(seen)) == 100
        assert set(seen) == {f"run-{index}" for index in range(1, 101)}
    asyncio.run(check())


def test_history_can_page_past_one_thousand_events_and_rechecks_acl_and_cutoff():
    """A recent_transcript-only implementation cannot reach the older visible rows."""
    async def check():
        session, connection, store, app, scope, identity, access = await setup()
        for index in range(1, 1106):
            await append(store, identity, index)
        seen = []
        cursor = None
        for _ in range(20):
            result = await page(session, connection, cursor=cursor)
            assert result["type"] == "history_result"
            seen.extend(item["run_id"] for item in result["events"])
            cursor = result["next_cursor"]
            if not result["has_more"]:
                break
        assert len(seen) == len(set(seen)) == 1105
        assert "run-1" in seen
        first = await page(session, connection, limit=10)
        access.allowed = False
        denied = await page(session, connection, cursor=first["next_cursor"])
        assert denied["type"] == "error"
        access.allowed = True
        await app.invalidate_memory(scope)
        invalidated = await page(session, connection, cursor=first["next_cursor"])
        assert invalidated["events"] == [] and invalidated["has_more"] is False
    asyncio.run(check())


def test_oversized_single_record_is_marked_and_unicode_json_still_fits():
    """A huge newest record must not stall pagination or exceed escaped UTF-8 bytes."""
    async def check():
        session, connection, store, _, _, identity, _ = await setup(max_bytes=4096)
        await append(store, identity, 1, "older")
        await append(store, identity, 2, {"nested": "中\"\\\n" * 12000})
        result = await page(session, connection)
        assert result["type"] == "history_result"
        assert len(connection.raw[-1].encode("utf-8")) <= 4096
        assert result["events"][-1]["run_id"] == "run-2"
        assert result["events"][-1]["truncated"] is True
        assert result["has_more"] is True
        older = await page(session, connection, cursor=result["next_cursor"])
        assert [item["run_id"] for item in older["events"]] == ["run-1"]
    asyncio.run(check())


def test_legacy_history_adapter_does_not_claim_cursor_support():
    class Legacy:
        async def history_authenticated(self, message, principal, limit):
            return ()

    async def check():
        session, connection, *_ = await setup(dispatcher=Legacy())
        result = await page(session, connection)
        assert result["type"] == "history_result"
        assert result["pagination_supported"] is False
        assert result["has_more"] is None and result["next_cursor"] is None
        result = await page(session, connection, cursor=100)
        assert result["type"] == "error"
    asyncio.run(check())


def test_legacy_store_does_not_advertise_pagination_it_cannot_serve():
    class LegacyStore(InMemorySessionStore):
        transcript_page = SessionStore.transcript_page

    async def check():
        session, connection, store, _, _, identity, _ = await setup(store=LegacyStore())
        await append(store, identity, 1)
        first = await page(session, connection)
        assert first["type"] == "history_result"
        assert [item["run_id"] for item in first["events"]] == ["run-1"]
        assert first["pagination_supported"] is False and first["has_more"] is None
        refused = await page(session, connection, cursor=2)
        assert refused["type"] == "error"
    asyncio.run(check())


def test_hidden_audit_pages_do_not_hide_older_visible_history():
    async def check():
        session, connection, store, _, _, identity, _ = await setup()
        await append(store, identity, 1, "oldest")
        for index in range(2, 1107):
            await append(store, identity, index, event_type=TranscriptEventType.TOOL_RESULT)
        await append(store, identity, 1107, "newest")
        first = await page(session, connection, limit=1)
        assert [item["run_id"] for item in first["events"]] == ["run-1107"]
        assert first["has_more"] is True
        second = await page(session, connection, limit=1, cursor=first["next_cursor"])
        assert [item["run_id"] for item in second["events"]] == ["run-1"]
        assert second["has_more"] is False
    asyncio.run(check())


def test_history_page_host_closed_guard_prevents_access():
    from suiteharness.server.host import _HostedEnterpriseChannelGateway

    async def check():
        session, _, _, app, _, _, _ = await setup()
        closed = True
        gateway = _HostedEnterpriseChannelGateway(
            tenant_id="company", authenticator=session._authenticator,
            router=ProductRouter(("product",)), application=app,
            host_closed=lambda: closed,
        )
        message = session._control_message(
            control_id="history", conversation_id="conversation", product_id="product",
        )
        with pytest.raises(RuntimeError, match="closed"):
            await gateway.history_page_authenticated(message, session._principal, 10)
    asyncio.run(check())


@pytest.mark.parametrize("cursor", [True, 0, -1, "10", 1.2])
def test_history_cursor_rejects_invalid_protocol_values(cursor):
    from suiteharness.channels.websocket.models import WebSocketProtocolError

    with pytest.raises(WebSocketProtocolError):
        parse_client_frame(json.dumps({
            "type": "history", "history_id": "h", "conversation_id": "conversation",
            "limit": 10, "before_sequence": cursor,
        }))
