/** Pure projections for the operator dashboard. Call state stays in HA. */

const ACTIVE_STATES = new Set(["ringing", "calling", "connecting", "answering", "in_call", "held", "terminating"]);
const INTERNAL_KINDS = new Set(["browser", "ha_softphone", "esphome", "sip_account"]);

export function normaliseDashboardConfig(value = {}) {
  if (value.device_id != null && typeof value.device_id !== "string") {
    throw new Error("Choose the calling phone using device_id.");
  }
  if (value.title != null && typeof value.title !== "string") throw new Error("Title must be text.");
  return { ...value, device_id: String(value.device_id || "").trim() };
}

export function parseDashboardRoster(value) {
  try {
    const data = typeof value === "string" ? JSON.parse(value) : value;
    const entries = Array.isArray(data) ? data : data?.contacts || data?.entries || [];
    return entries.filter((entry) => entry && entry.enabled !== false);
  } catch (_) { return []; }
}

export class DashboardPhoneStore {
  constructor() { this.reset(); }
  reset() { this.revision = -1; this.ready = false; this.error = ""; this.phones = new Map(); }
  apply(event = {}) {
    if (event.kind === "reset") { this.reset(); return true; }
    if (event.kind === "error") {
      this.ready = false;
      this.error = String(event.error || "Phone state is unavailable.");
      return true;
    }
    const revision = event.revision;
    if (!Number.isSafeInteger(revision) || revision < 0) return false;
    if (!["snapshot", "delta"].includes(event.kind)) return false;
    if (event.kind === "delta" && (!this.ready || revision <= this.revision)) return false;
    if (event.kind === "snapshot") this.phones.clear();
    for (const id of event.removed || []) this.phones.delete(String(id));
    for (const phone of event.phones || []) {
      if (!phone?.endpoint_id) continue;
      this.phones.set(String(phone.endpoint_id), {
        ...phone,
        calls: Array.isArray(phone.calls) ? phone.calls : [],
        mailbox: phone.mailbox || {},
      });
    }
    this.revision = revision;
    this.ready = true;
    this.error = "";
    return true;
  }
}

export function ownerDeviceInfo(devices, phones, deviceId) {
  if (!deviceId) return null;
  const device = devices.find((entry) => entry.device_id === deviceId);
  if (!device) return null;
  const phone = [...phones.values()].find((entry) => entry.device_id === deviceId);
  const kind = phone?.kind || device.kind || device.endpoint_type || (device.softphone ? "browser" : "esphome");
  if (!["browser", "ha_softphone", "esphome"].includes(kind)) return null;
  return { ...device, ...phone, kind, softphone: kind !== "esphome", calls: phone?.calls || [] };
}

