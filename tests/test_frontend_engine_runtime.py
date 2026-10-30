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
MEDIA_DEVICES = ENGINE.with_name("voip-stack-media-devices.js")


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
).replace(
  /const \{{ BrowserMediaDevices, exactDeviceConstraint \}} = await import\([\s\S]*?\);/,
  "const {{ BrowserMediaDevices, exactDeviceConstraint }} = globalThis.__deviceModel;",
).replace("class VoipStackEngine", "export class VoipStackEngine");

const storage = new Map();
const session = new Map();
const context = vm.createContext({{
  __mediaModel: mediaModel,
  __sessionModel: sessionModel,
  __deviceModel: {{
    exactDeviceConstraint(base, deviceId) {{
      return deviceId ? {{ ...base, deviceId: {{ exact: deviceId }} }} : base;
    }},
    BrowserMediaDevices: class BrowserMediaDevices extends EventTarget {{
      constructor() {{
        super();
        this.selected = {{ audioinput: "", audiooutput: "", videoinput: "" }};
        this.active = {{ audioinput: "", audiooutput: "", videoinput: "" }};
      }}
      get state() {{
        return {{
          devices: {{ audioinput: [], audiooutput: [], videoinput: [] }},
          selected: {{ ...this.selected }}, active: {{ ...this.active }},
        }};
      }}
      preference(kind) {{ return this.selected[kind] || ""; }}
      setActive(kind, value) {{ this.active[kind] = value || ""; }}
      commit(kind, value, settings = {{}}) {{
        this.selected[kind] = value || "";
        this.active[kind] = settings.deviceId || value || "";
      }}
      async refresh() {{ return this.state; }}
    }},
  }},
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

// Terminal control recovers only transport failures and preserves exact-call scope.
for (const mode of ["retry", "already-ended", "new-call", "offline", "denied"]) {{
  const control = new Engine();
  control.claimSoftphoneSession("control-A", "control-endpoint");
  control._callId = "control-A";
  control._endpointId = "control-endpoint";
  control._state = "IN_CALL";
  let sends = 0, reconnects = 0, trackStops = 0;
  control._mediaStream = {{ getTracks: () => [{{ stop() {{ trackStops++; }} }}] }};
  const listeners = new Map();
  const connection = {{
    addEventListener(name, callback) {{ listeners.set(name, callback); }},
    removeEventListener(name) {{ listeners.delete(name); }},
    reconnect(force) {{
      assert.equal(force, true); reconnects++;
      if (mode === "offline") throw new Error("network unavailable");
      if (mode === "new-call") {{
        control.claimSoftphoneSession("control-B", "control-endpoint");
        control._callId = "control-B";
      }}
      queueMicrotask(() => listeners.get("ready")?.());
    }},
  }};
  control._hass = {{
    connection,
    async callService(domain, service, data) {{
      assert.equal(domain, "voip_stack"); assert.equal(service, "hangup");
      assert.equal(data.call_id, "control-A");
      assert.equal("endpoint_id" in data, false);
      sends++;
      if (mode === "denied") throw new Error("service denied");
      if (sends === 1) throw Object.assign(new Error("stalled"), {{ code: "voip_control_timeout" }});
    }},
    async callWS() {{
      return {{ call_id: mode === "new-call" ? "control-B" : "control-A",
        state: mode === "already-ended" ? "idle" : "in_call" }};
    }},
  }};
  const operation = control.terminateSoftphoneCall("hangup", {{
    call_id: "control-A", endpoint_id: "control-endpoint", device_id: "control-device",
  }});
  if (mode === "denied" || mode === "offline") {{
    await assert.rejects(operation, mode === "denied" ? /service denied/ : /closed locally/);
  }} else await operation;
  assert.equal(sends, mode === "retry" ? 2 : 1);
  assert.equal(reconnects, mode === "denied" ? 0 : 1);
  assert.equal(trackStops, mode === "denied" || mode === "new-call" ? 0 : 1);
  assert.equal(control._terminalControlRequests.size, 0);
  assert.equal(listeners.size, 0);
  if (mode === "new-call") assert.equal(control.softphoneCallIdFor("control-endpoint"), "control-B");
  else assert.equal(control.ownsSoftphoneSession("control-A", "control-endpoint"), mode === "denied");
}}

// Browser media capability is checked before SIP Answer or Call. Missing
// getUserMedia must fail here, not after a remote dialog has received 200 OK.
const presentation = new Engine();
const cardOwner = {{ isConnected: true }};
const popupOwner = {{ isConnected: true }};
assert.equal(presentation.claimSoftphoneController(cardOwner, "device:default"), true);
assert.equal(presentation.claimSoftphoneController(popupOwner, "device:default"), false);
assert.equal(presentation.activateSoftphoneController(popupOwner, "device:default", "default"), true);
presentation.claimSoftphoneSession("popup-call", "default");
assert.equal(presentation.claimSoftphoneController(cardOwner, "device:default"), false);
assert.equal(presentation.activateSoftphoneController(cardOwner, "device:default", "default"), false);
presentation.releaseSoftphoneSession("popup-call", "default");
assert.equal(presentation.activateSoftphoneController(cardOwner, "device:default", "default"), true);
presentation.claimSoftphoneSession("card-call", "default");
assert.equal(presentation.activateSoftphoneController(popupOwner, "device:default", "default"), false);
presentation.releaseSoftphoneSession("card-call", "default");

const audioPreflight = new Engine();
await assert.rejects(
  audioPreflight.prepareAudioCall(),
  /Microphone access is unavailable/,
);
let audioTrackStops = 0;
const preparationOrder=[];
context.navigator.mediaDevices = {{
  getUserMedia: async () => {{
    preparationOrder.push("input");
    const track = {{ stop() {{ audioTrackStops++; }} }};
    return {{
      getAudioTracks() {{ return [track]; }},
      getTracks() {{ return [track]; }},
    }};
  }},
}};
let outputStarted;
let contextCloses = 0;
class PreparedContext {{
  constructor() {{
    this.audioWorklet = {{ addModule: async () => {{}} }};
    preparationOrder.push("output");
    this.state = "running"; this.sampleRate = 48000; this.destination = {{}};
  }}
  async resume() {{}}
  createMediaStreamSource() {{ return {{ connect(node) {{ return node; }}, disconnect() {{}} }}; }}
  createGain() {{ return {{ gain: {{value: 1}}, connect(node) {{return node;}}, disconnect() {{}} }}; }}
  createBuffer() {{return {{}};}}
  createBufferSource() {{
    const source = {{connect() {{}}, disconnect() {{}}, stop() {{}}, start() {{ outputStarted=()=>source.onended(); }} }};
    return source;
  }}
  async close() {{contextCloses++; this.state="closed";}}
}}
context.window.AudioContext = PreparedContext;
const intentToken = {{}};
audioPreflight._ownedSoftphoneCalls.clear();
assert.equal(audioPreflight.tryAcquireMediaIntent("kitchen", intentToken),true);
let prepared = false;
const preparing = audioPreflight.prepareAudioCall().then(()=>{{prepared=true;}});
for(let i=0;i<30;i++) await Promise.resolve();
assert.equal(prepared, false, "Do not dial before the audio output has rendered");
assert.equal(typeof outputStarted, "function");
outputStarted();await preparing;
assert.equal(audioTrackStops, 0, "Retain the initialized microphone for the call");
assert.equal(audioPreflight._preparedAudio.ready, true);
assert.deepEqual(preparationOrder,["input","output"]);
await audioPreflight.close("switch", true, true);
assert.equal(audioPreflight._preparedAudio.ready, true, "Opening media must preserve prepared devices");
audioPreflight.claimSoftphoneSession("new-call", "kitchen");
audioPreflight.releaseMediaIntent(intentToken);
assert.equal(audioTrackStops, 0, "Keep audio ready while the destination rings");
audioPreflight.releaseSoftphoneSession("new-call", "kitchen");
for(let i=0;i<30;i++) await Promise.resolve();
assert.equal(audioTrackStops, 1, "Release prepared capture when an unanswered call ends");
assert.equal(contextCloses, 1);
const cancelToken={{}};
audioPreflight.tryAcquireMediaIntent("kitchen",cancelToken);
const cancelPrepare=audioPreflight.prepareAudioCall();
for(let i=0;i<30;i++) await Promise.resolve();
audioPreflight.releaseMediaIntent(cancelToken);
await assert.rejects(cancelPrepare,/cancelled|superseded/i);
assert.equal(audioTrackStops,2);
assert.equal(audioPreflight._preparedAudio,null);
const originalInput=context.navigator.mediaDevices.getUserMedia;
let grantInput;
context.navigator.mediaDevices.getUserMedia=()=>new Promise(resolve=>{{grantInput=resolve;}});
const lateToken={{}};audioPreflight.tryAcquireMediaIntent("kitchen",lateToken);
const latePreparation=audioPreflight.prepareAudioCall();
for(let i=0;i<30;i++) await Promise.resolve();
audioPreflight.releaseMediaIntent(lateToken);
let lateStops=0;
grantInput({{getAudioTracks:()=>[{{}}],getTracks:()=>[{{stop(){{lateStops++;}}}}]}});
await assert.rejects(latePreparation,/cancelled/i);
assert.equal(lateStops,1,"A late microphone grant must be released after cancellation");
assert.equal(audioPreflight._preparedAudio,null);
context.navigator.mediaDevices.getUserMedia=originalInput;


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

// The shared activity feed exists only for consumers or an owned conversation.
const lazyEngine = new Engine();
lazyEngine._ownedSoftphoneCalls.clear();
let busRequests = 0, busStops = 0;
const lazyConnection = {{subscribeMessage: async () => {{
  busRequests++;
  return () => busStops++;
}}}};
lazyEngine._busConnection = lazyConnection;
lazyEngine._ensureBusSubscriptions(lazyConnection);
assert.equal(busRequests, 0);
const stopOne = lazyEngine.subscribeCallEvents(() => {{}});
const stopTwo = lazyEngine.subscribeCallEvents(() => {{}});
await new Promise(resolve => setImmediate(resolve));
assert.equal(busRequests, 1);
stopOne();
assert.equal(busStops, 0);
stopTwo();
assert.equal(busStops, 1);
const stopPending = lazyEngine.subscribeCallEvents(() => {{}});
stopPending();
await new Promise(resolve => setImmediate(resolve));
assert.equal(busStops, 2);

// Activity replay is independent of call termination and does not revive old slots.
const activityEngine = new Engine();
activityEngine.claimSoftphoneSession("active-call", "test");
activityEngine._onBusEvent({{event_type: "voip_stack.activity", data: {{parked: ["701"], phones: []}}}});
const activityEvents = [];
const stopActivity = activityEngine.subscribeCallEvents(event => activityEvents.push(event));
assert.equal(activityEvents.at(-1).data.parked[0], "701");
activityEngine._onBusEvent({{event_type: "voip_stack.activity", data: {{parked: [], phones: []}}}});
assert.equal(activityEvents.at(-1).data.parked.length, 0);
assert.equal(activityEngine.softphoneCallIdFor("test"), "active-call");
stopActivity();
activityEngine.releaseSoftphoneSession("active-call", "test");

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
let navigationType = "reload";
context.performance = {{ getEntriesByType: () => [{{type:navigationType}}] }};
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

// Opening/duplicating a tab copies sessionStorage, including call hints.
// It is a new media client, not an authorized reconnect of the original page.
navigationType = "navigate";
session.set("voip_stack_media_client_id", signedA._mediaClientId);
context.__voipStackMediaClientId = undefined;
const cloned = new Engine();
assert.notEqual(cloned._mediaClientId, signedA._mediaClientId);
assert.equal(new Engine()._mediaClientId, cloned._mediaClientId,
  "Cards and SPA views in one document keep one media identity");
navigationType = "reload";
context.__voipStackMediaClientId = undefined;
assert.equal(new Engine()._mediaClientId, cloned._mediaClientId,
  "Reloading the new tab reconnects only its own media identity");
navigationType = "back_forward";
context.__voipStackMediaClientId = undefined;
assert.notEqual(new Engine()._mediaClientId, cloned._mediaClientId,
  "A new history document cannot inherit an opener's media identity");
navigationType = "reload";
session.set("voip_stack_media_client_id", signedA._mediaClientId);
context.__voipStackMediaClientId = signedA._mediaClientId;

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

// A renderer from the previous call cannot expose video on the next call.
const visibility = new Engine();
visibility._callId = "new-call";
visibility._video = {{ visible: true, callId: "old-call" }};
assert.equal(visibility.videoVisible, false);
visibility._video.callId = "new-call";
assert.equal(visibility.videoVisible, true);
visibility._callId = "";
assert.equal(visibility.videoVisible, false);

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
// The public phone service returns schema 2 with nested phone/call objects.
// Its returned identity must work before any independent state event arrives.
const canonical = new Engine();
canonical._hass = {{ callWS: async () => ({{ response: {{
  schema_version: 2, success: true, operation: "call",
  phone: {{device_id: "canonical-device", kind: "browser", name: "Kitchen"}},
  call: {{call_id: "canonical-call", state: "in_call", destination: "*97"}},
}} }}) }};
const resumed = [];
canonical.resumeSession = async (...args) => resumed.push(args);
const canonicalReply = await canonical.startHaSoftphone(
  {{name: "*97"}}, {{endpoint_id: "kitchen", device_id: "canonical-device"}},
);
assert.equal(canonicalReply.call_id, "canonical-call");
assert.equal(canonicalReply.state, "in_call");
assert.equal(canonical.ownsSoftphoneSession("canonical-call", "kitchen"), true);
assert.equal(resumed.length, 1);
assert.equal(resumed[0][2].call_id, "canonical-call");

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
    this.sampleRate = 48000;
    this.state = "running";
    this.destination = {{}};
    this.loadedModules = [];
    this.audioWorklet = {{ addModule: async (url) => {{this.loadedModules.push(url);}} }};
  }}
  async resume() {{ this.state = "running"; }}
  createBuffer() {{ return {{}}; }}
  createBufferSource() {{
    const node = {{connect() {{}}, disconnect() {{}}, stop() {{}},
      start() {{ Promise.resolve().then(()=>node.onended?.()); }} }};
    return node;
  }}
  createMediaStreamSource() {{
    return {{ connect(target) {{ return target; }}, disconnect() {{}} }};
  }}
  createGain() {{
    return {{ gain: {{ value: 1 }}, connect(target) {{ return target; }}, disconnect() {{}} }};
  }}
  async close() {{ this.state = "closed"; }}
}}
class RuntimeWorkletNode {{
  constructor(_context, name, options) {{
    this.name = name;
    this.options = options;
    this.port = {{ onmessage: null, postMessage() {{}} }};
  }}
  connect(target) {{ return target; }}
  disconnect() {{}}
}}
context.window.AudioContext = RuntimeAudioContext;
context.AudioWorkletNode = RuntimeWorkletNode;
// Parking can replace the audio path while retaining the same logical Call-ID.
// A late completion from the retired setup must not hang up its replacement.
const parked = new Engine();
const parkedServices = [];
const parkedErrors = [];
parked._hass = {{callService: async (...args) => parkedServices.push(args)}};
parked._mediaAttachState = async () => ({{state:"in_call",call_id:"parked-call"}});
parked._sessionAttachKey = "phone|parked-call";
parked._connect = async (deviceId, callId, endpointId) => {{
  parked._deviceId=deviceId;parked._callId=callId;parked._endpointId=endpointId;
}};
parked.addEventListener("error", event => parkedErrors.push(event.detail));
let releaseRetiredSetup, setupEntered, retiredContext;
const retiredSetupReady = new Promise(resolve => {{releaseRetiredSetup=resolve;}});
const setupStarted = new Promise(resolve => {{setupEntered=resolve;}});
let loads = 0;
parked._loadAudioWorklets = async (audioContext) => {{
  if (++loads === 1) {{retiredContext=audioContext; setupEntered(); await retiredSetupReady;}}
}};
const parkedPcm = {{call_id:"parked-call",tx_format:"48000:s16le:1:10",
  rx_format:"48000:s16le:1:10",audio_direction:"recvonly"}};
