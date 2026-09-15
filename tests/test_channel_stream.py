import asyncio
import pytest
from contextlib import aclosing
from datetime import UTC, datetime
from dataclasses import replace

from suiteharness.agents.react import ReActWorkflow
from suiteharness.channels import InboundMessage, ChannelKind, OutboundEventKind
from suiteharness.execution.in_memory import (
    InMemoryApprovalStore, InMemoryAuditJournal, InMemoryCapabilityAuthority, InMemoryToolRegistry,
)
from suiteharness.execution.protocols import ExecutionStrategies
from suiteharness.execution.runner import ExecutionRunner
from suiteharness.execution.events import RunEvent
from suiteharness.execution.models import RunResult, RunStatus
from suiteharness.runtime import RequestScope, ScopePath
from suiteharness.sessions import InMemorySessionStore
from suiteharness.server.application import ChannelApplication
from suiteharness.server.access import ConfiguredGrantIssuer, ToolAccessTemplate
from suiteharness.models import ModelStreamEvent, ModelStreamEventType, FinishReason
from suiteharness.sessions import SessionIdentity, SessionStatus, TranscriptEventType
from suiteharness.memory.session_memory import SessionConversationMemory


def channel_fixture(gateway):
    authority = InMemoryCapabilityAuthority()
    registry = InMemoryToolRegistry()
    runner = ExecutionRunner(tools=registry, capabilities=authority,
        approvals=InMemoryApprovalStore(), journal=InMemoryAuditJournal())
    calls = []
    class Activation:
        tenant_id = 'company'
        def stream(self, request):
            calls.append(request.run_id)
            return runner.stream(request, ExecutionStrategies(workflow=ReActWorkflow(gateway, 'chat')))
    sessions = InMemorySessionStore()
    app = ChannelApplication(activation=Activation(), sessions=sessions,
        grants=ConfiguredGrantIssuer(registry=registry, authority=authority,
            templates=[ToolAccessTemplate(channel_id='web', product_id='product')]))
    scope = RequestScope(path=ScopePath.agent('company', 'product', 'assistant', 'session'),
        principal_id='seller', channel_id='web', request_id='request1', correlation_id='corr1')
    message = InboundMessage(channel=ChannelKind.WEB, event_id='event1', message_id='message1',
        conversation_id='conversation1', sender_external_id='seller', text='hello',
        product_id='product', received_at=datetime.now(UTC))
    identity = SessionIdentity(tenant_id='company', product_id='product', agent_id='assistant',
        session_id='session', principal_id='seller')
    return app, sessions, scope, message, identity, calls


@pytest.mark.parametrize('mode', ['close_started', 'close_delta', 'cancel_task'])
def test_channel_cancellation_is_durable_without_reexecuting(mode):
    async def check():
        gateway = PausedGateway()
        app, sessions, scope, message, identity, calls = channel_fixture(gateway)
        source = app.handle(message, scope)
        assert (await anext(source)).kind is OutboundEventKind.STARTED
        if mode != 'close_started':
            assert (await anext(source)).kind is OutboundEventKind.DELTA
        if mode == 'cancel_task':
            pending = asyncio.create_task(anext(source))
            await asyncio.sleep(0)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            await source.aclose()
        assert (await sessions.get(identity)).status is SessionStatus.CANCELLED
        transcript = await sessions.recent_transcript(identity, limit=10)
        assert transcript[-1].type is TranscriptEventType.CANCEL
        replay = [event async for event in app.handle(message, scope)]
        assert len(calls) == (0 if mode == 'close_started' else 1)
        assert replay[-1].payload['run_status'] == 'cancelled'
        if mode != 'close_started':
            assert replay[-1].payload['partial_output'] == '你'
            assert replay[-1].payload['incomplete'] is True
    asyncio.run(check())


def test_channel_failure_preserves_partial_without_successful_history():
    class BrokenGateway:
        async def stream(self, route, request):
            yield ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta='未完成')
            raise RuntimeError('transport lost')
    async def check():
        app, sessions, scope, message, identity, calls = channel_fixture(BrokenGateway())
        events = [event async for event in app.handle(message, scope)]
        assert events[-1].kind is OutboundEventKind.FAILED
        assert events[-1].payload['partial_output'] == '未完成'
        assert events[-1].payload['incomplete'] is True
        transcript = await sessions.recent_transcript(identity, limit=10)
        assert all(event.type is not TranscriptEventType.ASSISTANT_MESSAGE for event in transcript)
        replay = [event async for event in app.handle(message, scope)]
        assert replay[-1].payload['partial_output'] == '未完成'
        assert len(calls) == 1
    asyncio.run(check())


