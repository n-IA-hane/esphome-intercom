/**
 * Pure data normalization shared by the VoIP Stack card and its editor.
 *
 * Keep this module independent from Home Assistant and browser globals so the
 * same roster entry always produces the same dialing target in every UI path.
 */

export function formatListFromMetadata(value) {
  if (Array.isArray(value)) return value.filter(Boolean).map((item) => String(item));
  if (typeof value === "string") {
    return value.split(";").map((item) => item.trim()).filter(Boolean);
  }
  return [];
}

export function publicDialTarget(entry) {
  if (typeof entry === "string") return entry.trim();
  const kind = String(entry?.metadata?.endpoint_kind || entry?.endpoint_kind || entry?.kind || "").trim().toLowerCase();
  const phone = ["esphome", "sip_account", "browser", "ha_softphone"].includes(kind);
  const id = String(entry?.id || "").trim();
  const deviceId = String(entry?.device_id || entry?.metadata?.device_id || "").trim();
  const username = id !== deviceId && /^[a-zA-Z0-9_.-]+$/.test(id) ? id : "";
  const preferred = phone
    ? [entry?.username || entry?.metadata?.username, entry?.sip_uri_user || entry?.metadata?.sip_uri_user,
      username, entry?.extension, entry?.number, entry?.sip_uri, entry?.name]
    : [entry?.extension, entry?.number, entry?.sip_uri, entry?.name];
  for (const value of preferred) {
    const address = String(value ?? "").trim();
    if (address) return address;
  }
  return "";
}

export function publicDisplayName(entry) {
  return String(entry?.name || "").trim() || publicDialTarget(entry);
}

export function targetFromRosterEntry(entry) {
  const metadata = entry?.metadata || {};
  const id = entry?.id || entry?.name;
  const signaling = metadata.sip_transport || metadata.signaling_transport || "";
  return {
    id: entry?.id || "",
    username: entry?.username || metadata.username || "",
    sip_uri_user: entry?.sip_uri_user || metadata.sip_uri_user || "",
    endpoint_id: metadata.endpoint_id || "",
    device_id: metadata.device_id || "",
    name: publicDisplayName(entry),
    route_id: id,
    host: entry?.address || "",
    sip_transport: signaling,
    sip_uri: entry?.sip_uri || "",
    extension: entry?.extension || "",
    number: entry?.number || "",
    ha_bridge: !!entry?.ha_bridge,
    endpoint_kind: String(metadata.endpoint_kind || "").trim().toLowerCase(),
    capabilities: formatListFromMetadata(metadata.capabilities)
      .map((value) => value.toLowerCase()),
    camera_entity_id: String(metadata.camera_entity_id || "").trim(),
    sip_video_codec: metadata.sip_video_codec == null
      ? metadata.sip_video_codec
      : String(metadata.sip_video_codec).trim().toLowerCase(),
    audio_mode: metadata.audio_mode || "full_duplex",
    tx_formats: formatListFromMetadata(metadata.tx_formats),
    rx_formats: formatListFromMetadata(metadata.rx_formats),
    sip_port: entry?.port || metadata.port || metadata.sip_port,
    rtp_port: metadata.rtp_port,
    max_payload_bytes: metadata.max_payload_bytes,
    roster: true,
  };
}

export function queueDeclarationText(value) {
  return Array.isArray(value) ? value.filter(item => typeof item === "string").join(", ") : String(value || "").trim();
}

export function availableQueueNames(entries = []) {
  return [...new Set(entries.filter(entry => entry?.enabled !== false && entry?.metadata?.virtual_endpoint === "queue")
    .map(entry => String(entry.name || entry.metadata?.name || "").trim()).filter(Boolean))].sort();
}

/** A shared application's ringing state is not an offer to its caller. */
export function phoneCallPresentation(snapshot = {}) {
  snapshot ||= {};
  const state = String(snapshot.state || snapshot.sip_state || "idle").trim().toLowerCase();
  const direction = String(snapshot.direction || "").trim().toLowerCase();
  const role = String(snapshot.role || "").trim().toLowerCase();
  const outgoing = direction === "outgoing" || (!direction && role === "caller");
  const offered = ["ringing", "incoming", "early"].includes(state);
  const incoming = offered && !outgoing && snapshot.can_answer !== false;
  return { incoming, outgoing,
    state: offered && !incoming ? outgoing ? "remote_ringing" : "connecting" : state };
}

