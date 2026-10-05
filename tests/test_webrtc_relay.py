import asyncio
import importlib.util
import json
import unittest
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "custom_components" / "eufy_nvr" / "webrtc.py"
SPEC = importlib.util.spec_from_file_location("eufy_nvr_webrtc", MODULE_PATH)
webrtc = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(webrtc)


@dataclass
class IceServer:
    urls: object
    username: str | None = None
    credential: str | None = None


class Message:
    def __init__(self, data):
        self.data = data


class FakeWebSocket:
    """Minimal aiohttp ClientWebSocketResponse stand-in."""

    def __init__(self):
        self.sent = []
        self.closed = False
        self.incoming = asyncio.Queue()

    async def send_str(self, data):
        if self.closed:
            raise ConnectionResetError
        self.sent.append(json.loads(data))

    def push(self, payload):
        self.incoming.put_nowait(Message(json.dumps(payload)))

    def end(self):
        self.incoming.put_nowait(None)

    async def close(self):
        if not self.closed:
            self.closed = True
            self.incoming.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        return message


class MessageFormatTest(unittest.TestCase):
    def test_ice_servers_use_url_lists_and_skip_empty_fields(self):
        payload = webrtc.ice_servers_payload(
            [
                IceServer("stun:stun.example.org:3478"),
                IceServer(["turn:turn.example.org"], "user", "secret"),
                IceServer([]),
            ]
        )
        self.assertEqual(
            payload,
            [
                {"urls": ["stun:stun.example.org:3478"]},
                {
                    "urls": ["turn:turn.example.org"],
                    "username": "user",
                    "credential": "secret",
                },
            ],
        )

    def test_offer_matches_home_assistant_go2rtc_client_shape(self):
        message = json.loads(webrtc.offer_message("v=0", [{"urls": ["stun:x"]}]))
        self.assertEqual(
            message,
            {
                "type": "webrtc",
                "value": {
                    "type": "offer",
                    "sdp": "v=0",
                    "ice_servers": [{"urls": ["stun:x"]}],
                },
            },
        )

    def test_parses_both_answer_shapes_candidates_and_errors(self):
        parse = webrtc.parse_message
        self.assertEqual(
            parse(json.dumps({"type": "webrtc", "value": {"type": "answer", "sdp": "a"}})),
            ("answer", "a"),
        )
        self.assertEqual(
            parse(json.dumps({"type": "webrtc/answer", "value": "b"})), ("answer", "b")
        )
        self.assertEqual(
            parse(json.dumps({"type": "webrtc/candidate", "value": "candidate:1"})),
            ("candidate", "candidate:1"),
        )
        self.assertEqual(
            parse(json.dumps({"type": "error", "value": "stream not found"})),
            ("error", "stream not found"),
        )
        for junk in (None, b"bytes", "not json", "[]", json.dumps({"type": "other"})):
            with self.subTest(junk=junk):
                self.assertIsNone(parse(junk))


class SessionTest(unittest.IsolatedAsyncioTestCase):
    def make_session(self, connect):
        self.events = []
        self.closed_calls = 0

        def on_closed():
            self.closed_calls += 1

        return webrtc.WebRtcSession(
            connect,
            on_answer=lambda value: self.events.append(("answer", value)),
            on_candidate=lambda value: self.events.append(("candidate", value)),
            on_error=lambda value: self.events.append(("error", value)),
            on_closed=on_closed,
        )

    async def test_relays_offer_answer_and_candidates_then_releases(self):
        ws = FakeWebSocket()

        async def connect():
            return ws

        session = self.make_session(connect)
        # The frontend may trickle a candidate before the handshake finishes.
        early = asyncio.create_task(session.async_send_candidate("candidate:early"))
        await session.async_start("v=0 offer", [])
        await early
        self.assertEqual(ws.sent[0]["value"]["sdp"], "v=0 offer")
        self.assertEqual(ws.sent[1], {"type": "webrtc/candidate", "value": "candidate:early"})

        ws.push({"type": "webrtc", "value": {"type": "answer", "sdp": "v=0 answer"}})
        ws.push({"type": "webrtc/candidate", "value": "candidate:remote"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(
            self.events,
            [("answer", "v=0 answer"), ("candidate", "candidate:remote")],
        )

        await session.async_close()
        self.assertTrue(ws.closed, "closing the viewer must close the go2rtc consumer")
        self.assertEqual(self.closed_calls, 1)
        await session.async_send_candidate("candidate:late")
        self.assertEqual(len(ws.sent), 2)

    async def test_connect_failure_reports_error_without_details(self):
        async def connect():
            raise OSError("http://user:secret@host refused")

        session = self.make_session(connect)
        await session.async_start("v=0", [])
        self.assertEqual(len(self.events), 1)
        kind, message = self.events[0]
        self.assertEqual(kind, "error")
        self.assertNotIn("secret", message)
        self.assertTrue(session.closed)
        self.assertEqual(self.closed_calls, 1)

    async def test_go2rtc_error_or_early_hangup_is_reported_and_cleaned_up(self):
        ws = FakeWebSocket()

        async def connect():
            return ws

        session = self.make_session(connect)
        await session.async_start("v=0", [])
        ws.end()
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(self.events, [("error", "go2rtc closed the session before answering")])
        self.assertTrue(session.closed)
        self.assertEqual(self.closed_calls, 1)

    async def test_viewer_leaving_during_handshake_closes_socket(self):
        ws = FakeWebSocket()
        gate = asyncio.Event()

        async def connect():
            await gate.wait()
            return ws

        session = self.make_session(connect)
        start = asyncio.create_task(session.async_start("v=0", []))
        await asyncio.sleep(0)
        await session.async_close()
        gate.set()
        await start
        self.assertTrue(ws.closed)
        self.assertEqual(ws.sent, [])


if __name__ == "__main__":
    unittest.main()