@pytest.mark.parametrize('hosted', [False, True])
@pytest.mark.parametrize('authenticated', [False, True])
def test_gateway_close_propagates_to_application_immediately(hosted, authenticated):
    from suiteharness.channels.gateway import EnterpriseChannelGateway, ProductRouter, PrincipalAuthenticator
    from suiteharness.channels import AuthenticatedPrincipal, OutboundEvent
    from suiteharness.server.host import _HostedEnterpriseChannelGateway
    principal = AuthenticatedPrincipal(tenant_id='company', principal_id='seller')
    class Auth(PrincipalAuthenticator):
        async def authenticate(self, message):
            return principal
    class App:
        closed = False
        async def handle(self, message, scope):
            try:
                yield OutboundEvent(kind=OutboundEventKind.DELTA, request_id=scope.request_id,
                    correlation_id=scope.correlation_id, payload={'text': 'partial'})
            finally:
                self.closed = True
    async def check():
        app = App()
        cls = _HostedEnterpriseChannelGateway if hosted else EnterpriseChannelGateway
        gateway = cls(tenant_id='company', authenticator=Auth(), router=ProductRouter(('product',)),
            application=app, **({'host_closed': lambda: False} if hosted else {}))
        message = InboundMessage(channel=ChannelKind.WEB, event_id='event1', message_id='message1',
            conversation_id='conversation1', sender_external_id='seller', text='hello',
            product_id='product', received_at=datetime.now(UTC))
        source = gateway.dispatch_authenticated(message, principal) if authenticated else gateway.dispatch(message)
        await anext(source)
        await source.aclose()
        assert app.closed
    asyncio.run(check())


@pytest.mark.parametrize('channel', ['web', 'feishu'])
def test_delivery_error_closes_dispatch_iterator(channel):
    from suiteharness.channels import AuthenticatedPrincipal, OutboundEvent
    class Dispatcher:
        closed = False
        async def dispatch(self, message):
            try:
                yield OutboundEvent(kind=OutboundEventKind.DELTA, request_id='req1',
                    correlation_id='corr1', payload={'text': 'partial'})
            finally:
                self.closed = True
        def dispatch_authenticated(self, message, principal):
            return self.dispatch(message)
    async def fail(*args):
        raise ConnectionError('delivery lost')
    async def check():
        dispatcher = Dispatcher()
        if channel == 'web':
            from suiteharness.channels.websocket.server import WebSocketSession
            from suiteharness.channels.websocket.models import MessageFrame
            class Connection:
                send_text = staticmethod(fail)
            session = WebSocketSession(Connection(), connection_id='connection1',
                principal=AuthenticatedPrincipal(tenant_id='company', principal_id='seller'),
                handshake=None, authenticator=None, revalidator=None, dispatcher=dispatcher,
                approvals=None, approval_hub=None)
            with pytest.raises(ConnectionError):
                await session._dispatch(MessageFrame(type='message', client_message_id='msg1', conversation_id='chat1', text='hello'))
        else:
            from suiteharness.channels.feishu.processor import FeishuEventProcessor
            processor = FeishuEventProcessor(dispatcher, bot_open_id=None, outbound_sink=fail)
            with pytest.raises(ConnectionError):
                await processor.process_payload({'header': {'event_type': 'im.message.receive_v1', 'event_id': 'event1'},
                    'event': {'sender': {'sender_id': {'open_id': 'seller'}}, 'message': {
                        'message_type': 'text', 'message_id': 'msg1', 'chat_id': 'chat1',
                        'chat_type': 'p2p', 'content': '{"text":"hello"}'}}})
        assert dispatcher.closed
    asyncio.run(check())


class PausedGateway:
    def __init__(self):
        self.release = asyncio.Event()
        self.finished = False

    async def stream(self, route_id, request):
        yield ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta='你')
        await self.release.wait()
        yield ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta='好')
        yield ModelStreamEvent(type=ModelStreamEventType.MESSAGE_END, finish_reason=FinishReason.STOP)
        self.finished = True