export function isEstablishedCallState(state) {
  return ["in_call", "held"].includes(String(state || "").trim().toLowerCase());
}

export function isPagingAutoAnswer(snapshot = {}) {
  return phoneCallPresentation(snapshot).incoming && !!String(snapshot.announcement_id || "").trim() && snapshot.answer_mode === "auto" &&
    snapshot.audio_direction === "recvonly";
}

export function terminalPeerLabel(snapshot = {}) {
  const direction = String(snapshot.direction || "").trim().toLowerCase();
  const dialed = String(snapshot.dialed_target || "").trim();
  const names = direction === "incoming"
    ? [snapshot.caller_name, snapshot.caller, snapshot.peer_name, snapshot.callee_name, snapshot.callee]
    : [snapshot.connected_party_name, snapshot.connected_party, snapshot.answered_by_name,
      snapshot.peer_name, snapshot.callee_name, snapshot.callee,
      dialed, snapshot.caller_name, snapshot.caller];
  return String(names.find((value) => String(value || "").trim()) || "");
}

export function mirroredEndpointPeerLabel(snapshot = {}, localName = "") {
  const local = String(localName || "").trim();
  const direction = String(snapshot.direction || "").trim().toLowerCase();
  const incoming = direction === "incoming" || (!direction && snapshot.callee === local);
  const candidates = incoming
    ? [snapshot.caller_name, snapshot.caller, snapshot.peer_name, snapshot.connected_party]
    : [snapshot.connected_party_name, snapshot.connected_party, snapshot.answered_by_name,
      snapshot.peer_name, snapshot.callee_name, snapshot.callee, snapshot.caller_name, snapshot.caller];
  return String(candidates.find((value) => value && String(value).trim() !== local) || "");
}

const ACTIVE_CALL_STATES = new Set([
  "calling", "remote_ringing", "ringing", "answering", "connecting", "in_call", "held",
]);
const TERMINAL_CALL_STATES = new Set([
  "idle", "busy", "declined", "cancelled", "media_incompatible",
  "transport_unreachable", "auth_required_unsupported", "protocol_error", "error",
]);

export function softphoneSnapshotSupersedes(current, next) {
  if (!current) return true;
  const currentCallId = String(current.call_id || "");
  const nextCallId = String(next.call_id || "");
  const currentState = String(current.state || "").toLowerCase();
  const nextState = String(next.state || "").toLowerCase();
  const terminal = TERMINAL_CALL_STATES.has(nextState);
  const currentGeneration = Number(current.generation || 0);
  const nextGeneration = Number(next.generation || 0);
  if (currentCallId === nextCallId && currentGeneration > 0 && nextGeneration > 0 && currentGeneration !== nextGeneration) {
    return nextGeneration > currentGeneration;
  }
  if (
    currentCallId
    && ACTIVE_CALL_STATES.has(currentState)
    && terminal
    && nextCallId !== currentCallId
  ) return false;
  if (!nextCallId || currentCallId !== nextCallId || terminal) return true;
  const currentSequence = Number(current.sequence || 0);
  const nextSequence = Number(next.sequence || 0);
  if (nextSequence > 0 && currentSequence > 0 && nextSequence < currentSequence) {
    return false;
  }
  return !(
    nextSequence === currentSequence
    && Number(next.revision || 0) > 0
    && Number(current.revision || 0) > Number(next.revision || 0)
  );
}

export function normaliseTransport(value) {
  const transport = String(value || "").trim().toLowerCase();
  return ["tcp", "udp", "sip_tcp", "sip_udp"].includes(transport)
    ? transport.replace(/^sip_/, "").toUpperCase()
    : "";
}

export function normaliseAudioMode(value) {
  const mode = String(value || "").trim().toLowerCase();
  return ["full_duplex", "mic_only", "speaker_only"].includes(mode)
    ? mode
    : "full_duplex";
}

export function audioModeLabel(mode) {
  switch (normaliseAudioMode(mode)) {
    case "mic_only": return "MIC";
    case "speaker_only": return "SPK";
    default: return "FULL";
  }
}

