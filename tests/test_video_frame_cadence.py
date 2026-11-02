"""Exercise the actual camera and WebSocket senders against one cadence oracle."""

from __future__ import annotations

import ast
import asyncio
from collections import defaultdict
from fractions import Fraction
import json
import math
import random
from pathlib import Path
import struct
import subprocess
import textwrap
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "custom_components/voip_stack/frontend"
BACKEND = ROOT / "custom_components/voip_stack/video_ws_view.py"
VECTORS = json.loads((ROOT / "tests/fixtures/video_cadence.json").read_text())


def _run_camera(vector):
    script = r'''
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";
const frontend=FRONTEND, vector=VECTOR;
let clock=0;
const context=vm.createContext({EventTarget,performance:{now:()=>clock*1000},console,URL,setTimeout,clearTimeout,
 CustomEvent:class extends Event{constructor(type,init){super(type);this.detail=init?.detail;}}});
const model=new vm.SourceTextModule(fs.readFileSync(frontend+"/voip-stack-video-model.js","utf8"),{context});
await model.link(()=>{throw Error("unexpected model import");});await model.evaluate();
const engine=new vm.SourceTextModule(fs.readFileSync(frontend+"/voip-stack-video.js","utf8"),{
 context,initializeImportMeta(meta){meta.url="https://ha.invalid/voip-stack-video.js?v=cadence";},
 importModuleDynamically:async()=>model});
await engine.link(()=>{throw Error("unexpected engine import");});await engine.evaluate();
const Video=engine.namespace.VoipStackVideo;
const owner=new Video();owner._emit=()=>{};
async function run(generation,senderGeneration,timestamps,estimate=vector.fps,ceiling=vector.fps){
 const accepted=[],closed=[];let index=0;
 const encoder={kind:"jpeg",worker:{},state:"configured",close(){this.state="closed";}};
 const reader={async read(){if(index===timestamps.length)return{done:true};const i=index++;clock=vector.times[i];return{done:false,value:{timestamp:timestamps[i],index:i,close(){closed.push(i);}}};}};
 Object.assign(owner,{_generation:generation,_senderGeneration:senderGeneration,_cameraEnabled:true,_cameraAllowed:true,
  _cameraReader:reader,_encoder:encoder,_jpegEncoderWorker:encoder.worker});
 owner._dispatchJpegFrame=(frame)=>{accepted.push(frame.index);frame.close();};
 await owner._encodeCamera(estimate,reader,encoder,generation,senderGeneration,ceiling);
 assert.equal(closed.length,timestamps.length);assert.equal(new Set(closed).size,timestamps.length);
 assert.equal(owner._cameraReader,null);assert.equal(encoder.state,"closed");return accepted;
}
const timestamps=vector.times.map(t=>t*1e6);
const accepted=await run(1,1,timestamps);
if(vector.accepted)assert.deepEqual(accepted,vector.accepted);
else assert.equal(accepted.length,vector.accepted_count);
// A replacement encoder/session starts a fresh phase even with identical TS.
assert.deepEqual(await run(2,2,timestamps),accepted);
if(vector.name==="ten_fps_jitter"){
 // Missing source timestamps use the wall clock, and changing clock domain
 // must not reuse a deadline from an unrelated epoch.
 assert.deepEqual(await run(3,3,timestamps.map(()=>undefined)),vector.accepted);
 assert.deepEqual(await run(4,4,timestamps.map((t,i)=>i%2?undefined:t+1e6)),vector.accepted);
}
if(vector.name==="thirty_fps_jitter"){
 // Missing/low track estimates affect encoder metadata, not the absence of
 // a negotiated FPS ceiling. A device can deliver faster than its estimate.
 assert.deepEqual(await run(5,5,timestamps,15,Infinity),vector.accepted);
}
if(vector.name==="ten_fps_jitter"){
 context.Worker=class {};
 context.MediaStreamTrackProcessor=class {
  constructor(){this.readable={getReader:()=>({cancel:async()=>{}})};}
 };
 for(const [limit,trackRate,estimate] of [[10,60,10],[25,30,25],[30,undefined,30],[null,undefined,30],[null,5,5]]){
  let constraints,encodeArguments;
  const track={getSettings:()=>({width:640,height:360,frameRate:trackRate}),stop(){}};
  context.navigator={mediaDevices:{getUserMedia:async c=>{constraints=c.video;return{getVideoTracks:()=>[track],getTracks:()=>[track]};}}};
  const prepared=new Video();Object.assign(prepared,{_generation:1,_cameraAllowed:true,_cameraEnabled:true,
   _negotiated:{send:{encoding:"JPEG",codec:"jpeg",max_framerate:limit}}});
  prepared._emit=()=>{};prepared._sendTxEpoch=()=>{};
  prepared._createJpegEncoder=async()=>({kind:"jpeg",worker:{},state:"configured",close(){this.state="closed";}});
  prepared._encodeCamera=(...args)=>{encodeArguments=args;return Promise.resolve();};
  await prepared._replaceSender("jpeg",1,"",false);
  assert.equal(constraints.frameRate.ideal,limit||30);
  assert.equal(constraints.frameRate.max,limit||undefined);
  assert.equal(encodeArguments[0],estimate);
  assert.equal(encodeArguments[5],limit||Infinity);
  await prepared._cleanupSender();
 }
}
console.log(JSON.stringify(accepted));
'''.replace("FRONTEND", json.dumps(str(FRONTEND))).replace("VECTOR", json.dumps(vector))
    result = subprocess.run(
        ["node", "--no-warnings", "--experimental-vm-modules", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.js_runtime
@pytest.mark.parametrize("vector", VECTORS, ids=lambda item: item["name"])
def test_camera_sender_preserves_source_cadence(vector):
    assert _run_camera(vector) == vector["accepted"]


def _backend_sender(events, *, encoding="JPEG", wait_hook=None):
    """Load the complete production nested sender; replace only external I/O."""
    tree = ast.parse(BACKEND.read_text())
    session_body = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_video_session")
    sender = next(node for node in session_body.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "browser_to_rtp")
    helpers = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "_VideoFrameCadence"]
    clock = SimpleNamespace(now=10.0)
    session = SimpleNamespace(media_generation=1, remote_connection_held=False, can_send=True, removed=False,
                              send_video_format=SimpleNamespace(encoding=encoding, max_framerate=10, payload_type=26))
    closed = asyncio.Event()
    ended = asyncio.Event()
    sent, waits = [], []
    counters = defaultdict(int)
    header = struct.Struct("!BBI")

    class WebSocket:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if not events:
                raise StopAsyncIteration
            event = events.pop(0)
            clock.now = max(clock.now, 10.0 + event["at"])
            if "generation" in event:
                session.media_generation = event["generation"]
            session.send_video_format.max_framerate = event.get("fps", session.send_video_format.max_framerate)
            if "control" in event:
                return SimpleNamespace(type=2, data=json.dumps({"type": event["control"]}))
            return SimpleNamespace(type=1, data=header.pack(1, 1, event.get("timestamp", 0)) + event["id"].to_bytes(4, "big"))

    async def packetize(data, **kwargs):
        if not kwargs["should_continue"]():
            return []
        sent.append((int.from_bytes(data, "big"), clock.now))
        return [b"packet"]

    async def send_datagrams(_transport, _packets, _target, *, should_continue, on_sent):
        if not should_continue():
            return False
        on_sent(16, 4)
        return True

    async def wait_for(waiter, timeout):
        waiter.close()
        waits.append(timeout)
        clock.now += timeout
        if wait_hook is not None:
            wait_hook(session, closed)
        if closed.is_set():
            return None
        raise TimeoutError

    namespace = {
        "math": math, "asyncio": SimpleNamespace(sleep=asyncio.sleep, wait_for=wait_for), "json": json,
        "WSMsgType": SimpleNamespace(BINARY=1, TEXT=2, CLOSE=3, CLOSED=4, ERROR=5),
        "_VIDEO_RTP_RECEIVE_BURST_PACKETS": 32, "_VIDEO_HEADER": header, "_VIDEO_ACCESS_UNIT": 1,
        "_MAX_BROWSER_ACCESS_UNIT_BYTES": 1024 * 1024,
        "_async_packetize_browser_access_unit": packetize, "_send_packetized_datagrams": send_datagrams,
        "H264RtpError": ValueError, "_LOGGER": SimpleNamespace(debug=lambda *args: None),
    }
    for helper in helpers:
        exec(compile(ast.Module(body=[helper], type_ignores=[]), str(BACKEND), "exec"), namespace)
    wrapper = """def create_sender(ws, session, loop, closed, call_ended, counters, rtp_send_lock, outbound_clock):
    sequence = 0
    remote_host, remote_port, latched_source, latched_ssrc = '127.0.0.1', 40002, None, None
    last_regular_rtp_tx_at = 0.0
    jpeg_normalization_required, browser_tx_drop_logs = False, 0
    ssrc, transport, jpeg_normalizer = 123, None, None
    rtp_source = type('Source', (), {'sequence': 0})()
    async def refresh_media_state(generation):
        return True
    def store_counters():
        pass
    def request_key_frame(now):
        pass
""" + textwrap.indent(ast.unparse(sender), "    ") + "\n    return browser_to_rtp\n"
    exec(compile(wrapper, str(BACKEND), "exec"), namespace)
    task = namespace["create_sender"](WebSocket(), session, SimpleNamespace(time=lambda: clock.now), closed, ended, counters,
        asyncio.Lock(), SimpleNamespace(map_browser=lambda timestamp, now: timestamp, reset_browser=lambda: None))
    return task, sent, waits, counters


@pytest.mark.parametrize("vector", [v for v in VECTORS if v["name"] != "clock_restart"], ids=lambda item: item["name"])
async def test_jpeg_backend_preserves_wall_clock_cadence(vector):
    events = [{"id": index, "at": at, "fps": vector["fps"], "timestamp": index * 1234567} for index, at in enumerate(vector["times"])]
    task, sent, waits, counters = _backend_sender(events)
    await task()
    assert [index for index, _ in sent] == vector.get("backend_accepted", vector["accepted"])
    assert not waits
    assert counters["video_rate_limited_access_units"] == len(vector["times"]) - len(sent)


@pytest.mark.js_runtime
async def test_stacked_sender_and_server_preserve_ten_fps_with_arrival_jitter():
    vector = VECTORS[0]
    selected = _run_camera(vector)
    events = [{"id": index, "at": vector["times"][index] + (0.01004 if index % 2 else 0.01),
               "timestamp": round(vector["times"][index] * 90000)} for index in selected]
    task, sent, waits, counters = _backend_sender(events)
    await task()
    assert [index for index, _ in sent] == vector["accepted"]
    assert not waits and counters["video_rate_limited_access_units"] == 0


@pytest.mark.js_runtime
async def test_stacked_faster_capture_and_server_keep_negotiated_twenty_five_fps():
    vector = {"name": "faster_capture", "fps": 25,
              "times": [index / 30 for index in range(120)], "accepted_count": 100}
    selected = _run_camera(vector)
    jitter = [0.005, 0.002, 0.008, 0.001, 0.004, 0.006]
    events = [{"id": index, "at": vector["times"][index] + jitter[number % len(jitter)],
               "fps": 25, "timestamp": round(vector["times"][index] * 90000)}
              for number, index in enumerate(selected)]
    task, sent, waits, counters = _backend_sender(events)
    await task()
    assert [index for index, _ in sent] == selected
    assert not waits and counters["video_rate_limited_access_units"] == 0


@pytest.mark.parametrize("encoding", ["H264", "VP8"])
async def test_dependent_frames_wait_without_losing_reference_pictures(encoding):
    task, sent, waits, counters = _backend_sender([{"id": i, "at": 0} for i in range(6)], encoding=encoding)
    await task()
    assert [index for index, _ in sent] == list(range(6))
    assert len(waits) == 4 and all(0 < delay <= 0.1 for delay in waits)
    assert sent[-1][1] - sent[0][1] == pytest.approx(0.4)
    assert counters["video_rate_limited_access_units"] == 4


@pytest.mark.parametrize("stop", [False, True])
async def test_dependent_wait_does_not_publish_stale_generation_or_ignore_close(stop):
    def interrupt(session, closed):
        if stop:
            closed.set()
        else:
            session.media_generation = 2
    task, sent, waits, _ = _backend_sender(
        [{"id": 0, "at": 0}, {"id": 1, "at": 0.01}, {"id": 2, "at": 0.02}, {"id": 3, "at": 0.03}],
        encoding="H264", wait_hook=interrupt,
    )
    await task()
    assert [index for index, _ in sent] == ([0, 1] if stop else [0, 1, 3])
    assert len(waits) == 1


async def test_client_epoch_and_timestamps_cannot_reset_server_budget():
    events = [{"id": 0, "at": 0}, {"control": "tx_epoch", "at": 0.01},
              {"id": 1, "at": 0.02, "timestamp": 0xffffffff}, {"control": "tx_epoch", "at": 0.025},
              {"id": 2, "at": 0.03, "timestamp": 0}, {"id": 3, "at": 0.1999, "timestamp": 0}]
    task, sent, waits, counters = _backend_sender(events)
    await task()
    assert [index for index, _ in sent] == [0, 1, 3]
    assert not waits and counters["video_rate_limited_access_units"] == 1


async def test_server_resets_for_committed_rate_and_generation_changes():
    events = [{"id": 0, "at": 0, "fps": 10}, {"id": 1, "at": 0.01, "fps": 25},
              {"id": 2, "at": 0.02, "generation": 2}, {"id": 3, "at": 0.03, "fps": None},
              {"id": 4, "at": 0.031, "fps": None}]
    task, sent, waits, _ = _backend_sender(events)
    await task()
    assert [index for index, _ in sent] == [0, 1, 2, 3, 4]
    assert not waits


@pytest.mark.parametrize("vector", VECTORS, ids=lambda item: item["name"])
def test_server_cadence_preserves_bounded_arrival_credit(vector):
    tree = ast.parse(BACKEND.read_text())
    helper = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "_VideoFrameCadence")
    namespace = {"math": math}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), str(BACKEND), "exec"), namespace)
    cadence = namespace["_VideoFrameCadence"]()
    assert [i for i, at in enumerate(vector["times"]) if cadence.delay(at, vector["fps"]) == 0] == vector.get("backend_accepted", vector["accepted"])


