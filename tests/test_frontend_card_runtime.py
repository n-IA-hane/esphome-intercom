#!/usr/bin/env python3
"""Runtime anti-regressions for HA softphone card signaling state."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.js_runtime


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
const entityIds = await import(pathToFileURL({json.dumps(str(CARD.with_name("voip-stack-entity-ids.js")))}));

let source = fs.readFileSync({json.dumps(str(CARD))}, "utf8");
source = source
  .replace(/const \{{ VoipEntityIds \}} = await import\(`\.\/voip-stack-entity-ids\.js[^;]+;/, "const {{ VoipEntityIds }} = globalThis.__entityIds;")
  .replace(
    /const \{{ voipStackTranslate \}} = await import\(`\.\/voip-stack-i18n\.js[^;]+;/,
    "const {{ voipStackTranslate }} = globalThis.__i18n;",
  )
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
      mirroredEndpointPeerLabel, normaliseAudioMode, normaliseTransport, reasonKey,
      softphoneSnapshotSupersedes, targetFromRosterEntry, terminalPeerLabel,
    }} = globalThis.__cardModel;`,
  )
  .replace(/await import[^;]*voip-stack-dashboard-card[^;]*;/, "")
  .replace("class VoipStackCard extends HTMLElement", "export class VoipStackCard extends HTMLElement");

const serviceCalls = [];
const engineEvents = [];
let cameraPermissionChecks = 0;
let audioPermissionChecks = 0;
const engine = {{
  configure() {{}},
  async terminateSoftphoneCall(service, scope) {{
    const {{ endpoint_id, ...data }} = scope;
    serviceCalls.push(["voip_stack", service, data]);
    this.releaseSoftphoneSession(scope.call_id, endpoint_id);
    if (this.endpointId === endpoint_id && this.callId === scope.call_id) {{
      await this.close("terminal_control");
    }}
  }},
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
  activateSoftphoneController(card) {{ return !!card?.isConnected; }},
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
  async prepareAudioCall() {{ audioPermissionChecks++; return true; }},
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
    this.dataset = {{}};
  }}
  setAttribute(name, value) {{ this.attributes.set(name, String(value)); }}
  replaceChildren(...children) {{
    this.children = children;
    for (const child of children) child.parentNode = this;
  }}
  appendChild(child) {{ (this.children ||= []).push(child); child.parentNode = this; return child; }}
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
  __i18n: {{ voipStackTranslate: (_hass, value) => String(value ?? "") }},
  __cardModel: cardModel,
  __entityIds: entityIds,
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

assert.equal(cardModel.terminalPeerLabel({{
  direction: "incoming",
  caller: "n-IA-hane",
  peer_name: "Waveshare P4 Touch",
  connected_party: "Waveshare P4 Touch",
  answered_by: "Waveshare P4 Touch",
}}), "n-IA-hane");
assert.equal(cardModel.terminalPeerLabel({{
  direction: "outgoing",
  callee: "RG Casa",
  peer_name: "RG Casa",
  connected_party: "Waveshare P4 Touch",
}}), "Waveshare P4 Touch");
assert.equal(cardModel.mirroredEndpointPeerLabel({{
  direction: "outgoing",
  caller: "CG Casa",
  callee: "Waveshare S3 Audio",
  peer_name: "Waveshare S3 Audio",
}}, "Waveshare S3 Audio"), "CG Casa");
assert.equal(cardModel.mirroredEndpointPeerLabel({{
  direction: "outgoing",
  caller: "Waveshare P4 Touch",
  callee: "Daniele",
  peer_name: "Daniele",
}}, "Waveshare P4 Touch"), "Daniele");

function elements() {{
  const names = [
    "answerBtn", "autoAnswerCheckbox", "autoAnswerRow", "callBtn", "card",
    "declineBtn", "destRow", "destSelect", "destValue", "destValueWrap",
    "dndCheckbox", "dndRow", "err", "hangupBtn", "hangupDuration",
    "hangupPeer", "hangupState", "header", "headerName", "keypadBtn", "keypadLabel",
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

const activityIdentity = makeCard();
activityIdentity._getSoftphoneEndpointId = () => "office";
activityIdentity._getConfigDeviceId = () => "device-office";
activityIdentity._activeSoftphonePeerDevice = () => null;
activityIdentity._layoutPhoneActivity = () => {{}};
activityIdentity._els.phoneActivity = new FakeElement();
activityIdentity._els.parkingNotice = new FakeElement();
activityIdentity._softphoneSnapshot = {{state: "in_call", call_id: "current-call"}};
activityIdentity._phoneActivity = {{parked: [], phones: [
  {{endpoint_id: "peer", name: "Peer", state: "busy", call_ids: ["current-call"]}},
  {{endpoint_id: "other", name: "Other", state: "busy", call_ids: ["another-call"]}},
]}};
activityIdentity._renderPhoneActivity(false);
assert.deepEqual(JSON.parse(activityIdentity._els.phoneActivity.dataset.rows), ["Other: Busy"]);
activityIdentity._softphoneSnapshot = {{state: "idle", call_id: ""}};
activityIdentity._renderPhoneActivity(false);
assert.deepEqual(JSON.parse(activityIdentity._els.phoneActivity.dataset.rows), ["Peer: Busy", "Other: Busy"]);

const manualIdentity = makeCard();
manualIdentity._softphoneTargets = () => [{{device_id: "ball", name: "Spotpear"}}];
manualIdentity._softphoneTargetDeviceId = "ball";
manualIdentity._softphoneSnapshot = {{state:"remote_ringing",direction:"outgoing",call_id:"new",dialed_target:"426"}};
assert.equal(manualIdentity._getDestination(), "426", "Use the current call, not the idle picker");
assert.equal(manualIdentity._activeSoftphonePeerDevice(), null, "Do not attach the previously selected ESP camera");
manualIdentity._softphoneSnapshot = {{state:"remote_ringing",direction:"outgoing",call_id:"new"}};
assert.equal(manualIdentity._getDestination(), "Unknown");
manualIdentity._softphoneSnapshot = {{state:"idle"}};
assert.equal(manualIdentity._getDestination(), "Spotpear");

const ws3Mirror = makeCard();
ws3Mirror.config = {{ mode: "esp_mirror", name: "Waveshare S3 Audio" }};
ws3Mirror._activeDeviceInfo = {{
  device_id: "device-ws3", name: "Waveshare S3 Audio", softphone: false,
}};
ws3Mirror._onMirroredBridgeStateEvent({{ data: {{
  scope: "sip_bridge", state: "in_call", direction: "outgoing",
  caller: "CG Casa", callee: "Waveshare S3 Audio",
  peer_name: "Waveshare S3 Audio", dest_device_id: "device-ws3",
}} }});
assert.equal(ws3Mirror._mirroredConnectedPeer, "CG Casa");
ws3Mirror._videoSendSwitchEntityId = "switch.p4_send_video";
ws3Mirror._hass.states["switch.p4_send_video"] = {{ state: "on" }};
const mirrorVideoCallsBefore = serviceCalls.length;
await ws3Mirror._toggleVideoCamera(false);
assert.equal(
  JSON.stringify(serviceCalls[mirrorVideoCallsBefore]),
  JSON.stringify(["switch", "turn_off", {{ entity_id: "switch.p4_send_video" }}]),
);

// The ESP accepts video direction changes during a call, so its mirror must
// expose the same Options surface there as it does before dialing.
const p4Mirror = makeCard();
p4Mirror.config = {{ mode: "esp_mirror", device_id: "device-p4" }};
p4Mirror._getEspState = () => "in_call";
p4Mirror._videoSendSwitchEntityId = "switch.p4_send_video";
p4Mirror._hass.states["switch.p4_send_video"] = {{ state: "on" }};
p4Mirror._settingsOpen = true;
p4Mirror._render();
assert.equal(p4Mirror._els.settingsBtn.hidden, false);
assert.equal(p4Mirror._els.settingsPanel.hidden, false);
assert.equal(p4Mirror._els.videoCameraRow.hidden, false);
assert.equal(p4Mirror._els.videoCameraCheckbox.checked, true);
// Service completion can precede the ESP state event. That later update must
// repaint the checkbox on its own, without another click or call-state change.
p4Mirror._voipStateEntityId = "sensor.p4_voip_state";
p4Mirror._hass.states["sensor.p4_voip_state"] = {{ state: "in_call" }};
p4Mirror._availableDevices = [{{ device_id: "device-p4", name: "P4" }}];
p4Mirror._loadSharedRoster = () => {{}};
await p4Mirror._toggleVideoCamera(false);
p4Mirror.hass = {{ ...p4Mirror._hass, states: {{
  ...p4Mirror._hass.states, "switch.p4_send_video": {{ state: "off" }},
}} }};
assert.equal(p4Mirror._els.videoCameraCheckbox.checked, false);
await p4Mirror._toggleVideoCamera(true);
p4Mirror.hass = {{ ...p4Mirror._hass, states: {{
  ...p4Mirror._hass.states, "switch.p4_send_video": {{ state: "on" }},
}} }};
assert.equal(p4Mirror._els.videoCameraCheckbox.checked, true);
for (const pendingState of ["calling", "ringing", "connecting"]) {{
  p4Mirror._getEspState = () => pendingState;
  p4Mirror._render();
  assert.equal(p4Mirror._els.settingsPanel.hidden, true);
}}

const card = makeCard();
// Routing controls display names while persistent references remain opaque.
const routingCard = makeCard();
routingCard.config = {{ mode: "ha_softphone", device_id: "device-kitchen" }};
const routingRequests = [];
routingCard._hass = {{
  services: {{ voip_stack: {{ set_phone_settings: {{}} }} }},
  callService: async (...args) => routingRequests.push(args),
}};
routingCard._rosterEntries = [
  {{ name: "Kitchen", metadata: {{ device_id: "device-kitchen", endpoint_id: "kitchen",
    call_policy: {{ always: {{ kind: "contact", target: "bedroom" }} }} }} }},
  {{ name: "Renamed bedroom", metadata: {{ endpoint_id: "bedroom" }} }},
];
const routingInput = {{ value: "" }};
routingCard._els.routingPanel = {{ hidden: true }};
routingCard._els.routingControls = {{
  forward_to: {{ input: routingInput, options: {{ dataset: {{}}, replaceChildren() {{}} }} }},
}};
routingCard._renderRoutingControls(true);
assert.equal(routingCard._els.routingPanel.hidden, false);
assert.equal(routingInput.value, "Renamed bedroom");
routingCard._render = () => {{}};
await routingCard._setPhoneRouting("forward_to", "");
assert.deepEqual(JSON.parse(JSON.stringify(routingRequests)), [["voip_stack", "set_phone_settings", {{
  device_id: "device-kitchen", forward_to: "",
}}]]);
assert.equal(routingCard._routingSettingsBusy, false);
// Trunk choices reuse HA's device registry and keep missing selections explicit.
routingCard._hass.devices = {{ line: {{ model: "SIP trunk", name: "Office",
  identifiers: [["voip_stack", "trunk:work"]] }} }};
routingCard._els.trunkRow = {{ hidden: true }};
let trunkOptions = [];
routingCard._els.trunkSelect = {{ value: "", dataset: {{}}, replaceChildren(...items) {{ trunkOptions = items; }} }};
routingCard._renderTrunkPreference(null);
assert.equal(routingCard._els.trunkSelect.disabled, true);
assert.equal(trunkOptions[0].textContent, "Loading...");
routingCard._renderTrunkPreference({{ outbound_trunk_id: "work" }});
assert.equal(routingCard._els.trunkSelect.disabled, false);
assert.equal(routingCard._els.trunkSelect.value, "work");
assert.equal(trunkOptions[1].textContent, "Office");
routingCard._renderTrunkPreference({{ outbound_trunk_id: "removed-line" }});
assert.equal(routingCard._els.trunkSelect.value, "removed-line");
assert.equal(trunkOptions.at(-1).textContent, "Unavailable trunk");
await routingCard._setPhoneRouting("outbound_trunk", "work");
assert.deepEqual(JSON.parse(JSON.stringify(routingRequests.pop())), [
  "voip_stack", "set_phone_settings", {{ device_id: "device-kitchen", outbound_trunk: "work" }},
]);
// Enabling a timeout requires an explicit outcome; disabling sends only zero.
routingCard._ringingDraft = {{ deviceId: "device-kitchen", enabled: true,
  seconds: "25", action: "", destination: "Renamed bedroom" }};
await routingCard._saveRingingPolicy();
assert.equal(routingRequests.length, 1);
assert.match(routingCard._errorMsg, /no-answer action/);
routingCard._ringingDraft.action = "forward";
await routingCard._saveRingingPolicy();
assert.deepEqual(JSON.parse(JSON.stringify(routingRequests.at(-1))), [
  "voip_stack", "set_phone_settings", {{ device_id: "device-kitchen",
    ringing_timeout: 25, no_answer_action: "forward", no_answer_to: "Renamed bedroom" }},
]);
assert.equal(routingCard._ringingDraft.seconds, "25");
// Keep the submitted form visible until the matching backend state arrives.
routingCard._els.ringingControls = Object.fromEntries(
  ["enabled","seconds","action","destination","secondsRow","actionRow","destinationRow","save"].map(key => [key, {{}}])
);
const ringingEntries = [
  {{name:"Kitchen",metadata:{{device_id:"device-kitchen",endpoint_id:"kitchen"}}}},
  {{name:"Renamed bedroom",metadata:{{endpoint_id:"bedroom"}}}},
];
routingCard._renderRingingControls("device-kitchen", {{}}, ringingEntries);
assert.equal(routingCard._els.ringingControls.seconds.value, "25");
assert.equal(routingCard._els.ringingControls.enabled.checked, true);
routingCard._renderRingingControls("device-kitchen", {{ringing_timeout:25,no_answer:{{kind:"contact",target:"bedroom"}}}}, ringingEntries);
assert.equal(routingCard._ringingDraft, null);

routingCard._ringingDraft = {{ deviceId: "device-kitchen", enabled: false,
  seconds: "25", action: "forward", destination: "Renamed bedroom" }};
await routingCard._saveRingingPolicy();
assert.deepEqual(JSON.parse(JSON.stringify(routingRequests.at(-1))), [
  "voip_stack", "set_phone_settings", {{ device_id: "device-kitchen", ringing_timeout: 0 }},
]);
const antiAliasPreference = makeCard();
// Mailbox timeout controls use the same public settings action, including
// the ordinary own-mailbox default without a manually entered endpoint ID.
routingCard._ringingDraft = {{ deviceId: "device-kitchen", enabled: true,
  seconds: "30", action: "voicemail", destination: "" }};
await routingCard._saveRingingPolicy();
assert.deepEqual(JSON.parse(JSON.stringify(routingRequests.at(-1))), [
  "voip_stack", "set_phone_settings", {{device_id: "device-kitchen",
    ringing_timeout: 30, no_answer_action: "voicemail", no_answer_to: ""}},
]);
routingCard._renderRingingControls("device-kitchen", {{ringing_timeout:10,no_answer:{{kind:"voicemail",target:"kitchen"}}}}, ringingEntries);
assert.equal(routingCard._els.ringingControls.seconds.value, "30");
routingCard._renderRingingControls("device-kitchen", {{ringing_timeout:30,no_answer:{{kind:"voicemail",target:"kitchen"}}}}, ringingEntries);
assert.equal(routingCard._ringingDraft, null);
routingCard._loadSoftphoneState = async () => {{}};
await routingCard._setMailboxEnabled(true);
assert.deepEqual(JSON.parse(JSON.stringify(routingRequests.at(-1))), [
  "voip_stack", "set_voicemail_settings", {{device_id: "device-kitchen", enabled: true}},
]);
// Parking delegates to the public action; the list follows authoritative activity.
const parkRequests = [];
routingCard._hass.callWS = async (request) => {{
  parkRequests.push(request);
  return {{response: {{slot: "705", call_id: "retained-call", generation: 3}}}};
}};
await routingCard._parkCall();
assert.deepEqual(JSON.parse(JSON.stringify(parkRequests[0])), {{
  type: "call_service", domain: "voip_stack", service: "park",
  service_data: {{device_id: "device-kitchen"}}, return_response: true,
}});
routingCard._onCallEvent({{event_type: "voip_stack.activity", data: {{parked: ["705"], phones: []}}}});
assert.deepEqual(routingCard._phoneActivity.parked, ["705"]);
routingCard._onCallEvent({{event_type: "voip_stack.activity", data: {{parked: [], phones: []}}}});
assert.equal(routingCard._phoneActivity.parked.length, 0);
assert.equal(routingCard._parkingBusy, false);
routingCard._softphoneSnapshot = {{state: "in_call", call_id: "recording-call", recording: {{active: false}}}};
await routingCard._toggleRecording();
assert.deepEqual(JSON.parse(JSON.stringify(routingRequests.at(-1))), [
  "voip_stack", "start_recording", {{device_id: "device-kitchen", call_id: "recording-call"}},
]);
routingCard._softphoneSnapshot.recording = {{active: true, owned: true}};
await routingCard._toggleRecording();
assert.equal(routingRequests.at(-1)[1], "stop_recording");
assert.equal(routingCard._recordingBusy, false);
routingCard._hass.callWS = async () => {{throw new Error("Parking lot is full");}};
await routingCard._parkCall();
assert.equal(routingCard._parkingBusy, false);
assert.match(routingCard._errorMsg, /Parking lot is full/);
assert.equal(antiAliasPreference._microphoneAntiAliasEnabled(), true);
antiAliasPreference._toggleMicrophoneAntiAlias();
assert.equal(antiAliasPreference._microphoneAntiAliasEnabled(), false);
assert.equal(
  storage.get("voip_microphone_anti_alias_preferred"),
  "false",
);
antiAliasPreference._toggleMicrophoneAntiAlias();
assert.equal(antiAliasPreference._microphoneAntiAliasEnabled(), true);
assert.equal(storage.get("voip_microphone_anti_alias_preferred"), "true");
const base = {{
  endpoint_id: "browser:office",
  device_id: "device-office",
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

// Cards accept only snapshots belonging to their configured logical phone.
const kitchen = makeCard();
kitchen.config = {{
  mode: "ha_softphone",
  name: "Kitchen",
  device_id: "device-kitchen",
}};
kitchen._onSoftphoneState({{ endpoint_id: "default", state: "ringing", call_id: "wrong" }});
assert.equal(kitchen._softphoneSnapshot, null);
kitchen._onSoftphoneState({{ endpoint_id: "kitchen", device_id: "device-kitchen", state: "ringing", call_id: "right" }});
assert.equal(kitchen._softphoneSnapshot.call_id, "right");
const defaultCard = makeCard();
defaultCard._onSoftphoneState({{ state: "ringing", call_id: "legacy" }});
assert.equal(defaultCard._softphoneSnapshot, null);

// A newly opened page can adopt authoritative media for an already answered
// call even when it did not initiate or answer the dialog locally.
engine.ownedSoftphoneCalls.clear();
engine.active = false;
let recoveredMediaCalls = 0;
engine.resumeSession = async () => {{ recoveredMediaCalls++; return true; }};
const recoveredCard = makeCard();
recoveredCard.config = {{
  mode: "ha_softphone", device_id: "device-kitchen",
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
  endpoint_id: "browser:disabled", device_id: "device-disabled",
  state: "idle", enabled: false, sequence: 1,
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
  mode: "ha_softphone", device_id: "device-kitchen",
}};
kitchenRace._applySoftphoneSnapshot({{
  endpoint_id: "kitchen", device_id: "device-kitchen", state: "ringing",
  direction: "incoming", call_id: "race-kitchen", caller: "Door", sequence: 1,
}});
const hallRace = makeCard();
hallRace.config = {{
  mode: "ha_softphone", device_id: "device-hall",
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
  mode: "ha_softphone", device_id: "device-kitchen",
}};
startKitchen._rosterEntries = [{{ id: "desk", name: "Desk", enabled: true, metadata: {{}} }}];
const startHall = makeCard();
startHall.config = {{
  mode: "ha_softphone", device_id: "device-hall",
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
releaseStartLookup({{
  device_id: "device-kitchen", endpoint_id: "kitchen", softphone: true,
}});
await pendingKitchenStart;
assert.equal(outboundRequests.length, 1);
assert.equal(outboundRequests[0][1].endpoint_id, "kitchen");
assert.equal(outboundRequests[0][1].microphone_anti_alias, true);
engine.releaseSoftphoneSession("start-kitchen", "kitchen");

// A Device-only card resolves its internal endpoint from the backend before
// starting media, without persisting that endpoint in card configuration.
const deviceOnlyOutbound = makeCard();
deviceOnlyOutbound.config = {{ mode: "ha_softphone", device_id: "device-kitchen" }};
deviceOnlyOutbound._getDeviceInfo = async () => ({{
  device_id: "device-kitchen", endpoint_id: "kitchen", softphone: true,
}});
deviceOnlyOutbound._rosterEntries = [{{
  id: "desk", name: "Desk", enabled: true, metadata: {{}},
}}];
let deviceOnlyStart;
engine.startHaSoftphone = async (target, session, options) => {{
  deviceOnlyStart = {{ target, session, options }};
  return {{
    state: "calling", call_id: "endpoint-only", endpoint_id: "kitchen", sequence: 1,
  }};
}};
await deviceOnlyOutbound._startCall();
assert.equal(deviceOnlyStart.session.endpoint_id, "kitchen");
assert.equal(deviceOnlyStart.session.device_id, "device-kitchen");
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
  send_video: true,
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

// A service acknowledgement is not a complete phone-state snapshot.
const acknowledged = makeCard();
acknowledged.config.device_id = "device-office";
acknowledged._softphoneSnapshot = {{...base, state:"idle", send_video:true, capabilities:["audio","video"]}};
acknowledged._ensureHaSoftphoneAudioPath = () => {{}};
let stateReads=0;
acknowledged._hass.connection={{sendMessagePromise:async request=>{{
 assert.equal(request.type,"voip_stack/ha_softphone_state");stateReads++;
 return {{...base, state:"remote_ringing", callee:"426", peer_name:"426", send_video:true,
  capabilities:["audio","video"], sequence:10}};
}}}};
acknowledged._softphoneKeypadOpen = true;
acknowledged._softphoneManualTarget = "426";
engine.startHaSoftphone=async(target, session, options)=>{{
 assert.equal(target.name, "426");
 assert.equal(options.callee, "426");
 assert.equal(acknowledged._softphoneKeypadOpen, false);
 return {{schema_version:2,success:true,call_id:base.call_id,
  state:"remote_ringing",destination:"426",device_id:"device-office"}};
}};
await acknowledged._startCall();
assert.equal(acknowledged._softphoneKeypadOpen, false);
assert.equal(acknowledged._softphoneManualTarget, "426");
assert.equal(stateReads,1);
assert.equal(acknowledged._getDestination(),"426");
assert.equal(acknowledged._softphoneSnapshot.send_video,true);
acknowledged._applySoftphoneSnapshot({{...acknowledged._softphoneSnapshot,state:"in_call",sequence:11}});
context.requestAnimationFrame = callback => callback();
acknowledged._toggleKeypad();
assert.equal(acknowledged._softphoneKeypadOpen, true);
acknowledged._applySoftphoneSnapshot({{...acknowledged._softphoneSnapshot,state:"in_call",sequence:12}});
assert.equal(acknowledged._softphoneKeypadOpen, true);

assert.deepEqual(acknowledged._softphoneSnapshot.capabilities,["audio","video"]);

// Transfer reuses the destination UI without sending DTMF or changing preferences.
const transfer = makeCard();
transfer.config.device_id = "phone-device";
transfer._softphoneSnapshot = {{...base, state:"in_call",call_id:"transfer-original"}};
transfer._softphoneTargets=()=>[{{device_id:"desk",name:"Reception"}}];
transfer._els.transferBtn=new FakeElement();
transfer._els.transferHangupBtn=new FakeElement();
transfer._toggleTransfer();
assert.equal(transfer._els.callBtn.textContent,"Transfer");
assert.equal(transfer._els.callBtn.hidden,false);
assert.equal(transfer._els.hangupBtn.hidden,true);
assert.equal(transfer._els.transferHangupBtn.hidden,false);
transfer._toggleKeypad();
transfer._pressKeypadKey("4");transfer._pressKeypadKey("2");
assert.equal(transfer._manualTarget(),"42");
assert.equal(transfer._els.keypadInput.hidden,false);
let transferRequests=[];
transfer._hass.callWS=async request=>{{transferRequests.push(request);return {{response:{{success:false,status:486}}}}}};
await transfer._submitTransfer();
assert.equal(transferRequests[0].service,"transfer");
assert.equal(transferRequests[0].service_data.call_id,"transfer-original");
assert.equal(transferRequests[0].service_data.destination,"42");
assert.ok(transfer._transferDraft);
assert.equal(transfer._softphoneSnapshot.state,"in_call");
transfer._toggleKeypad();
transfer._hass.callWS=async request=>{{transferRequests.push(request);return {{response:{{success:true}}}}}};
await transfer._submitTransfer();
assert.equal(transferRequests[1].service_data.destination,"Reception");
assert.equal(transfer._transferDraft,null);
transfer._toggleTransfer();
transfer._toggleTransfer();
assert.equal(transfer._transferDraft,null);
transfer._toggleTransfer();
transfer._softphoneSnapshot={{...base,state:"idle",call_id:""}};
transfer._render();
assert.equal(transfer._transferDraft,null);
assert.equal(transfer._els.callBtn.textContent,"Call");
transfer._softphoneSnapshot = {{...base,state:"in_call",call_id:"old-transfer"}};
transfer._toggleTransfer();
let finishTransfer;
transfer._hass.callWS=()=>new Promise(resolve=>{{finishTransfer=resolve;}});
const pendingTransfer=transfer._submitTransfer();
const oldDraft=transfer._transferDraft;
await transfer._submitTransfer();
assert.equal(transfer._transferDraft,oldDraft);
transfer._softphoneSnapshot={{...base,state:"in_call",call_id:"next-call"}};
transfer._render();
finishTransfer({{response:{{success:false,status:486}}}});
await pendingTransfer;
assert.equal(transfer._errorMsg,"");
assert.equal(transfer._softphoneSnapshot.call_id,"next-call");
assert.equal(transfer._transferDraft,null);


// Video parking is unsupported and must not expose an actionable Park button.
const videoParking = makeCard();
videoParking._els.parkBtn = new FakeElement();
videoParking._els.parkingNotice = new FakeElement();
videoParking._softphoneSnapshot = {{...base,state:"in_call",parking:{{enabled:true}},video_active:true}};
videoParking._render();
assert.equal(videoParking._els.parkBtn.disabled, true);
assert.equal(videoParking._els.parkBtn.title, "Parking requires an audio-only call");
videoParking._softphoneSnapshot.video_active = false;
videoParking._render();
assert.equal(videoParking._els.parkBtn.disabled, false);

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
assert.equal(card._applySoftphoneSnapshot({{
  ...base, state: "idle", call_id: "old-call", sequence: 0, revision: 0,
}}), false);
assert.equal(card._applySoftphoneSnapshot({{
  ...base, state: "idle", call_id: "", sequence: 0, revision: 0,
}}), false);
assert.equal(card._softphoneSnapshot.state, "in_call");
assert.equal(card._applySoftphoneSnapshot({{
  ...base, state: "idle", call_id: base.call_id, sequence: 0, revision: 0,
}}), true);

// Pressing Answer sends the exact call service but does not invent an
// in-call state before the backend publishes a final 200/answer snapshot.
const incoming = makeCard();
incoming._applySoftphoneSnapshot({{
  endpoint_id: "browser:office", device_id: "device-office",
  state: "ringing",
  direction: "incoming",
  call_id: "incoming-A",
  caller: "Door",
  peer_name: "Door",
  sequence: 1,
}});
incoming._softphoneKeypadOpen = true;
await incoming._answer();
assert.equal(incoming._softphoneKeypadOpen, false);
assert.equal(incoming._softphoneSnapshot.state, "ringing");
assert.equal(audioPermissionChecks > 0, true);
assert.equal(cameraPermissionChecks, 0);
assert.equal(JSON.stringify(serviceCalls.at(-1)), JSON.stringify([
  "voip_stack",
  "answer",
  {{ device_id: "device-office", call_id: "incoming-A", send_video: false }},
]));
engine.releaseSoftphoneSession("incoming-A", "browser:office");

// A browser without getUserMedia must not answer the SIP dialog and then
// compensate with BYE. The call remains ringing and the card explains why.
const unsupportedMedia = makeCard();
unsupportedMedia._applySoftphoneSnapshot({{
  endpoint_id: "browser:unsupported", device_id: "device-unsupported",
  state: "ringing", direction: "incoming", call_id: "unsupported-A",
  caller: "Dahua", sequence: 1,
}});
engine.prepareAudioCall = async () => {{
  throw new Error("Microphone access is unavailable in this browser or Home Assistant app.");
}};
const unsupportedServiceCount = serviceCalls.length;
await unsupportedMedia._answer();
assert.equal(serviceCalls.length, unsupportedServiceCount);
assert.equal(unsupportedMedia._softphoneSnapshot.state, "ringing");
assert.match(unsupportedMedia._errorMsg, /Microphone access is unavailable/);
assert.equal(engine.ownsSoftphoneSession("unsupported-A", "browser:unsupported"), false);
engine.prepareAudioCall = async () => {{ audioPermissionChecks++; return true; }};
incoming._applySoftphoneSnapshot({{
  endpoint_id: "browser:office", device_id: "device-office",
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
  endpoint_id: "browser:office", device_id: "device-office",
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
  endpoint_id: "browser:office", device_id: "device-office",
  state: "ringing", direction: "incoming", call_id: "race-A", caller: "A", sequence: 1,
}});
let releaseLookup;
race._getDeviceInfo = () => new Promise((resolve) => {{ releaseLookup = resolve; }});
const pendingAnswer = race._answer();
await Promise.resolve();
race._applySoftphoneSnapshot({{
  endpoint_id: "browser:office", device_id: "device-office",
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
  endpoint_id: "browser:office", device_id: "device-office",
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
  {{ device_id: "device-office", call_id: "replace-A", send_video: false }},
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
  {{ device_id: "device-office", call_id: "incoming-A" }},
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
  endpoint_id: "browser:office", device_id: "device-office",
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
  endpoint_id: "browser:office", device_id: "device-office",
  state: "idle", direction: "incoming", call_id: "incoming-ended",
  caller: "Waveshare S3 Audio", callee: "Test", dialed_target: "Test",
  terminal_reason: "local_hangup", sequence: 1,
}});
incomingEnded._render();
assert.equal(
  incomingEnded._els.statusText.textContent,
  "Call with Waveshare S3 Audio ended.",
);

// A preferred HA softphone has no fixed device_id in its Lovelace config.
// Terminal snapshots may omit both device identifiers, but must not turn the
// already-bound card into the unconfigured skeleton while showing the error.
const terminalWithoutDevice = makeCard();
terminalWithoutDevice._applySoftphoneSnapshot({{
  endpoint_id: "browser:office", device_id: "device-office",
  session_device_id: "device-office", state: "in_call",
  direction: "outgoing", call_id: "terminal-no-device",
  peer_name: "Controlled peer", sequence: 1,
}});
terminalWithoutDevice._render();
assert.equal(terminalWithoutDevice._getConfigDeviceId(), "device-office");
assert.equal(terminalWithoutDevice._skeletonMode, "main");
terminalWithoutDevice._applySoftphoneSnapshot({{
  endpoint_id: "browser:office", state: "idle",
  direction: "outgoing", call_id: "terminal-no-device",
  peer_name: "Controlled peer", terminal_reason: "sip_403", sequence: 2,
}});
terminalWithoutDevice._render();
assert.equal(terminalWithoutDevice._getConfigDeviceId(), "device-office");
assert.equal(terminalWithoutDevice._skeletonMode, "main");
assert.match(terminalWithoutDevice._els.statusText.textContent, /ended/i);
assert.match(terminalWithoutDevice._els.statusReason.textContent, /403/);

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
engine.endpointId = "";
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

// Cancelling an outbound start before its reply has no call ID yet. Invalidate
// the start operation without issuing an unscoped terminal command.
const pendingStartCancel = makeCard();
pendingStartCancel._starting = true;
pendingStartCancel._loadSoftphoneState = async () => {{}};
const beforePendingCancel = serviceCalls.length;
await pendingStartCancel._hangup();
assert.equal(serviceCalls.length, beforePendingCancel);
assert.equal(pendingStartCancel._starting, false);
assert.equal(pendingStartCancel._errorMsg, "");

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
engine.terminateSoftphoneCall = async () => {{ throw new Error("hangup denied"); }};
rejectedHangup._loadSoftphoneState = async () => {{}};
await rejectedHangup._hangup();
assert.equal(engine.ownsSoftphoneSession("hangup-rejected", "default"), true);
assert.match(rejectedHangup._errorMsg, /hangup denied/i);

// Mailbox shortcuts use the existing phone call and DTMF. A retained Call-ID
// in idle history must not be mistaken for a live call owned by someone else.
const mailbox = makeCard();
mailbox._render = () => {{}};
mailbox._renderMailbox = () => {{}};
mailbox._loadMailbox = async () => {{}};
mailbox._softphoneSnapshot = {{endpoint_id: "default", state: "in_call", call_id: "ordinary-call"}};
let mailboxStarts = 0;
mailbox._startCall = async (destination) => {{
  mailboxStarts++;
  assert.equal(destination, "*97");
  mailbox._softphoneSnapshot = {{endpoint_id: "default", state: "in_call", call_id: "mailbox-call"}};
  engine.callId = "mailbox-call";
  return {{state: "in_call", call_id: "mailbox-call"}};
}};
await mailbox._mailboxMessage("message-id", false);
assert.equal(mailboxStarts, 0);
assert.match(mailbox._errorMsg, /current call/);
mailbox._softphoneSnapshot = {{endpoint_id: "default", state: "idle", call_id: "old-finished-call"}};
mailbox._softphoneManualTarget = "Desk";
mailbox._hass.callWS = async (request) => {{
  assert.equal(request.type, "voip_stack/voicemail/list");
  assert.equal(request.call_id, "mailbox-call");
  return {{messages: [{{id: "message-id"}}], selections: {{"message-id": "02#"}}}};
}};
const mailboxDigits = [];
let mailboxNotification;
let mailboxUnsubscribed = false;
let mailboxHungUp = false;
engine.sendDtmf = (digit) => {{
  mailboxDigits.push(digit);
  if (mailboxDigits.join("").endsWith("02#2") && mailboxNotification) mailboxNotification({{data: {{
    call_id: "mailbox-call", mailbox_id: "default", message_id: "message-id", operation: "deleted",
  }}}});
  return true;
}};
await mailbox._mailboxMessage("message-id", false);
assert.equal(mailboxStarts, 1);
assert.equal(mailboxDigits.join(""), "02#");
assert.equal(mailbox._softphoneManualTarget, "Desk");
mailbox._hass.connection = {{subscribeEvents: async (cb, type) => {{
  assert.equal(type, "voip_stack_voicemail_operation");
  mailboxNotification = cb;
  return () => {{mailboxUnsubscribed = true;}};
}}}};
mailbox._hangup = async () => {{mailboxHungUp = true;}};
await mailbox._mailboxMessage("message-id", true);
assert.equal(mailboxStarts, 1);
assert.equal(mailboxDigits.join(""), "02#02#2");
assert.equal(mailboxHungUp, true);
assert.equal(mailboxUnsubscribed, true);
assert.equal(mailbox._mailboxBusy, false);

const pagedMailbox = makeCard();
pagedMailbox._renderMailbox = () => {{}};
const offsets = [];
pagedMailbox._hass.callWS = async (request) => {{
  offsets.push(request.offset);
  return {{total: 3, messages: request.offset === 0 ? [{{id: "one"}}, {{id: "two"}}] : [{{id: "two"}}, {{id: "three"}}]}};
}};
await pagedMailbox._loadMailbox();
await pagedMailbox._loadMailbox(true);
assert.deepEqual(offsets, [0, 2]);
assert.equal(pagedMailbox._mailboxMessages.map((item) => item.id).join(","), "one,two,three");
assert.equal(pagedMailbox._mailboxOffset, 4);
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


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_shared_entity_identity_refreshes_without_polling() -> None:
    script = f'''
import assert from "node:assert/strict";
import {{ pathToFileURL }} from "node:url";
const {{ VoipEntityIds }} = await import(pathToFileURL({json.dumps(str(CARD.with_name("voip-stack-entity-ids.js")))}));
const resolver = new VoipEntityIds();
let requests = 0, updates = 0, selected = "sensor.original";
const connection = {{ sendMessagePromise: async () => {{
  requests++; return {{ entity_ids: {{ phonebook: selected }} }};
}} }};
let hass = {{ connection, entities: {{}}, states: {{ "sensor.original": {{}} }} }};
const changed = () => updates++;
resolver.update(hass, changed);
await resolver._pending;
assert.equal(resolver.phonebook, "sensor.original");
for (let i = 0; i < 100; i++) resolver.update(hass, changed);
assert.equal(requests, 1);
selected = "sensor.renamed";
hass = {{ ...hass, entities: {{}}, states: {{ "sensor.renamed": {{}}, "sensor.original": {{}} }} }};
resolver.update(hass, changed);
await resolver._pending;
assert.equal(resolver.phonebook, "sensor.renamed");
assert.equal(requests, 2);
selected = null;
resolver.update({{ ...hass, entities: {{}} }}, changed);
await resolver._pending;
assert.equal(resolver.phonebook, null);
assert.equal(updates, 3);
// An obsolete connection response cannot overwrite the new connection's identity.
let finish;
const old = {{ sendMessagePromise: () => new Promise(resolve => {{ finish = resolve; }}) }};
resolver.update({{ connection: old, entities: {{}}, states: {{}} }}, changed);
selected = "sensor.new_server";
resolver.update(hass, changed);
finish({{ entity_ids: {{ phonebook: "sensor.wrong_server" }} }});
await resolver._pending;
if (resolver._pending) await resolver._pending;
assert.equal(resolver.phonebook, "sensor.new_server");
'''
    subprocess.run(["node", "--input-type=module", "-e", script], check=True, capture_output=True, text=True)
