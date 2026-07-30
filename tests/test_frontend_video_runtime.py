#!/usr/bin/env python3
"""Runtime anti-regressions for the browser SIP video media engine."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
VIDEO_ENGINE = (
    ROOT
    / "custom_components"
    / "voip_stack"
    / "frontend"
    / "voip-stack-video.js"
)
VIDEO_MODEL = VIDEO_ENGINE.with_name("voip-stack-video-model.js")
VIDEO_WORKER = VIDEO_ENGINE.with_name("voip-stack-video-worker.js")
BROWSER_PROBE = ROOT / "tools" / "sip_video_browser_probe.py"


def test_browser_probe_keeps_waits_light_and_bounded() -> None:
    source = BROWSER_PROBE.read_text()

    assert "LIGHT_CARD_STATE = r" in source
    assert "globalThis.__voipStackProbeCard = next" in source
    assert "globalThis.__voipStackProbeFindCard = () =>" in source
    light = source.split('LIGHT_CARD_STATE = r"""', 1)[1].split('"""', 1)[0]
    assert "globalThis.__voipStackProbeCard" in light
    assert "globalThis.__voipStackProbeFindCard?.()" in light
    assert light.index("__voipStackProbeCard") < light.index("__voipStackProbeFindCard")
    assert "querySelectorAll" not in light
    assert "...(deviceId ? { device_id: deviceId } : {})" in source
    assert "INSTALL_RESPONSIVENESS_MONITOR = r" in source
    assert "READ_RESPONSIVENESS_MONITOR = r" in source
    assert "STOP_RESPONSIVENESS_MONITOR = r" in source
    assert "({CARD_SAMPLE})" not in source
    assert source.count("page.evaluate(CARD_SAMPLE)") == 1
    assert source.count("polling=100") == source.count("page.wait_for_function(")
    assert "max_main_thread_gap_ms" in source
    assert "max_ws_rtt_ms" in source
    assert "to_ui_idle_ms" in source
    assert "to_backend_cleanup_ms" in source
    assert "--auth-check-only" in source
    assert "--allow-dark-video" in source
    assert "and not args.allow_dark_video" in source
    assert 'sample("authenticated_card_ready")' in source
    assert "['ringing','in_call'].includes" in source
    assert 'sample("incoming_progress")' in source
    assert '== "ringing":' in source
    start = source.split('START_OUTBOUND = r"""', 1)[1].split('"""', 1)[0]
    assert "Promise.resolve(card._startCall())" in start
    assert "await card._startCall()" not in start


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_video_engine_runtime_recovery_contracts() -> None:
    script = f"""
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";

const source = fs.readFileSync({json.dumps(str(VIDEO_ENGINE))}, "utf8");
const modelSource = fs.readFileSync({json.dumps(str(VIDEO_MODEL))}, "utf8");
const cameraStorage = new Map();
const context = vm.createContext({{
  EventTarget,
  performance,
  console,
  Blob,
  setTimeout,
  clearTimeout,
  CustomEvent: class CustomEvent extends Event {{
    constructor(type, init) {{ super(type); this.detail = init?.detail; }}
  }},
  localStorage: {{
    getItem(key) {{ return cameraStorage.has(key) ? cameraStorage.get(key) : null; }},
    setItem(key, value) {{ cameraStorage.set(key, String(value)); }},
  }},
  WebSocket: {{ OPEN: 1, CONNECTING: 0 }},
  EncodedVideoChunk: class EncodedVideoChunk {{ constructor(init) {{ Object.assign(this, init); }} }},
}});
const modelModule = new vm.SourceTextModule(modelSource, {{ context }});
const module = new vm.SourceTextModule(source, {{ context }});
await module.link((specifier) => {{
  if (specifier === "./voip-stack-video-model.js?v=2") return modelModule;
  throw new Error(`unexpected import: ${{specifier}}`);
}});
await module.evaluate();
const Video = module.namespace.VoipStackVideo;
const {{ cameraEncoderContract }} = modelModule.namespace;

// Android may expose a portrait camera track after accepting landscape
// constraints. Both H.264 and JPEG preserve the native orientation; the
// receiver admits the equivalent rotated macroblock envelope and letterboxes
// it without stretching.
const portraitSettings = {{ width: 288, height: 352, frameRate: 30 }};
const level13Capture = {{
  idealWidth: 352,
  idealHeight: 288,
  maxFs: 396,
  maxMbps: 11880,
  maxFr: 10,
}};
assert.deepEqual(
  Object.values(cameraEncoderContract(level13Capture, portraitSettings, "H264")),
  [288, 352, 396, 10],
);
assert.deepEqual(
  Object.values(cameraEncoderContract(level13Capture, portraitSettings, "JPEG")),
  [288, 352, 396, 10],
);
const encoderProbes = [];
class ProbeEncoder {{
  static async isConfigSupported(config) {{
    encoderProbes.push([config.hardwareAcceleration || null, config.latencyMode]);
    return {{ supported: true, config }};
  }}
}}
const encoderPreference = new Video();
const hardwareConfig = await encoderPreference._supportedConfig(
  ProbeEncoder,
  {{ codec: "avc1.42C00D" }},
  true,
);
assert.deepEqual(encoderProbes, [["prefer-hardware", "realtime"]]);
assert.equal(hardwareConfig.hardwareAcceleration, "prefer-hardware");
assert.equal(hardwareConfig.latencyMode, "realtime");
const fallbackProbes = [];
class SoftwareFallbackEncoder {{
  static async isConfigSupported(config) {{
    fallbackProbes.push([config.hardwareAcceleration || null, config.latencyMode]);
    return {{ supported: config.hardwareAcceleration === "prefer-software", config }};
  }}
}}
const softwareConfig = await encoderPreference._supportedConfig(
  SoftwareFallbackEncoder,
  {{ codec: "avc1.42C00D" }},
  true,
);
assert.deepEqual(fallbackProbes, [
  ["prefer-hardware", "realtime"],
  ["prefer-software", "quality"],
]);
assert.equal(softwareConfig.hardwareAcceleration, "prefer-software");
assert.equal(softwareConfig.latencyMode, "quality");