const retiredSetup = parked._setupAudioOrAbort("phone-device",{{audio_mode:"full_duplex"}},
  parkedPcm,"phone|parked-call","phone");
await setupStarted;
await parked._cleanupAudio("route_replaced");
await parked._setupAudio({{audio_mode:"full_duplex"}},parkedPcm,"phone|parked-call");
const replacementContext = parked._audioContext;
const replacementPlayback = parked._playbackNode;
releaseRetiredSetup();
assert.equal(await retiredSetup,false);
assert.deepEqual(parkedServices,[],"Retired audio setup must never issue hangup");
assert.deepEqual(parkedErrors,[]);
assert.equal(parked._audioContext,replacementContext);
assert.equal(parked._playbackNode,replacementPlayback);
assert.equal(retiredContext.state,"closed");
assert.equal(replacementContext.state,"running");
assert.equal(parked._audioReady,true);
assert.equal(parked._callId,"parked-call");
await parked.close();

const preparedSession = new Engine();
const loadWorklets = preparedSession._loadAudioWorklets.bind(preparedSession);
let releaseModules;
const modulesReady = new Promise(resolve => {{releaseModules = resolve;}});
preparedSession._loadAudioWorklets = async (...args) => {{
  await modulesReady;
  return loadWorklets(...args);
}};
let readyToDial = false;
const prepareSession = preparedSession.prepareAudioCall().then(() => {{readyToDial = true;}});
for (let i=0;i<30;i++) await Promise.resolve();
assert.equal(readyToDial, false, "A slow processor download must finish before SIP starts");
releaseModules();
await prepareSession;
const preparedContext = preparedSession._preparedAudio.audioContext;
const preparedStream = preparedSession._preparedAudio.mediaStream;
const preparedRequests = microphoneRequests;
assert.equal(preparedContext.loadedModules.length, 2, "Processors must load before dialing");
preparedContext.audioWorklet.addModule = async () => {{throw new Error("Late worklet download after dialing");}};
preparedSession._callId = "prepared-duplex";
await preparedSession._setupAudio({{audio_mode: "full_duplex"}}, {{
  call_id: "prepared-duplex", tx_format: "16000:s16le:1:20",
  rx_format: "16000:s16le:1:20", audio_direction: "sendrecv",
}});
assert.equal(microphoneRequests, preparedRequests, "Reuse input instead of opening it twice");
assert.equal(preparedSession._mediaStream, preparedStream);
assert.equal(preparedSession._audioContext, preparedContext);
assert.equal(preparedSession._preparedAudio, null);
await preparedSession.close();
microphoneRequests=0;stoppedTracks=0;
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