@pytest.mark.parametrize("fps", [10, 25, 29.97])
def test_server_credit_matches_token_bucket_and_never_accumulates_stall_bursts(fps):
    tree = ast.parse(BACKEND.read_text())
    helper = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "_VideoFrameCadence")
    namespace = {"math": math}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), str(BACKEND), "exec"), namespace)
    cadence = namespace["_VideoFrameCadence"]()
    # Independent RFC 3290 token occupancy oracle uses exact rational time,
    # unlike production's progressing deadline representation.
    rng = random.Random(3290)
    ticks = [0] * 20
    for _ in range(600):
        ticks.append(ticks[-1] + rng.randrange(1, 11000))
    ticks.extend([ticks[-1] + 60_000_000] * 20)
    times = [Fraction(tick, 1_000_000) for tick in ticks]
    rate = Fraction(str(fps))
    tokens, previous = Fraction(2), times[0]
    expected = []
    for index, now in enumerate(times):
        tokens = min(2, tokens + (now - previous) * rate)
        previous = now
        if tokens >= 1:
            tokens -= 1
            expected.append(index)
    admitted = [i for i, at in enumerate(times) if cadence.delay(float(at), fps) == 0]
    assert admitted == expected
    assert admitted[:2] == [0, 1]
    assert [i for i in admitted if i >= len(times) - 20] == [len(times) - 20, len(times) - 19]
    for first, left in enumerate(admitted):
        for last in range(first, len(admitted)):
            right = admitted[last]
            assert last - first + 1 <= rate * (times[right] - times[left]) + 2


