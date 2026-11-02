"""Qualification must observe real media and restore only its owned LAB views."""

import asyncio
import base64
from copy import deepcopy
import json
import subprocess
from types import SimpleNamespace

import pytest

from tests.test_tool_output_storage import load_tool


@pytest.fixture
def tool():
    return load_tool("qualify_voip_dashboard")


def sample():
    return {"call_id": "owned-call", "state": "IN_CALL", "context_state": "running", "context_time": 1,
            "wire": {side: {"samples": 160} for side in ("tx", "rx")},
            "stats": {"sent": 1, "received": 1, "frames_in": 1, "frames_out": 1,
                      "video": {"received": 1, "rendered": 1}},
            "canvases": [{"width": 320, "height": 240, "non_black": True}]}


def progressed():
    value = sample()
    value["context_time"] = 2
    for side in ("tx", "rx"):
        value["wire"][side]["samples"] += 160
    for field in ("sent", "received", "frames_in", "frames_out"):
        value["stats"][field] += 1
    value["stats"]["video"] = {"received": 2, "rendered": 2}
    return value


@pytest.mark.js_runtime
def test_real_play_wrapper_calls_the_original_and_preserves_errors(tool):
    script = r"""
const assert=require('node:assert/strict');
global.window={};Object.defineProperty(globalThis,'navigator',{
 value:{mediaDevices:{getUserMedia:async()=>({live:true})}},configurable:true});
let calls=0;
global.HTMLMediaElement=class {
 constructor(){this.tagName='VIDEO';this.muted=false;this.paused=true;this.readyState=4;}
 play(){calls++;if(this.fail)return Promise.reject(Object.assign(new Error('denied'),{name:'NotAllowedError'}));
   this.paused=false;return Promise.resolve('native-play');}
};
OBSERVE
(async()=>{
 const video=new HTMLMediaElement();assert.equal(await video.play(),'native-play');
 assert.equal(calls,1);assert.equal(video.muted,true);assert.equal(video.paused,false);
 const denied=new HTMLMediaElement();denied.fail=true;
 await assert.rejects(denied.play(),{name:'NotAllowedError'});assert.equal(calls,2);
 assert.deepEqual(window.__dashboardPlayEvents.map(e=>e.status),['resolved','rejected']);
 assert.equal(window.__dashboardPlayEvents[1].error,'NotAllowedError');
 for(let i=0;i<100;i++)await video.play();assert.equal(window.__dashboardPlayEvents.length,64);
})().catch(error=>{console.error(error);process.exit(1)});
""".replace("OBSERVE", tool.MEDIA_OBSERVER_SCRIPT)
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


def test_media_oracle_requires_wire_clock_worklet_and_decoded_canvas(tool):
    tool.validate_media_progress(sample(), progressed(), video=True)


def test_dark_physical_camera_still_requires_decoded_visible_video(tool):
    after = progressed()
    after["canvases"][0]["non_black"] = False
    with pytest.raises(AssertionError, match="non-black"):
        tool.validate_media_progress(sample(), after, video=True)
    tool.validate_media_progress(sample(), after, video=True, allow_dark_video=True)
    after["stats"]["video"]["rendered"] = 1
    with pytest.raises(AssertionError, match="rendered"):
        tool.validate_media_progress(sample(), after, video=True, allow_dark_video=True)
    after["stats"]["video"]["rendered"] = 2
    after["canvases"] = []
    with pytest.raises(AssertionError, match="canvas"):
        tool.validate_media_progress(sample(), after, video=True, allow_dark_video=True)


def test_idle_guard_uses_live_state_and_quiescence_not_retained_terminal_id(tool):
    value = {"state": "idle", "call_id": "old-terminal-call", "media_debug": {
        "runtime_resources": {"call_scoped_quiescent": True}}}
    tool.require_idle_phone(value)
    for changed in ({**value, "state": "in_call"}, {**value, "media_debug": {}},
                    {**value, "media_debug": {"runtime_resources": {"call_scoped_quiescent": False}}}):
        with pytest.raises(RuntimeError):
            tool.require_idle_phone(changed)


