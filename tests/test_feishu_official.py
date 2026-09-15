"""Framework-only tests: no suite-products import, SDK connection or HTTP call."""
import asyncio
import importlib
import json
import threading
from datetime import UTC, datetime

from suiteharness.channels import ChannelKind, InboundMessage, OutboundEvent, OutboundEventKind
from suiteharness.channels.feishu import FeishuEventOutcome


def official():
    return importlib.import_module('suiteharness.channels.feishu.official')


def completed():
    return OutboundEvent(kind=OutboundEventKind.COMPLETED, request_id='request',
                         correlation_id='correlation', payload={'output': 'answer'})


def message(**metadata):
    return InboundMessage(channel=ChannelKind.FEISHU, event_id='event', message_id='message',
        conversation_id='chat', sender_external_id='employee', text='question',
        received_at=datetime.now(UTC), metadata=metadata)


def test_formatter_suppresses_mentions_and_provider_errors():
    formatter = official().SafeFeishuTextFormatter(max_chars=256)
    event = completed().model_copy(update={'payload': {'output': '<at user_id="all">all</at>\x00' + 'x' * 500}})
    text = formatter.format(event)
    assert '<at' not in text and '\x00' not in text and len(text) <= 256
    failure = OutboundEvent(kind=OutboundEventKind.FAILED, request_id='request',
        correlation_id='correlation', payload={'failure': {'message': 'provider-secret'}})
    assert 'provider-secret' not in formatter.format(failure)
    assert 'request' in formatter.format(failure)


def test_sink_coalesces_concurrent_retries_and_keeps_stable_provider_uuid():
    class Client:
        def __init__(self):
            self.calls = []

        async def send_text(self, receive_id, text, **options):
            self.calls.append((receive_id, text, options))
            await asyncio.sleep(0)
            if len(self.calls) == 1:
                raise RuntimeError('uncertain provider outcome')

    async def check():
        client = Client()
        sink = official().FinalReplySink(client)
        results = await asyncio.gather(sink(message(), completed()), sink(message(), completed()),
                                       return_exceptions=True)
        assert all(isinstance(value, RuntimeError) for value in results)
        assert len(client.calls) == 1
        await asyncio.gather(sink(message(), completed()), sink(message(), completed()))
        await sink(message(), completed())
        assert len(client.calls) == 2
        assert client.calls[0] == client.calls[1]
        assert client.calls[0][0:2] == ('chat', 'answer')
        assert len(client.calls[0][2]['idempotency_key']) <= 50
    asyncio.run(check())


def test_sink_uses_thread_reply_only_for_trusted_thread_metadata():
    class Client:
        def __init__(self):
            self.sent = []

        async def send_text(self, *args, **kwargs):
            self.sent.append(('chat', args, kwargs))

        async def reply_text(self, *args, **kwargs):
            self.sent.append(('thread', args, kwargs))

    async def check():
        client = Client()
        await official().FinalReplySink(client, reply_in_threads=True)(message(chat_type='group'), completed())
        assert client.sent[0][0:2] == ('thread', ('message', 'answer'))
        assert client.sent[0][2]['reply_in_thread'] is True
    asyncio.run(check())


def test_sdk_ack_waits_for_framework_callback_and_closes_owned_resources_once():
    acknowledged = threading.Event()
    entered, release = asyncio.Event(), asyncio.Event()
    closed = []

    def factory(app_id, app_secret, on_event):
        class Client:
            def start(self):
                on_event({'event': {'message': 'safe-fixture'}})
                acknowledged.set()
        return Client()

    async def closer():
        closed.append(True)

    async def check():
        sdk = official().OfficialLarkLongConnectionSdk(app_id='app-id', app_secret='test-only-secret',
            client_factory=factory, owned_closers=(closer,))

        async def callback(body):
            assert json.loads(body)['event']['message'] == 'safe-fixture'
            entered.set()
            await release.wait()
            return FeishuEventOutcome.DISPATCHED

        running = asyncio.create_task(sdk.run(callback))
        await asyncio.wait_for(entered.wait(), 2)
        assert not acknowledged.is_set()
        release.set()
        await asyncio.wait_for(running, 2)
        assert acknowledged.is_set()
        await sdk.close()
        await sdk.close()
        assert closed == [True]
        assert not sdk.process_replacement_required
        assert 'test-only-secret' not in repr(sdk)
    asyncio.run(check())