@pytest.mark.parametrize("encoding,backlog", [("JPEG", False), ("JPEG", True), ("H264", True)])
async def test_receiver_has_no_fps_ceiling_and_only_coalesces_independent_backlog(encoding, backlog):
    tree = ast.parse(BACKEND.read_text())
    sender = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_video_access_units_to_ws")
    # Type annotations are not relevant to the real coroutine's I/O contract.
    for arg in sender.args.args:
        arg.annotation = None
    sender.returns = None
    closed = asyncio.Event()
    queue = asyncio.Queue()
    sent, waits = [], []
    clock = SimpleNamespace(now=10.0)

    def frame(index):
        return SimpleNamespace(encoding=encoding, key_frame=True, timestamp=index * 3000, data=bytes([index]))

    class Source:
        index = 0

        async def get(self):
            if backlog:
                return await queue.get()
            index = self.index
            self.index += 1
            clock.now = 10 + index / 30
            return frame(index)

        def get_nowait(self):
            return queue.get_nowait()

    class WebSocket:
        async def send_bytes(self, data):
            sent.append(data[-1])
            if data[-1] == 19:
                closed.set()

    async def wait_for(waiter, timeout):
        waiter.close()
        waits.append(timeout)
        clock.now += timeout
        raise TimeoutError

    if backlog:
        for i in range(20):
            queue.put_nowait(frame(i))
    namespace = {"asyncio": SimpleNamespace(QueueEmpty=asyncio.QueueEmpty, get_running_loop=lambda: SimpleNamespace(time=lambda: clock.now), wait_for=wait_for),
                 "_VIDEO_HEADER": struct.Struct("!BBI"), "_VIDEO_ACCESS_UNIT": 1, "_JPEG_BROWSER_MAX_FPS": 15}
    exec(compile(ast.Module(body=[sender], type_ignores=[]), str(BACKEND), "exec"), namespace)
    await namespace["_video_access_units_to_ws"](WebSocket(), closed, Source(), asyncio.Lock())
    assert sent == ([19] if backlog and encoding == "JPEG" else list(range(20)))
    assert not waits