// Camera intent comes from the logical-phone snapshot, not localStorage.
const preferences = new Video();
assert.equal(preferences.cameraEnabled, false);
assert.equal(cameraStorage.size, 0);
// Reconciliation can project the same receive-only camera preference after
// every engine state event. It must be a true no-op or it feeds another state
// event back into reconciliation and spins the HA main thread.
let receiveOnlyEvents = 0;
preferences.addEventListener("state", () => receiveOnlyEvents++);
await preferences.setCameraEnabled(false);
assert.equal(receiveOnlyEvents, 0);
preferences.close = async () => {{}};
preferences._wsUrl = async () => "/signed";
// start() projects send_video before any camera sender can be prepared.
const originalWebSocket = context.WebSocket;
context.window = {{
  isSecureContext: true,
  location: {{ protocol: "https:", host: "ha.example" }},
  setTimeout,
}};
context.WebSocket = class {{
  static OPEN = 1;
  constructor() {{ throw new Error("stop after state projection"); }}
}};
await assert.rejects(
  preferences.start({{
    call_id: "snapshot-call",
    endpoint_id: "kitchen",
    video_active: true,
    send_video: true,
  }}),
);
assert.equal(preferences.cameraEnabled, true);
context.WebSocket = originalWebSocket;

// RFC 6184 Main/High streams can arrive in decode order with non-monotonic
// presentation timestamps (I00, R03, N01, N02). Preserve those timestamps.
const timestamps = new Video();
timestamps._clockRate = 90000;
assert.deepEqual(
  [0, 3000, 1000, 2000].map((value) => timestamps._unwrapRtpTimestamp(value)),
  [0, 33333, 11111, 22222],
);

// Decoder output without a currently-owned canvas is a dropped frame, not a
// rendered frame. This keeps debug counters truthful during card handoff.
const noCanvas = new Video();
noCanvas._active = true;
noCanvas._canReceive = true;
assert.equal(noCanvas.visible, false);
let noCanvasClosed = false;
noCanvas._queueDecodedFrame({{
  timestamp: 1,
  displayWidth: 1,
  displayHeight: 1,
  close() {{ noCanvasClosed = true; }},
}});
assert.equal(noCanvasClosed, true);
assert.equal(noCanvas._stats.rendered, 0);
assert.equal(noCanvas._stats.dropped, 1);
assert.equal(noCanvas._stats.dropped_no_canvas, 1);
noCanvas._stats.rendered = 1;
assert.equal(noCanvas.visible, true);
const wrapped = new Video();
wrapped._clockRate = 90000;
assert.deepEqual(
  [0xffffff00, 0x100].map((value) => wrapped._unwrapRtpTimestamp(value)),
  [0, 5689],
);

// Losing a keyframe to WebSocket backpressure invalidates its dependent GOP.
// Deltas stay blocked until a later keyframe is actually sent.
const sender = new Video();
const sent = [];
sender._clockRate = 90000;
sender._ws = {{ readyState: 1, bufferedAmount: 3 * 1024 * 1024, send(value) {{ sent.push(value); }} }};
const chunk = (type) => ({{
  type,
  byteLength: 2,
  timestamp: 0,
  copyTo(target) {{ target.set([1, 2]); }},
}});
sender._sendEncodedChunk(chunk("key"));
assert.equal(sender._sendDropUntilKeyFrame, true);
sender._ws.bufferedAmount = 0;
sender._sendEncodedChunk(chunk("delta"));
assert.equal(sent.length, 0);
sender._sendEncodedChunk(chunk("key"));
assert.equal(sent.length, 1);
assert.equal(sender._sendDropUntilKeyFrame, false);

// A synchronous decoder failure must re-enter keyframe acquisition and emit
// an RTCP feedback request through the media WebSocket.
const receiver = new Video();
const controls = [];
receiver._clockRate = 90000;
receiver._ws = {{ readyState: 1, send(value) {{ controls.push(value); }} }};
receiver._decoder = {{
  state: "configured",
  decodeQueueSize: 0,
  decode() {{ throw new Error("decoder rejected frame"); }},
}};
const frame = new ArrayBuffer(8);
const bytes = new Uint8Array(frame);
bytes[0] = 1;
bytes[1] = 1;
new DataView(frame).setUint32(2, 9000, false);
receiver._decodeMessage(frame);
assert.equal(receiver._stats.decode_errors, 1);
assert.equal(receiver._dropUntilKeyFrame, true);
assert.equal(JSON.parse(controls.at(-1)).type, "request_key_frame");

// Codec setup without a buffered key frame must retain the resync gate. A
// later delta cannot be treated as independently decodable.
const emptyFlush = new Video();
emptyFlush._dropUntilKeyFrame = true;
emptyFlush._flushPendingDecode();
assert.equal(emptyFlush._dropUntilKeyFrame, true);

// Remote RTCP PLI/FIR control must force the next camera encode to be a key
// frame, while unrelated JSON remains available to the negotiation handler.
const camera = new Video();
camera._forceCameraKeyFrame = false;
assert.equal(camera._handleEncoderControl({{ type: "force_key_frame", feedback: "pli" }}), true);
assert.equal(camera._forceCameraKeyFrame, true);
assert.equal(camera._handleEncoderControl({{ type: "media_update" }}), false);
const epochs = [];
camera._ws = {{ readyState: 1, send(value) {{ epochs.push(JSON.parse(value)); }} }};
camera._sendTxEpoch();
assert.equal(epochs.at(-1).type, "tx_epoch");

// A JPEG worker reply from call A must not clear call B's in-flight decode or
// consume the frame that B coalesced behind it.
const jpeg = new Video();
jpeg._encoding = "JPEG";
jpeg._active = true;
jpeg._generation = 1;
const jpegPostsA = [];
const jpegWorkerA = {{
  postMessage(message, transfer = []) {{ jpegPostsA.push([message, transfer]); }},
}};
jpeg._decoderWorker = jpegWorkerA;
jpeg._decoder = {{
  state: "configured",
  kind: "jpeg",
  worker: jpegWorkerA,
}};
const jpegFrame = (timestamp) => {{
  const value = new ArrayBuffer(8);
  const view = new Uint8Array(value);
  view[0] = 1;
  view[1] = 1;
  new DataView(value).setUint32(2, timestamp, false);
  view[6] = 0xff;
  view[7] = 0xd8;
  return value;
}};
const frameA = jpegFrame(1);
const frameB = jpegFrame(2);
const frameBLatest = jpegFrame(3);
jpeg._decodeJpegMessage(frameA);
assert.equal(jpegPostsA.length, 1);
jpeg._generation = 2;
jpeg._jpegDecodePending = false;
jpeg._jpegQueuedBuffer = null;
jpeg._jpegDecodeToken = null;
const jpegPostsB = [];
const jpegWorkerB = {{
  postMessage(message, transfer = []) {{ jpegPostsB.push([message, transfer]); }},
}};
jpeg._decoderWorker = jpegWorkerB;
jpeg._decoder = {{
  state: "configured",
  kind: "jpeg",
  worker: jpegWorkerB,
}};
jpeg._decodeJpegMessage(frameB);
jpeg._decodeJpegMessage(frameBLatest);
assert.equal(jpegPostsB.length, 1);
let staleBitmapClosed = false;
jpeg._handleDecoderWorkerMessage(jpegWorkerA, 1, {{
  type: "jpeg_bitmap",
  generation: 1,
  timestamp: 1,
  bitmap: {{ width: 1, height: 1, close() {{ staleBitmapClosed = true; }} }},
}});
assert.equal(staleBitmapClosed, true);
assert.equal(jpeg._jpegDecodePending, true);
assert.equal(jpeg._jpegQueuedBuffer, frameBLatest);
jpeg._handleDecoderWorkerMessage(jpegWorkerB, 2, {{
  type: "jpeg_bitmap",
  generation: 2,
  timestamp: 2,
  bitmap: {{ width: 1, height: 1, close() {{}} }},
}});
assert.equal(jpegPostsB.length, 2);

