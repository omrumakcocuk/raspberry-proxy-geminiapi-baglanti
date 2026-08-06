import asyncio

import pytest
from starlette.websockets import WebSocketDisconnect

from gemini_live_proxy.proxy import client_to_gemini, gemini_to_client


class FakeClient:
    def __init__(self, messages=None):
        self.messages = asyncio.Queue()
        for message in messages or []:
            self.messages.put_nowait(message)
        self.sent = []

    async def receive(self):
        return await self.messages.get()

    async def send_text(self, message):
        self.sent.append(("text", message))

    async def send_bytes(self, message):
        self.sent.append(("bytes", message))


class FakeUpstream:
    def __init__(self, incoming=None):
        self.incoming = list(incoming or [])
        self.sent = []

    async def send(self, message):
        self.sent.append(message)

    def __aiter__(self):
        self.iterator = iter(self.incoming)
        return self

    async def __anext__(self):
        try:
            return next(self.iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


@pytest.mark.asyncio
async def test_client_messages_are_forwarded_without_changes() -> None:
    client = FakeClient(
        [
            {"type": "websocket.receive", "text": '{"setup":{}}'},
            {"type": "websocket.receive", "bytes": b"binary"},
            {"type": "websocket.disconnect", "code": 1000},
        ]
    )
    upstream = FakeUpstream()

    with pytest.raises(WebSocketDisconnect):
        await client_to_gemini(client, upstream)

    assert upstream.sent == ['{"setup":{}}', b"binary"]


@pytest.mark.asyncio
async def test_gemini_messages_are_forwarded_without_changes() -> None:
    client = FakeClient()
    upstream = FakeUpstream(['{"setupComplete":{}}', b"audio"])

    await gemini_to_client(upstream, client)

    assert client.sent == [
        ("text", '{"setupComplete":{}}'),
        ("bytes", b"audio"),
    ]