// Incoming codec/rate changes must never become the microphone TX contract.
const asymmetric = new Engine();
asymmetric._callId = "asymmetric";
const asymmetricSession = {{
  call_id: "asymmetric", audio_direction: "sendrecv",
  tx_format: "8000:s16le:1:20", selected_tx_format: "8000:s16le:1:20",
  rx_format: "16000:s16le:1:20", selected_rx_format: "16000:s16le:1:20",
}};
asymmetric._lastSessionPayload = asymmetricSession;
await asymmetric._setupAudio({{audio_mode: "full_duplex"}}, asymmetricSession);
assert.equal(asymmetric._captureNode.options.processorOptions.format.sampleRate, 8000);
assert.equal(asymmetric._captureNode.options.processorOptions.format.channels, 1);
assert.equal(asymmetric._captureNode.options.processorOptions.format.frameMs, 20);
assert.equal(asymmetric._playbackNode.options.processorOptions.format.sampleRate, 16000);
await asymmetric._reconcileAudioMedia({{
  rx_format: "48000:s16le:2:20", selected_rx_format: "48000:s16le:2:20",
}});
assert.equal(asymmetric._txFormat.sampleRate, 8000);
assert.equal(asymmetric._captureNode.options.processorOptions.format.sampleRate, 8000);
assert.equal(asymmetric._captureNode.options.processorOptions.format.channels, 1);
assert.equal(asymmetric._captureNode.options.processorOptions.format.frameMs, 20);
assert.equal(asymmetric._playbackNode.options.processorOptions.format.sampleRate, 48000);
assert.equal(asymmetric._playbackNode.options.outputChannelCount[0], 2);
await asymmetric.close("test-complete");