// JPEG decoder errors are coalesced by the worker. The cumulative count keeps
// the public counter exact without causing one card render per malformed AU.
let jpegErrorEvents = 0;
jpeg.addEventListener("state", () => {{ jpegErrorEvents++; }});
jpeg._handleDecoderWorkerMessage(jpegWorkerB, 2, {{
  type: "jpeg_decoder_error",
  generation: 2,
  error_count: 1,
  error: "bad jpeg",
}});
jpeg._handleDecoderWorkerMessage(jpegWorkerB, 2, {{
  type: "jpeg_decoder_error",
  generation: 2,
  error_count: 5,
  error: "bad jpeg",
}});
assert.equal(jpeg._stats.decode_errors, 5);
assert.equal(jpegErrorEvents, 2);
jpeg._handleDecoderWorkerMessage(jpegWorkerB, 2, {{
  type: "jpeg_decoder_error",
  generation: 2,
  error_count: 5,
  error: "duplicate report",
}});
assert.equal(jpeg._stats.decode_errors, 5);
assert.equal(jpegErrorEvents, 2);

// A media update blocked in call A must not head-of-line block call B after
// close/start ownership changes.
const updates = new Video();
let releaseOldUpdate;
const oldUpdate = new Promise((resolve) => {{ releaseOldUpdate = resolve; }});
const applied = [];
updates._applyMediaUpdate = async (payload) => {{
  if (payload.id === "A") await oldUpdate;
  applied.push(payload.id);
}};
const wsA = {{ readyState: 3 }};
updates._ws = wsA;
updates._callId = "A";
updates._enqueueMediaUpdate({{ id: "A" }}, wsA, "A");
await Promise.resolve();
await updates.close();
const wsB = {{ readyState: 1 }};
updates._ws = wsB;
updates._callId = "B";
updates._enqueueMediaUpdate({{ id: "B" }}, wsB, "B");
await Promise.resolve();
await Promise.resolve();
assert.deepEqual(applied, ["B"]);
releaseOldUpdate();
await Promise.resolve();

// A same-call media update starts a fresh RTP timestamp epoch. Reset both the
// unwrap state and render/decode monotonic guards or the first new frame can
// be discarded forever as a timestamp regression.
const updateEpoch = new Video();
const updateWs = {{ readyState: 1 }};
updateEpoch._ws = updateWs;
updateEpoch._callId = "epoch";
updateEpoch._lastRenderedAt = 10;
updateEpoch._lastRenderedTimestamp = 999999;
updateEpoch._lastDecodedAt = 10;
updateEpoch._lastDecodedTimestamp = 999999;
updateEpoch._cleanupSender = async () => {{}};
updateEpoch._cleanupReceiver = () => {{}};
updateEpoch._setupCodecs = async () => {{}};
await updateEpoch._applyMediaUpdate({{
  encoding: "H264",
  clock_rate: 90000,
  can_receive: false,
  can_send: false,
}}, updateWs, "epoch");
assert.equal(updateEpoch._lastRenderedAt, 0);
assert.equal(updateEpoch._lastRenderedTimestamp, null);
assert.equal(updateEpoch._lastDecodedAt, 0);
assert.equal(updateEpoch._lastDecodedTimestamp, null);

// A direct/transcode topology change must release the old server-side owner
// and reconnect. Reconfiguring codecs on the same WebSocket would leave its
// FFmpeg process, loopback queue, and receive task bound to the old SDP.
const pipelineRestart = new Video();
const pipelineWs = {{ readyState: 1 }};
pipelineRestart._ws = pipelineWs;
pipelineRestart._callId = "pipeline";
const restartCalls = [];
pipelineRestart.close = async function() {{
  restartCalls.push("close");
  this._generation++;
  this._ws = null;
  this._callId = "";
}};
pipelineRestart.start = async function(payload) {{
  restartCalls.push(["start", payload]);
  return true;
}};
assert.equal(await pipelineRestart._applyMediaUpdate({{
  type: "media_update",
  restart_required: true,
  restart_reason: "video_pipeline_changed",
  call_id: "pipeline",
  encoding: "VP8",
}}, pipelineWs, "pipeline"), true);
assert.equal(restartCalls[0], "close");
assert.equal(restartCalls[1][0], "start");
assert.equal(restartCalls[1][1].call_id, "pipeline");
assert.equal(restartCalls[1][1].video_active, true);

// A newer owner that starts while cleanup is pending wins; a stale pipeline
// restart must never tear it down or resurrect the preceding call.
const staleRestart = new Video();
const staleRestartWs = {{ readyState: 1 }};
staleRestart._ws = staleRestartWs;
staleRestart._callId = "old";
let releaseRestartClose;
staleRestart.close = async function() {{
  this._generation++;
  this._ws = null;
  this._callId = "";
  await new Promise((resolve) => {{ releaseRestartClose = resolve; }});
}};
let staleRestartStarted = false;
staleRestart.start = async () => {{ staleRestartStarted = true; }};
const staleRestartPromise = staleRestart._applyMediaUpdate({{
  restart_required: true,
}}, staleRestartWs, "old");
while (!releaseRestartClose) await Promise.resolve();
staleRestart._generation++;
staleRestart._callId = "new";
releaseRestartClose();
await staleRestartPromise;
assert.equal(staleRestartStarted, false);
assert.equal(staleRestart._callId, "new");

