"""Dashboard observer functions qualify on host; real Lovelace stays pending."""

import base64
import json
from pathlib import Path
import struct
import subprocess

import pytest

from tools.ha_voip_lab.browser_audio_capture import BrowserAudioCapture
from tools.qualify_dashboard_session import DASHBOARD_SAMPLE, DashboardSession, validate_browser_pcm, verify_served_frontend


def test_passive_snapshot_uses_actual_store_without_capability_or_render_side_effects(tmp_path):
    model = Path(__file__).resolve().parents[1] / "custom_components/voip_stack/frontend/voip-stack-dashboard-model.js"
    program = tmp_path / "snapshot.mjs"
    program.write_text('''import assert from 'node:assert/strict';
const {DashboardPhoneStore}=await import('data:text/javascript;base64,'+Buffer.from(MODEL).toString('base64'));
const store=new DashboardPhoneStore();store.apply({kind:'snapshot',revision:4,phones:[{endpoint_id:'desk',device_id:'owner',state:'idle'}]});
let effects=0;const card={_store:store,_config:{device_id:'owner'},_pending:'',shadowRoot:{querySelectorAll(){return[];}}};
Object.defineProperty(card,'capabilities',{get(){effects++;throw new Error('capability discovery side effect');}});
card._render=()=>{effects++;throw new Error('render mutation');};card._owner=()=>{effects++;throw new Error('owner method');};
const before=JSON.stringify([...store.phones]);const sample=(SAMPLE)(card);
assert.equal(effects,0);assert.equal(sample.ready,true);assert.equal(sample.revision,4);assert.equal(sample.phones[0].endpoint_id,'desk');
assert.equal(JSON.stringify([...store.phones]),before);
'''.replace("MODEL", json.dumps(model.read_text())).replace("SAMPLE", DASHBOARD_SAMPLE))
    subprocess.run(["node", program], check=True, capture_output=True, text=True)


def audio_capture(*, receive=True, silent=False):
    capture = BrowserAudioCapture()
    capture.record("socket", "rx", {"opcode": 1, "payloadData": json.dumps({
        "tx_format": "16000:s16le:1:10", "rx_format": "48000:s16le:1:10"})}, 0)
    for side, rate in [("tx", 16000), *([("rx", 48000)] if receive else [])]:
        payload = b"\x01" + struct.pack(f"<{rate // 100}h", *([0 if silent else 200] * (rate // 100)))
        for number in range(4):
            capture.record("socket", side, {"opcode": 2, "payloadData": base64.b64encode(payload).decode()}, number * .01)
    return capture


def test_actual_cdp_observer_decodes_both_asymmetric_pcm_directions():
    measured = validate_browser_pcm(audio_capture(), request_ids={"socket"}, minimum_seconds=.03)
    assert measured["tx"]["samples"] == 640 and measured["rx"]["samples"] == 1920
    assert measured["tx"]["rms"] == measured["rx"]["rms"] == 200


def test_previous_call_audio_cannot_qualify_the_selected_current_websocket():
    capture = audio_capture()
    capture.record("current", "rx", {"opcode": 1, "payloadData": json.dumps({
        "tx_format": "16000:s16le:1:10", "rx_format": "48000:s16le:1:10"})}, 1)
    with pytest.raises(ValueError, match="PCM"):
        validate_browser_pcm(capture, request_ids={"current"}, minimum_seconds=.03)


@pytest.mark.parametrize("fault", ["missing-direction", "silent", "capture-error"])
def test_dashboard_media_cannot_pass_from_gui_or_byte_counters_alone(fault):
    capture = audio_capture(receive=fault != "missing-direction", silent=fault == "silent")
    if fault == "capture-error":
        capture.errors.append("actual observer rejected a frame")
    with pytest.raises(ValueError):
        validate_browser_pcm(capture, request_ids={"socket"}, minimum_seconds=.03)


def test_served_frontend_guard_rejects_home_or_remote_targets_before_http(tmp_path):
    with pytest.raises(ValueError, match="loopback"):
        verify_served_frontend("http://192.0.2.1:8123", tmp_path)


class Page:
    def __init__(self):
        self.clicks = []
    def locator(self, selector):
        self.selector = selector
        return self
    async def evaluate(self, _script):
        return {"ready": True, "pending": ""}
    async def wait_for(self, **_kwargs):
        pass
    async def click(self, **_kwargs):
        self.clicks.append(self.selector)


async def test_failed_dashboard_stage_cleans_peer_without_hanging_a_foreign_dialog():
    page, peer_cleanup = Page(), []
    async def owner():
        return {"state": "in_call", "calls": [{"call_id": "foreign", "peer_endpoint_id": "desk"}]}
    async def baseline():
        return {"state": "idle", "active_dialogs": 0, "call_scoped_quiescent": True, "resource_counts": {"calls": 0}}
    async def stage():
        raise RuntimeError("real actor setup failed")
    async def measure(_call_id):
        raise AssertionError("must not measure a failed setup")
    async def cleanup():
        peer_cleanup.append("only-owned-peer")
    driver = DashboardSession(page, owner_state=owner, peer_state=owner, runtime_snapshot=baseline)
    result = await driver.run("call", peer_endpoint="desk", stage_peer=stage, measure_media=measure, cleanup_peer=cleanup)
    assert result["status"] == "failed" and "another actor" in result["cleanup_error"]
    assert result["error"] == "RuntimeError: real actor setup failed"
    assert page.clicks == [] and peer_cleanup == ["only-owned-peer"]
    assert result["physical"] is False and result["G0_pass"] is False