// An operator view is passive and shares one subscription between cards.
const dashboard = new Engine();
const observers = [];
let monitorUnsubscribed = 0;
const monitorConnection = {{
  subscribeMessage: async (listener, request) => {{
    observers.push({{listener, request}});
    return () => {{monitorUnsubscribed++;}};
  }},
}};
dashboard.configure({{connection: monitorConnection}});
const firstMonitorEvents = [], secondMonitorEvents = [];
const releaseMonitor = dashboard.subscribePhoneStates(event => firstMonitorEvents.push(event));
await new Promise(resolve => setImmediate(resolve));
assert.equal(observers.length, 1);
assert.equal(observers[0].request.type, "voip_stack/subscribe_phone_states");
observers[0].listener({{kind:"snapshot",revision:0,phones:[{{endpoint_id:"desk",state:"idle"}}]}});
const releaseSecondMonitor = dashboard.subscribePhoneStates(event => secondMonitorEvents.push(event));
assert.equal(secondMonitorEvents[0].phones[0].state,"idle");
assert.equal(observers.length,1);
observers[0].listener({{kind:"delta",revision:1,phones:[{{endpoint_id:"desk",state:"ringing"}}],removed:[]}});
observers[0].listener({{kind:"delta",revision:0,phones:[{{endpoint_id:"desk",state:"idle"}}],removed:[]}});
assert.equal(secondMonitorEvents.at(-1).phones[0].state,"ringing");
// A server reconnect starts a new revision stream, even on the same HA socket object.
observers[0].listener({{kind:"snapshot",revision:0,phones:[{{endpoint_id:"desk",state:"in_call"}}]}});
assert.equal(dashboard._phoneStates.get("desk").state,"in_call");
observers[0].listener({{kind:"delta",revision:1,phones:[],removed:["desk"]}});
assert.equal(dashboard._phoneStates.size,0);
releaseMonitor();
assert.equal(monitorUnsubscribed,0);
releaseSecondMonitor();
assert.equal(monitorUnsubscribed,1);
assert.equal(dashboard._phoneStates.size,0);
assert.equal(dashboard._softphoneScopeSubscriptions.size,0);