// A closed call waiting for auth/sign_path must not create a WebSocket when
// the stale signing Promise eventually resolves.
const constructedSockets = [];
class TestWebSocket {{
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 3;
  constructor(url) {{
    this.url = url;
    this.readyState = TestWebSocket.CONNECTING;
    this.bufferedAmount = 0;
    constructedSockets.push(this);
  }}
  close() {{ this.readyState = TestWebSocket.CLOSED; }}
  send() {{}}
}}
context.WebSocket = TestWebSocket;
context.window = {{
  isSecureContext: true,
  location: {{ protocol: "https:", host: "ha.example" }},
  setTimeout,
}};
const videoSignedPaths = [];
const signedVideo = new Video();
signedVideo.configure({{
  callWS: async (msg) => {{
    videoSignedPaths.push(msg.path);
    return {{ path: msg.path }};
  }},
}}, "tab-0123456789abcdef");
await signedVideo._wsUrl("signed-video-call");
assert.equal(
  new URL(`https://ha.example${{videoSignedPaths[0]}}`).searchParams.get("client_id"),
  "tab-0123456789abcdef",
);
let resolveSignedPath;
const staleStart = new Video();
staleStart.configure({{
  callWS() {{
    return new Promise((resolve) => {{ resolveSignedPath = resolve; }});
  }},
}});
const staleStartPromise = staleStart.start({{ call_id: "A", video_active: true }});
while (!resolveSignedPath) await Promise.resolve();
await staleStart.close();
resolveSignedPath({{ path: "/signed-A" }});
assert.equal(await staleStartPromise, false);
assert.equal(constructedSockets.length, 0);

// Closing a CONNECTING socket rejects the open wait; start() must settle
// immediately instead of hanging until its three-second hello timeout.
const connecting = new Video();
connecting.configure({{ callWS: async () => ({{ path: "/signed-B" }}) }});
const connectingStart = connecting.start({{ call_id: "B", video_active: true }});
while (!constructedSockets.length) await Promise.resolve();
const socketB = constructedSockets.at(-1);
socketB.readyState = TestWebSocket.CLOSED;
socketB.onclose();
assert.equal(await connectingStart, false);

// Two camera-enable actions share one setup. They must not prompt twice or
// publish two encoders for the same dialog generation.
const cameraRace = new Video();
cameraRace._generation = 7;
cameraRace._active = true;
cameraRace._cameraAllowed = true;
cameraRace._negotiated = {{ codec: "vp8" }};
let cameraSetups = 0;
let releaseCameraSetup;
const cameraSetupGate = new Promise((resolve) => {{ releaseCameraSetup = resolve; }});
cameraRace._setupEncoder = async () => {{
  cameraSetups++;
  await cameraSetupGate;
  cameraRace._encoder = {{ state: "configured" }};
}};
const enableOne = cameraRace.setCameraEnabled(true);
const enableTwo = cameraRace.setCameraEnabled(true);
await Promise.resolve();
assert.equal(cameraSetups, 1);
releaseCameraSetup();
await Promise.all([enableOne, enableTwo]);
assert.equal(cameraRace._canSend, true);

// Cleanup from call A detaches its resources synchronously. If cancellation
// finishes after call B has published a new pipeline, it must not null/close B.
const ownership = new Video();
let releaseReaderCancel;
const oldReader = {{
  cancel() {{ return new Promise((resolve) => {{ releaseReaderCancel = resolve; }}); }},
}};
const oldEncoder = {{ state: "configured", close() {{ this.state = "closed"; }} }};
const oldTrack = {{ stopped: false, stop() {{ this.stopped = true; }} }};
ownership._cameraReader = oldReader;
ownership._encoder = oldEncoder;
ownership._cameraStream = {{ getTracks() {{ return [oldTrack]; }} }};
ownership._encodeTask = Promise.resolve();
const oldCleanup = ownership._cleanupSender();
assert.equal(ownership._cameraReader, null);
const newReader = {{ cancel() {{ throw new Error("must remain owned by B"); }} }};
const newEncoder = {{ state: "configured", close() {{ throw new Error("must remain open"); }} }};
const newStream = {{ getTracks() {{ return []; }} }};
ownership._cameraReader = newReader;
ownership._encoder = newEncoder;
ownership._cameraStream = newStream;
releaseReaderCancel();
await oldCleanup;
assert.equal(ownership._cameraReader, newReader);
assert.equal(ownership._encoder, newEncoder);
assert.equal(ownership._cameraStream, newStream);
assert.equal(oldEncoder.state, "closed");
assert.equal(oldTrack.stopped, true);

// A browser implementation that never settles reader.cancel() must not hold
// call teardown forever. Published media ownership is removed synchronously
// and the one-shot cleanup deadline releases the caller.
const stuckCleanup = new Video();
stuckCleanup._cameraReader = {{ cancel: () => new Promise(() => {{}}) }};
stuckCleanup._encodeTask = new Promise(() => {{}});
stuckCleanup._encoder = {{ state: "configured", close() {{ this.state = "closed"; }} }};
stuckCleanup._cameraStream = {{ getTracks: () => [{{ stop() {{}} }}] }};
const stuckStarted = performance.now();
await stuckCleanup._cleanupSender();
assert.ok(performance.now() - stuckStarted < 1000);
assert.equal(stuckCleanup._cameraReader, null);
assert.equal(stuckCleanup._encoder, null);

// Unexpected camera EOF releases the published sender immediately instead
// of leaving the card in a false video-transmitting state.
const cameraEof = new Video();
cameraEof._generation = 3;
cameraEof._senderGeneration = 4;
cameraEof._cameraEnabled = true;
cameraEof._cameraAllowed = true;
cameraEof._canSend = true;
const eofReader = {{ read: async () => ({{ done: true }}) }};
const eofEncoder = {{ state: "configured", encodeQueueSize: 0, close() {{ this.state = "closed"; }} }};
let eofTrackStopped = 0;
cameraEof._cameraReader = eofReader;
cameraEof._encoder = eofEncoder;
cameraEof._cameraStream = {{ getTracks: () => [{{ stop() {{ eofTrackStopped++; }} }}] }};
const eofTask = cameraEof._encodeCamera(15, eofReader, eofEncoder, 3, 4);
cameraEof._encodeTask = eofTask;
await eofTask;
assert.equal(cameraEof._cameraReader, null);
assert.equal(cameraEof._encoder, null);
assert.equal(cameraEof._cameraStream, null);
assert.equal(cameraEof._canSend, false);
assert.equal(eofEncoder.state, "closed");
assert.equal(eofTrackStopped, 1);

// Camera acquisition must obey the receive envelope advertised by the peer.
// The former fixed 640x360 request violated H.264 Level 1.3 (MaxFS 396).
const h264Low = new Video();
h264Low._encoding = "H264";
h264Low._negotiated = {{
  codec: "avc1.42800D",
  profile_level_id: "42800d",
  fmtp: "profile-level-id=42800d;packetization-mode=1",
}};
const h264LowContract = h264Low._cameraCaptureContract();
assert.equal(h264LowContract.maxFs, 396);
assert.deepEqual(
  [h264LowContract.maxWidth, h264LowContract.maxHeight],
  [352, 288],
);
assert.equal(h264LowContract.maxFr, 20);

