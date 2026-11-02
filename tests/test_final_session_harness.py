"""Host witnesses exercise the final-session oracles, never physical claims."""

import asyncio
from io import BytesIO
import math
import socket
import struct
from types import SimpleNamespace
import wave

from aioesphomeapi import TextSensorState
from PIL import Image
import pytest

from qualification.final_session import (
    AudioWireFormat, decode_l16_rtp, final_session_plan, run_owned_case,
)
from tools.qualify_esp_session import EspObserver, validate_screenshot_response


def test_grouped_plan_is_prepared_without_forging_physical_success():
    plan = final_session_plan("head", "candidate")
    assert plan["status"] == "prepared" and plan["hardware_actions"] == []
    assert plan["physical"] == plan["acoustic"] == "not_run"
    assert {"assist", "mww", "audio", "display"} <= set(plan["required_firmware_features"])
    assert plan["preserve"] == {"ha_owner": "home", "default_line": "ha", "master_volume": 0}
    assert plan["audio_contracts"]["ha-d1"] == {"ha_tx": [48000, 10], "esp_tx": [16000, 10], "d1_required": True}
    assert plan["audio_contracts"]["asterisk-standard"]["packet_times_ms"] == [20, 10, 16]
    assert {"ha-to-esp", "esp-to-ha", "esp-to-esp", "qa-to-esp", "esp-to-qa"} <= set(plan["routes"])
    assert {"register", "mwi", "dtmf", "blf", "ldap", "screenshot"} <= set(plan["feature_oracles"])


