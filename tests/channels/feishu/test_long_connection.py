from __future__ import annotations

import asyncio
import json

from suiteharness.channels.feishu import (
    FeishuEventOutcome,
    FeishuEventProcessor,
    FeishuLongConnectionRunner,
)


class Dispatcher:
    def __init__(self) -> None:
        self.messages = []

    async def dispatch(self, message):  # type: ignore[no-untyped-def]
        self.messages.append(message)
        if False:
            yield message


class Sdk:
    def __init__(self, event: bytes) -> None:
        self.event = event
        self.outcomes = []
        self.closed = False

    async def run(self, callback):  # type: ignore[no-untyped-def]
        self.outcomes.append(await callback(self.event))

    async def close(self) -> None:
        self.closed = True


def test_long_connection_reuses_verified_event_processor() -> None:
    event = json.dumps(
        {
            "header": {
                "event_id": "event-sdk",
                "event_type": "im.message.receive_v1",
                "create_time": "1700000000000",
            },
            "event": {
                "sender": {"sender_type": "user", "sender_id": {"open_id": "ou-user"}},
                "message": {
                    "message_id": "om-sdk",
                    "chat_id": "oc-sdk",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": json.dumps({"text": "from sdk"}),
                },
            },
        }
    ).encode()

    async def exercise():
        dispatcher = Dispatcher()
        sdk = Sdk(event)
        runner = FeishuLongConnectionRunner(
            sdk, FeishuEventProcessor(dispatcher, bot_open_id="ou-bot")
        )
        await runner.run()
        await runner.close()
        return sdk, dispatcher

    sdk, dispatcher = asyncio.run(exercise())
    assert sdk.outcomes == [FeishuEventOutcome.DISPATCHED]
    assert sdk.closed
    assert dispatcher.messages[0].text == "from sdk"