const h264OneB = new Video();
h264OneB._encoding = "H264";
h264OneB._negotiated = {{
  codec: "avc1.42B00B",
  profile_level_id: "42b00b",
}};
const h264OneBContract = h264OneB._cameraCaptureContract();
assert.equal(h264OneBContract.maxFs, 99);
assert.deepEqual(
  [h264OneBContract.maxWidth, h264OneBContract.maxHeight],
  [176, 144],
);
assert.equal(h264OneBContract.maxFr, 15);

const h264Default = new Video();
h264Default._encoding = "H264";
h264Default._negotiated = {{
  codec: "avc1.42801F",
  profile_level_id: "42801f",
}};
const h264DefaultContract = h264Default._cameraCaptureContract();
assert.deepEqual(
  [h264DefaultContract.idealWidth, h264DefaultContract.idealHeight],
  [640, 360],
);
assert.deepEqual(
  [h264DefaultContract.maxWidth, h264DefaultContract.maxHeight],
  [1280, 720],
);

const vp8Limited = new Video();
vp8Limited._encoding = "VP8";
vp8Limited._negotiated = {{ fmtp: "max-fr=12;max-fs=1200" }};
const vp8Contract = vp8Limited._cameraCaptureContract();
assert.deepEqual(
  [vp8Contract.maxWidth, vp8Contract.maxHeight, vp8Contract.maxFr],
  [640, 360, 12],
);

const jpegLimited = new Video();
jpegLimited._encoding = "JPEG";
jpegLimited._negotiated = {{
  send: {{
    codec: "jpeg",
    encoding: "JPEG",
    max_framerate: 3,
  }},
  receive: {{
    codec: "jpeg",
    encoding: "JPEG",
  }},
}};
const jpegContract = jpegLimited._cameraCaptureContract();
assert.equal(jpegContract.maxFr, 3);
assert.deepEqual(
  [jpegContract.constraints.frameRate.ideal, jpegContract.constraints.frameRate.max],
  [3, 3],
);

// Decoder output/error callbacks are owned by the generation that created
// them. A delayed callback from A closes its frame and cannot mutate B.
class TestDecoder {{
  static async isConfigSupported(config) {{ return {{ supported: true, config }}; }}
  constructor(init) {{ this.init = init; this.state = "unconfigured"; this.decodeQueueSize = 0; }}
  configure() {{ this.state = "configured"; }}
  close() {{ this.state = "closed"; }}
}}
context.VideoDecoder = TestDecoder;

// Current HA/Companion builds decode in a DedicatedWorker. Codec output is
// event-driven and teardown terminates the worker synchronously.
const workerMessages = [];
class TestWorker {{
  constructor(url, options) {{
    this.url = url;
    this.options = options;
    this.terminated = false;
  }}
  postMessage(message, transfer = []) {{
    workerMessages.push([message, transfer]);
    if (message.type === "configure_decoder") {{
      queueMicrotask(() => this.onmessage({{
        data: {{ type: "reply", requestId: message.requestId, ok: true }},
      }}));
    }}
  }}
  terminate() {{ this.terminated = true; }}
}}
context.Worker = TestWorker;
const workerDecoder = new Video();
workerDecoder._codecWorkerUrl = () => "/worker.js";
workerDecoder._generation = 21;
await workerDecoder._setupDecoder("avc1.42801F", 21);
const decoderWorker = workerDecoder._decoderWorker;
assert.equal(workerDecoder._decoder.worker, decoderWorker);
assert.equal(decoderWorker.options.type, "module");
workerDecoder._dropUntilKeyFrame = false;
const workerFrame = new ArrayBuffer(8);
new Uint8Array(workerFrame).set([1, 1]);
new DataView(workerFrame).setUint32(2, 9000, false);
workerDecoder._decodeMessage(workerFrame);
assert.equal(workerMessages.at(-1)[0].type, "decode");
assert.equal(workerMessages.at(-1)[1][0], workerFrame);
workerDecoder._cleanupReceiver();
assert.equal(decoderWorker.terminated, true);

// Rendering is demand-driven by decoded frames and coalesces a burst to the
// newest frame. No timer wakes up merely to ask whether a frame exists.
let renderCallback;
context.requestAnimationFrame = (callback) => {{
  renderCallback = callback;
  return 7;
}};
const rendered = new Video();
let draws = 0;
let firstClosed = 0;
let secondClosed = 0;
rendered._canvas = {{ getContext: () => ({{ drawImage() {{ draws++; }} }}) }};
rendered._queueDecodedFrame({{
  timestamp: 1, displayWidth: 1, displayHeight: 1,
  close() {{ firstClosed++; }},
}});
rendered._queueDecodedFrame({{
  timestamp: 2, displayWidth: 1, displayHeight: 1,
  close() {{ secondClosed++; }},
}});
assert.equal(firstClosed, 1);
assert.equal(draws, 0);
renderCallback();
assert.equal(draws, 1);
assert.equal(secondClosed, 1);
assert.equal(rendered._stats.rendered, 1);
assert.equal(rendered._stats.dropped_render_coalesce, 1);

context.Worker = undefined;
const decoderOwner = new Video();
decoderOwner._generation = 11;
decoderOwner._encoding = "H264";
await decoderOwner._setupCodecs({{
  codec: "avc1.42E01F",
  can_receive: true,
  can_send: false,
}}, 11);
const decoderA = decoderOwner._decoder;
decoderOwner._generation = 12;
decoderOwner._decoder = {{ state: "configured" }};
let staleFrameClosed = false;
decoderA.init.output({{ close() {{ staleFrameClosed = true; }} }});
decoderA.init.error();
assert.equal(staleFrameClosed, true);
assert.equal(decoderOwner._stats.rendered, 0);
assert.equal(decoderOwner._stats.decode_errors, 0);
"""
    completed = subprocess.run(
        [
            "node",
            "--no-warnings",
            "--experimental-vm-modules",
            "--input-type=module",
            "-e",
            script,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_video_engine_directional_media_contracts() -> None:
    script = f"""
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";

const source = fs.readFileSync({json.dumps(str(VIDEO_ENGINE))}, "utf8");
const modelSource = fs.readFileSync({json.dumps(str(VIDEO_MODEL))}, "utf8");
const context = vm.createContext({{
  EventTarget,
  performance,
  console,
  Blob,
  CustomEvent: class CustomEvent extends Event {{
    constructor(type, init) {{ super(type); this.detail = init?.detail; }}
  }},
  localStorage: {{ getItem() {{ return null; }}, setItem() {{}} }},
  WebSocket: {{ OPEN: 1, CONNECTING: 0 }},
  EncodedVideoChunk: class EncodedVideoChunk {{ constructor(init) {{ Object.assign(this, init); }} }},
}});
const modelModule = new vm.SourceTextModule(modelSource, {{ context }});
const module = new vm.SourceTextModule(source, {{ context }});
await module.link((specifier) => {{
  if (specifier === "./voip-stack-video-model.js?v=2") return modelModule;
  throw new Error(`unexpected import: ${{specifier}}`);
}});
await module.evaluate();
const Video = module.namespace.VoipStackVideo;

