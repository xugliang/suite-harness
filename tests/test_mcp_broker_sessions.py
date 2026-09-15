"""Persistent broker protocol tested through real ASGI request boundaries."""

import asyncio
import base64
import json
from dataclasses import replace

import httpx

from suiteharness.sandbox import SandboxAvailability, SandboxLimits, SandboxMount, SandboxRequest
from suiteharness.sandbox.broker import BrokerPolicy, create_app
from suiteharness.sandbox.remote import RemoteSandboxBackend, encode_request, parse_json


class EchoSession:
    def __init__(self):
        self.output = asyncio.Queue()
        self.finished = asyncio.Event()
        self.close_count = 0

    async def send_line(self, payload):
        await self.output.put(payload)

    async def receive_line(self):
        return await self.output.get()

    async def close(self):
        self.close_count += 1
        self.finished.set()

    async def wait_closed(self):
        await self.finished.wait()


class Backend:
    def __init__(self):
        self.sessions = []
        self.requests = []

    async def availability(self):
        return SandboxAvailability(True)

    async def open_session(self, request):
        self.requests.append(request)
        session = EchoSession()
        self.sessions.append(session)
        return session


def command(root):
    return SandboxRequest(
        "mcp-test",
        ("peer",),
        (SandboxMount(root, "/workspace", read_only=True),),
        limits=SandboxLimits(timeout_seconds=3600),
    )


def test_broker_session_remote_roundtrip_close_and_untrusted_mount_rejection(tmp_path):
    async def check():
        backend = Backend()
        app = create_app(
            BrokerPolicy({"/workspace/product": tmp_path}, SandboxLimits(), frozenset()), backend
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://broker"
            ) as client:

                class Remote(RemoteSandboxBackend):
                    async def _call(self, path, body, timeout, maximum):
                        response = await client.request(
                            "POST" if body is not None else "GET", path, content=body
                        )
                        response.raise_for_status()
                        assert len(response.content) <= maximum
                        return parse_json(response.content)

                remote = Remote(tmp_path / "broker.sock")
                session = await remote.open_session(command(tmp_path))
                await session.send_line(b"first")
                assert await session.receive_line() == b"first"
                await session.send_line(b"second")
                assert await session.receive_line() == b"second"
                await session.close()
                await session.close()
                await session.wait_closed()
                assert backend.sessions[0].close_count == 1
                assert backend.requests[0].mounts[0].read_only
                invalid = encode_request(command(tmp_path))
                invalid["mounts"][0]["read_only"] = False
                assert (await client.post("/session/open", json=invalid)).status_code == 400
                invalid["mounts"][0]["read_only"] = True
                invalid["mounts"][0]["host_path"] = str(tmp_path.parent)
                assert (await client.post("/session/open", json=invalid)).status_code == 400
                assert len(backend.sessions) == 1

    asyncio.run(check())


def test_broker_shutdown_closes_persistent_sessions_and_unknown_session_denies(tmp_path):
    async def check():
        backend = Backend()
        app = create_app(
            BrokerPolicy({"/workspace/product": tmp_path}, SandboxLimits(), frozenset()), backend
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://broker"
            ) as client:
                response = await client.post(
                    "/session/open", json=encode_request(command(tmp_path))
                )
                assert response.status_code == 200
                token = response.json()["session_id"]
                assert len(token) >= 32
                wrong = await client.post(
                    "/session/send",
                    json={"session_id": "not-owned", "payload": base64.b64encode(b"hi").decode()},
                )
                assert wrong.status_code == 404
        assert backend.sessions[0].finished.is_set()
        assert backend.sessions[0].close_count == 1

    asyncio.run(check())


def test_abandoned_broker_session_expires_and_frees_capacity(tmp_path, monkeypatch):
    from suiteharness.sandbox import session_broker

    monkeypatch.setattr(session_broker, "SESSION_IDLE_SECONDS", 0.03)

    async def check():
        backend = Backend()
        app = create_app(
            BrokerPolicy({"/workspace/product": tmp_path}, SandboxLimits(), frozenset()), backend
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://broker"
            ) as client:
                response = await client.post(
                    "/session/open", json=encode_request(command(tmp_path))
                )
                assert response.status_code == 200
                await asyncio.wait_for(backend.sessions[0].finished.wait(), 1)
                assert backend.sessions[0].close_count == 1
                result = await client.post(
                    "/session/send",
                    json={"session_id": response.json()["session_id"], "payload": "aGk="},
                )
                assert result.status_code == 404

    asyncio.run(check())


def test_receive_disconnect_closes_only_the_owned_session(tmp_path):
    async def check():
        backend = Backend()
        app = create_app(
            BrokerPolicy({"/workspace/product": tmp_path}, SandboxLimits(), frozenset()), backend
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://broker"
            ) as client:
                first = await client.post("/session/open", json=encode_request(command(tmp_path)))
                second = await client.post(
                    "/session/open",
                    json=encode_request(replace(command(tmp_path), request_id="other")),
                )
                assert first.status_code == second.status_code == 200
                sent = False

                async def receive():
                    nonlocal sent
                    if not sent:
                        sent = True
                        return {
                            "type": "http.request",
                            "body": json.dumps({"session_id": first.json()["session_id"]}).encode(),
                            "more_body": False,
                        }
                    return {"type": "http.disconnect"}

                messages = []

                async def send(value):
                    messages.append(value)

                await app(
                    {
                        "type": "http",
                        "asgi": {"version": "3.0"},
                        "http_version": "1.1",
                        "method": "POST",
                        "scheme": "http",
                        "path": "/session/receive",
                        "raw_path": b"/session/receive",
                        "query_string": b"",
                        "headers": [],
                        "client": None,
                        "server": None,
                    },
                    receive,
                    send,
                )
                assert messages[0]["status"] == 499
                assert backend.sessions[0].finished.is_set()
                assert not backend.sessions[1].finished.is_set()

    asyncio.run(check())