def test_channel_delivers_live_text_then_persists_and_replays_without_running_again():
    async def check():
        authority = InMemoryCapabilityAuthority()
        registry = InMemoryToolRegistry()
        runner = ExecutionRunner(tools=registry, capabilities=authority,
                                 approvals=InMemoryApprovalStore(), journal=InMemoryAuditJournal())
        gateway = PausedGateway()
        calls = []

        class Activation:
            tenant_id = 'company'
            def stream(self, request):
                calls.append(request.run_id)
                return runner.stream(request, ExecutionStrategies(workflow=ReActWorkflow(gateway, 'chat')))

        app = ChannelApplication(activation=Activation(), sessions=InMemorySessionStore(),
                                 grants=ConfiguredGrantIssuer(registry=registry, authority=authority,
                                     templates=[ToolAccessTemplate(channel_id='web', product_id='product')]))
        scope = RequestScope(path=ScopePath.agent('company', 'product', 'assistant', 'session'),
                             principal_id='seller', channel_id='web', request_id='request1', correlation_id='corr1')
        message = InboundMessage(channel=ChannelKind.WEB, event_id='event1', message_id='message1',
                                 conversation_id='conversation1', sender_external_id='seller',
                                 text='hello', product_id='product', received_at=datetime.now(UTC))
        async with aclosing(app.handle(message, scope)) as source:
            assert (await anext(source)).kind is OutboundEventKind.STARTED
            first = await asyncio.wait_for(anext(source), 1)
            assert first.kind is OutboundEventKind.DELTA
            assert first.payload['text'] == '你'
            assert not gateway.finished
            gateway.release.set()
            rest = [event async for event in source]
        assert rest[-1].kind is OutboundEventKind.COMPLETED
        assert rest[-1].payload['output'] == '你好'
        replay = [event async for event in app.handle(message, scope)]
        assert len(calls) == 1
        assert replay[-1].payload['output'] == '你好'
        assert replay[-1].payload['replayed'] is True
    asyncio.run(check())


def test_channel_prepares_memory_from_durable_transcript_and_checkpoints_summary():
    async def check():
        authority = InMemoryCapabilityAuthority()
        registry = InMemoryToolRegistry()
        sessions = InMemorySessionStore()
        conversations = []

        async def summarize(previous, messages):
            return (previous + "|" + ",".join(str(item.content) for item in messages)).strip("|")

        class Resolver:
            def __init__(self):
                self.memory = SessionConversationMemory(short_term_pairs=1, summary_callback=summarize)

            def resolve(self, scope):
                del scope
                return self.memory

        class Activation:
            tenant_id = "company"

            async def _stream(self, request):
                conversations.append(request.conversation)
                now = datetime.now(UTC)
                yield RunEvent(
                    run_id=request.run_id,
                    sequence=1,
                    kind="completed",
                    result=RunResult(
                        run_id=request.run_id,
                        status=RunStatus.SUCCEEDED,
                        output=f"answer-{len(conversations)}",
                        iterations_used=1,
                        tool_calls_used=0,
                        started_at=now,
                        finished_at=now,
                    ),
                )

            def stream(self, request):
                return self._stream(request)

        app = ChannelApplication(
            activation=Activation(),
            sessions=sessions,
            grants=ConfiguredGrantIssuer(
                registry=registry,
                authority=authority,
                templates=[ToolAccessTemplate(channel_id="web", product_id="product")],
            ),
            memory_resolver=Resolver(),
        )
        scope = RequestScope(path=ScopePath.agent("company", "product", "assistant", "session"),
                             principal_id="seller", channel_id="web",
                             request_id="request1", correlation_id="corr1")

        for index in range(3):
            message = InboundMessage(channel=ChannelKind.WEB, event_id=f"event{index}",
                                     message_id=f"message{index}", conversation_id="conversation1",
                                     sender_external_id="seller", text=f"question-{index}",
                                     product_id="product", received_at=datetime.now(UTC))
            events = [item async for item in app.handle(
                message,
                replace(scope, request_id=f"request{index}", correlation_id=f"corr{index}"),
            )]
            assert events[-1].kind is OutboundEventKind.COMPLETED

        assert conversations[0].messages == ()
        assert [item.content for item in conversations[1].messages] == ["question-0", "answer-1"]
        assert [item.content for item in conversations[2].messages] == ["question-1", "answer-2"]
        assert "question-0,answer-1" in conversations[2].working_summary
        identity = SessionIdentity(tenant_id="company", product_id="product",
                                   agent_id="assistant", session_id="session", principal_id="seller")
        snapshot = await sessions.resume(identity)
        assert snapshot is not None and snapshot.checkpoint is not None
        memory = snapshot.checkpoint.workflow_state["memory"]
        assert memory["covered_revision"] == 3
        assert "question-0,answer-1" in memory["summary"]

    asyncio.run(check())
