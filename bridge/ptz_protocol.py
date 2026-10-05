"""PTZ command frames for the S4 PoE dual-lens cameras, plus the control-socket contract.

Taken from eufy's web client (security.eufy.com, command table ``Gt``). Every command
travels inside the NVR's open live session with the camera's channel in the header:

* move    1700/6030 {commandType, data:{cmd_type, rotate_type, zoom:1}}
          cmd_type 1 = one step, 4 = start continuous, 3 = stop
* zoom    1350/6203 {dstZoom}  (absolute zoom level)
* presets 1700/6034 {commandType}           -> reply payload.points[<=8]
* goto    1700/6035 {commandType, data:{value:index}}

For command_id 1700 the web client merges the payload into the top level of the JSON
body; for 1350 it nests it under "payload".

Pure and dependency-free so the engine, the control server and the tests share it.
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path
from typing import Any

CMD_GENERIC = 1350
CMD_PTZ = 1700
PTZ_MOVE = 6030
PTZ_PRESET_LIST = 6034
PTZ_PRESET_GOTO = 6035
PTZ_ZOOM = 6203

DIRECTIONS = {"left": 1, "right": 2, "up": 3, "down": 4}
MOVE_MODES = {"step": 1, "start": 4, "stop": 3}
PRESET_SLOTS = 8
# The web client's preset editor offers 1x and 3x. Widen only after the NVR's real
# range has been confirmed on hardware.
ZOOM_MIN = 1
ZOOM_MAX = 3

CONTROL_DIR = Path(
    os.environ.get(
        "EUFY_CONTROL_DIR",
        "/data" if Path("/data").is_dir() else Path(__file__).resolve().parent,
    )
)


def control_socket_path(channel: int, directory: Path | None = None) -> Path:
    """Unix socket a live engine listens on for its camera channel."""
    return (directory or CONTROL_DIR) / f"eufy-ctl-{int(channel)}.sock"


def frame(user_id: str, command_id: int, cmd: int, payload: dict[str, Any],
          channel: int) -> bytes:
    """Build one XZYH-framed command for ``channel``."""
    if command_id == CMD_PTZ:
        body_obj = {"account_id": user_id, "cmd": cmd, **payload}
    else:
        body_obj = {"account_id": user_id, "cmd": cmd, "payload": payload}
    body = json.dumps(body_obj, separators=(",", ":")).encode()
    header = bytearray(16)
    header[0:4] = b"XZYH"
    struct.pack_into("<H", header, 4, command_id)
    struct.pack_into("<I", header, 6, len(body))
    header[12] = channel & 0xFF
    header[15] = 2
    return bytes(header) + body


def build_request(user_id: str, channel: int, request: dict[str, Any],
                  current_zoom: int = ZOOM_MIN) -> tuple[int, bytes, int | None]:
    """Validate a control request and return (reply cmd, frame, new zoom or None).

    Request shapes:
      {"action": "move", "direction": "left|right|up|down", "mode": "step|start|stop"}
      {"action": "zoom", "zoom": N} or {"action": "zoom", "step": +1|-1}
      {"action": "preset", "preset": 1..8}   (1-based, like the Eufy app)
      {"action": "presets"}
    Raises ValueError for anything else.
    """
    if not isinstance(request, dict):
        raise ValueError("request must be an object")
    action = request.get("action")
    if action == "move":
        direction = DIRECTIONS.get(request.get("direction"))
        mode = MOVE_MODES.get(request.get("mode", "step"))
        if direction is None or mode is None:
            raise ValueError("move needs direction left/right/up/down and mode step/start/stop")
        payload = {"commandType": PTZ_MOVE,
                   "data": {"cmd_type": mode, "rotate_type": direction, "zoom": 1}}
        return PTZ_MOVE, frame(user_id, CMD_PTZ, PTZ_MOVE, payload, channel), None
    if action == "zoom":
        if "step" in request:
            step = request["step"]
            if step not in (1, -1):
                raise ValueError("zoom step must be 1 or -1")
            level = current_zoom + step
        else:
            level = request.get("zoom")
            if type(level) is not int:
                raise ValueError("zoom must be an integer")
        level = min(max(level, ZOOM_MIN), ZOOM_MAX)
        return PTZ_ZOOM, frame(user_id, CMD_GENERIC, PTZ_ZOOM, {"dstZoom": level}, channel), level
    if action == "preset":
        preset = request.get("preset")
        if type(preset) is not int or not 1 <= preset <= PRESET_SLOTS:
            raise ValueError(f"preset must be 1 to {PRESET_SLOTS}")
        payload = {"commandType": PTZ_PRESET_GOTO, "data": {"value": preset - 1}}
        return PTZ_PRESET_GOTO, frame(user_id, CMD_PTZ, PTZ_PRESET_GOTO, payload, channel), None
    if action == "presets":
        payload = {"commandType": PTZ_PRESET_LIST}
        return PTZ_PRESET_LIST, frame(user_id, CMD_PTZ, PTZ_PRESET_LIST, payload, channel), None
    raise ValueError("action must be move, zoom, preset or presets")


def reply_cmd(text: str) -> tuple[int | None, Any]:
    """Return (cmd, decoded JSON) for an NVR control reply, or (None, None)."""
    try:
        obj = json.loads(text)
    except ValueError:
        return None, None
    if not isinstance(obj, dict):
        return None, None
    for key in ("cmd", "commandType"):
        value = obj.get(key)
        if type(value) is int:
            return value, obj
    payload = obj.get("payload")
    if isinstance(payload, dict) and type(payload.get("commandType")) is int:
        return payload["commandType"], obj
    return None, obj