// Losing a delta requires a new full snapshot; the old callback cannot revive state.
const reconnectEvents=[];
const releaseReconnecting=dashboard.subscribePhoneStates(event=>reconnectEvents.push(event));
await new Promise(resolve => setImmediate(resolve));
observers[1].listener({{kind:"snapshot",revision:0,phones:[]}});
observers[1].listener({{kind:"delta",revision:2,phones:[{{endpoint_id:"stale"}}]}});
await new Promise(resolve => setImmediate(resolve));
assert.equal(observers.length,3);
assert.equal(reconnectEvents.at(-1).kind,"reset");
observers[1].listener({{kind:"snapshot",revision:9,phones:[{{endpoint_id:"stale"}}]}});
assert.equal(dashboard._phoneStates.size,0);
observers[2].listener({{kind:"snapshot",revision:0,phones:[{{endpoint_id:"fresh",state:"idle"}}]}});
assert.equal(dashboard._phoneStates.has("fresh"),true);
releaseReconnecting();

// Removing the final card must preserve the presence lease of its active call.
const ownedView = new Engine();
let viewUnsubscribe=0;
ownedView._busConnection={{subscribeMessage:async()=>()=>{{viewUnsubscribe++;}}}};
const releaseView=ownedView.subscribeSoftphoneState(()=>{{}},{{endpoint_id:"desk"}});
await new Promise(resolve => setImmediate(resolve));
ownedView.claimSoftphoneSession("view-call","desk");
releaseView();
assert.equal(viewUnsubscribe,0);
assert.equal(ownedView.softphoneCallIdFor("desk"),"view-call");
ownedView.releaseSoftphoneSession("view-call","desk");
assert.equal(viewUnsubscribe,1);
assert.equal(ownedView._softphoneScopeSubscriptions.size,0);

