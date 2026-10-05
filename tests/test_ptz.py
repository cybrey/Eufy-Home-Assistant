"""PTZ: frame layout, the add-on control endpoint and which streams are steerable."""
import asyncio
import base64
import importlib.util
import json
import os
import struct
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
import ptz_protocol as ptz  # noqa: E402
import eufy_ptz  # noqa: E402

API_SPEC = importlib.util.spec_from_file_location(
    "eufy_nvr_go2rtc_api_ptz", ROOT / "custom_components/eufy_nvr/go2rtc_api.py"
)
api = importlib.util.module_from_spec(API_SPEC)
API_SPEC.loader.exec_module(api)

USER, PASSWORD = "eufy", "0123456789abcdef"


def decode(frame_bytes):
    assert frame_bytes[:4] == b"XZYH"
    command_id = struct.unpack_from("<H", frame_bytes, 4)[0]
    length = struct.unpack_from("<I", frame_bytes, 6)[0]
    body = frame_bytes[16:]
    assert len(body) == length
    return command_id, frame_bytes[12], json.loads(body)


def test_move_is_a_1700_frame_with_the_payload_merged_at_top_level():
    reply, frame_bytes, zoom = ptz.build_request("user", 2, {"action": "move", "direction": "left"})
    command_id, channel, body = decode(frame_bytes)
    assert (reply, command_id, channel, zoom) == (6030, 1700, 2, None)
    assert body == {"account_id": "user", "cmd": 6030, "commandType": 6030,
                    "data": {"cmd_type": 1, "rotate_type": 1, "zoom": 1}}


def test_move_modes_and_directions_follow_the_web_client():
    for direction, rotate in (("left", 1), ("right", 2), ("up", 3), ("down", 4)):
        for mode, cmd_type in (("step", 1), ("start", 4), ("stop", 3)):
            request = {"action": "move", "direction": direction, "mode": mode}
            _, frame_bytes, _ = ptz.build_request("u", 0, request)
            assert decode(frame_bytes)[2]["data"] == {
                "cmd_type": cmd_type, "rotate_type": rotate, "zoom": 1}


def test_zoom_is_a_1350_frame_with_nested_payload_and_clamped_steps():
    reply, frame_bytes, zoom = ptz.build_request("u", 1, {"action": "zoom", "step": 1}, current_zoom=1)
    command_id, channel, body = decode(frame_bytes)
    assert (reply, command_id, channel, zoom) == (6203, 1350, 1, 2)
    assert body == {"account_id": "u", "cmd": 6203, "payload": {"dstZoom": 2}}
    assert ptz.build_request("u", 1, {"action": "zoom", "step": 1}, current_zoom=ptz.ZOOM_MAX)[2] == ptz.ZOOM_MAX
    assert ptz.build_request("u", 1, {"action": "zoom", "step": -1}, current_zoom=ptz.ZOOM_MIN)[2] == ptz.ZOOM_MIN
    assert ptz.build_request("u", 1, {"action": "zoom", "zoom": 99})[2] == ptz.ZOOM_MAX


def test_presets_are_one_based_for_users_and_zero_based_on_the_wire():
    reply, frame_bytes, _ = ptz.build_request("u", 3, {"action": "preset", "preset": 1})
    assert reply == 6035
    assert decode(frame_bytes)[2] == {"account_id": "u", "cmd": 6035, "commandType": 6035,
                                      "data": {"value": 0}}
    reply, frame_bytes, _ = ptz.build_request("u", 3, {"action": "presets"})
    assert reply == 6034 and decode(frame_bytes)[2]["commandType"] == 6034


@pytest.mark.parametrize("request_body", [
    None, [], {}, {"action": "spin"},
    {"action": "move", "direction": "sideways"},
    {"action": "move", "direction": "left", "mode": "forever"},
    {"action": "zoom", "zoom": "3"}, {"action": "zoom", "step": 2},
    {"action": "preset", "preset": 0}, {"action": "preset", "preset": 9},
    {"action": "preset", "preset": True},
])
def test_invalid_requests_are_rejected(request_body):
    with pytest.raises(ValueError):
        ptz.build_request("u", 0, request_body)


def test_reply_cmd_reads_cmd_or_command_type():
    assert ptz.reply_cmd('{"cmd":6203,"payload":{"dstZoom":1}}')[0] == 6203
    assert ptz.reply_cmd('{"commandType":6030,"data":{}}')[0] == 6030
    assert ptz.reply_cmd('{"payload":{"commandType":6034,"points":[]}}')[0] == 6034
    assert ptz.reply_cmd("not json") == (None, None)


