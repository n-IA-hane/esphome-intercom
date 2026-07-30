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

export function targetFromRosterEntry(entry) {
  const metadata = entry?.metadata || {};
  const id = entry?.id || entry?.name;
  const signaling = metadata.sip_transport || metadata.signaling_transport || "";
  return {
    endpoint_id: metadata.endpoint_id || "",
    device_id: metadata.device_id || id,
    name: entry?.name || id,
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
    sip_video_codec: String(metadata.sip_video_codec || "").trim().toLowerCase(),
    audio_mode: metadata.audio_mode || "full_duplex",
    tx_formats: formatListFromMetadata(metadata.tx_formats),
    rx_formats: formatListFromMetadata(metadata.rx_formats),
    sip_port: entry?.port || metadata.port || metadata.sip_port,
    rtp_port: metadata.rtp_port,
    max_payload_bytes: metadata.max_payload_bytes,
    roster: true,
  };
}

export function terminalPeerLabel(snapshot = {}) {
  const connected = snapshot.connected_party || snapshot.answered_by || snapshot.peer_name;
  if (connected) return String(connected);

  const direction = String(snapshot.direction || "").trim().toLowerCase();
  if (direction === "incoming") {
    return String(snapshot.caller || snapshot.target || snapshot.dialed_target || snapshot.callee || "");
  }
  if (direction === "outgoing") {
    return String(snapshot.callee || snapshot.target || snapshot.dialed_target || snapshot.caller || "");
  }
  return String(
    snapshot.callee || snapshot.caller || snapshot.target || snapshot.dialed_target || "",
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