export function dashboardSections(phones, roster, ownerId) {
  const rosterByIdentity = new Map();
  for (const entry of roster) {
    for (const identity of [entry.metadata?.endpoint_id, entry.endpoint_id, entry.metadata?.device_id, entry.device_id, entry.id]) {
      if (identity) rosterByIdentity.set(identity, entry);
    }
  }
  const internals = [...phones.values()].map((phone) => {
    const entry = rosterByIdentity.get(phone.endpoint_id) || rosterByIdentity.get(phone.device_id);
    return { ...phone, icon: phone.icon || entry?.icon,
      metadata: { ...entry?.metadata, ...phone.metadata },
      tile_key: `endpoint:${phone.endpoint_id}`, owner: !!ownerId && phone.device_id === ownerId };
  });
  const owner = internals.find((phone) => phone.owner) || null;
  const endpointIds = new Set(internals.map((phone) => phone.endpoint_id));
  const deviceIds = new Set(internals.map((phone) => phone.device_id).filter(Boolean));
  const contacts = roster.filter((entry) => {
    const meta = entry.metadata || {};
    const endpointId = meta.endpoint_id || entry.endpoint_id;
    const deviceId = meta.device_id || entry.device_id;
    if (endpointIds.has(endpointId) || deviceIds.has(deviceId) ||
        endpointIds.has(entry.id) || deviceIds.has(entry.id) ||
        (ownerId && [deviceId, entry.id].includes(ownerId))) return false;
    if (INTERNAL_KINDS.has(String(meta.endpoint_kind || "").toLowerCase())) return false;
    return !["voicemail", "parking"].includes(String(entry.type || meta.type || "").toLowerCase());
  }).map((entry) => ({
    ...entry,
    tile_key: `contact:${entry.id || entry.name}`,
    name: String(entry.name || entry.id || "Unnamed"),
    kind: "contact", state: "unknown", availability: "unknown", calls: [],
  }));
  const sort = (a, b) => String(a.name || "").localeCompare(String(b.name || ""), undefined, { sensitivity: "base", numeric: true });
  const extensionSort = (a, b) => {
    const left = String(a.extension || ""); const right = String(b.extension || "");
    if (left && right && left !== right) return left.localeCompare(right, undefined, { numeric: true });
    if (!!left !== !!right) return left ? -1 : 1;
    return sort(a, b);
  };
  return { owner, internals: internals.filter((phone) => !phone.owner).sort(extensionSort), contacts: contacts.sort(sort) };
}

export function dashboardTypeFor(phone) {
  const meta = phone?.metadata || {};
  if (meta.virtual_endpoint === "automation" || phone?.type === "automation") return "Automation";
  if (meta.virtual_endpoint === "assist_pipeline") return "Assist pipeline";
  if (meta.group_type === "conference") return "Conference group";
  if (meta.group_type === "ring") return "Ring group";
  const kind = String(phone?.kind || meta.endpoint_kind || "").toLowerCase();
  if (["browser", "ha_softphone"].includes(kind)) return "Home Assistant phone";
  if (kind === "esphome") return "ESPHome phone";
  if (kind === "sip_account") return "SIP phone";
  return "Saved contact";
}

export function dashboardIconFor(phone) {
  for (const custom of [phone?.icon, phone?.metadata?.icon]) {
    if (typeof custom === "string" && /^mdi:[a-z0-9]+(?:-[a-z0-9]+)*$/.test(custom)) return custom;
  }
  return {
    "Home Assistant phone": "mdi:home-assistant", "ESPHome phone": "mdi:deskphone",
    "SIP phone": "mdi:phone-classic", "Saved contact": "mdi:contacts",
    "Automation": "mdi:robot", "Assist pipeline": "mdi:account-voice",
    "Ring group": "mdi:phone-ring", "Conference group": "mdi:account-group",
  }[dashboardTypeFor(phone)];
}

export function distinctCalls(phone) {
  const result = new Map();
  for (const call of phone?.calls || []) {
    const id = String(call.id || call.call_id || "");
    if (!id) continue;
    const previous = result.get(id);
    if (!previous || call.pickup_available || (previous.held && !call.held)) result.set(id, { ...call, id });
  }
  return [...result.values()];
}

export function primaryDashboardCall(phone) {
  const calls = distinctCalls(phone);
  return calls.find((call) => call.pickup_available || (call.direction === "incoming" && call.state === "early")) ||
    calls.find((call) => call.state === "confirmed" && !call.held) || calls[0] || null;
}