export function formatCallDuration(connectedAt, nowSeconds = Date.now() / 1000) {
  const connected = Number(connectedAt || 0);
  if (!connected) return "00:00";
  const elapsed = Math.max(0, Math.floor(Number(nowSeconds) - connected));
  const hours = Math.floor(elapsed / 3600);
  const minutes = Math.floor((elapsed % 3600) / 60);
  const seconds = elapsed % 60;
  const mm = String(minutes).padStart(2, "0");
  const ss = String(seconds).padStart(2, "0");
  return hours ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`;
}

export function reasonKey(reason) {
  const text = String(reason || "").trim();
  if (!text) return "";
  if (text === "busy") return "busy";
  const normalized = text.toLowerCase().replace(/[\s-]+/g, "_");
  const known = new Set([
    "local_hangup",
    "remote_hangup",
    "remote_device_lost",
    "declined",
    "timeout",
    "busy",
    "cancelled",
    "forwarded",
    "media_incompatible",
    "transport_unreachable",
    "auth_required_unsupported",
    "protocol_error",
    "bridge_error",
  ]);
  return known.has(normalized) ? normalized : "";
}

export function formatKnownReason(reason) {
  switch (reasonKey(reason)) {
    case "local_hangup": return "Local hangup";
    case "remote_hangup": return "Remote hangup";
    case "remote_device_lost": return "Remote device lost";
    case "declined": return "Declined";
    case "timeout": return "Timeout";
    case "busy": return "Busy";
    case "cancelled": return "Cancelled";
    case "forwarded": return "Forwarded";
    case "media_incompatible": return "Media incompatible";
    case "transport_unreachable": return "Unreachable";
    case "auth_required_unsupported": return "Authentication unsupported";
    case "protocol_error": return "Protocol error";
    case "bridge_error": return "Bridge error";
    default: return "";
  }
}

export function formatEndReason(info) {
  if (!info) return "";
  const { kind, reason, origin } = info;
  const knownReason = formatKnownReason(reason);
  if (knownReason) return knownReason;
  const isSelf = origin === "self";
  const who = isSelf ? null
    : origin === "remote" ? "Remote"
    : origin === "source" ? "Caller"
    : origin === "dest" ? "Callee"
    : null;

  if (kind === "idle") {
    if (reason === "local_hangup") return "Local hangup";
    if (reason === "remote_hangup") return who ? `${who} hung up` : "Remote hangup";
    if (reason === "remote_device_lost") return who ? `${who} lost` : "Remote device lost";
    return reason || "Idle";
  }
  if (kind === "declined") {
    if (isSelf) return reason ? `Local decline: "${reason}"` : "Local decline";
    const head = who ? `${who} declined` : "Declined";
    return reason ? `${head}: "${reason}"` : head;
  }
  if (kind === "error") {
    const numericCode = reason && /^[0-9]+$/.test(String(reason));
    if (isSelf) {
      if (!reason) return "Local error";
      return numericCode ? `Local error (code ${reason})` : `Local error: "${reason}"`;
    }
    const head = who ? `${who} error` : "Error";
    if (!reason) return head;
    return numericCode ? `${head} (code ${reason})` : `${head}: "${reason}"`;
  }
  return reason || kind;
}

export function formatVideoFailureReason(reason) {
  switch (String(reason || "").trim().toLowerCase()) {
    case "local_video_resources_unavailable":
      return "Home Assistant could not allocate video media.";
    case "remote_video_rejected":
      return "The remote endpoint rejected video.";
    case "endpoint_video_unsupported":
      return "This endpoint does not support video.";
    default:
      return reason ? String(reason).replaceAll("_", " ") : "";
  }
}
export function supportsHaClient(device = {}) {
  const kind = String(device.kind || device.endpoint_type || device.type || "").toLowerCase();
  return device.ha_client_supported === true || device.softphone === true ||
    ["sip_account", "browser", "ha_softphone", "home_assistant", "softphone"].includes(kind);
}

export function normaliseCardConfig(rawConfig = {}) {
  const config = { ...rawConfig };
  delete config.endpoint_id;
  return { config };
}

/** Media can be negotiated before the SIP INVITE is answered. */
export function hasAttachableCallMedia(snapshot = {}) {
  return isEstablishedCallState(snapshot.state) || (
    snapshot.early_media === true && snapshot.can_attach_media === true &&
    snapshot.direction === "outgoing" &&
    ["calling", "connecting", "remote_ringing"].includes(String(snapshot.state || "").toLowerCase())
  );
}