// Permission preflight may take time: never answer the next caller by accident.
const guarded = new Engine();
const guardedOwner={{device_id:"desk-device",endpoint_id:"desk",softphone:true}};
let guardedState={{endpoint_id:"desk",call_id:"incoming-A",state:"ringing"}};
let guardedCommands=0;
guarded._hass={{callWS:async()=>guardedState,callService:async()=>{{guardedCommands++;}}}};
guarded.prepareAudioCall=async()=>{{guardedState={{endpoint_id:"desk",call_id:"incoming-B",state:"ringing"}};}};
await assert.rejects(guarded.dashboardAnswer(guardedOwner,{{call_id:"incoming-A"}}),/no longer ringing/);
assert.equal(guardedCommands,0);
assert.equal(guarded.softphoneCallIdFor("desk"),"");
assert.equal(guarded._dashboardOperations.size,0);
assert.equal(guarded._mediaIntent,null);

// A callee can answer before the originate service returns to the browser.
const immediate = new Engine();
const immediateOwner={{device_id:"fast-device",endpoint_id:"fast",softphone:true}};
let immediateResumes=0;
immediate._hass={{callWS:async()=>({{endpoint_id:"fast",device_id:"fast-device",state:"idle"}})}};
immediate.prepareAudioCall=async()=>{{}};
immediate.resumeSession=async()=>{{immediateResumes++;}};
immediate.startHaSoftphone=async()=>{{
  immediate.claimSoftphoneSession("fast-call","fast");
  immediate._onSoftphoneState({{endpoint_id:"fast",device_id:"fast-device",state:"in_call",call_id:"fast-call"}});
  return {{state:"calling",call_id:"fast-call"}};
}};
await immediate.dashboardStartCall(immediateOwner,{{extension:"101"}});
assert.equal(immediateResumes,1,"A committed answer must attach media after the command gate releases");