const asymmetric = {{
  can_send: true,
  can_receive: true,
  // Flat fields deliberately describe RX for compatibility with old cards.
  codec: "avc1.64001F",
  encoding: "H264",
  clock_rate: 90000,
  payload_type: 121,
  fmtp: "profile-level-id=64001f;packetization-mode=1",
  profile_level_id: "64001f",
  packetization_mode: 1,
  send: {{
    codec: "avc1.42800D",
    encoding: "H264",
    clock_rate: 45000,
    payload_type: 103,
    fmtp: "profile-level-id=42800d;packetization-mode=1;max-fs=396",
    profile_level_id: "42800d",
    packetization_mode: 1,
    format: "pt=103:H264/45000",
  }},
  receive: {{
    codec: "avc1.64001F",
    encoding: "H264",
    clock_rate: 90000,
    payload_type: 121,
    fmtp: "profile-level-id=64001f;packetization-mode=1",
    profile_level_id: "64001f",
    packetization_mode: 1,
    format: "pt=121:H264/90000",
  }},
}};

// The nested directional objects win over legacy flat RX aliases, including
// PT and clock. The browser itself does not packetize RTP, but exposing the
// negotiated PTs in state makes diagnostics verify the backend contract.
const media = new Video();
media._negotiated = asymmetric;
media._updateLegacyMediaAliases(asymmetric);
assert.deepEqual(
  [media._mediaContract("send").codec, media._mediaContract("send").clockRate,
    media._mediaContract("send").payloadType],
  ["avc1.42800D", 45000, 103],
);
assert.deepEqual(
  [media._mediaContract("receive").codec, media._mediaContract("receive").clockRate,
    media._mediaContract("receive").payloadType],
  ["avc1.64001F", 90000, 121],
);
let emitted;
media.addEventListener("state", (event) => {{ emitted = event.detail; }});
media._emit();
assert.deepEqual(
  [emitted.send_encoding, emitted.receive_encoding,
    emitted.send_clock_rate, emitted.receive_clock_rate,
    emitted.send_payload_type, emitted.receive_payload_type],
  ["H264", "H264", 45000, 90000, 103, 121],
);

// TX access-unit timestamps use the sender clock; RX timestamp unwrapping
// independently uses the decoder clock.
const wire = [];
media._ws = {{ readyState: 1, bufferedAmount: 0, send(value) {{ wire.push(value); }} }};
media._sendEncodedChunk({{
  type: "key",
  byteLength: 2,
  timestamp: 1000000,
  copyTo(target) {{ target.set([1, 2]); }},
}});
assert.equal(new DataView(wire.at(-1).buffer).getUint32(2, false), 45000);
assert.deepEqual(
  [0, 9000].map((value) => media._unwrapRtpTimestamp(value)),
  [0, 100000],
);

// H.264 level asymmetry is directional: camera constraints follow TX level
// 1.3 even though the decoder may accept High Profile Level 3.1.
const capture = media._cameraCaptureContract();
assert.equal(capture.maxFs, 396);
assert.deepEqual([capture.maxWidth, capture.maxHeight], [352, 288]);

// Codec setup probes/configures the decoder with RX and the encoder with TX.
const decoderConfigs = [];
class TestDecoder {{
  static async isConfigSupported(config) {{
    decoderConfigs.push(config);
    return {{ supported: true, config }};
  }}
  constructor(init) {{ this.init = init; this.state = "unconfigured"; this.decodeQueueSize = 0; }}
  configure(config) {{ this.config = config; this.state = "configured"; }}
  close() {{ this.state = "closed"; }}
}}
context.VideoDecoder = TestDecoder;
const codecs = new Video();
codecs._generation = 7;
codecs._cameraEnabled = true;
codecs._negotiated = asymmetric;
codecs._updateLegacyMediaAliases(asymmetric);
const senderCodecs = [];
codecs._ensureSender = async (codec) => {{
  senderCodecs.push(codec);
  codecs._encoder = {{ state: "configured" }};
}};
await codecs._setupCodecs(asymmetric, 7);
await Promise.resolve();
assert.equal(decoderConfigs.at(-1).codec, "avc1.64001F");
assert.deepEqual(senderCodecs, ["avc1.42800D"]);
assert.equal(codecs._canReceive, true);
assert.equal(codecs._canSend, true);

// Decoder dispatch follows RX encoding even when the camera sends a different
// codec. This also covers the direct JPEG browser path.
const splitEncoding = new Video();
splitEncoding._negotiated = {{
  can_send: true,
  can_receive: true,
  send: {{ codec: "vp8", encoding: "VP8", clock_rate: 90000, payload_type: 104 }},
  receive: {{ codec: "jpeg", encoding: "JPEG", clock_rate: 90000, payload_type: 26 }},
}};
let jpegCalls = 0;
splitEncoding._decodeJpegMessage = () => {{ jpegCalls++; }};
splitEncoding._decodeMessage(new ArrayBuffer(8));
assert.equal(jpegCalls, 1);
assert.equal(splitEncoding._mediaContract("send").encoding, "VP8");

// Hold/resume media_update retains the exact directional codec contract. A
// temporary can_send=false must not substitute RX codec/constraints into TX.
const updates = new Video();
const updateWs = {{ readyState: 1 }};
updates._ws = updateWs;
updates._callId = "hold-resume";
updates._cleanupSender = async function() {{ this._canSend = false; }};
updates._cleanupReceiver = function() {{ this._canReceive = false; }};
const applied = [];
updates._setupCodecs = async function(payload) {{
  applied.push({{
    send: this._mediaContract("send", payload),
    receive: this._mediaContract("receive", payload),
    canSend: payload.can_send,
    canReceive: payload.can_receive,
  }});
  this._cameraAllowed = Boolean(payload.can_send);
  this._canReceive = Boolean(payload.can_receive);
}};
await updates._applyMediaUpdate({{ ...asymmetric, can_send: false }}, updateWs, "hold-resume");
assert.equal(updates.canSend, false);
await updates._applyMediaUpdate({{ ...asymmetric, can_send: true }}, updateWs, "hold-resume");
assert.equal(updates.canSend, true);
assert.deepEqual(applied.map((item) => [
  item.send.codec,
  item.receive.codec,
  item.send.payloadType,
  item.receive.payloadType,
]), [
  ["avc1.42800D", "avc1.64001F", 103, 121],
  ["avc1.42800D", "avc1.64001F", 103, 121],
]);

