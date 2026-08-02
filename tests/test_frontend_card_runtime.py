#!/usr/bin/env python3
"""Runtime anti-regressions for HA softphone card signaling state."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
CARD = (
    ROOT
    / "custom_components"
    / "voip_stack"
    / "frontend"
    / "voip-stack-card.js"
)
CARD_MODEL = CARD.with_name("voip-stack-card-model.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_card_runtime_follows_authoritative_sip_state_and_call_identity() -> None:
    """Exercise the real card class instead of matching source-code strings."""

    script = rf"""
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";
import {{ pathToFileURL }} from "url";

const cardModel = await import(pathToFileURL({json.dumps(str(CARD_MODEL))}));

let source = fs.readFileSync({json.dumps(str(CARD))}, "utf8");
source = source
  .replace(/await import\(`\.\/voip-phonebook-card\.js[^;]+;/, "")
  .replace(/await import\(`\.\/voip-stack-card-editor\.js[^;]+;/, "")
  .replace(
    /const \{{\s*buildMainCardSkeleton[\s\S]*?\}} = await import\(`\.\/voip-stack-card-view\.js[^;]+;/,
    "const {{ buildMainCardSkeleton, buildUnconfiguredCardSkeleton }} = globalThis.__cardView;",
  )
  .replace(
    /const \{{ voipStackEngine \}} = await import\(`\.\/voip-stack-engine\.js[^;]+;/,
    "const {{ voipStackEngine }} = globalThis.__engine;",
  )
  .replace(
    /const \{{\s*audioModeLabel[\s\S]*?\}} = await import\(`\.\/voip-stack-card-model\.js[^;]+;/,
    `const {{
      audioModeLabel, formatCallDuration, formatEndReason,
      formatKnownReason, formatListFromMetadata, formatVideoFailureReason,
      normaliseAudioMode, normaliseTransport, reasonKey,
      targetFromRosterEntry, terminalPeerLabel,
    }} = globalThis.__cardModel;`,
  )
  .replace("class VoipStackCard extends HTMLElement", "export class VoipStackCard extends HTMLElement");

const serviceCalls = [];
const engineEvents = [];
let cameraPermissionChecks = 0;
const engine = {{
  active: false,
  callId: "",
  deviceId: "",
  endpointId: "default",
  softphoneCallId: "",
  ownedSoftphoneCalls: new Map(),
  mediaIntent: null,
  mediaCleanupPending: false,
  videoVisible: false,
  videoCameraEnabled: false,
  addEventListener() {{}},
  removeEventListener() {{}},
  claimSoftphoneController(card) {{ return !!card?.isConnected; }},
  releaseSoftphoneController() {{}},
  claimVideoCanvas() {{ return false; }},
  releaseVideoCanvas() {{ return false; }},
  clearRingtoneRequest() {{}},
  setRingtoneRequest() {{}},
  statsText() {{ return ""; }},
  ownsSoftphoneSession(callId, endpointId = "default") {{
    return this.ownedSoftphoneCalls.get(endpointId) === callId;
  }},
  softphoneCallIdFor(endpointId = "default") {{
    return this.ownedSoftphoneCalls.get(endpointId) || "";
  }},
  claimSoftphoneSession(callId, endpointId = "default") {{
    const wanted = String(callId || "");
    if (wanted) this.ownedSoftphoneCalls.set(endpointId, wanted);
    else this.ownedSoftphoneCalls.delete(endpointId);
    this.softphoneCallId = this.ownedSoftphoneCalls.get("default") || "";
  }},
  releaseSoftphoneSession(callId = "", endpointId = "default") {{
    if (!callId || this.ownedSoftphoneCalls.get(endpointId) === callId) {{
      this.ownedSoftphoneCalls.delete(endpointId);
    }}
    this.softphoneCallId = this.ownedSoftphoneCalls.get("default") || "";
  }},
  hasOwnedSoftphoneSessionForOtherEndpoint(endpointId = "default") {{
    for (const [candidate, callId] of this.ownedSoftphoneCalls) {{
      if (candidate !== endpointId && callId) return true;
    }}
    return this.active && this.endpointId !== endpointId;
  }},
  tryAcquireMediaIntent(endpointId = "default", token = null) {{
    if (!token) return false;
    if (this.mediaIntent) return this.mediaIntent.token === token;
    if (this.hasOwnedSoftphoneSessionForOtherEndpoint(endpointId)) return false;
    this.mediaIntent = {{ endpointId, token }};
    return true;
  }},
  releaseMediaIntent(token) {{
    if (!token || this.mediaIntent?.token !== token) return false;
    this.mediaIntent = null;
    return true;
  }},
  tryRecoverSoftphoneSession(callId, endpointId = "default") {{
    if (!callId || this.hasOwnedSoftphoneSessionForOtherEndpoint(endpointId)) return false;
    this.claimSoftphoneSession(callId, endpointId);
    return true;
  }},
  async close(reason) {{
    engineEvents.push(["close", reason, this.callId]);
    this.active = false;
    this.callId = "";
  }},
  suspendVideoForHangup(callId, endpointId = "default") {{
    engineEvents.push(["video-suspend", callId, endpointId]);
    this.videoVisible = false;
    return Promise.resolve();
  }},
  async reconcileSession() {{}},
  async resumeSession() {{ return true; }},
  async prepareVideoCameraPermission() {{ cameraPermissionChecks++; return false; }},
  async startHaSoftphone() {{ throw new Error("not configured"); }},
}};

class FakeClassList {{
  constructor() {{ this.values = new Set(); }}
  toggle(name, wanted) {{
    if (wanted) this.values.add(name);
    else this.values.delete(name);
    return !!wanted;
  }}
}}
class FakeElement {{
  constructor() {{
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.className = "";
    this.classList = new FakeClassList();
    this.style = {{ display: "", setProperty() {{}} }};
    this.attributes = new Map();
  }}
  setAttribute(name, value) {{ this.attributes.set(name, String(value)); }}
  replaceChildren(...children) {{
    this.children = children;
    for (const child of children) child.parentNode = this;
  }}
  addEventListener() {{}}
  focus() {{}}
}}
class FakeHTMLElement extends EventTarget {{
  constructor() {{
    super();
    this.isConnected = true;
    this.shadowRoot = null;
  }}
  attachShadow() {{
    this.shadowRoot = {{ querySelector() {{ return null; }} }};
    return this.shadowRoot;
  }}
}}
class FakeResizeObserver {{ observe() {{}} disconnect() {{}} }}

const storage = new Map();
const registry = new Map();
let microphonePermissionState = "prompt";
let microphonePermissionError = null;
let microphonePermissionRequests = 0;
const context = vm.createContext({{
  __engine: {{ voipStackEngine: engine }},
  __cardModel: cardModel,
  __cardView: {{
    buildMainCardSkeleton() {{}},
    buildUnconfiguredCardSkeleton() {{}},
  }},
  EventTarget,
  Event,
  CustomEvent: class CustomEvent extends Event {{
    constructor(type, init) {{ super(type); this.detail = init?.detail; }}
  }},
  HTMLElement: FakeHTMLElement,
  ResizeObserver: FakeResizeObserver,
  WheelEvent: {{ DOM_DELTA_LINE: 1, DOM_DELTA_PAGE: 2 }},
  customElements: {{
    get(name) {{ return registry.get(name); }},
    define(name, value) {{ registry.set(name, value); }},
  }},
  document: {{ createElement() {{ return new FakeElement(); }} }},
  localStorage: {{
    getItem(key) {{ return storage.get(key) || null; }},
    setItem(key, value) {{ storage.set(key, String(value)); }},
    removeItem(key) {{ storage.delete(key); }},
  }},
  sessionStorage: {{ getItem() {{ return null; }}, setItem() {{}}, removeItem() {{}} }},
  navigator: {{
    permissions: {{
      async query() {{ return {{ state: microphonePermissionState }}; }},
    }},
    mediaDevices: {{
      async getUserMedia() {{
        microphonePermissionRequests++;
        if (microphonePermissionError) throw microphonePermissionError;
        const track = {{ stop() {{}} }};
        return {{
          getAudioTracks() {{ return [track]; }},
          getTracks() {{ return [track]; }},
        }};
      }},
    }},
  }},
  console,
  URL,
  encodeURIComponent,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  requestAnimationFrame(callback) {{ callback(); }},
  window: {{
    customCards: [],
    innerHeight: 800,
    location: {{ href: "https://ha.example/", protocol: "https:", host: "ha.example" }},
    scrollBy() {{}},
  }},
}});
const module = new vm.SourceTextModule(source, {{ context }});
await module.link(() => {{ throw new Error("unexpected import"); }});
await module.evaluate();
const Card = module.namespace.VoipStackCard;

function elements() {{
  const names = [
    "answerBtn", "autoAnswerCheckbox", "autoAnswerRow", "callBtn", "card",
    "declineBtn", "destRow", "destSelect", "destValue", "destValueWrap",
    "dndCheckbox", "dndRow", "err", "hangupBtn", "hangupDuration",
    "hangupPeer", "hangupState", "header", "headerName", "keypadBtn",
    "keypadInput", "keypadPanel", "nextBtn", "offlinePanel", "placeholderBtn",
    "prevBtn", "ringtoneCheckbox", "ringtoneRow", "runtimeControls",
    "microphoneAntiAliasCheckbox", "microphoneAntiAliasRow",
    "settingsBtn", "settingsPanel", "softphoneGroupsPanel", "stats",
    "statusIndicator", "statusReason", "statusText", "videoCameraCheckbox",
    "videoCameraRow", "videoCanvas", "nativeCameraHost", "videoShade",
  ];
  const result = Object.fromEntries(names.map((name) => [name, new FakeElement()]));
  result.keypadKeys = {{ one: new FakeElement() }};
  return result;
}}

function makeCard() {{
  const card = new Card();
  card.config = {{ mode: "ha_softphone", name: "Office HA" }};
  card._hass = {{
    config: {{ location_name: "Office HA" }},
    user: {{ is_admin: true }},
    states: {{}},
    callService: async (...args) => serviceCalls.push(args),
  }};
  card._skeletonMode = "main";
  card._els = elements();
  card._renderSoftphoneDestinationSelect = () => {{}};
  card._renderGroupControls = () => {{}};
  card._maybeAnswerFromUrl = () => {{}};
  card._captureEndReason = (kind, reason, origin, peer) => {{
    card._lastEndInfo = {{ kind, reason, origin, peer }};
  }};
  card._getDeviceInfo = async () => ({{
    device_id: "__voip_stack_ha_softphone__",
    name: "Office HA",
    softphone: true,
  }});
  return card;
}}

const card = makeCard();
const antiAliasPreference = makeCard();
assert.equal(antiAliasPreference._microphoneAntiAliasEnabled(), true);
antiAliasPreference._toggleMicrophoneAntiAlias();
assert.equal(antiAliasPreference._microphoneAntiAliasEnabled(), false);
assert.equal(
  storage.get("voip_microphone_anti_alias_default"),
  "false",
);
antiAliasPreference._toggleMicrophoneAntiAlias();
assert.equal(antiAliasPreference._microphoneAntiAliasEnabled(), true);
assert.equal(storage.get("voip_microphone_anti_alias_default"), "true");
const base = {{
  call_id: "call-A",
  direction: "outgoing",
  caller: "Office HA",
  callee: "Home HA",
  peer_name: "Home HA",
}};

// Native ESPHome camera preview is a presentation fallback for an audio-only
// ESP endpoint. A compile-time SIP video codec must always keep the true SIP
// canvas path authoritative even when the same device also owns a camera entity.
const nativePreview = makeCard();
nativePreview._hass.states["camera.p4"] = {{ state: "idle" }};
nativePreview._availableDevices = [{{
  device_id: "device-p4",
  name: "P4",
  capabilities: ["audio", "dtmf", "camera_preview"],
  camera_entity_id: "camera.p4",
  sip_video_codec: "",
}}];
nativePreview._softphoneSnapshot = {{
  state: "in_call",
  target_device_id: "device-p4",
  peer_name: "P4",
}};
assert.equal(nativePreview._activeNativeCameraEntityId("in_call"), "camera.p4");
nativePreview._availableDevices[0].sip_video_codec = "jpeg";
nativePreview._availableDevices[0].capabilities.push("video");
assert.equal(nativePreview._activeNativeCameraEntityId("in_call"), "");
nativePreview._availableDevices[0].sip_video_codec = "";
nativePreview._availableDevices[0].capabilities = ["audio", "dtmf", "camera_preview"];
assert.equal(nativePreview._activeNativeCameraEntityId("ringing"), "");
nativePreview._hass.states["camera.p4"].state = "unavailable";
assert.equal(nativePreview._activeNativeCameraEntityId("in_call"), "camera.p4");
nativePreview._softphoneSnapshot = {{
  state: "in_call",
  target_device_id: "",
  peer_name: "Waveshare_P4_Touch",
}};
nativePreview._availableDevices[0].name = "Waveshare P4 Touch";
assert.equal(nativePreview._activeNativeCameraEntityId("in_call"), "camera.p4");

// Engine cleanup emits before HA's terminal snapshot necessarily arrives.
// A card already dispatching Hangup must not reconnect the same audio socket
// from the briefly stale in-call snapshot.
const stoppingCard = makeCard();
stoppingCard._softphoneSnapshot = {{
  endpoint_id: "default",
  device_id: "__voip_stack_ha_softphone__",
  session_device_id: "__voip_stack_ha_softphone__",
  state: "in_call",
  call_id: "stopping-call",
}};
engine.claimSoftphoneSession("stopping-call", "default");
let stoppingAttachAttempts = 0;
const savedResumeSession = engine.resumeSession;
engine.resumeSession = async () => {{ stoppingAttachAttempts++; return true; }};
stoppingCard._stopping = true;
stoppingCard._ensureHaSoftphoneAudioPath(stoppingCard._softphoneSnapshot);
await Promise.resolve();
assert.equal(stoppingAttachAttempts, 0);
stoppingCard._stopping = false;
engine.mediaCleanupPending = true;
stoppingCard._ensureHaSoftphoneAudioPath(stoppingCard._softphoneSnapshot);
await Promise.resolve();
assert.equal(stoppingAttachAttempts, 0);
engine.mediaCleanupPending = false;
engine.resumeSession = savedResumeSession;
engine.releaseSoftphoneSession("stopping-call", "default");

// Enabling Auto Answer on a HA browser phone must prove microphone access for
// the local phone, regardless of the remote destination selected in the
// dialer. That successful user-gesture acquisition is authoritative even when
// the browser Permissions API still reports "prompt".
const autoAnswerCard = makeCard();
autoAnswerCard.config = {{
  mode: "ha_softphone",
  name: "Casa",
  device_id: "device-casa",
}};
autoAnswerCard._rosterEntries = [{{
  id: "test",
  name: "Test",
  metadata: {{
    device_id: "device-test",
    endpoint_kind: "browser",
    audio_mode: "mic_only",
  }},
}}];
autoAnswerCard._softphoneTargetDeviceId = "device-test";
let automaticAnswer = null;
autoAnswerCard._answer = async (options) => {{ automaticAnswer = options; }};
await autoAnswerCard._toggleAutoAnswer();
assert.equal(microphonePermissionRequests, 1);
assert.equal(autoAnswerCard._autoAnswer, true);
assert.equal(autoAnswerCard._autoAnswerMicReady, true);
assert.equal(serviceCalls.at(-1)[0], "voip_stack");
assert.equal(serviceCalls.at(-1)[1], "set_auto_answer");
assert.equal(serviceCalls.at(-1)[2].device_id, "device-casa");
assert.equal(serviceCalls.at(-1)[2].auto_answer, true);
assert.equal(storage.has("voip_auto_answer_device-casa"), false);
autoAnswerCard._applySoftphoneSnapshot({{
  endpoint_id: "default",
  device_id: "device-casa",
  auto_answer: true,
  state: "ringing",
  direction: "incoming",
  call_id: "auto-answer-A",
  audio_direction: "sendrecv",
  sequence: 1,
}});
await Promise.resolve();
await Promise.resolve();
assert.equal(automaticAnswer?.callId, "auto-answer-A");

// A denied permission cannot leave a checked preference that will silently
// remain ringing on every future incoming call.
const deniedAutoAnswerCard = makeCard();
deniedAutoAnswerCard.config = {{
  mode: "ha_softphone",
  name: "Denied",
  device_id: "device-denied",
}};
microphonePermissionError = new Error("permission denied");
const deniedServiceCount = serviceCalls.length;
await deniedAutoAnswerCard._toggleAutoAnswer();
microphonePermissionError = null;
assert.equal(deniedAutoAnswerCard._autoAnswer, false);
assert.equal(deniedAutoAnswerCard._autoAnswerMicReady, false);
assert.equal(storage.has("voip_auto_answer_device-denied"), false);
assert.equal(serviceCalls.length, deniedServiceCount);
assert.match(deniedAutoAnswerCard._errorMsg, /was not enabled.*permission denied/i);

// Cache-local state cannot override the logical phone. Two cards project the
// same backend preference even with an empty browser storage.
storage.clear();
const sharedPreferenceA = makeCard();
const sharedPreferenceB = makeCard();
for (const sharedCard of [sharedPreferenceA, sharedPreferenceB]) {{
  sharedCard.config = {{
    mode: "ha_softphone",
    endpoint_id: "shared",
    device_id: "device-shared",
  }};
  sharedCard._applySoftphoneSnapshot({{
    endpoint_id: "shared",
    device_id: "device-shared",
    state: "idle",
    auto_answer: true,
    send_video: true,
  }});
  assert.equal(sharedCard._autoAnswer, true);
  assert.equal(sharedCard._softphoneSnapshot.send_video, true);
}}

// Send Video follows the same backend-owned path. Camera access is proved by
// the current browser, then the logical phone service persists the preference.
engine.prepareVideoCameraPermission = async () => {{
  cameraPermissionChecks++;
  return true;
}};
await sharedPreferenceA._toggleVideoCamera(false);
assert.equal(serviceCalls.at(-1)[1], "set_send_video");
assert.equal(serviceCalls.at(-1)[2].send_video, false);
await sharedPreferenceA._toggleVideoCamera(true);
assert.equal(serviceCalls.at(-1)[1], "set_send_video");
assert.equal(serviceCalls.at(-1)[2].device_id, "device-shared");
assert.equal(serviceCalls.at(-1)[2].send_video, true);
assert.equal(cameraPermissionChecks, 1);
assert.equal(
  [...storage.keys()].some((key) => key.includes("video_camera")),
  false,
);
cameraPermissionChecks = 0;
engine.prepareVideoCameraPermission = async () => {{
  cameraPermissionChecks++;
  return false;
}};

// Cards accept only snapshots belonging to their configured logical phone;
// a legacy endpoint-less snapshot remains exclusive to the default card.
const kitchen = makeCard();
kitchen.config = {{
  mode: "ha_softphone",
  name: "Kitchen",
  endpoint_id: "kitchen",
  device_id: "device-kitchen",
}};
kitchen._onSoftphoneState({{ endpoint_id: "default", state: "ringing", call_id: "wrong" }});
assert.equal(kitchen._softphoneSnapshot, null);
kitchen._onSoftphoneState({{ endpoint_id: "kitchen", device_id: "device-kitchen", state: "ringing", call_id: "right" }});
assert.equal(kitchen._softphoneSnapshot.call_id, "right");
const defaultCard = makeCard();
defaultCard._onSoftphoneState({{ state: "ringing", call_id: "legacy" }});
assert.equal(defaultCard._softphoneSnapshot.call_id, "legacy");

// A newly opened page can adopt authoritative media for an already answered
// call even when it did not initiate or answer the dialog locally.
engine.ownedSoftphoneCalls.clear();
engine.active = false;
let recoveredMediaCalls = 0;
engine.resumeSession = async () => {{ recoveredMediaCalls++; return true; }};
const recoveredCard = makeCard();
recoveredCard.config = {{
  mode: "ha_softphone", endpoint_id: "kitchen", device_id: "device-kitchen",
}};
recoveredCard._onSoftphoneState({{
  endpoint_id: "kitchen", device_id: "device-kitchen",
  session_device_id: "device-kitchen", state: "in_call",
  call_id: "already-answered", capabilities: ["audio"], sequence: 1,
}});
await Promise.resolve();
assert.equal(engine.ownsSoftphoneSession("already-answered", "kitchen"), true);
assert.equal(recoveredMediaCalls, 1);
engine.releaseSoftphoneSession("already-answered", "kitchen");
engine.resumeSession = async () => true;

// Device-only card identity is immutable across backend resolution. Local
// settings and controller ownership must not jump from a Device key to an
// endpoint key after the first snapshot.
const deviceOnly = makeCard();
deviceOnly.config = {{
  mode: "ha_softphone", name: "Device only", device_id: "device-kiosk",
}};
const runtimeKeyBefore = deviceOnly._softphoneRuntimeKey();
const storageKeyBefore = deviceOnly._phonePreferenceStorageId();
const targetKeyBefore = deviceOnly._softphoneTargetStorageKey();
deviceOnly._onSoftphoneState({{
  endpoint_id: "browser:kiosk", device_id: "device-kiosk", state: "idle", sequence: 1,
}});
assert.equal(deviceOnly._softphoneRuntimeKey(), runtimeKeyBefore);
assert.equal(runtimeKeyBefore, "device:device-kiosk");
assert.equal(deviceOnly._phonePreferenceStorageId(), storageKeyBefore);
assert.equal(deviceOnly._softphoneTargetStorageKey(), targetKeyBefore);

// A disabled or removed idle logical phone must stop advertising a Call
// action immediately. Active calls retain their answer/hangup controls until
// teardown, so the availability flag is handled only by the idle branch.
const disabledPhone = makeCard();
disabledPhone._applySoftphoneSnapshot({{
  endpoint_id: "default", state: "idle", enabled: false, sequence: 1,
}});
disabledPhone._render();
assert.equal(disabledPhone._els.statusText.textContent, "Phone unavailable");
assert.equal(disabledPhone._els.statusIndicator.className, "status-indicator unavailable");
assert.equal(disabledPhone._els.callBtn.hidden, true);
assert.equal(disabledPhone._els.settingsBtn.hidden, true);

// One browser tab has one media pipeline. A call already claimed by one
// logical phone blocks Call/Answer on every other card, including the
// pre-media phase where engine.active is still false.
engine.active = false;
engine.claimSoftphoneSession("kitchen-call", "kitchen");
const hall = makeCard();
hall.config = {{
  mode: "ha_softphone",
  name: "Hall",
  endpoint_id: "hall",
  device_id: "device-hall",
}};
hall._rosterEntries = [{{
  id: "desk", name: "Desk", enabled: true, metadata: {{}},
}}];
hall._applySoftphoneSnapshot({{
  endpoint_id: "hall",
  device_id: "device-hall",
  state: "idle",
  sequence: 1,
}});
hall._render();
assert.equal(hall._els.callBtn.disabled, true);
const blockedActionCount = serviceCalls.length;
await hall._startCall();
assert.equal(serviceCalls.length, blockedActionCount);
assert.match(hall._errorMsg, /already handling another phone call/i);
hall._applySoftphoneSnapshot({{
  endpoint_id: "hall",
  device_id: "device-hall",
  state: "ringing",
  direction: "incoming",
  call_id: "hall-call",
  caller: "Door",
  sequence: 2,
}});
hall._render();
assert.equal(hall._els.answerBtn.disabled, true);
await hall._answer();
assert.equal(serviceCalls.length, blockedActionCount);
engine.releaseSoftphoneSession("kitchen-call", "kitchen");

// The page-level reservation is atomic across awaits: two cards cannot both
// answer after observing an initially free engine.
const kitchenRace = makeCard();
kitchenRace.config = {{
  mode: "ha_softphone", endpoint_id: "kitchen", device_id: "device-kitchen",
}};
kitchenRace._applySoftphoneSnapshot({{
  endpoint_id: "kitchen", device_id: "device-kitchen", state: "ringing",
  direction: "incoming", call_id: "race-kitchen", caller: "Door", sequence: 1,
}});
const hallRace = makeCard();
hallRace.config = {{
  mode: "ha_softphone", endpoint_id: "hall", device_id: "device-hall",
}};
hallRace._applySoftphoneSnapshot({{
  endpoint_id: "hall", device_id: "device-hall", state: "ringing",
  direction: "incoming", call_id: "race-hall", caller: "Door", sequence: 1,
}});
let releaseRaceLookup;
kitchenRace._getDeviceInfo = () => new Promise((resolve) => {{
  releaseRaceLookup = resolve;
}});
const answerRaceCount = serviceCalls.length;
const pendingKitchenAnswer = kitchenRace._answer();
await Promise.resolve();
await hallRace._answer();
assert.equal(serviceCalls.length, answerRaceCount);
assert.match(hallRace._errorMsg, /already handling another phone call/i);
releaseRaceLookup({{ device_id: "device-kitchen" }});
await pendingKitchenAnswer;
assert.equal(serviceCalls.length, answerRaceCount + 1);
assert.equal(serviceCalls.at(-1)[2].call_id, "race-kitchen");
engine.releaseSoftphoneSession("race-kitchen", "kitchen");

// A rejected Answer must release only the claim created by that operation.
// Otherwise every other logical phone in the page stays blocked even though
// no media WebSocket was ever attached.
const rejectedAnswer = makeCard();
rejectedAnswer.config = {{
  mode: "ha_softphone", endpoint_id: "patio", device_id: "device-patio",
}};
rejectedAnswer._applySoftphoneSnapshot({{
  endpoint_id: "patio", device_id: "device-patio", state: "ringing",
  direction: "incoming", call_id: "reject-patio", caller: "Door", sequence: 1,
}});
rejectedAnswer._hass.callService = async () => {{
  throw new Error("answer rejected");
}};
await rejectedAnswer._answer();
assert.match(rejectedAnswer._errorMsg, /answer rejected/i);
assert.equal(engine.ownsSoftphoneSession("reject-patio", "patio"), false);
const postFailureIntent = {{}};
assert.equal(engine.tryAcquireMediaIntent("hall", postFailureIntent), true);
assert.equal(engine.releaseMediaIntent(postFailureIntent), true);

// The same reservation covers outbound calls before the first WS reply has
// supplied a call-id to claim.
const startKitchen = makeCard();
startKitchen.config = {{
  mode: "ha_softphone", endpoint_id: "kitchen", device_id: "device-kitchen",
}};
startKitchen._rosterEntries = [{{ id: "desk", name: "Desk", enabled: true, metadata: {{}} }}];
const startHall = makeCard();
startHall.config = {{
  mode: "ha_softphone", endpoint_id: "hall", device_id: "device-hall",
}};
startHall._rosterEntries = [{{ id: "desk", name: "Desk", enabled: true, metadata: {{}} }}];
let releaseStartLookup;
startKitchen._getDeviceInfo = () => new Promise((resolve) => {{
  releaseStartLookup = resolve;
}});
const outboundRequests = [];
engine.startHaSoftphone = async (target, session) => {{
  outboundRequests.push([target, session]);
  return {{
    state: "calling", call_id: "start-kitchen", endpoint_id: session.endpoint_id,
    sequence: 1,
  }};
}};
const pendingKitchenStart = startKitchen._startCall();
await Promise.resolve();
await startHall._startCall();
assert.equal(outboundRequests.length, 0);
assert.match(startHall._errorMsg, /already handling another phone call/i);
releaseStartLookup({{ device_id: "device-kitchen", softphone: true }});
await pendingKitchenStart;
assert.equal(outboundRequests.length, 1);
assert.equal(outboundRequests[0][1].endpoint_id, "kitchen");
assert.equal(outboundRequests[0][1].microphone_anti_alias, true);
engine.releaseSoftphoneSession("start-kitchen", "kitchen");

// An endpoint-only card may be used before its first state snapshot. It must
// not pair that non-default endpoint with the legacy default Device selector.
const endpointOnly = makeCard();
endpointOnly.config = {{ mode: "ha_softphone", endpoint_id: "kitchen" }};
endpointOnly._rosterEntries = [{{
  id: "desk", name: "Desk", enabled: true, metadata: {{}},
}}];
let endpointOnlyStart;
engine.startHaSoftphone = async (target, session, options) => {{
  endpointOnlyStart = {{ target, session, options }};
  return {{
    state: "calling", call_id: "endpoint-only", endpoint_id: "kitchen", sequence: 1,
  }};
}};
await endpointOnly._startCall();
assert.equal(endpointOnlyStart.session.endpoint_id, "kitchen");
assert.equal(endpointOnlyStart.session.device_id, "");
engine.releaseSoftphoneSession("endpoint-only", "kitchen");

// An audio-only roster target must never initialize the browser camera. The
// backend already suppresses video in SDP; doing the same before getUserMedia
// prevents needless camera/driver work while calling ESPHome endpoints.
const audioOnly = makeCard();
audioOnly._rosterEntries = [{{
  id: "waveshare-s3", name: "Waveshare S3 Audio", enabled: true,
  metadata: {{ endpoint_kind: "esphome", capabilities: ["audio", "dtmf"] }},
}}];
audioOnly._softphoneSnapshot = {{
  state: "idle", capabilities: ["audio", "video"],
  video_camera_send_enabled: true,
}};
engine.videoCameraEnabled = true;
let audioOnlyStart;
engine.startHaSoftphone = async (target, session, options) => {{
  audioOnlyStart = {{ target, session, options }};
  return {{ state: "calling", call_id: "audio-only", sequence: 1 }};
}};
const cameraChecksBeforeAudioOnly = cameraPermissionChecks;
await audioOnly._startCall();
assert.equal(audioOnlyStart.target.endpoint_kind, "esphome");
assert.deepEqual(audioOnlyStart.target.capabilities, ["audio", "dtmf"]);
assert.equal(audioOnlyStart.options.sendVideo, false);
assert.equal(cameraPermissionChecks, cameraChecksBeforeAudioOnly);
engine.releaseSoftphoneSession("audio-only", "default");

// The local Call action may publish only the backend's provisional result.
// It must not optimistically promote the card to in_call.
const outbound = makeCard();
outbound._rosterEntries = [{{
  id: "home-ha",
  name: "Home HA",
  enabled: true,
  metadata: {{}},
}}];
let outboundStart;
engine.startHaSoftphone = async (target, session, options) => {{
  outboundStart = {{ target, session, options }};
  return {{ ...base, state: "calling", sequence: 1 }};
}};
await outbound._startCall();
assert.equal(outboundStart.target.name, "Home HA");
assert.equal(outboundStart.options.callee, "Home HA");
assert.equal(outbound._softphoneSnapshot.state, "calling");
outbound._render();
assert.equal(outbound._els.statusText.textContent, "Calling Home HA...");
// Lovelace recreating the element is not a user cancellation.  The
// page-level engine must keep the outbound SIP transaction alive so the
// replacement card can adopt it while it is calling or ringing.
assert.equal(outboundStart.options.shouldAbort(), false);
outbound.isConnected = false;
outbound.disconnectedCallback();
assert.equal(outboundStart.options.shouldAbort(), false);

// Provisional SIP responses never render as an answered call. Only the
// authoritative final state can switch the card to "In Call".
assert.equal(card._applySoftphoneSnapshot({{ ...base, state: "calling", sequence: 1 }}), true);
card._render();
assert.equal(card._els.statusText.textContent, "Calling Home HA...");
assert.equal(card._els.hangupBtn.hidden, false);
assert.equal(card._els.answerBtn.hidden, true);

assert.equal(card._applySoftphoneSnapshot({{ ...base, state: "remote_ringing", sequence: 2 }}), true);
card._render();
assert.equal(card._els.statusText.textContent, "Home HA is ringing...");
assert.equal(card._els.hangupState.textContent, "Ringing");
assert.equal(card._applySoftphoneSnapshot({{ ...base, state: "calling", sequence: 1 }}), false);
assert.equal(card._softphoneSnapshot.state, "remote_ringing");

assert.equal(card._applySoftphoneSnapshot({{ ...base, state: "in_call", sequence: 3 }}), true);
card._render();
assert.equal(card._els.statusText.textContent, "In Call: Home HA");
assert.equal(card._els.hangupState.textContent, "In call");
assert.equal(card._applySoftphoneSnapshot({{ ...base, state: "remote_ringing", sequence: 2 }}), false);
assert.equal(card._softphoneSnapshot.state, "in_call");

// Pressing Answer sends the exact call service but does not invent an
// in-call state before the backend publishes a final 200/answer snapshot.
const incoming = makeCard();
incoming._applySoftphoneSnapshot({{
  state: "ringing",
  direction: "incoming",
  call_id: "incoming-A",
  caller: "Door",
  peer_name: "Door",
  sequence: 1,
}});
await incoming._answer();
assert.equal(incoming._softphoneSnapshot.state, "ringing");
assert.equal(cameraPermissionChecks, 0);
assert.equal(JSON.stringify(serviceCalls.at(-1)), JSON.stringify([
  "voip_stack",
  "answer",
  {{ device_id: "__voip_stack_ha_softphone__", call_id: "incoming-A", send_video: false }},
]));
incoming._applySoftphoneSnapshot({{
  state: "answering",
  direction: "incoming",
  call_id: "incoming-A",
  caller: "Door",
  peer_name: "Door",
  sequence: 2,
}});
incoming._render();
assert.equal(incoming._els.statusText.textContent, "Answering Door...");
incoming._applySoftphoneSnapshot({{
  state: "in_call",
  direction: "incoming",
  call_id: "incoming-A",
  caller: "Door",
  peer_name: "Door",
  sequence: 3,
}});
incoming._render();
assert.equal(incoming._els.statusText.textContent, "In Call: Door");

// If a different call replaces the ring while device lookup is pending, the
// stale Answer operation is discarded and cannot answer the new call.
const race = makeCard();
race._applySoftphoneSnapshot({{
  state: "ringing", direction: "incoming", call_id: "race-A", caller: "A", sequence: 1,
}});
let releaseLookup;
race._getDeviceInfo = () => new Promise((resolve) => {{ releaseLookup = resolve; }});
const pendingAnswer = race._answer();
await Promise.resolve();
race._applySoftphoneSnapshot({{
  state: "ringing", direction: "incoming", call_id: "race-B", caller: "B", sequence: 1,
}});
const serviceCount = serviceCalls.length;
releaseLookup({{ device_id: "__voip_stack_ha_softphone__" }});
await pendingAnswer;
assert.equal(serviceCalls.length, serviceCount);
assert.equal(race._softphoneSnapshot.call_id, "race-B");

// Home Assistant may recreate the Lovelace element while an Answer request is
// in flight.  The page-level engine owns the media session, so the detached
// element must not compensate with a late hangup: the replacement card will
// adopt the same authoritative backend call.
const replacement = makeCard();
replacement._applySoftphoneSnapshot({{
  state: "ringing", direction: "incoming", call_id: "replace-A", caller: "Door", sequence: 1,
}});
let releaseAnswer;
const replacementCalls = [];
replacement._hass.callService = async (...args) => {{
  replacementCalls.push(args);
  if (args[1] === "answer") await new Promise((resolve) => {{ releaseAnswer = resolve; }});
}};
const pendingReplacementAnswer = replacement._answer({{ videoPermission: false }});
for (let attempt = 0; attempt < 10 && !releaseAnswer; attempt++) {{
  await Promise.resolve();
}}
assert.equal(replacementCalls.length, 1);
replacement._lifecycleGeneration++;
replacement.isConnected = false;
releaseAnswer();
await pendingReplacementAnswer;
assert.equal(JSON.stringify(replacementCalls), JSON.stringify([[
  "voip_stack",
  "answer",
  {{ device_id: "__voip_stack_ha_softphone__", call_id: "replace-A", send_video: false }},
]]));

// Hangup is call-scoped and likewise waits for the backend terminal snapshot.
engine.active = true;
engine.callId = "incoming-A";
engine.softphoneCallId = "incoming-A";
incoming._loadSoftphoneState = async () => {{}};
await incoming._hangup();
assert.equal(JSON.stringify(serviceCalls.at(-1)), JSON.stringify([
  "voip_stack",
  "hangup",
  {{ device_id: "__voip_stack_ha_softphone__", call_id: "incoming-A" }},
]));

// A non-default phone keeps endpoint_id for subscriptions/media correlation,
// but every public action sends only its Home Assistant Device selector.
const testPhone = makeCard();
testPhone.config = {{
  mode: "ha_softphone", endpoint_id: "browser:test", device_id: "device-test",
}};
testPhone._applySoftphoneSnapshot({{
  endpoint_id: "browser:test", device_id: "device-test", state: "in_call",
  direction: "outgoing", call_id: "test-to-ws3", peer_name: "WS3", sequence: 1,
}});
testPhone._loadSoftphoneState = async () => {{}};
await testPhone._hangup();
assert.equal(JSON.stringify(serviceCalls.at(-1)), JSON.stringify([
  "voip_stack",
  "hangup",
  {{ device_id: "device-test", call_id: "test-to-ws3" }},
]));
assert.equal(incoming._softphoneSnapshot.state, "in_call");
incoming._applySoftphoneSnapshot({{
  ...incoming._softphoneSnapshot,
  state: "idle",
  terminal_reason: "local_hangup",
  sequence: 4,
}});
incoming._render();
assert.equal(incoming._els.statusText.textContent, "Call with Door ended.");
assert.match(incoming._els.statusReason.textContent, /Local hangup/);

// A dialed extension or local logical-phone name identifies routing, not the
// remote participant shown after teardown.
const outgoingEnded = makeCard();
outgoingEnded._applySoftphoneSnapshot({{
  state: "idle", direction: "outgoing", call_id: "outgoing-ended",
  caller: "Casa", callee: "Waveshare S3 Audio",
  peer_name: "Waveshare S3 Audio", dialed_target: "1000",
  terminal_reason: "remote_hangup", sequence: 1,
}});
outgoingEnded._render();
assert.equal(
  outgoingEnded._els.statusText.textContent,
  "Call with Waveshare S3 Audio ended.",
);

const incomingEnded = makeCard();
incomingEnded._applySoftphoneSnapshot({{
  state: "idle", direction: "incoming", call_id: "incoming-ended",
  caller: "Waveshare S3 Audio", callee: "Test", dialed_target: "Test",
  terminal_reason: "local_hangup", sequence: 1,
}});
incomingEnded._render();
assert.equal(
  incomingEnded._els.statusText.textContent,
  "Call with Waveshare S3 Audio ended.",
);

// The authoritative endpoint-wide idle snapshot normally has no call_id.
// It must still release and close the browser media session that owned the
// just-ended call; an empty call_id is not a stale terminal for another call.
engine.active = true;
engine.endpointId = "default";
engine.callId = "remote-ended";
engine.claimSoftphoneSession("remote-ended", "default");
const remoteEnded = makeCard();
remoteEnded._applySoftphoneSnapshot({{
  endpoint_id: "default",
  state: "idle",
  call_id: "",
  terminal_reason: "remote_hangup",
  sequence: 5,
}});
await Promise.resolve();
assert.equal(engine.ownsSoftphoneSession("remote-ended", "default"), false);
assert.equal(engine.active, false);
assert.deepEqual(engineEvents.at(-1), ["close", "terminal", "remote-ended"]);

// Media-engine failures are visible in the owning card instead of being
// console-only diagnostics.
const engineErrorCard = makeCard();
engineErrorCard._engineErrorListener({{ detail: "Audio media update failed" }});
assert.match(engineErrorCard._errorMsg, /audio media update failed/i);

// A standards-valid audio fallback must still tell the user why the offered
// video stream was rejected instead of hiding the failure in backend logs.
const degradedVideo = makeCard();
    degradedVideo._applySoftphoneSnapshot({{
  endpoint_id: "default", device_id: "__voip_stack_ha_softphone__",
  state: "in_call", direction: "incoming", call_id: "video-degraded",
  peer_name: "Door", sequence: 1, video_requested: true,
  video_negotiated: false, video_status: "degraded",
  video_failure_reason: "local_video_resources_unavailable",
    }});
    degradedVideo._render();
    assert.equal(degradedVideo._els.statusReason.textContent, "");
    degradedVideo.config.show_extended_info = true;
    degradedVideo._render();
    assert.match(degradedVideo._els.statusReason.textContent, /Video unavailable/i);
assert.match(degradedVideo._els.statusReason.textContent, /allocate video media/i);

// A rejected Hangup keeps the exact call claim and attached media available
// so the user can retry instead of silently becoming a spectator.
const rejectedHangup = makeCard();
rejectedHangup._applySoftphoneSnapshot({{
  endpoint_id: "default", device_id: "__voip_stack_ha_softphone__",
  state: "in_call", direction: "outgoing", call_id: "hangup-rejected",
  peer_name: "Desk", sequence: 1,
}});
engine.claimSoftphoneSession("hangup-rejected", "default");
engine.active = true;
engine.endpointId = "default";
engine.callId = "hangup-rejected";
rejectedHangup._hass.callService = async () => {{ throw new Error("hangup denied"); }};
rejectedHangup._loadSoftphoneState = async () => {{}};
await rejectedHangup._hangup();
assert.equal(engine.ownsSoftphoneSession("hangup-rejected", "default"), true);
assert.match(rejectedHangup._errorMsg, /hangup denied/i);
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
def test_card_model_normalises_roster_targets_and_media_capabilities() -> None:
    """Exercise the shared pure model used by both the card and its editor."""

    script = rf"""
import assert from "assert/strict";
import {{ pathToFileURL }} from "url";
const model = await import(pathToFileURL({json.dumps(str(CARD_MODEL))}));

assert.deepEqual(model.formatListFromMetadata("audio; video ;"), ["audio", "video"]);
assert.deepEqual(model.formatListFromMetadata(["audio", 8, ""]), ["audio", "8"]);
assert.deepEqual(model.formatListFromMetadata(null), []);

const esp = model.targetFromRosterEntry({{
  id: "ws3",
  name: "Kitchen ESP",
  address: "192.0.2.10",
  port: 5060,
  metadata: {{
    endpoint_kind: " ESPHome ",
    capabilities: "audio;dtmf",
    sip_transport: "udp",
    audio_mode: "speaker_only",
    camera_entity_id: "camera.kitchen_esp",
    sip_video_codec: "",
  }},
}});
assert.equal(esp.route_id, "ws3");
assert.equal(esp.endpoint_kind, "esphome");
assert.deepEqual(esp.capabilities, ["audio", "dtmf"]);
assert.equal(esp.camera_entity_id, "camera.kitchen_esp");
assert.equal(esp.sip_video_codec, "");

assert.equal(model.normaliseTransport("sip_tcp"), "TCP");
assert.equal(model.normaliseTransport(" UDP "), "UDP");
assert.equal(model.normaliseTransport("ws"), "");
assert.equal(model.normaliseAudioMode("mic_only"), "mic_only");
assert.equal(model.normaliseAudioMode("invalid"), "full_duplex");
assert.equal(model.audioModeLabel("speaker_only"), "SPK");
assert.equal(model.audioModeLabel("invalid"), "FULL");
assert.equal(model.formatCallDuration(100, 165), "01:05");
assert.equal(model.formatCallDuration(100, 3761), "1:01:01");
assert.equal(model.reasonKey("Remote Hangup"), "remote_hangup");
assert.equal(model.reasonKey("vendor failure"), "");
assert.equal(model.formatKnownReason("media-incompatible"), "Media incompatible");
assert.equal(model.formatEndReason({{ kind: "declined", reason: "No thanks", origin: "self" }}), "Local decline: \"No thanks\"");
assert.equal(model.formatEndReason({{ kind: "error", reason: "488", origin: "remote" }}), "Remote error (code 488)");
assert.equal(model.formatVideoFailureReason("remote_video_rejected"), "The remote endpoint rejected video.");
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
def test_card_picker_suggests_only_primary_controllable_phone_entities() -> None:
    """Exercise the synchronous Home Assistant card-picker callback."""

    script = rf"""
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";

const source = fs.readFileSync({json.dumps(str(CARD))}, "utf8");
const registration = source.slice(source.indexOf("window.customCards ="));
const window = {{ customCards: [] }};
vm.runInNewContext(registration, {{ window }});
const suggest = window.customCards[0].getEntitySuggestion;

const hass = {{
  entities: {{
    "sensor.default_call_state": {{ platform: "voip_stack", device_id: "device-default" }},
    "sensor.casa_call_state": {{ platform: "voip_stack", device_id: "device-casa", translation_key: "phone_endpoint_call_state" }},
    "sensor.ws3_call_state": {{ platform: "voip_stack", device_id: "device-ws3", translation_key: "phone_endpoint_call_state" }},
    "sensor.new_phone_call_state": {{ platform: "voip_stack", device_id: "device-new", translation_key: "phone_endpoint_call_state" }},
    "sensor.new_esp_call_state": {{ platform: "voip_stack", device_id: "device-new-esp", translation_key: "phone_endpoint_call_state" }},
    "sensor.mobile_call_state": {{ platform: "voip_stack", device_id: "device-mobile", translation_key: "phone_endpoint_call_state" }},
    "sensor.casa_endpoint": {{ platform: "voip_stack", device_id: "device-casa", translation_key: "phone_endpoint_connectivity" }},
    "switch.casa_dnd": {{ platform: "voip_stack", device_id: "device-casa", translation_key: "phone_endpoint_dnd" }},
    "sensor.foreign_call_state": {{ platform: "other", device_id: "device-foreign" }},
    "sensor.voip_phonebook": {{ platform: "voip_stack" }},
  }},
  devices: {{
    "device-default": {{ model: "Home Assistant softphone" }},
    "device-casa": {{ model: "Home Assistant softphone" }},
    "device-ws3": {{ model: "ESP32-S3" }},
    "device-new": {{ model: "Home Assistant softphone" }},
    "device-new-esp": {{ model: "ESP32-S3" }},
    "device-mobile": {{ model: "SIP account" }},
  }},
  states: {{
    "sensor.default_call_state": {{ attributes: {{ endpoint_id: "default", endpoint_kind: "browser" }} }},
    "sensor.casa_call_state": {{ attributes: {{ endpoint_id: "browser:casa", endpoint_kind: "browser" }} }},
    "sensor.ws3_call_state": {{ attributes: {{ endpoint_id: "esphome:ws3", endpoint_kind: "esphome" }} }},
    "sensor.new_phone_call_state": {{ state: "unavailable", attributes: {{}} }},
    "sensor.new_esp_call_state": {{ state: "unavailable", attributes: {{}} }},
    "sensor.mobile_call_state": {{ attributes: {{ endpoint_id: "sip:mobile", endpoint_kind: "sip_account" }} }},
    "sensor.casa_endpoint": {{ attributes: {{ endpoint_id: "browser:casa", endpoint_kind: "browser" }} }},
    "switch.casa_dnd": {{ attributes: {{ endpoint_id: "browser:casa", endpoint_kind: "browser" }} }},
    "sensor.foreign_call_state": {{ attributes: {{ endpoint_id: "browser:foreign", endpoint_kind: "browser" }} }},
    "sensor.voip_phonebook": {{ attributes: {{}} }},
  }},
}};

assert.deepEqual(
  JSON.parse(JSON.stringify(suggest(hass, "sensor.default_call_state"))),
  {{ config: {{ type: "custom:voip-stack-card", mode: "ha_softphone", device_id: "device-default" }} }},
);
assert.deepEqual(
  JSON.parse(JSON.stringify(suggest(hass, "sensor.casa_call_state"))),
  {{ config: {{ type: "custom:voip-stack-card", mode: "ha_softphone", device_id: "device-casa" }} }},
);
assert.deepEqual(
  JSON.parse(JSON.stringify(suggest(hass, "sensor.ws3_call_state"))),
  {{ config: {{ type: "custom:voip-stack-card", mode: "esp_mirror", device_id: "device-ws3" }} }},
);
assert.deepEqual(
  JSON.parse(JSON.stringify(suggest(hass, "sensor.new_phone_call_state"))),
  {{ config: {{ type: "custom:voip-stack-card", mode: "ha_softphone", device_id: "device-new" }} }},
);
assert.deepEqual(
  JSON.parse(JSON.stringify(suggest(hass, "sensor.new_esp_call_state"))),
  {{ config: {{ type: "custom:voip-stack-card", mode: "esp_mirror", device_id: "device-new-esp" }} }},
);
assert.equal(suggest(hass, "sensor.mobile_call_state"), null);
assert.equal(suggest(hass, "sensor.casa_endpoint"), null);
assert.equal(suggest(hass, "switch.casa_dnd"), null);
assert.equal(suggest(hass, "sensor.foreign_call_state"), null);
assert.equal(suggest(hass, "sensor.voip_phonebook"), null);
assert.equal(suggest(hass, "sensor.missing"), null);
assert.equal(JSON.stringify(suggest(hass, "sensor.casa_call_state")).includes("endpoint_id"), false);
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