// ShadowNameVsExtension: named tiles always reach the central PBX resolver by name.
// Exercise the real dashboard and originate methods, including their wire payload.
const namedSessionBackup=new Map(session);
for (const browser of [false,true]) {{
  for (const [target,expected] of [
    [{{name:" Shadow destination ",extension:"101",number:"202"}},"Shadow destination"],
    [{{name:"Saved contact",number:"+12025550123"}},"Saved contact"],
    [{{name:"101",extension:"202"}},"101"],
    [{{name:"  ",extension:"101"}},"101"],
    [{{extension:" ",number:"202"}},"202"],
  ]) {{
    session.clear();
    const named=new Engine();const sent=[];
    const who={{device_id:"named-device",endpoint_id:"named",softphone:browser}};
    named._phoneStates.set("named",{{state:"idle",calls:[]}});
    named.prepareAudioCall=async()=>{{}};
    named._hass={{callWS:async request=>{{
      if(request.type==="call_service")sent.push(request.service_data);
      return {{endpoint_id:"named",device_id:"named-device",state:"idle"}};
    }},callService:async(domain,service,data)=>{{sent.push(data);return {{}};}}}};
    await named.dashboardStartCall(who,target);
    assert.equal(sent.length,1);
    assert.equal(sent[0].destination,expected,`ShadowNameVsExtension browser=${{browser}}`);
    assert.equal(sent[0].device_id,"named-device");
  }}
}}
session.clear();
const manualDial=new Engine();let manualRequest;
manualDial._hass={{callWS:async request=>{{manualRequest=request;return {{state:"idle",device_id:"manual-device",endpoint_id:"manual"}};}}}};
await manualDial.startHaSoftphone({{name:"101",manual:true}},{{device_id:"manual-device",endpoint_id:"manual"}},{{callee:"101"}});
assert.equal(manualRequest.service_data.destination,"101","Explicit keypad numbers retain their own meaning");
session.clear();for(const [key,value] of namedSessionBackup)session.set(key,value);

