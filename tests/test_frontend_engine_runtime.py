#!/usr/bin/env python3
"""Runtime anti-regressions for browser softphone ownership and permission gates."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.js_runtime


ROOT = Path(__file__).resolve().parents[1]
ENGINE = (
    ROOT
    / "custom_components"
    / "voip_stack"
    / "frontend"
    / "voip-stack-engine.js"
)
MEDIA_MODEL = ENGINE.with_name("voip-stack-media-model.js")
SESSION_MODEL = ENGINE.with_name("voip-stack-session-model.js")
CARD_MODEL = ENGINE.with_name("voip-stack-card-model.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_card_config_persists_only_device_selection() -> None:
    script = rf"""
import assert from "assert/strict";
import {{ pathToFileURL }} from "url";

const {{ normaliseCardConfig }} = await import(
  pathToFileURL({json.dumps(str(CARD_MODEL))})
);
const legacy = normaliseCardConfig({{
  mode: "ha_softphone",
  endpoint_id: "default",
}});
assert.deepEqual(legacy.config, {{ mode: "ha_softphone" }});
assert.equal("legacyEndpointId" in legacy, false);

const current = normaliseCardConfig({{
  mode: "ha_softphone",
  endpoint_id: "stale",
  device_id: "device-casa",
}});
assert.deepEqual(current.config, {{
  mode: "ha_softphone",
  device_id: "device-casa",
}});
assert.equal("legacyEndpointId" in current, false);
"""
    subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        cwd=ROOT,
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_softphone_engine_runtime_ownership_and_permission_contracts() -> None:
    script = rf"""
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";
import {{ pathToFileURL }} from "url";

const mediaModel = await import(pathToFileURL({json.dumps(str(MEDIA_MODEL))}));
const sessionModel = await import(pathToFileURL({json.dumps(str(SESSION_MODEL))}));