// Legacy flat payloads remain a symmetric contract for both paths.
const legacy = new Video();
legacy._negotiated = {{
  codec: "vp8",
  encoding: "VP8",
  clock_rate: 90000,
  payload_type: 104,
  fmtp: "max-fr=12;max-fs=1200",
  can_send: true,
  can_receive: true,
}};
assert.deepEqual(legacy._mediaContract("send"), legacy._mediaContract("receive"));
assert.deepEqual(
  [legacy._mediaContract("send").codec, legacy._mediaContract("send").payloadType],
  ["vp8", 104],
);
assert.deepEqual(
  [legacy._cameraCaptureContract().maxWidth, legacy._cameraCaptureContract().maxFr],
  [640, 12],
);
"""
    completed = subprocess.run(
        [
            "node",
            "--no-warnings",
            "--experimental-vm-modules",
            "--input-type=module",
            "-e",
            script,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_jpeg_camera_sender_is_worker_driven_bounded_and_generation_safe() -> None:
    script = f"""
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";

const source = fs.readFileSync({json.dumps(str(VIDEO_ENGINE))}, "utf8");
const modelSource = fs.readFileSync({json.dumps(str(VIDEO_MODEL))}, "utf8");
const context = vm.createContext({{
  EventTarget,
  performance,
  console,
  Blob,
  URL,
  setTimeout,
  clearTimeout,
  CustomEvent: class CustomEvent extends Event {{
    constructor(type, init) {{ super(type); this.detail = init?.detail; }}
  }},
  WebSocket: {{ OPEN: 1, CONNECTING: 0 }},
  EncodedVideoChunk: class EncodedVideoChunk {{ constructor(init) {{ Object.assign(this, init); }} }},
}});
const modelModule = new vm.SourceTextModule(modelSource, {{ context }});
const module = new vm.SourceTextModule(source, {{ context }});
await module.link((specifier) => {{
  if (specifier === "./voip-stack-video-model.js?v=2") return modelModule;
  throw new Error(`unexpected import: ${{specifier}}`);
}});
await module.evaluate();
const Video = module.namespace.VoipStackVideo;

class ControlledReader {{
  constructor() {{
    this.waiters = [];
    this.cancelled = false;
  }}
  read() {{
    if (this.cancelled) return Promise.resolve({{ done: true }});
    return new Promise((resolve) => this.waiters.push(resolve));
  }}
  push(frame) {{
    const resolve = this.waiters.shift();
    assert.ok(resolve, "camera reader must be awaiting the next event-driven frame");
    resolve({{ value: frame, done: false }});
  }}
  cancel() {{
    this.cancelled = true;
    for (const resolve of this.waiters.splice(0)) resolve({{ done: true }});
    return Promise.resolve();
  }}
}}
const reader = new ControlledReader();
const track = {{
  stopped: false,
  getSettings() {{ return {{ width: 640, height: 360, frameRate: 15 }}; }},
  stop() {{ this.stopped = true; }},
}};
context.navigator = {{
  mediaDevices: {{
    async getUserMedia() {{
      return {{
        getVideoTracks: () => [track],
        getTracks: () => [track],
      }};
    }},
  }},
}};
context.MediaStreamTrackProcessor = class {{
  constructor(init) {{
    assert.equal(init.track, track);
    this.readable = {{ getReader: () => reader }};
  }}
}};

const workers = [];
class JpegWorker {{
  constructor(url, options) {{
    this.url = url;
    this.options = options;
    this.messages = [];
    this.terminated = false;
    workers.push(this);
  }}
  postMessage(message, transfer = []) {{
    this.messages.push([message, transfer]);
    if (message.type === "configure_jpeg_encoder") {{
      queueMicrotask(() => this.onmessage({{
        data: {{ type: "reply", requestId: message.requestId, ok: true }},
      }}));
    }}
  }}
  terminate() {{ this.terminated = true; }}
}}
context.Worker = JpegWorker;

const sender = new Video();
sender._codecWorkerUrl = () => "/voip-stack-video-worker.js";
sender._generation = 3;
sender._senderGeneration = 4;
sender._cameraAllowed = true;
sender._cameraEnabled = true;
sender._negotiated = {{
  can_send: true,
  can_receive: false,
  send: {{
    codec: "jpeg",
    encoding: "JPEG",
    clock_rate: 90000,
    payload_type: 26,
  }},
}};
const wire = [];
sender._ws = {{
  readyState: 1,
  bufferedAmount: 0,
  send(value) {{ wire.push(value); }},
}};
await sender._setupEncoder("jpeg", 3, 4);
assert.equal(workers.length, 1);
assert.equal(sender._encoder.kind, "jpeg");
assert.equal(sender._encoder.state, "configured");
assert.equal(workers[0].options.type, "module");
assert.equal(workers[0].messages[0][0].type, "configure_jpeg_encoder");
assert.equal(typeof context.VideoEncoder, "undefined");

const frame = (name, timestamp) => ({{
  name,
  timestamp,
  closed: 0,
  close() {{ this.closed++; }},
}});
const first = frame("first", 1000000);
const middle = frame("middle", 1100000);
const latest = frame("latest", 1200000);
reader.push(first);
await new Promise((resolve) => setImmediate(resolve));
assert.equal(workers[0].messages.at(-1)[0].type, "encode_jpeg");
assert.equal(workers[0].messages.at(-1)[0].frame, first);
assert.equal(workers[0].messages.at(-1)[1].length, 1);
assert.equal(workers[0].messages.at(-1)[1][0], first);

reader.push(middle);
await new Promise((resolve) => setImmediate(resolve));
reader.push(latest);
await new Promise((resolve) => setImmediate(resolve));
assert.equal(middle.closed, 1);
assert.equal(sender._jpegQueuedFrame, latest);
assert.equal(
  workers[0].messages.filter(([message]) => message.type === "encode_jpeg").length,
  1,
);

first.close();
workers[0].onmessage({{
  data: {{
    type: "jpeg_frame",
    generation: 3,
    senderGeneration: 4,
    timestamp: first.timestamp,
    buffer: new Uint8Array([0xff, 0xd8, 0xff, 0xd9]).buffer,
  }},
}});
assert.equal(
  workers[0].messages.filter(([message]) => message.type === "encode_jpeg").length,
  2,
);
assert.equal(workers[0].messages.at(-1)[0].frame, latest);
assert.equal(wire.length, 2); // tx_epoch control followed by one binary AU.
const sent = wire.at(-1);
assert.equal(sent[0], 1);
assert.equal(sent[1], 1);
assert.equal(new DataView(sent.buffer).getUint32(2, false), 90000);
assert.deepEqual([...sent.slice(6)], [0xff, 0xd8, 0xff, 0xd9]);

