# tests/test_realtime_bridge_session_id_event.py
import asyncio
import json

import pytest

from app import realtime_bridge


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.client = None

    async def accept(self):
        pass

    async def send_text(self, text):
        self.sent.append(text)

    async def receive_text(self):
        # Block forever so _client_loop never returns on its own in this test;
        # the test only drives run_voice_bridge far enough to observe the
        # session_id frame, then cancels.
        await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_sends_session_id_event_before_openai_connect(monkeypatch):
    fake_ws = FakeWebSocket()

    class _FailingConnect:
        async def __aenter__(self):
            raise RuntimeError("stop after session id is sent")

        async def __aexit__(self, *args):
            pass

    def fake_connect(*args, **kwargs):
        return _FailingConnect()

    monkeypatch.setattr(realtime_bridge.websockets, "connect", fake_connect)

    # run_voice_bridge catches connection errors internally (logs + ends the
    # session) rather than propagating them, so we drive it to completion and
    # assert on the observable side effect: the session_id frame was sent
    # before the (failing) OpenAI connect attempt.
    await realtime_bridge.run_voice_bridge(fake_ws, index=None)

    assert len(fake_ws.sent) == 1
    payload = json.loads(fake_ws.sent[0])
    assert payload["type"] == "voicebot.session_id"
    assert isinstance(payload["session_id"], str) and payload["session_id"]
