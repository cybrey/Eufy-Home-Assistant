"""Relay Home Assistant WebRTC viewers straight to the add-on's go2rtc.

Without a native WebRTC implementation Home Assistant offers the frontend HLS
as well, and its HLS worker keeps an RTSP consumer attached after the viewer
closes. Because the NVR serves one live session at a time, that idle consumer
blocks every other camera until it times out.

Here each viewer is one go2rtc WebSocket. go2rtc counts it as the stream's only
consumer, so closing the viewer releases the NVR session immediately.

This module only needs aiohttp-style WebSocket objects, so it is importable and
testable without Home Assistant.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Covers the WebSocket handshake only; go2rtc answers the offer once the Eufy
# producer is running, which the add-on bounds separately.
CONNECT_TIMEOUT = 10.0


def ice_servers_payload(servers: Iterable[Any]) -> list[dict[str, Any]]:
    """Convert ``webrtc_models.RTCIceServer`` objects to go2rtc's JSON shape.

    go2rtc only accepts ``urls`` as a list of strings.
    """
    payload: list[dict[str, Any]] = []
    for server in servers:
        urls = getattr(server, "urls", None)
        if isinstance(urls, str):
            urls = [urls]
        if not urls:
            continue
        entry: dict[str, Any] = {"urls": [str(url) for url in urls]}
        for key in ("username", "credential"):
            if value := getattr(server, key, None):
                entry[key] = value
        payload.append(entry)
    return payload


def offer_message(sdp: str, ice_servers: list[dict[str, Any]]) -> str:
    """Build the offer frame Home Assistant's own go2rtc client sends."""
    return json.dumps(
        {
            "type": "webrtc",
            "value": {"type": "offer", "sdp": sdp, "ice_servers": ice_servers},
        }
    )


def candidate_message(candidate: str) -> str:
    """Build a trickle ICE candidate frame."""
    return json.dumps({"type": "webrtc/candidate", "value": candidate})


def parse_message(data: Any) -> tuple[str, str] | None:
    """Return ``(kind, value)`` for a go2rtc frame; kind is answer/candidate/error."""
    if not isinstance(data, str):
        return None
    try:
        message = json.loads(data)
    except ValueError:
        return None
    if not isinstance(message, dict):
        return None
    kind = message.get("type")
    value = message.get("value")
    if (
        kind == "webrtc"
        and isinstance(value, dict)
        and value.get("type") == "answer"
        and isinstance(value.get("sdp"), str)
    ):
        return "answer", value["sdp"]
    if kind == "webrtc/answer" and isinstance(value, str):
        return "answer", value
    if kind == "webrtc/candidate" and isinstance(value, str):
        return "candidate", value
    if kind == "error":
        return "error", str(value)
    return None


class WebRtcSession:
    """One browser viewer relayed to go2rtc over its WebSocket API."""

    def __init__(
        self,
        connect: Callable[[], Awaitable[Any]],
        *,
        on_answer: Callable[[str], None],
        on_candidate: Callable[[str], None],
        on_error: Callable[[str], None],
        on_closed: Callable[[], None],
    ) -> None:
        self._connect = connect
        self._on_answer = on_answer
        self._on_candidate = on_candidate
        self._on_error = on_error
        self._on_closed = on_closed
        self._ws: Any = None
        self._ready = asyncio.Event()
        self._closed = False
        self._answered = False
        self._reader: asyncio.Task[None] | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    async def async_start(
        self, offer_sdp: str, ice_servers: list[dict[str, Any]]
    ) -> None:
        """Connect, send the offer and relay go2rtc's replies in the background."""
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                ws = await self._connect()
        except Exception as error:  # noqa: BLE001 - reported to the viewer
            # Never include the exception text: aiohttp may echo the URL.
            if getattr(error, "status", None) == 404:
                # Add-on builds before 0.7.23 did not allow /api/ws.
                self._on_error(
                    "the Eufy NVR Local Server add-on does not allow WebRTC "
                    "signaling; update it to 0.7.23 or later"
                )
            else:
                self._on_error(
                    f"cannot open the Eufy go2rtc WebSocket ({type(error).__name__})"
                )
            await self.async_close()
            return
        if self._closed:
            # The viewer left while the handshake was in flight.
            await ws.close()
            return
        self._ws = ws
        try:
            await ws.send_str(offer_message(offer_sdp, ice_servers))
        except Exception as error:  # noqa: BLE001 - reported to the viewer
            self._on_error(f"cannot send the WebRTC offer ({type(error).__name__})")
            await self.async_close()
            return
        self._ready.set()
        self._reader = asyncio.create_task(self._read())

    async def async_send_candidate(self, candidate: str) -> None:
        """Forward a browser candidate once the offer has been sent."""
        if not candidate or self._closed:
            return
        # The frontend may trickle candidates before the handshake completes.
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                await self._ready.wait()
        except TimeoutError:
            return
        if self._closed or self._ws is None or self._ws.closed:
            return
        try:
            await self._ws.send_str(candidate_message(candidate))
        except Exception as error:  # noqa: BLE001 - a lost candidate is not fatal
            _LOGGER.debug("Dropped WebRTC candidate (%s)", type(error).__name__)

    async def _read(self) -> None:
        try:
            async for message in self._ws:
                parsed = parse_message(getattr(message, "data", None))
                if parsed is None:
                    continue
                kind, value = parsed
                if kind == "answer":
                    self._answered = True
                    self._on_answer(value)
                elif kind == "candidate":
                    self._on_candidate(value)
                else:
                    self._on_error(value)
                    break
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - reported below
            _LOGGER.debug("go2rtc WebSocket failed (%s)", type(error).__name__)
        if not self._closed and not self._answered:
            self._on_error("go2rtc closed the session before answering")
        await self.async_close()

    async def async_close(self) -> None:
        """Close the WebSocket so go2rtc drops the consumer immediately."""
        if self._closed:
            return
        self._closed = True
        self._ready.set()
        reader = self._reader
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception as error:  # noqa: BLE001 - already shutting down
                _LOGGER.debug("go2rtc WebSocket close failed (%s)", type(error).__name__)
        self._on_closed()