const cleanup = sender._cleanupSender();
assert.equal(sender._encoder, null);
assert.equal(sender._jpegEncoderWorker, null);
assert.equal(workers[0].terminated, true);
assert.equal(track.stopped, true);
await cleanup;
const sentBeforeStaleCallback = wire.length;
workers[0].onmessage({{
  data: {{
    type: "jpeg_frame",
    generation: 3,
    senderGeneration: 4,
    timestamp: latest.timestamp,
    buffer: new Uint8Array([1]).buffer,
  }},
}});
assert.equal(wire.length, sentBeforeStaleCallback);
"""
    completed = subprocess.run(
        [
            "node",
            "--no-warnings",
            "--experimental-vm-modules",
            "--input-type=module",
            "-e",
            script,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_jpeg_worker_uses_offscreen_canvas_and_transfers_complete_frame() -> None:
    script = f"""
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";

const source = fs.readFileSync({json.dumps(str(VIDEO_WORKER))}, "utf8");
const posted = [];
let closed = false;
let drawCount = 0;
let encodedOptions;
class TestOffscreenCanvas {{
  constructor(width, height) {{
    this.width = width;
    this.height = height;
  }}
  getContext(kind, options) {{
    assert.equal(kind, "2d");
    assert.equal(options.alpha, false);
    return {{
      drawImage(frame, x, y, width, height) {{
        assert.equal(frame.name, "camera-frame");
        assert.deepEqual([x, y, width, height], [0, 0, 320, 240]);
        drawCount++;
      }},
    }};
  }}
  async convertToBlob(options) {{
    encodedOptions = options;
    return new Blob([new Uint8Array([0xff, 0xd8, 0xff, 0xd9])], {{
      type: "image/jpeg",
    }});
  }}
}}
const self = {{
  postMessage(message, transfer = []) {{ posted.push([message, transfer]); }},
  close() {{ closed = true; }},
}};
const context = vm.createContext({{
  self,
  Blob,
  Uint8Array,
  ArrayBuffer,
  Number,
  Math,
  String,
  Error,
  OffscreenCanvas: TestOffscreenCanvas,
}});
vm.runInContext(source, context);
await self.onmessage({{ data: {{
  type: "configure_jpeg_encoder",
  requestId: 7,
  generation: 9,
  width: 640,
  height: 360,
  quality: 0.72,
}} }});
assert.deepEqual(
  [posted[0][0].type, posted[0][0].requestId, posted[0][0].ok],
  ["reply", 7, true],
);

const frame = {{
  name: "camera-frame",
  timestamp: 123,
  displayWidth: 320,
  displayHeight: 240,
  closed: 0,
  close() {{ this.closed++; }},
}};
await self.onmessage({{ data: {{
  type: "encode_jpeg",
  generation: 9,
  senderGeneration: 11,
  timestamp: 123,
  frame,
}} }});
assert.equal(drawCount, 1);
assert.equal(frame.closed, 1);
assert.equal(encodedOptions.type, "image/jpeg");
assert.equal(encodedOptions.quality, 0.72);
const encoded = posted.at(-1);
assert.equal(encoded[0].type, "jpeg_frame");
assert.equal(encoded[0].generation, 9);
assert.equal(encoded[0].senderGeneration, 11);
assert.equal(encoded[0].timestamp, 123);
assert.deepEqual([...new Uint8Array(encoded[0].buffer)], [0xff, 0xd8, 0xff, 0xd9]);
assert.equal(encoded[1].length, 1);
assert.equal(encoded[1][0], encoded[0].buffer);

await self.onmessage({{ data: {{ type: "close" }} }});
assert.equal(closed, true);
"""
    completed = subprocess.run(
        [
            "node",
            "--no-warnings",
            "--input-type=module",
            "-e",
            script,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_jpeg_worker_rejects_oversized_frame_before_browser_decode() -> None:
    script = f"""
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";

const source = fs.readFileSync({json.dumps(str(VIDEO_WORKER))}, "utf8");
const posted = [];
let decodeCalls = 0;
let now = 0;
let nextTimerId = 0;
const timers = new Map();
const self = {{
  postMessage(message, transfer = []) {{ posted.push([message, transfer]); }},
  close() {{}},
}};
const context = vm.createContext({{
  self,
  Blob,
  Uint8Array,
  ArrayBuffer,
  Number,
  Math,
  String,
  Error,
  Set,
  Date,
  performance: {{ now: () => now }},
  setTimeout(callback, delay) {{
    const id = ++nextTimerId;
    timers.set(id, {{ callback, delay }});
    return id;
  }},
  clearTimeout(id) {{ timers.delete(id); }},
  async createImageBitmap() {{
    decodeCalls++;
    return {{ width: 640, height: 480, close() {{}} }};
  }},
}});
vm.runInContext(source, context);
await self.onmessage({{ data: {{
  type: "configure_jpeg_decoder",
  requestId: 1,
  generation: 4,
}} }});

function jpegHeader(width, height) {{
  return new Uint8Array([
    0xff, 0xd8,
    0xff, 0xc0, 0x00, 0x08, 0x08,
    (height >> 8) & 0xff, height & 0xff,
    (width >> 8) & 0xff, width & 0xff,
    0x01,
    0xff, 0xd9,
  ]);
}}

const oversized = jpegHeader(1920, 1080);
await self.onmessage({{ data: {{
  type: "decode_jpeg",
  generation: 4,
  timestamp: 10,
  buffer: oversized.buffer,
  length: oversized.byteLength,
}} }});
assert.equal(decodeCalls, 0);
assert.equal(posted.at(-1)[0].type, "jpeg_decoder_error");
assert.equal(posted.at(-1)[0].error_count, 1);
assert.match(posted.at(-1)[0].error, /rendering budget/);

now = 1;
await self.onmessage({{ data: {{
  type: "decode_jpeg",
  generation: 4,
  timestamp: 10,
  buffer: oversized.buffer,
  length: oversized.byteLength,
}} }});
assert.equal(
  posted.filter(([message]) => message.type === "jpeg_decoder_error").length,
  1,
);
assert.equal(timers.size, 1);
now = 251;
const scheduled = [...timers.values()][0];
timers.clear();
scheduled.callback();
const errorReports = posted.filter(
  ([message]) => message.type === "jpeg_decoder_error",
);
assert.equal(errorReports.length, 2);
assert.equal(errorReports.at(-1)[0].error_count, 2);

const valid = jpegHeader(640, 480);
await self.onmessage({{ data: {{
  type: "decode_jpeg",
  generation: 4,
  timestamp: 11,
  buffer: valid.buffer,
  length: valid.byteLength,
}} }});
assert.equal(decodeCalls, 1);
assert.equal(posted.at(-1)[0].type, "jpeg_bitmap");
assert.equal(posted.at(-1)[0].timestamp, 11);
"""
    completed = subprocess.run(
        [
            "node",
            "--no-warnings",
            "--input-type=module",
            "-e",
            script,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