def test_steerable_streams_are_those_with_a_wide_sibling():
    streams = ["eufy_garage", "eufy_garage_wide", "eufy_doorbell", "eufy_shed",
               "eufy_shed_wide", "other_cam", "eufy_lonely_wide"]
    assert api.ptz_streams(streams) == ["eufy_garage", "eufy_shed"]
    assert api.ptz_url("192.168.1.220", 1985) == "http://192.168.1.220:1986/api/ptz"
    with pytest.raises(ValueError):
        api.ptz_url("192.168.1.220", 65535)


def test_generator_writes_the_stream_map(tmp_path):
    spec = importlib.util.spec_from_file_location("gen_for_ptz", ROOT / "bridge/gen_go2rtc.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    manifest = {"nvr_sn": "N", "cameras": [
        {"channel": 0, "name": "Garage", "sn": "A", "status": 1, "dev_type": 301},
        {"channel": 1, "name": "Doorbell", "sn": "B", "status": 1},
    ]}
    source = tmp_path / "cameras.json"
    source.write_text(json.dumps(manifest))
    gen.generate(source, tmp_path / "go2rtc.yaml", tmp_path / "stream_names.json",
                 username=USER, password=PASSWORD)
    assert json.loads((tmp_path / "eufy-streams.json").read_text()) == {
        "eufy_garage": {"channel": 0, "sensor": 1, "ptz": True},
        "eufy_garage_wide": {"channel": 0, "sensor": 0, "ptz": False},
        "eufy_doorbell": {"channel": 1, "sensor": 1, "ptz": False},
    }


def auth(user=USER, password=PASSWORD):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def streams_file(tmp_path):
    path = tmp_path / "eufy-streams.json"
    path.write_text(json.dumps({
        "eufy_garage": {"channel": 0, "sensor": 1, "ptz": True},
        "eufy_garage_wide": {"channel": 0, "sensor": 0, "ptz": False},
    }))
    return path


async def client_for(streams_file, control_dir):
    client = TestClient(TestServer(eufy_ptz.build_app(USER, PASSWORD, streams_file, control_dir)))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_endpoint_requires_auth_and_validates(streams_file, tmp_path):
    client = await client_for(streams_file, tmp_path)
    try:
        body = {"stream": "eufy_garage", "action": "move", "direction": "left"}
        assert (await client.post("/api/ptz", json=body)).status == 401
        assert (await client.post("/api/ptz", json=body, headers=auth(password="x" * 16))).status == 401
        unknown = {**body, "stream": "eufy_nope"}
        assert (await client.post("/api/ptz", json=unknown, headers=auth())).status == 404
        wide = {**body, "stream": "eufy_garage_wide"}
        assert (await client.post("/api/ptz", json=wide, headers=auth())).status == 400
        bad = {"stream": "eufy_garage", "action": "spin"}
        assert (await client.post("/api/ptz", json=bad, headers=auth())).status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_endpoint_refuses_when_the_camera_is_not_live(streams_file, tmp_path):
    client = await client_for(streams_file, tmp_path)
    try:
        body = {"stream": "eufy_garage", "action": "move", "direction": "left"}
        response = await client.post("/api/ptz", json=body, headers=auth())
        assert response.status == 409
        assert "not live" in (await response.json())["error"]
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="Unix sockets")
async def test_endpoint_forwards_to_the_live_engine(streams_file, tmp_path):
    received = []

    async def engine(reader, writer):
        received.append(json.loads(await reader.readline()))
        writer.write(b'{"ok": true, "acked": true, "zoom": 1, "reply": null}\n')
        await writer.drain()
        writer.close()

    # Short directory: Unix socket paths are limited to ~100 bytes.
    control_dir = Path("/tmp") / f"eufy-ptz-test-{os.getpid()}"
    control_dir.mkdir(exist_ok=True)
    server = await asyncio.start_unix_server(engine, path=str(ptz.control_socket_path(0, control_dir)))
    client = await client_for(streams_file, control_dir)
    try:
        body = {"stream": "eufy_garage", "action": "move", "direction": "left"}
        response = await client.post("/api/ptz", json=body, headers=auth())
        assert response.status == 200
        assert (await response.json())["acked"] is True
        assert received == [{"action": "move", "direction": "left"}]
    finally:
        await client.close()
        server.close()
        ptz.control_socket_path(0, control_dir).unlink(missing_ok=True)
        control_dir.rmdir()