export function phonePresentation(phone) {
  const state = String(phone?.state || "unknown").toLowerCase();
  const calls = distinctCalls(phone);
  const primary = primaryDashboardCall(phone);
  const unavailable = ["offline", "unavailable"].includes(String(phone?.availability || "")) && !ACTIVE_STATES.has(state);
  const visual = state === "ringing" ? "ringing" : ACTIVE_STATES.has(state) ? "busy" : unavailable || state === "unavailable" ? "offline" : "idle";
  let label = "Available";
  if (phone?.kind === "contact") label = "";
  else if (visual === "offline") label = "Offline";
  else if (state === "ringing") label = phone?.owner ? "Incoming" : "Ringing";
  else if (state === "calling") label = "Calling";
  else if (state === "held") label = "On hold";
  else if (visual === "busy") label = "In call with";
  else if (phone?.dnd) label = "Do not disturb";
  else if (state === "unknown") label = "Unknown";
  return {
    visual, state, label, primary, calls,
    peer: String(primary?.peer_name || ""),
    held: calls.filter((call) => call.held),
    incoming: calls.filter((call) => call.pickup_available || (call.direction === "incoming" && call.state === "early")),
    // Transfer intent must be explicit; two calls alone do not imply it.
    attended: phone?.attended_transfer === true || calls.some((call) => call.attended_transfer === true),
  };
}

export function dashboardCanCall(owner, target, ready = true) {
  return ready && !!owner?.device_id && owner.state === "idle" && !target?.owner &&
    !["offline", "unavailable"].includes(owner.availability) &&
    (target?.kind === "contact" || target?.state === "idle");
}

export function dashboardPickupCandidate(owner, target, ready = true) {
  if (!ready || !owner?.device_id || owner.state !== "idle" || target?.owner || target?.state !== "ringing") return null;
  const eligible = distinctCalls(target).filter((call) => call.pickup_available);
  return eligible.length === 1 ? eligible[0] : null;
}

export function dashboardColumnCount(width) {
  return width >= 1420 ? 5 : width >= 1050 ? 4 : width >= 700 ? 3 : width >= 350 ? 2 : 1;
}

const IT = {
  "VoIP Dashboard": "Dashboard VoIP", "Extensions": "Interni", "Contacts": "Contatti",
  "Registered endpoints on your system": "Telefoni del sistema", "Saved external numbers": "Numeri esterni salvati",
  "Your phone": "Il tuo telefono", "Available": "Disponibile", "Offline": "Non in linea",
  "Ringing": "Squilla", "Incoming": "In arrivo", "Incoming call": "Chiamata in arrivo",
  "Calling": "Chiamata in corso", "In call with": "In chiamata con", "On hold": "In attesa",
  "Do not disturb": "Non disturbare", "Pick up": "Rispondi qui", "Choose call": "Scegli chiamata",
  "Answer": "Rispondi", "Decline": "Rifiuta", "Hang up": "Chiudi", "Call details": "Dettagli chiamata",
  "Attended transfer": "Trasferimento assistito", "Close": "Chiudi", "Unknown": "Sconosciuto", "Connected": "Connessa",
  "Select the calling phone in the card editor.": "Seleziona il telefono chiamante nell'editor della card.",
  "Waiting for phone state": "In attesa degli stati", "Reconnecting": "Riconnessione in corso",
  "No extensions available.": "Nessun interno disponibile.", "No saved contacts.": "Nessun contatto salvato.",
  "Calling phone": "Telefono chiamante", "Title (optional)": "Titolo (facoltativo)",
  "Home Assistant phone": "Telefono Home Assistant", "ESPHome phone": "Telefono ESPHome",
  "SIP phone": "Telefono SIP", "Saved contact": "Contatto salvato", "Automation": "Automazione",
  "Assist pipeline": "Pipeline Assist", "Ring group": "Gruppo di squillo", "Conference group": "Gruppo conferenza",
  "Saved numbers, groups and automations": "Numeri salvati, gruppi e automazioni",
  "Choose a phone": "Scegli un telefono", "Reload phones": "Ricarica telefoni",
  "Missing phone": "Telefono non disponibile", "Call": "Chiama", "Voicemail": "Segreteria",
  "Call already active on the calling phone.": "Il telefono chiamante ha già una chiamata attiva.",
};

export function dashboardTranslate(language, text, fallback = (value) => value) {
  return String(language || "").toLowerCase().startsWith("it") ? IT[text] || fallback(text) : fallback(text);
}
