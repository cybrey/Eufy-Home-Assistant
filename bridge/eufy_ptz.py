#!/usr/bin/env python3
"""Authenticated HTTP endpoint that steers a live PTZ camera.

PTZ commands only work inside the NVR's single live session, which belongs to the
eufy_stream.py process serving that camera. That process listens on a per-channel
Unix socket while it is live; this server maps a stream name to its channel and
forwards one request. If the camera is not live it answers 409 instead of opening a
session of its own, so it can never steal the NVR from someone watching.

POST /api/ptz  {"stream": "eufy_garage", "action": "move", "direction": "left"}
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hmac
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from aiohttp import web

import ptz_protocol as ptz

MAX_BODY = 4096
ENGINE_TIMEOUT = 6.0


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] ptz: {message}", file=sys.stderr, flush=True)


def authorized(header: str | None, username: str, password: str) -> bool:
    """Constant-time check of an HTTP Basic Authorization header."""
    if not header or not header.startswith("Basic "):
        return False
    try:
        supplied = base64.b64decode(header[6:], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    user, _, secret = supplied.partition(":")
    return hmac.compare_digest(user.encode(), username.encode()) & hmac.compare_digest(
        secret.encode(), password.encode()
    )


def load_streams(path: Path) -> dict[str, dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


async def forward(socket_path: Path, request: dict[str, Any]) -> dict[str, Any] | None:
    """Send one request to a live engine; None when no engine is listening."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(socket_path)), timeout=2
        )
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        writer.write((json.dumps(request) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=ENGINE_TIMEOUT)
    finally:
        writer.close()
    try:
        reply = json.loads(line) if line else None
    except ValueError:
        reply = None
    return reply if isinstance(reply, dict) else {"ok": False, "error": "no reply from camera session"}


def build_app(username: str, password: str, streams_path: Path,
              control_dir: Path | None = None) -> web.Application:
    async def handle(request: web.Request) -> web.Response:
        if not authorized(request.headers.get("Authorization"), username, password):
            return web.json_response(
                {"ok": False, "error": "unauthorized"}, status=401,
                headers={"WWW-Authenticate": 'Basic realm="eufy-ptz"'},
            )
        if request.content_length is not None and request.content_length > MAX_BODY:
            return web.json_response({"ok": False, "error": "request too large"}, status=413)
        try:
            body = json.loads(await request.content.read(MAX_BODY))
        except ValueError:
            return web.json_response({"ok": False, "error": "body must be JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "body must be an object"}, status=400)

        stream = body.pop("stream", None)
        info = load_streams(streams_path).get(stream) if isinstance(stream, str) else None
        if not isinstance(info, dict):
            return web.json_response({"ok": False, "error": "unknown stream"}, status=404)
        if not info.get("ptz"):
            return web.json_response(
                {"ok": False, "error": f"{stream} has no PTZ (use the PTZ lens stream)"},
                status=400,
            )
        try:
            # Validate here so a bad request never reaches the camera session.
            ptz.build_request("validation", int(info["channel"]), body)
        except (ValueError, KeyError, TypeError) as error:
            return web.json_response({"ok": False, "error": str(error)}, status=400)

        reply = await forward(ptz.control_socket_path(int(info["channel"]), control_dir), body)
        if reply is None:
            return web.json_response(
                {"ok": False, "error": f"{stream} is not live; open its live view first"},
                status=409,
            )
        log(f"{stream} {body.get('action')} -> ok={reply.get('ok')} acked={reply.get('acked')}")
        return web.json_response(reply, status=200 if reply.get("ok") else 502)

    app = web.Application(client_max_size=MAX_BODY)
    app.router.add_post("/api/ptz", handle)
    return app


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("EUFY_PTZ_PORT", "1986")))
    args = parser.parse_args()
    username = os.environ.get("GO2RTC_USERNAME", "")
    password = os.environ.get("GO2RTC_PASSWORD", "")
    if not username or len(password) < 16:
        log("GO2RTC_USERNAME/GO2RTC_PASSWORD missing; PTZ endpoint disabled")
        return 1
    manifest = Path(os.environ.get("EUFY_CAMERAS", "/data/cameras.json"))
    streams_path = manifest.with_name("eufy-streams.json")
    log(f"listening on :{args.port}")
    web.run_app(build_app(username, password, streams_path), port=args.port,
                print=None, access_log=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