// A retained backend scope must also consume an answer after the last view
// unmounts. Keeping browser presence alone does not attach the call's audio.
const unmounted = new Engine();
let unmountedResumes = 0;
const releaseUnmounted = unmounted.subscribeSoftphoneState(() => {{}}, {{device_id:"unmounted-device"}});
unmounted._lastSoftphoneStates.set("endpoint:unmounted-desk", {{
  endpoint_id:"unmounted-desk",device_id:"unmounted-device",state:"calling",call_id:"unmounted-call",
}});
unmounted.claimSoftphoneSession("unmounted-call","unmounted-desk");
unmounted.resumeSession = async (owner, deviceId, state) => {{
  assert.equal(owner.endpoint_id,"unmounted-desk");
  assert.equal(deviceId,"unmounted-device");
  assert.equal(state.call_id,"unmounted-call");
  unmountedResumes++;
}};
releaseUnmounted();
assert.equal(unmounted._softphoneSubscribers.size,0);
assert.equal(unmounted._softphoneScopeSubscriptions.size,1);
unmounted._onSoftphoneState({{
  endpoint_id:"unmounted-desk",device_id:"unmounted-device",state:"in_call",call_id:"unmounted-call",
}}, {{device_id:"unmounted-device"}});
await new Promise(resolve => setTimeout(resolve,0));
assert.equal(unmountedResumes,1,"An answer after dashboard unmount must attach media exactly once");

// A live phone card remains first to supply its microphone filtering choice.
const preferred = new Engine();
preferred.claimSoftphoneSession("preferred-call","preferred-desk");
const preferredAttachments=[];
preferred.resumeSession=async(owner)=>{{preferredAttachments.push(owner.microphone_anti_alias);}};
const releasePreferred=preferred.subscribeSoftphoneState((snapshot)=>{{
  void preferred.resumeSession({{microphone_anti_alias:false}},"preferred-device",snapshot);
}},{{device_id:"preferred-device"}});
storage.set("voip_microphone_anti_alias_preferred-device","false");
preferred._onSoftphoneState({{endpoint_id:"preferred-desk",device_id:"preferred-device",call_id:"preferred-call",state:"in_call"}});
await new Promise(resolve=>setTimeout(resolve,0));
assert.deepEqual(preferredAttachments,[false]);
releasePreferred();
preferred._onSoftphoneState({{endpoint_id:"preferred-desk",device_id:"preferred-device",call_id:"preferred-call",state:"in_call"}});
await new Promise(resolve=>setTimeout(resolve,0));
assert.deepEqual(preferredAttachments,[false,false],"An unmounted phone keeps its saved filtering preference");
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