@pytest.mark.parametrize("rate,ptime", [(48000, 10), (16000, 10), (16000, 16), (16000, 20)])
def test_real_udp_payloads_decode_exact_pcm_and_sequence_wrap(tmp_path, rate, ptime):
    samples = [int(8000 * math.sin(2 * math.pi * 317 * n / rate)) for n in range(rate * ptime // 1000)]
    payload = struct.pack(f">{len(samples)}h", *samples)
    packets = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(1)
        for number in range(4):
            packet = struct.pack("!BBHII", 0x80, 97, (65534 + number) & 65535,
                                 (0xFFFFFF00 + number * len(samples)) & 0xFFFFFFFF, 0x1234) + payload
            sender.sendto(packet, receiver.getsockname())
            wire, _ = receiver.recvfrom(4096)
            assert wire == packet
            packets.append(wire)
    output = tmp_path / "decoded.wav"
    result = decode_l16_rtp(packets, AudioWireFormat(rate, ptime, 97), output, minimum_seconds=0.03)
    assert result["proof_layer"] == "software" and result["physical"] is False
    assert result["samples_per_channel"] == 4 * len(samples) and result["rms"] > 1000
    with wave.open(str(output), "rb") as decoded:
        assert decoded.getframerate() == rate and decoded.getnchannels() == 1
        assert list(struct.unpack(f"<{4 * len(samples)}h", decoded.readframes(4 * len(samples)))) == samples * 4


@pytest.mark.parametrize("fault", ["gap", "duplicate", "clock", "payload", "short", "silence"])
def test_pcm_oracle_rejects_false_media_success(tmp_path, fault):
    payload = struct.pack(">160h", *range(160))
    packets = [struct.pack("!BBHII", 0x80, 97, n, n * 160, 1) + payload for n in range(4)]
    if fault == "gap":
        del packets[1]
    elif fault == "duplicate":
        packets[1] = packets[0]
    elif fault == "clock":
        packets[2] = struct.pack("!BBHII", 0x80, 97, 2, 960, 1) + payload
    elif fault == "payload":
        packets[1] = packets[1][:1] + bytes([98]) + packets[1][2:]
    elif fault == "short":
        packets[1] = packets[1][:-2]
    else:
        packets = [p[:12] + bytes(320) for p in packets]
    with pytest.raises(ValueError):
        decode_l16_rtp(packets, AudioWireFormat(16000, 10, 97), tmp_path / "bad.wav", minimum_seconds=0.03)
    assert not (tmp_path / "bad.wav").exists()


def snapshot(**changes):
    return {"state": "idle", "call_scoped_quiescent": True, "active_dialogs": 0,
            "resource_counts": {"calls": 0, "legs": 0, "rtp": 0}, **changes}


@pytest.mark.parametrize("action_error,cleanup_error,leak", [(True, False, False), (False, True, False), (False, False, True)])
async def test_every_failure_cleans_owned_call_and_cannot_report_success(action_error, cleanup_error, leak):
    events = []
    async def action():
        events.append("action")
        if action_error:
            raise RuntimeError("exact action failure")
    async def cleanup():
        events.append("cleanup")
        if cleanup_error:
            raise OSError("exact cleanup failure")
    async def observe():
        events.append("snapshot")
        return snapshot(resource_counts={"calls": 1}) if leak and len(events) > 1 else snapshot()
    result = await run_owned_case(action, cleanup, observe)
    assert result["status"] == "failed" and events.count("cleanup") == 1
    assert result["physical"] is False
    if action_error:
        assert result["error"] == "RuntimeError: exact action failure"
    if cleanup_error:
        assert result["cleanup_error"] == "OSError: exact cleanup failure"


async def test_cleanup_cause_survives_failed_quiescence_observation():
    observations = 0

    async def observe():
        nonlocal observations
        observations += 1
        if observations > 1:
            raise RuntimeError("secondary observation failed")
        return snapshot()

    async def action():
        pass

    async def cleanup():
        raise LookupError("first visible click failed")

    result = await run_owned_case(action, cleanup, observe)
    assert result["status"] == "failed"
    assert result["cleanup_error"] == "LookupError: first visible click failed"
    assert result["quiescence_error"] == "RuntimeError: secondary observation failed"


def jpeg_bytes():
    output = BytesIO()
    Image.new("RGB", (24, 16), (40, 90, 180)).save(output, "JPEG")
    return output.getvalue()


@pytest.mark.parametrize("fault", ["etag", "mime", "url", "status", "truncated", "pixels", "geometry"])
def test_screenshot_http_oracle_rejects_stale_or_invalid_capture(fault):
    url = "http://192.0.2.1/esp_screenshot/screen/image.jpg?capture=fresh"
    kwargs = dict(status=200, url=url, expected_url=url, mime="image/jpeg", etag='"fresh"',
                  capture_id="fresh", payload=jpeg_bytes(), expected_size=(24, 16))
    kwargs.update({"etag": '"old"'} if fault == "etag" else {"mime": "text/html"} if fault == "mime"
        else {"url": url + "-other"} if fault == "url" else {"status": 304} if fault == "status"
        else {"payload": jpeg_bytes()[:-2]} if fault == "truncated"
        else {"payload": b"\xff\xd8garbage\xff\xd9"} if fault == "pixels" else {"expected_size": (25, 16)})
    with pytest.raises(RuntimeError):
        validate_screenshot_response(**kwargs)


def test_screenshot_pixels_are_bound_to_the_fresh_etag_without_demanding_a_scene_change():
    url = "http://192.0.2.1/esp_screenshot/screen/image.jpg?capture=fresh"
    result = validate_screenshot_response(status=200, url=url, expected_url=url, mime="image/jpeg",
        etag='"fresh"', capture_id="fresh", payload=jpeg_bytes(), expected_size=(24, 16))
    assert result["width"] == 24 and result["height"] == 16 and len(result["pixels_sha256"]) == 64


@pytest.mark.parametrize("status", ["Busy", "Capturing", None])
async def test_screenshot_busy_or_unobserved_status_never_invokes_the_action(tmp_path, status):
    observer = EspObserver(None)
    info = type("TextSensorInfo", (), {})
    observer.entities = [info(), info()]
    for entity, key, object_id in zip(observer.entities, [1, 2], ["screenshot_id", "screenshot_status"]):
        entity.key, entity.object_id, entity.name = key, object_id, object_id
    observer.on_state(TextSensorState(key=1, state="old"))
    if status is not None:
        observer.on_state(TextSensorState(key=2, state=status))
    calls = []
    async def service(*args):
        calls.append(args)
    observer.service = service
    with pytest.raises(RuntimeError):
        await observer.screenshot("http://192.0.2.1", tmp_path / "screen.jpg", expected_size=(24, 16), timeout=0.03)
    assert calls == [] and not (tmp_path / "screen.jpg").exists()


class ApiClient:
    def __init__(self, mac="AA:BB:CC:DD:EE:FF", build="candidate-build"):
        self.mac, self.build, self.events = mac, build, []

    async def connect(self, **kwargs):
        self.events.append("connect")

    async def device_info(self):
        return SimpleNamespace(mac_address=self.mac, compilation_time=self.build)

    async def list_entities_services(self):
        self.events.append("inventory")
        return [], []

    def subscribe_states(self, callback):
        self.events.append("subscribe")

    async def disconnect(self):
        self.events.append("disconnect")


@pytest.mark.parametrize("fault", ["mac", "build", "missing-build"])
async def test_wrong_or_unbound_identity_stops_before_inventory_or_mutation(fault):
    client = ApiClient(mac="00:11:22:33:44:55" if fault == "mac" else "AA:BB:CC:DD:EE:FF",
                       build="old" if fault == "build" else "candidate-build")
    observer = EspObserver(client)
    with pytest.raises(RuntimeError):
        await observer.open(expected_mac="AA:BB:CC:DD:EE:FF", expected_build="" if fault == "missing-build" else "candidate-build")
    assert not observer.identity_verified and "inventory" not in client.events
    assert client.events == [] if fault == "missing-build" else client.events == ["connect", "disconnect"]
    with pytest.raises(RuntimeError, match="identity"):
        await observer.service("take_screenshot")


async def test_partially_connected_api_is_closed_before_failure_returns():
    class PartialClient(ApiClient):
        async def connect(self, **_kwargs):
            self.events.append("allocated-transport")
            raise OSError("exact connection failure")
    client = PartialClient()
    with pytest.raises(OSError, match="exact connection failure"):
        await EspObserver(client).open(expected_mac="AA:BB:CC:DD:EE:FF", expected_build="candidate-build")
    assert client.events == ["allocated-transport", "disconnect"]


def test_cross_domain_key_collision_and_repeated_error_keep_independent_revisions():
    from aioesphomeapi import NumberState
    observer = EspObserver(None)
    observer.on_state(TextSensorState(key=1, state="Error"))
    observer.on_state(NumberState(key=1, state=0))
    observer.on_state(TextSensorState(key=1, state="Error"))
    assert observer.values[("TextSensorState", 1)] == "Error"
    assert observer.state_revisions[("TextSensorState", 1)] == 2


def observer_with_screen(status="Ready"):
    observer = EspObserver(None)
    info = type("TextSensorInfo", (), {})
    observer.entities = [info(), info()]
    for entity, key, object_id in zip(observer.entities, [1, 2], ["screenshot_id", "screenshot_status"]):
        entity.key, entity.object_id, entity.name = key, object_id, object_id
    observer.on_state(TextSensorState(key=1, state="old"))
    observer.on_state(TextSensorState(key=2, state=status))
    return observer


@pytest.mark.parametrize("scene", [None, {}, {"page": "unknown", "transition": "0", "avatar_visible": "NO"}])
async def test_unidentified_native_scene_is_rejected_before_capture_action(tmp_path, scene):
    observer = observer_with_screen()
    actions = []
    async def checkpoint():
        return scene
    async def service(name):
        actions.append(name)
    observer.service = service
    with pytest.raises(RuntimeError, match="scene"):
        await observer.screenshot("http://192.0.2.1", tmp_path / "screen.jpg", expected_size=(24, 16),
                                  checkpoint=checkpoint, timeout=.02)
    assert actions == []


@pytest.mark.parametrize("fault", ["old-id", "old-ready", "fresh-error", "fresh-busy"])
async def test_prior_success_or_error_can_never_complete_the_new_capture(tmp_path, fault):
    observer = observer_with_screen("Error" if fault == "fresh-error" else "Ready")
    calls = []
    async def service(name):
        calls.append(name)
        if fault != "old-id":
            observer.on_state(TextSensorState(key=1, state="fresh"))
        if fault != "old-ready":
            observer.on_state(TextSensorState(key=2, state="Error" if fault == "fresh-error" else "Busy" if fault == "fresh-busy" else "Ready"))
    observer.service = service
    with pytest.raises((RuntimeError, TimeoutError)):
        await observer.screenshot("http://192.0.2.1", tmp_path / "screen.jpg", expected_size=(24, 16), timeout=0.03)
    assert calls == ["take_screenshot"] and not (tmp_path / "screen.jpg").exists()


@pytest.mark.parametrize("race", [None, "scene", "capture"], ids=["valid", "scene-changed", "capture-replaced"])
async def test_actual_http_decode_binds_fresh_native_capture_and_scene(tmp_path, race):
    from contextlib import contextmanager
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    loop = asyncio.get_running_loop()
    @contextmanager
    def origin():
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                if race == "capture":
                    loop.call_soon_threadsafe(observer.on_state, TextSensorState(key=1, state="other"))
                payload = jpeg_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("ETag", '"fresh"')
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", requests
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    observer = observer_with_screen()
    async def service(_name):
        observer.on_state(TextSensorState(key=2, state="Capturing"))
        observer.on_state(TextSensorState(key=1, state="fresh"))
        observer.on_state(TextSensorState(key=2, state="Ready"))
    observer.service = service
    scene = {"page": "idle", "transition": "0", "avatar_visible": "NO"}
    checkpoint_calls = 0
    async def checkpoint():
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        if race == "scene" and checkpoint_calls == 2:
            return {**scene, "page": "ringing"}
        return dict(scene)
    with origin() as (url, requests):
        if race:
            with pytest.raises(RuntimeError, match="scene|capture"):
                await observer.screenshot(url, tmp_path / "screen.jpg", expected_size=(24, 16), checkpoint=checkpoint)
            assert not (tmp_path / "screen.jpg").exists()
            return
        result = await observer.screenshot(url, tmp_path / "screen.jpg", expected_size=(24, 16), checkpoint=checkpoint)
    assert requests == ["/esp_screenshot/screen/image.jpg?capture=fresh"]
    assert result["capture_id"] == "fresh" and result["scene_verified"]
    assert result["physical_panel_qualified"] is False
    assert (tmp_path / "screen.jpg").stat().st_mode & 0o777 == 0o600


async def test_cancelled_owned_action_still_runs_its_cleanup():
    events = []
    async def action():
        raise asyncio.CancelledError
    async def cleanup():
        events.append("cleanup")
    async def observe():
        return snapshot()
    with pytest.raises(asyncio.CancelledError):
        await run_owned_case(action, cleanup, observe)
    assert events == ["cleanup"]


def test_preparation_receipt_cannot_satisfy_existing_g0(tmp_path):
    import json
    from qualification.evidence import EvidenceError, derive_scenario_evidence, MEASURED_HIL_SCENARIO
    prepared = tmp_path / "prepared.json"
    prepared.write_text(json.dumps(final_session_plan("head", "candidate")))
    with pytest.raises(EvidenceError, match="did not prove"):
        derive_scenario_evidence("hil-s3", {"head": "head", "plan_id": "plan",
            "scenarios": [{"id": MEASURED_HIL_SCENARIO}]}, [prepared], candidate_id="candidate", artifact_root=tmp_path)