def test_physical_peer_selection_rejects_browser_offline_busy_and_identity_mismatch(tool):
    value = {"endpoint_id": "sip:owned", "kind": "sip_account", "availability": "available", "state": "idle", "calls": []}
    tool.require_physical_peer(value, "sip:owned")
    for patch in ({"endpoint_id": "sip:other"}, {"kind": "browser"}, {"availability": "offline"},
                  {"state": "in_call"}, {"calls": [{"id": "unowned"}]}, {"dnd": True}):
        with pytest.raises((ValueError, RuntimeError)):
            tool.require_physical_peer({**value, **patch}, "sip:owned")


def test_signal_files_are_private_and_cannot_reuse_an_old_ready(tool, tmp_path):
    tool.write_signal(tmp_path / "signals", "ready", {"peer": "sip:owned"})
    path = tmp_path / "signals/ready.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "signals").stat().st_mode & 0o777 == 0o700
    with pytest.raises(FileExistsError):
        tool.write_signal(path.parent, "ready", {"peer": "sip:another"})


def test_physical_mode_rejects_non_lab_origin_before_auth_or_any_actor(tool):
    with pytest.raises(ValueError, match="loopback"):
        asyncio.run(tool.qualify_physical(SimpleNamespace(url="http://192.168.1.200:8123")))


@pytest.mark.parametrize("failure", ["call", "clock", "wire", "worklet", "drop", "canvas", "video"])
def test_media_oracle_rejects_counter_only_or_stalled_media(tool, failure):
    after = progressed()
    if failure == "call":
        after["call_id"] = "another-dialog"
    elif failure == "clock":
        after["context_time"] = 1
    elif failure == "wire":
        after["wire"]["rx"]["samples"] = 160
    elif failure == "worklet":
        after["stats"]["frames_out"] = 1
    elif failure == "drop":
        after["stats"]["frames_drop"] = 1
    elif failure == "canvas":
        after["canvases"][0]["non_black"] = False
    elif failure == "video":
        after["stats"]["video"]["rendered"] = 1
    with pytest.raises(AssertionError):
        tool.validate_media_progress(sample(), after, video=True)


def test_audio_wire_observer_uses_actual_negotiated_pcm_geometry(tool):
    wire = tool.DashboardAudioWire()
    wire.opened({"url": "ws://127.0.0.1:18123/api/voip_stack/ws", "requestId": "owned"})
    wire.frame("rx", {"requestId": "owned", "timestamp": 0,
                      "response": {"opcode": 1, "payloadData": json.dumps(
                          {"tx_format": "16000:s16le:1:10", "rx_format": "16000:s16le:1:10"})}})
    for side in ("tx", "rx"):
        wire.frame(side, {"requestId": "owned", "timestamp": 1,
                          "response": {"opcode": 2, "payloadData": base64.b64encode(b"\x01" + bytes(320)).decode()}})
    assert wire.snapshot() == {side: {"packets": 1, "samples": 160, "bytes": 320} for side in ("tx", "rx")}
    assert not wire.capture.errors and not wire.capture.streams
    wire.frame("rx", {"requestId": "other", "timestamp": 2, "response": {"opcode": 2, "payloadData": "invalid"}})
    assert not wire.capture.errors


def test_owned_view_restore_preserves_unrelated_lab_changes(tool, tmp_path):
    original = {"views": [{"path": "home"}, {"path": "voip-dashboard-peer", "title": "Original"}]}

    class Ws:
        def __init__(self):
            self.config = {"views": [{"path": "home", "title": "Updated by another owner"},
                                     {"path": "voip-dashboard-peer", "title": "Fixture"},
                                     {"path": "voip-dashboard-qualification"}, {"path": "new-unrelated"}]}

        async def command(self, item):
            if item["type"] == "lovelace/config/save":
                self.config = deepcopy(item["config"])
            return {"result": deepcopy(self.config)}

    ws = Ws()
    results = {"cleanup_errors": []}
    asyncio.run(tool.restore_owned_views(ws, original, tmp_path, results))
    assert results["owned_views_restored"] and not results["cleanup_errors"]
    assert ws.config["views"] == [{"path": "home", "title": "Updated by another owner"},
                                  {"path": "voip-dashboard-peer", "title": "Original"},
                                  {"path": "new-unrelated"}]