let source = fs.readFileSync({json.dumps(str(ENGINE))}, "utf8");
source = source.replace(
  /const \{{ RINGTONE_REPEAT_MS, playVoipRingtone \}} =\s*await import\([^;]+;/,
  "const RINGTONE_REPEAT_MS = 4000; const playVoipRingtone = () => {{}};",
).replace(
  /const \{{\s*desiredAudioPaths[\s\S]*?\}} = await import\(`\.\/voip-stack-media-model\.js[^;]+;/,
  `const {{
    desiredAudioPaths, normaliseAudioDirection, normaliseAudioMode,
    parsePcmFormat, resolveSessionFormats, sameAudioFormat,
  }} = globalThis.__mediaModel;`,
).replace(
  /const \{{\s*normaliseSoftphoneSelector[\s\S]*?\}} = await import\(`\.\/voip-stack-session-model\.js[^;]+;/,
  `const {{
    normaliseSoftphoneSelector, softphoneScopeKey, softphoneStateMatches,
  }} = globalThis.__sessionModel;`,
).replace("class VoipStackEngine", "export class VoipStackEngine");

const storage = new Map();
const session = new Map();
const context = vm.createContext({{
  __mediaModel: mediaModel,
  __sessionModel: sessionModel,
  EventTarget,
  Event,
  CustomEvent: class CustomEvent extends Event {{
    constructor(type, init) {{ super(type); this.detail = init?.detail; }}
  }},
  console,
  URL,
  encodeURIComponent,
  localStorage: {{
    getItem(key) {{ return storage.get(key) || null; }},
    setItem(key, value) {{ storage.set(key, String(value)); }},
  }},
  sessionStorage: {{
    getItem(key) {{ return session.get(key) || null; }},
    setItem(key, value) {{ session.set(key, String(value)); }},
    removeItem(key) {{ session.delete(key); }},
  }},
  navigator: {{}},
  AudioWorkletNode: class AudioWorkletNode {{}},
  window: {{
    location: {{ protocol: "https:", host: "ha.example" }},
    isSecureContext: true,
    AudioContext: class AudioContext {{}},
    addEventListener() {{}},
    setInterval,
    clearInterval,
    setTimeout,
    clearTimeout,
  }},
  WebSocket: {{ OPEN: 1, CONNECTING: 0 }},
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
}});
const module = new vm.SourceTextModule(source, {{ context }});
await module.link(() => {{ throw new Error("unexpected import"); }});
await module.evaluate();
const Engine = module.namespace.VoipStackEngine;

// Browser media capability is checked before SIP Answer or Call. Missing
// getUserMedia must fail here, not after a remote dialog has received 200 OK.
const audioPreflight = new Engine();
await assert.rejects(
  audioPreflight.prepareAudioCall(),
  /Microphone access is unavailable/,
);
let audioTrackStops = 0;
context.navigator.mediaDevices = {{
  getUserMedia: async () => {{
    const track = {{ stop() {{ audioTrackStops++; }} }};
    return {{
      getAudioTracks() {{ return [track]; }},
      getTracks() {{ return [track]; }},
    }};
  }},
}};
assert.equal(await audioPreflight.prepareAudioCall(), true);
assert.equal(audioTrackStops, 1);

// sessionStorage is only a reload/handoff hint. A backend snapshot is
// authoritative and must prune a claim whose call already ended, while a
// genuinely active call on another logical phone must continue to block the
// single browser media pipeline.
const staleClaim = new Engine();
staleClaim.claimSoftphoneSession("ended-test-call", "test");
let claimedSnapshot = {{
  endpoint_id: "test",
  call_id: "ended-test-call",
  state: "in_call",
}};
staleClaim._hass = {{
  callWS: async () => claimedSnapshot,
}};
await staleClaim.reconcileOwnedSoftphoneSessions();
assert.equal(
  staleClaim.hasOwnedSoftphoneSessionForOtherEndpoint("default"),
  true,
);
claimedSnapshot = {{
  endpoint_id: "test",
  call_id: "ended-test-call",
  state: "idle",
}};
await staleClaim.reconcileOwnedSoftphoneSessions();
assert.equal(staleClaim.softphoneCallIdFor("test"), "");
assert.equal(
  staleClaim.hasOwnedSoftphoneSessionForOtherEndpoint("default"),
  false,
);
assert.equal(storage.has("voip_stack_owned_softphone_calls"), false);

// A terminal event clears the same stale hint even when the Test card was
// removed before its endpoint-scoped state subscription saw the terminal.
staleClaim.claimSoftphoneSession("cancelled-test-call", "test");
staleClaim._onBusEvent({{
  data: {{
    type: "ended",
    state: "cancelled",
    call_id: "cancelled-test-call",
    endpoint_id: "test",
    scope: "session",
  }},
}});
assert.equal(staleClaim.softphoneCallIdFor("test"), "");

// The backend phone snapshot remains authoritative while a video call is
// already attached: changing Send Video must reconcile the live camera
// sender, not merely affect the next call.
const liveVideoPreference = new Engine();
liveVideoPreference._callId = "live-video-call";
liveVideoPreference._endpointId = "kitchen";
const cameraSelections = [];
liveVideoPreference._video = {{
  active: true,
  callId: "live-video-call",
  setCameraEnabled: async (enabled, endpointId) => {{
    cameraSelections.push([enabled, endpointId]);
  }},
}};
await liveVideoPreference._ensureVideo({{
  video_active: true,
  call_id: "live-video-call",
  endpoint_id: "kitchen",
  send_video: true,
}});
await liveVideoPreference._ensureVideo({{
  video_active: true,
  call_id: "live-video-call",
  endpoint_id: "kitchen",
  send_video: false,
}});
assert.deepEqual(cameraSelections, [[true, "kitchen"], [false, "kitchen"]]);

// Media ownership identity survives a reload of the originating browsing
// context and is part of the Home Assistant signed path. The backend rejects
// every other client ID, including observers racing before the first socket.
const signedPaths = [];
const signingHass = {{
  callWS: async (msg) => {{ signedPaths.push(msg.path); return {{ path: msg.path }}; }},
}};
session.set("voip_stack_media_client_id", "copied-session-token-1234");
context.__voipStackMediaClientId = undefined;
const signedA = new Engine();
signedA.configure(signingHass);
await signedA._wsUrl("device", "signed-call", "phone");
context.__voipStackMediaClientId = undefined;
const signedB = new Engine();
signedB.configure(signingHass);
await signedB._wsUrl("device", "signed-call", "phone");
assert.ok(signedA._mediaClientId.length >= 16);
assert.equal(signedA._mediaClientId, signedB._mediaClientId);
assert.equal(signedA._mediaClientId, "copied-session-token-1234");
assert.equal(
  new URL(`https://ha.example${{signedPaths[0]}}`).searchParams.get("client_id"),
  signedA._mediaClientId,
);
assert.equal(
  new URL(`https://ha.example${{signedPaths[0]}}`).searchParams.get("endpoint_id"),
  "phone",
);
await signedA._wsUrl("kitchen-device", "kitchen-call", "kitchen");
assert.equal(
  new URL(`https://ha.example${{signedPaths.at(-1)}}`).searchParams.get("endpoint_id"),
  "kitchen",
);

// Snapshot replay and delivery are isolated by explicit endpoint identity.
const isolated = new Engine();
const defaultStates = [];
const kitchenStates = [];
const kitchenDeviceStates = [];
isolated.subscribeSoftphoneState((state) => defaultStates.push(state.call_id), {{ endpoint_id: "default" }});
isolated.subscribeSoftphoneState((state) => kitchenStates.push(state.call_id), {{ endpoint_id: "kitchen" }});
isolated.subscribeSoftphoneState(
  (state) => kitchenDeviceStates.push(state.call_id),
  {{ device_id: "kitchen-device" }},
);
const kitchenSnapshot = {{
  endpoint_id: "kitchen",
  device_id: "kitchen-device",
  call_id: "K",
  state: "ringing",
}};
isolated._onSoftphoneState(kitchenSnapshot, {{ endpoint_id: "kitchen" }});
isolated._onSoftphoneState(kitchenSnapshot, {{ device_id: "kitchen-device" }});
isolated._onSoftphoneState({{ call_id: "D", state: "ringing" }}, {{ endpoint_id: "default" }});
assert.deepEqual(kitchenStates, ["K"]);
assert.deepEqual(kitchenDeviceStates, ["K"]);
assert.deepEqual(defaultStates, []);

// Browser media claims and UI controllers are endpoint-local. Two logical
// phones can therefore coexist without one endpoint releasing the other.
isolated.claimSoftphoneSession("call-default", "default");
isolated.claimSoftphoneSession("call-kitchen", "kitchen");
isolated.releaseSoftphoneSession("call-default", "default");
assert.equal(isolated.ownsSoftphoneSession("call-default", "default"), false);
assert.equal(isolated.ownsSoftphoneSession("call-kitchen", "kitchen"), true);
const controllerDefault = {{ isConnected: true }};
const controllerKitchen = {{ isConnected: true }};
assert.equal(isolated.claimSoftphoneController(controllerDefault, "default"), true);
assert.equal(isolated.claimSoftphoneController(controllerKitchen, "kitchen"), true);

// Register the in-flight media attach before its body starts. Engine state
// listeners can synchronously re-enter resumeSession() while _connect() tears
// down an older pipeline; the re-entrant call must join the same promise, not
// start a second setup that supersedes and hangs up the call.
const atomicAttach = new Engine();
let attachRuns = 0;
let reentrantAttach = null;
const attachPayload = {{
  state: "in_call",
  call_id: "local-room-call",
  endpoint_id: "kitchen",
}};
atomicAttach._resumeSessionLocked = async function(deviceInfo, deviceId, payload) {{
  attachRuns++;
  reentrantAttach = this.resumeSession(deviceInfo, deviceId, payload);
  await Promise.resolve();
}};
const initialAttach = atomicAttach.resumeSession(
  {{ endpoint_id: "kitchen", audio_mode: "full_duplex" }},
  "kitchen-device",
  attachPayload,
);
await initialAttach;
await reentrantAttach;
assert.equal(attachRuns, 1);

// HA may publish a canonical device ID shortly after the initial answer. The
// endpoint and Call-ID still identify the same media leg, so metadata churn
// must neither supersede an in-flight attach nor reconnect a healthy socket.
const stableMetadata = new Engine();
stableMetadata._state = "IN_CALL";
stableMetadata._endpointId = "kitchen";
stableMetadata._deviceId = "fallback-device";
stableMetadata._callId = "stable-call";
stableMetadata._audioReady = true;
stableMetadata._ws = {{ readyState: context.WebSocket.OPEN }};
let metadataReconnects = 0;
stableMetadata._setupAudioOrAbort = async () => {{ metadataReconnects++; return true; }};
await stableMetadata.resumeSession(
  {{ endpoint_id: "kitchen" }},
  "canonical-device",
  {{ state: "in_call", endpoint_id: "kitchen", call_id: "stable-call" }},
);
assert.equal(metadataReconnects, 0);
assert.equal(stableMetadata._callId, "stable-call");

// A brand-new page has no sessionStorage claim, but an authoritative in-call
// snapshot may recover that exact endpoint once. A newer call on the same
// logical phone replaces a stale, unattached claim instead of becoming a
// permanent spectator.
const recovered = new Engine();
assert.equal(recovered.ownsSoftphoneSession("fresh-call", "kitchen"), false);
assert.equal(recovered.tryRecoverSoftphoneSession("fresh-call", "kitchen"), true);
assert.equal(recovered.ownsSoftphoneSession("fresh-call", "kitchen"), true);
assert.equal(recovered.tryRecoverSoftphoneSession("replacement-call", "kitchen"), true);
assert.equal(recovered.ownsSoftphoneSession("fresh-call", "kitchen"), false);
assert.equal(recovered.ownsSoftphoneSession("replacement-call", "kitchen"), true);

// A permanently removed endpoint is surfaced once as unavailable and is not
// retried forever by the global subscription timer.
const missing = new Engine();
const missingStates = [];
missing.subscribeSoftphoneState(
  (state) => missingStates.push(state),
  {{ endpoint_id: "removed-phone" }},
);
const missingRecord = missing._softphoneScopeSubscriptions.get("endpoint:removed-phone");
let missingSubscribeAttempts = 0;
let missingRetrySchedules = 0;
const missingConnection = {{
  subscribeMessage: async () => {{
    missingSubscribeAttempts++;
    const error = new Error("Unknown phone endpoint");
    error.code = "unknown_endpoint";
    throw error;
  }},
}};
missing._busConnection = missingConnection;
missing._scheduleBusSubscriptionRetry = () => {{ missingRetrySchedules++; }};
missing._ensureSoftphoneScopeSubscription(missingConnection, missingRecord);
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(missingRecord.invalid, true);
assert.equal(missingSubscribeAttempts, 1);
assert.equal(missingRetrySchedules, 0);
assert.equal(missingStates.at(-1).state, "unavailable");
assert.equal(missingStates.at(-1).terminal_reason, "unknown_endpoint");

// Permission probing is independent from preference storage: camera intent is
// supplied by the authoritative logical-phone snapshot.
let mediaRequests = 0;
context.navigator.permissions = {{ query: async () => ({{ state: "prompt" }}) }};
context.navigator.mediaDevices = {{
  getUserMedia: async () => {{ mediaRequests++; throw new Error("must not prompt"); }},
}};
const permission = new Engine();
assert.equal(
  await permission.prepareVideoCameraPermission({{ persistentOnly: true }}),
  false,
);
assert.equal(mediaRequests, 0);
assert.equal(
  await permission.prepareVideoCameraPermission({{ persistentOnly: true, endpointId: "kitchen" }}),
  false,
);
assert.equal(mediaRequests, 0);

// A granted preflight does not start the camera twice. The negotiated sender
// performs the single real acquisition after the call is established.
let stopped = 0;
context.navigator.permissions = {{ query: async () => ({{ state: "granted" }}) }};
context.navigator.mediaDevices = {{
  getUserMedia: async () => {{
    mediaRequests++;
    const track = {{ stop() {{ stopped++; }} }};
    return {{ getVideoTracks() {{ return [track]; }}, getTracks() {{ return [track]; }} }};
  }},
}};
assert.equal(
  await permission.prepareVideoCameraPermission({{ persistentOnly: true }}),
  true,
);
assert.equal(mediaRequests, 0);
assert.equal(stopped, 0);

// An explicit user action with a prompt-state permission still probes the
// camera, then releases that one temporary stream.
context.navigator.permissions = {{ query: async () => ({{ state: "prompt" }}) }};
assert.equal(
  await permission.prepareVideoCameraPermission({{ persistentOnly: false }}),
  true,
);
assert.equal(mediaRequests, 1);
assert.equal(stopped, 1);

// A rejected WebSocket ownership claim did not attach media, so it must not
// send BYE for the dialog owned by the newer card.
const ownership = new Engine();
const services = [];
const ownershipErrors = [];
ownership._hass = {{ callService: async (...args) => services.push(args) }};
ownership._connect = async () => {{ throw new Error("HTTP 409 owner conflict"); }};
ownership.close = async () => {{}};
ownership._setState = () => {{}};
ownership.addEventListener("error", (event) => ownershipErrors.push(event.detail));
ownership.claimSoftphoneSession("call-A", "default");
assert.equal(
      await ownership._setupAudioOrAbort(
        "phone-device",
        {{ device_id: "phone-device" }},
        {{ call_id: "call-A" }},
        "",
        "default",
  ),
  false,
);
assert.deepEqual(services, []);
assert.equal(ownership.ownsSoftphoneSession("call-A", "default"), false);
assert.equal(ownership._state, "IDLE");
assert.match(ownershipErrors.at(-1), /another tab|could not be attached/i);

// A known owner in another browser makes this card a strict spectator. It
// discards its optimistic local claim and never opens microphone/camera media.
const spectator = new Engine();
let spectatorConnects = 0;
const spectatorErrors = [];
let spectatorOwner = "other";
spectator._hass = {{
  callWS: async () => ({{
    state: "in_call",
    call_id: "spectator-call",
    media_owner: spectatorOwner,
  }}),
}};
spectator._connect = async () => {{
  spectatorConnects++;
  spectator._callId = "spectator-call";
  return {{
    call_id: "spectator-call",
    selected_tx_format: "48000:s16le:1:20",
    selected_rx_format: "48000:s16le:1:20",
    audio_direction: "sendrecv",
  }};
}};
spectator._setupAudio = async () => {{}};
spectator._reconcileAudioMedia = async () => {{}};
spectator.addEventListener("error", (event) => spectatorErrors.push(event.detail));
spectator.claimSoftphoneSession("spectator-call", "test");
assert.equal(
  await spectator._setupAudioOrAbort(
    "test-device",
    {{ endpoint_id: "test" }},
    {{ call_id: "spectator-call" }},
    "",
    "test",
  ),
  false,
);
assert.equal(spectatorConnects, 0);
assert.deepEqual(spectatorErrors, []);
assert.equal(spectator.ownsSoftphoneSession("spectator-call", "test"), false);

// A media attach racing the terminal state is also expected: after BYE there
// is no call left to attach and therefore no user-facing media error.
const terminalRace = new Engine();
const terminalErrors = [];
terminalRace._hass = {{
  callWS: async () => ({{
    state: "idle",
    call_id: "terminal-call",
    media_owner: "available",
  }}),
}};
terminalRace._connect = async () => {{ throw new Error("socket closed during BYE"); }};
terminalRace.close = async () => {{}};
terminalRace.addEventListener("error", (event) => terminalErrors.push(event.detail));
terminalRace.claimSoftphoneSession("terminal-call", "default");
assert.equal(
      await terminalRace._setupAudioOrAbort(
        "phone-device",
        {{ endpoint_id: "default" }},
        {{ call_id: "terminal-call" }},
        "",
        "default",
  ),
  false,
);
assert.deepEqual(terminalErrors, []);
assert.equal(terminalRace.ownsSoftphoneSession("terminal-call", "default"), false);

// A reload can race the old document's socket teardown. An engine that owns
// the exact call retries the bounded media claim; a mirror that never claimed
// the call (the case above) remains fail-fast.
const reconnect = new Engine();
reconnect.claimSoftphoneSession("reload-call", "kitchen");
let reconnectAttempts = 0;
reconnect._connect = async () => {{
  reconnectAttempts++;
  if (reconnectAttempts < 3) throw new Error("HTTP 409 old document closing");
  reconnect._callId = "reload-call";
  return {{
    call_id: "reload-call",
    selected_tx_format: "48000:s16le:1:20",
    selected_rx_format: "48000:s16le:1:20",
    audio_direction: "sendrecv",
  }};
}};
reconnect._setupAudio = async () => {{}};
reconnect._reconcileAudioMedia = async () => {{}};
assert.equal(
  await reconnect._setupAudioOrAbort(
    "kitchen-device",
    {{ endpoint_id: "kitchen" }},
    {{ call_id: "reload-call" }},
    "",
    "kitchen",
  ),
  true,
);
assert.equal(reconnectAttempts, 3);

// Once the WebSocket is open, an actual local audio setup failure makes this
// browser leg unusable and intentionally terminates that exact call.
ownership._connect = async (deviceId, callId, endpointId) => {{
  ownership._deviceId = deviceId;
  ownership._endpointId = endpointId;
  ownership._callId = callId;
}};
ownership._setupAudio = async () => {{ throw new Error("unsupported PCM"); }};
assert.equal(
      await ownership._setupAudioOrAbort(
        "phone-device",
        {{ device_id: "phone-device" }},
        {{ call_id: "call-B" }},
        "",
        "default",
  ),
  false,
);
assert.equal(services.length, 1);
assert.equal(services[0][0], "voip_stack");
assert.equal(services[0][1], "hangup");
assert.equal(services[0][2].call_id, "call-B");

// Canvas ownership is explicit. A mirror/second card cannot silently redirect
// decoded frames away from the current HA softphone card.
const canvas = new Engine();
const ownerA = {{ id: "A" }};
const ownerB = {{ id: "B" }};
const canvasA = {{ id: "canvas-A" }};
const canvasB = {{ id: "canvas-B" }};
assert.equal(canvas.claimVideoCanvas(ownerA, canvasA, "kitchen"), true);
assert.equal(canvas.claimVideoCanvas(ownerB, canvasB, "kitchen"), false);
assert.equal(canvas._videoCanvas, canvasA);
assert.equal(canvas.releaseVideoCanvas(ownerB), false);
assert.equal(canvas.releaseVideoCanvas(ownerA), true);
assert.equal(canvas.claimVideoCanvas(ownerB, canvasB, "kitchen"), true);
assert.equal(canvas._videoCanvas, canvasB);
assert.equal(canvas.claimSoftphoneController({{ isConnected: false }}, "kitchen"), false);
assert.equal(canvas.claimVideoCanvas({{ isConnected: false }}, {{}}, "kitchen"), false);
canvas._endpointId = "kitchen";
assert.equal(canvas.claimVideoCanvas(ownerA, canvasA, "kitchen"), false);
assert.equal(canvas._videoCanvas, canvasB);

// A same-call audio-only -> video-active state update must reconcile the
// optional video channel even though the audio WebSocket is already open.
const reconcile = new Engine();
reconcile._callId = "call-video";
const reconciled = [];
reconcile._ensureVideo = async (payload) => reconciled.push(payload.video_active);
await reconcile.reconcileSession({{
  state: "in_call",
  call_id: "call-video",
  audio_direction: "recvonly",
  video_active: true,
}});
assert.deepEqual(reconciled, [true]);
assert.equal(reconcile._audioDirection, "recvonly");

// A stuck attach for A cannot head-of-line block B. Identity checks in the
// real attach path make A tear down only its local, unpublished pipeline.
const preempt = new Engine();
const entered = [];
let releaseAttachA;
const attachA = new Promise((resolve) => {{ releaseAttachA = resolve; }});
preempt._resumeSessionLocked = async (_info, _device, payload) => {{
  entered.push(payload.call_id);
  if (payload.call_id === "A") await attachA;
}};
const pendingA = preempt.resumeSession({{}}, "device", {{ state: "in_call", call_id: "A", endpoint_id: "kitchen" }});
await Promise.resolve();
const pendingB = preempt.resumeSession({{}}, "device", {{ state: "in_call", call_id: "B", endpoint_id: "kitchen" }});
await Promise.resolve();
assert.deepEqual(entered, ["A", "B"]);
releaseAttachA();
await Promise.all([pendingA, pendingB]);

// close(A) detaches its audio objects before awaiting slow video/context
// teardown, so a B pipeline installed meanwhile survives the continuation.
const closeRace = new Engine();
let releaseOldAudio;
let releaseOldVideo;
const oldAudioGate = new Promise((resolve) => {{ releaseOldAudio = resolve; }});
const oldVideoGate = new Promise((resolve) => {{ releaseOldVideo = resolve; }});
closeRace._audioContext = {{ close: () => oldAudioGate }};
closeRace._video = {{ close: () => oldVideoGate }};
closeRace._callId = "A";
const closingA = closeRace.close("test");
assert.equal(closeRace.mediaCleanupPending, true);
assert.equal(closeRace._audioContext, null);
let bClosed = 0;
const contextB = {{ close: async () => {{ bClosed++; }} }};
closeRace._audioContext = contextB;
closeRace._callId = "B";
closeRace._state = "IN_CALL";
releaseOldAudio();
releaseOldVideo();
await closingA;
assert.equal(closeRace.mediaCleanupPending, false);
assert.equal(bClosed, 0);
assert.equal(closeRace._audioContext, contextB);
assert.equal(closeRace._callId, "B");
assert.equal(closeRace._state, "IN_CALL");

// Concurrent connect switches are ordered by invocation, not by whichever
// asynchronous close/sign operation happens to resolve last.
const connectRace = new Engine();
let releaseCloseA;
let releaseCloseB;
const closeA = new Promise((resolve) => {{ releaseCloseA = resolve; }});
const closeB = new Promise((resolve) => {{ releaseCloseB = resolve; }});
let closeCount = 0;
connectRace.close = async () => (++closeCount === 1 ? closeA : closeB);
connectRace._wsUrl = async (_deviceId, callId) => `wss://ha.example/${{callId}}`;
const sockets = [];
class RuntimeWebSocket {{
  static CONNECTING = 0;
  static OPEN = 1;
  constructor(url) {{
    this.url = url;
    this.readyState = RuntimeWebSocket.CONNECTING;
    sockets.push(this);
    setTimeout(() => {{
      this.readyState = RuntimeWebSocket.OPEN;
      this.onopen?.();
      this.onmessage?.({{ data: JSON.stringify({{
        state: "in_call",
        call_id: url.split("/").at(-1),
        tx_format: "48000:s16le:1:20",
        rx_format: "48000:s16le:1:20",
        audio_direction: "sendrecv",
      }}) }});
    }}, 0);
  }}
  close() {{ this.readyState = 3; this.onclose?.(); }}
  send() {{}}
}}
context.WebSocket = RuntimeWebSocket;
const connectA = connectRace._connect("device", "A", "kitchen").then(
  () => "A:ok",
  (err) => `A:${{err.message}}`,
);
const connectB = connectRace._connect("device", "B", "kitchen").then(
  () => "B:ok",
  (err) => `B:${{err.message}}`,
);
releaseCloseB();
await new Promise((resolve) => setTimeout(resolve, 0));
releaseCloseA();
assert.deepEqual(await Promise.all([connectA, connectB]), [
  "A:Audio WebSocket superseded before connect",
  "B:ok",
]);
assert.equal(connectRace._callId, "B");
assert.equal(sockets.length, 1);
assert.equal(sockets[0].url, "wss://ha.example/B");

// A call created after the initiating UI operation was cancelled is
// compensated with an exact-call hangup and is never claimed by the engine.
const superseded = new Engine();
const supersededServices = [];
const supersededRequests = [];
superseded._hass = {{
  callWS: async (request) => {{
    supersededRequests.push(request);
      return {{ response: {{ state: "calling", call_id: "orphan-B", endpoint_id: "kitchen", device_id: "kitchen-device" }} }};
  }},
  callService: async (...args) => supersededServices.push(args),
}};
const staleReply = await superseded.startHaSoftphone(
  {{ name: "peer" }},
  {{ endpoint_id: "kitchen", device_id: "kitchen-device" }},
  {{ shouldAbort: () => true }},
);
assert.equal(staleReply.superseded, true);
assert.equal(superseded.ownsSoftphoneSession("orphan-B", "kitchen"), false);
assert.equal(supersededRequests[0].type, "call_service");
assert.equal(supersededRequests[0].domain, "voip_stack");
assert.equal(supersededRequests[0].service, "call");
assert.equal(supersededRequests[0].return_response, true);
assert.equal(supersededRequests[0].service_data.destination, "peer");
assert.equal(supersededServices.length, 1);
assert.equal(supersededServices[0][2].call_id, "orphan-B");

// Browser capture/playback follows the negotiated local SDP direction. A
// recvonly answer must not ask for microphone permission; hold/resume and
// direction expansion rebuild atomically when a path is added or removed.
let microphoneRequests = 0;
let stoppedTracks = 0;
context.navigator.mediaDevices = {{
  getUserMedia: async () => {{
    microphoneRequests++;
    const track = {{ enabled: true, stop() {{ stoppedTracks++; }} }};
    return {{
      getAudioTracks() {{ return [track]; }},
      getTracks() {{ return [track]; }},
    }};
  }},
}};
class RuntimeAudioContext {{
  constructor() {{
    this.state = "running";
    this.destination = {{}};
    this.audioWorklet = {{ addModule: async () => {{}} }};
  }}
  async resume() {{ this.state = "running"; }}
  createMediaStreamSource() {{
    return {{ connect(target) {{ return target; }}, disconnect() {{}} }};
  }}
  createGain() {{
    return {{ gain: {{ value: 1 }}, connect(target) {{ return target; }}, disconnect() {{}} }};
  }}
  async close() {{ this.state = "closed"; }}
}}
class RuntimeWorkletNode {{
  constructor(_context, name) {{
    this.name = name;
    this.port = {{ onmessage: null, postMessage() {{}} }};
  }}
  connect(target) {{ return target; }}
  disconnect() {{}}
}}
context.window.AudioContext = RuntimeAudioContext;
context.AudioWorkletNode = RuntimeWorkletNode;
const audio = new Engine();
audio._callId = "directional";
const pcm = {{
  call_id: "directional",
  selected_tx_format: "48000:s16le:1:20",
  selected_rx_format: "48000:s16le:1:20",
  audio_direction: "recvonly",
}};
audio._lastSessionPayload = pcm;
await audio._setupAudio({{ audio_mode: "full_duplex" }}, pcm);
assert.equal(microphoneRequests, 0);
assert.equal(audio._captureNode, null);
assert.equal(audio._playbackNode?.name, "voip-stack-playback-processor");

await audio._reconcileAudioMedia({{ ...pcm, audio_direction: "sendrecv" }});
assert.equal(microphoneRequests, 1);
assert.equal(audio._captureNode?.name, "voip-stack-processor");
assert.equal(audio._playbackNode?.name, "voip-stack-playback-processor");

await audio._reconcileAudioMedia({{ ...pcm, audio_direction: "inactive" }});
assert.equal(audio._captureNode, null);
assert.equal(audio._playbackNode, null);
assert.equal(stoppedTracks, 1);

const sendOnly = new Engine();
sendOnly._callId = "send-only";
await sendOnly._setupAudio(
  {{ audio_mode: "full_duplex" }},
  {{ ...pcm, call_id: "send-only", audio_direction: "sendonly" }},
);
assert.equal(microphoneRequests, 2);
assert.equal(sendOnly._captureNode?.name, "voip-stack-processor");
assert.equal(sendOnly._playbackNode, null);

// Reapplying an unchanged negotiated direction is a no-op. Card listeners
// call reconcileSession() from engine state events, so emitting here would
// recurse synchronously until the browser reports Maximum call stack size.
let unchangedDirectionEvents = 0;
sendOnly.addEventListener("state", () => {{ unchangedDirectionEvents++; }});
sendOnly._applyAudioDirection("sendonly");
assert.equal(unchangedDirectionEvents, 0);

// Lazy video-module resolution from call A cannot attach or close media after
// call B has replaced the session intent.
const lazyVideo = new Engine();
lazyVideo._callId = "video-A";
let resolveVideo;
let videoStarts = 0;
let videoCloses = 0;
lazyVideo._loadVideo = () => new Promise((resolve) => {{ resolveVideo = resolve; }});
const staleVideoAttach = lazyVideo._ensureVideo({{
  call_id: "video-A",
  endpoint_id: "kitchen",
  video_active: true,
}});
await Promise.resolve();
lazyVideo._callId = "video-B";
lazyVideo._videoAttachGeneration++;
resolveVideo({{
  active: false,
  callId: "",
  async start() {{ videoStarts++; }},
  async close() {{ videoCloses++; }},
}});
await staleVideoAttach;
assert.equal(videoStarts, 0);
assert.equal(videoCloses, 0);

// A remote hangup may close the optional video WebSocket before the card has
// consumed the terminal state event. Re-check the authoritative backend call
// before surfacing a transport error to the user.
const terminalVideo = new Engine();
terminalVideo._callId = "video-ended";
terminalVideo._endpointId = "casa";
terminalVideo._hass = {{
  callWS: async () => ({{
    endpoint_id: "casa",
    call_id: "",
    state: "idle",
  }}),
}};
terminalVideo._loadVideo = async () => ({{
  active: false,
  callId: "",
  async start() {{ throw new Error("SIP video WebSocket closed before negotiation"); }},
  async close() {{}},
}});
let terminalVideoErrors = 0;
terminalVideo.addEventListener("video-error", () => {{ terminalVideoErrors++; }});
await terminalVideo._ensureVideo({{
  call_id: "video-ended",
  endpoint_id: "casa",
  video_active: true,
}});
assert.equal(terminalVideoErrors, 0);

// Once a previous video dialog is already closed, an audio-only state update
// is a strict no-op. Calling close() again emits state and would otherwise
// create an endless reconcile -> close -> emit loop in the renderer.
const audioOnlyReconcile = new Engine();
audioOnlyReconcile._callId = "audio-after-video";
let redundantVideoCloses = 0;
audioOnlyReconcile._video = {{
  active: false,
  callId: "",
  async close() {{ redundantVideoCloses++; audioOnlyReconcile._emit(); }},
}};
await audioOnlyReconcile._ensureVideo({{
  call_id: "audio-after-video",
  endpoint_id: "kitchen",
  video_active: false,
}});
assert.equal(redundantVideoCloses, 0);

// Starting the next audio-only call must wait until camera/encoder teardown
// from the previous video call has completed in the browser process.
const serialized = new Engine();
let releaseVideoCleanup;
let outboundStarts = 0;
serialized._video = {{
  configure() {{}},
  close: () => new Promise((resolve) => {{ releaseVideoCleanup = resolve; }}),
}};
serialized.configure({{
  callWS: async (request) => {{
    if (request.type === "voip_stack/ha_softphone_state") {{
        return {{ state: "idle", call_id: "", endpoint_id: "default", device_id: "phone-device" }};
    }}
    outboundStarts++;
      return {{ state: "idle", call_id: "", endpoint_id: "default", device_id: "phone-device" }};
  }},
}});
const closingVideo = serialized.close("terminal");
await Promise.resolve();
const startingAudio = serialized.startHaSoftphone(
  {{ name: "Audio endpoint", device_id: "audio-device" }},
  {{ endpoint_id: "default" }},
  {{ endpoint_id: "default", sendVideo: false }},
);
await Promise.resolve();
assert.equal(outboundStarts, 0);
releaseVideoCleanup();
await closingVideo;
await startingAudio;
assert.equal(outboundStarts, 1);

// Browser codec/AudioContext shutdown is best effort. A platform Promise that
// never settles cannot keep the media gate or the next Hangup blocked forever.
const stuckClose = new Engine();
stuckClose._callId = "stuck-media";
stuckClose._audioContext = {{ close: () => new Promise(() => {{}}) }};
stuckClose._video = {{
  configure() {{}},
  close: () => new Promise(() => {{}}),
}};
const stuckCloseStarted = Date.now();
await stuckClose.close("hangup");
assert.ok(Date.now() - stuckCloseStarted < 1500);
assert.equal(stuckClose._callId, "");
assert.equal(stuckClose._audioContext, null);
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
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_browser_media_model_enforces_pcm_and_sdp_directions() -> None:
    script = rf"""
import assert from "assert/strict";
import {{ pathToFileURL }} from "url";
const model = await import(pathToFileURL({json.dumps(str(MEDIA_MODEL))}));

assert.deepEqual(model.parsePcmFormat("48000:s16le:2:20"), {{
  sampleRate: 48000, pcmFormat: "s16le", channels: 2, frameMs: 20,
}});
assert.throws(() => model.parsePcmFormat("48000:f32le:2:20"), /unsupported PCM/);
assert.throws(() => model.parsePcmFormat("44100:s16le:2:16"), /whole PCM frames/);
assert.deepEqual(model.resolveSessionFormats({{
  selected_tx_format: "16000:s16le:1:10",
  selected_rx_format: "48000:s24le_in_s32:2:20",
}}), {{
  tx: {{ sampleRate: 16000, pcmFormat: "s16le", channels: 1, frameMs: 10 }},
  rx: {{ sampleRate: 48000, pcmFormat: "s24le_in_s32", channels: 2, frameMs: 20 }},
}});
assert.throws(() => model.resolveSessionFormats({{ tx_format: "16000:s16le:1:10" }}), /missing/);

assert.deepEqual(model.desiredAudioPaths("full_duplex", "sendrecv"), {{ capture: true, playback: true }});
assert.deepEqual(model.desiredAudioPaths("full_duplex", "sendonly"), {{ capture: true, playback: false }});
assert.deepEqual(model.desiredAudioPaths("full_duplex", "recvonly"), {{ capture: false, playback: true }});
assert.deepEqual(model.desiredAudioPaths("full_duplex", "inactive"), {{ capture: false, playback: false }});
assert.deepEqual(model.desiredAudioPaths("mic_only", "sendrecv"), {{ capture: false, playback: true }});
assert.deepEqual(model.desiredAudioPaths("speaker_only", "sendrecv"), {{ capture: true, playback: false }});
assert.deepEqual(model.desiredAudioPaths("unsupported", "sendrecv"), {{ capture: true, playback: true }});

const format = model.parsePcmFormat("48000:s16le:2:20");
assert.equal(model.sameAudioFormat(format, {{ ...format }}), true);
assert.equal(model.sameAudioFormat(format, {{ ...format, channels: 1 }}), false);
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_softphone_session_model_isolates_logical_phone_state() -> None:
    script = rf"""
import assert from "assert/strict";
import {{ pathToFileURL }} from "url";
const model = await import(pathToFileURL({json.dumps(str(SESSION_MODEL))}));

assert.deepEqual(model.normaliseSoftphoneSelector({{}}), {{
  endpoint_id: "", device_id: "",
}});
assert.deepEqual(model.normaliseSoftphoneSelector({{ device_id: " kiosk " }}), {{
  endpoint_id: "", device_id: "kiosk",
}});
assert.equal(model.softphoneScopeKey({{ endpoint_id: "kitchen" }}), "endpoint:kitchen");
assert.equal(model.softphoneScopeKey({{ device_id: "kiosk" }}), "device:kiosk");
assert.equal(model.softphoneScopeKey({{}}), "preferred");
assert.equal(model.softphoneStateMatches(
  {{ endpoint_id: "default", device_id: "preferred-device", call_id: "P" }},
  {{}},
), true);
assert.equal(model.softphoneStateMatches(
  {{ call_id: "identity-missing" }},
  {{}},
), false);

assert.equal(model.softphoneStateMatches(
  {{ endpoint_id: "kitchen", call_id: "K" }},
  {{ endpoint_id: "kitchen" }},
), true);
assert.equal(model.softphoneStateMatches(
  {{ endpoint_id: "default", call_id: "D" }},
  {{ endpoint_id: "kitchen" }},
), false);
assert.equal(model.softphoneStateMatches(
  {{ call_id: "legacy" }},
  {{ endpoint_id: "default" }},
), false);
assert.equal(model.softphoneStateMatches(
  {{ call_id: "legacy" }},
  {{ endpoint_id: "kitchen" }},
), false);
assert.equal(model.softphoneStateMatches(
  {{ endpoint_id: "kitchen", endpoint_device_id: "device-kitchen" }},
  {{ device_id: "device-kitchen" }},
), true);
assert.equal(model.softphoneStateMatches(
  {{ endpoint_id: "kitchen", endpoint_device_id: "device-kitchen" }},
  {{ device_id: "device-hall" }},
), false);
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
