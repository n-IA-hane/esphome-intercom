const STORAGE_KEY = "voip_stack_media_devices_v1";
const MEDIA_KINDS = new Set(["audioinput", "audiooutput", "videoinput"]);

function cloneMap(value = {}) {
  return Object.fromEntries(
    [...MEDIA_KINDS].map((kind) => [kind, String(value[kind] || "")]),
  );
}

function stopStream(stream) {
  for (const track of stream?.getTracks?.() || []) track.stop();
}

export function exactDeviceConstraint(base, deviceId) {
  const selected = String(deviceId || "");
  return selected ? { ...base, deviceId: { exact: selected } } : base;
}

export class BrowserMediaDevices extends EventTarget {
  constructor({ mediaDevices = globalThis.navigator?.mediaDevices, storage = globalThis.localStorage } = {}) {
    super();
    this._mediaDevices = mediaDevices || null;
    this._storage = storage || null;
    this._devices = [];
    this._active = cloneMap();
    this._preferred = cloneMap();
    this._cameraFacingMode = "";
    this._refreshGeneration = 0;
    try {
      const saved = JSON.parse(this._storage?.getItem?.(STORAGE_KEY) || "{}");
      this._preferred = cloneMap(saved.selected);
      this._cameraFacingMode = String(saved.camera_facing_mode || "");
    } catch (_) {}
    this._onDeviceChange = () => { void this.refresh(); };
    this._mediaDevices?.addEventListener?.("devicechange", this._onDeviceChange);
  }

  get state() {
    const groups = { audioinput: [], audiooutput: [], videoinput: [] };
    const counts = { audioinput: 0, audiooutput: 0, videoinput: 0 };
    for (const device of this._devices) {
      if (!MEDIA_KINDS.has(device.kind)) continue;
      const index = ++counts[device.kind];
      const fallback = device.kind === "videoinput"
        ? `Camera ${index}`
        : device.kind === "audiooutput"
          ? `Speaker ${index}`
          : `Microphone ${index}`;
      groups[device.kind].push({
        deviceId: String(device.deviceId || ""),
        groupId: String(device.groupId || ""),
        label: String(device.label || fallback),
      });
    }
    return {
      devices: groups,
      selected: cloneMap(this._preferred),
      active: cloneMap(this._active),
      camera_facing_mode: this._cameraFacingMode,
    };
  }

  preference(kind) {
    return MEDIA_KINDS.has(kind) ? this._preferred[kind] : "";
  }

  cameraFacingMode() {
    return this._cameraFacingMode;
  }

  has(kind, deviceId) {
    const wanted = String(deviceId || "");
    return !wanted || this._devices.some(
      (device) => device.kind === kind && device.deviceId === wanted,
    );
  }

  setActive(kind, deviceId = "") {
    if (!MEDIA_KINDS.has(kind)) return;
    const active = String(deviceId || "");
    if (this._active[kind] === active) return;
    this._active[kind] = active;
    this._emit();
  }

  commit(kind, deviceId = "", settings = {}) {
    if (!MEDIA_KINDS.has(kind)) throw new Error(`Unsupported media device kind: ${kind}`);
    const selected = String(deviceId || "");
    this._preferred[kind] = selected;
    this._active[kind] = String(settings.deviceId || selected);
    if (kind === "videoinput") {
      this._cameraFacingMode = String(settings.facingMode || (selected ? this._cameraFacingMode : ""));
    }
    try {
      this._storage?.setItem?.(STORAGE_KEY, JSON.stringify({
        selected: this._preferred,
        camera_facing_mode: this._cameraFacingMode,
      }));
    } catch (_) {}
    this._emit();
  }

  async refresh({ requestPermission = false } = {}) {
    const generation = ++this._refreshGeneration;
    if (requestPermission && this._mediaDevices?.getUserMedia) {
      for (const constraints of [{ audio: true }, { video: true, audio: false }]) {
        try { stopStream(await this._mediaDevices.getUserMedia(constraints)); } catch (_) {}
      }
    }
    let devices = [];
    try {
      devices = await this._mediaDevices?.enumerateDevices?.() || [];
    } catch (_) {}
    if (generation !== this._refreshGeneration) return this.state;
    this._devices = [...devices];
    this._emit();
    return this.state;
  }

  _emit() {
    this.dispatchEvent(new CustomEvent("change", { detail: this.state }));
  }
}
