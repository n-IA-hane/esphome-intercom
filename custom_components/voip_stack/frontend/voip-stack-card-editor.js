/** Home Assistant card editor for VoIP Stack. */

const VOIP_STACK_MODULE_VERSION = (() => {
  try {
    const raw = new URL(import.meta.url).searchParams.get("v") || "";
    return raw || "dev";
  } catch (_) {
    return "dev";
  }
})();
const { voipStackTranslate } = await import(`./voip-stack-i18n.js?v=${encodeURIComponent(VOIP_STACK_MODULE_VERSION)}`);
const {
  audioModeLabel,
  normaliseAudioMode,
  normaliseCardConfig,
  supportsHaClient,
} = await import(`./voip-stack-card-model.js?v=${encodeURIComponent(VOIP_STACK_MODULE_VERSION)}`);

const { ensureNativeForm } = await import(`./voip-stack-form.js?v=${encodeURIComponent(VOIP_STACK_MODULE_VERSION)}`);

class VoipStackCardEditor extends HTMLElement {
  constructor() {
    super();
    this._config = {};
    this._hass = null;
    this._devices = [];
    this._devicesLoaded = false;
    this._devicesLoading = false;
    this._devicesRetryTimer = null;
    this._els = null;
  }

  connectedCallback() {
    void ensureNativeForm().then(() => this._render());
    if (this._hass && !this._devicesLoaded) this._loadDevices();
  }

  _t(text) {
    return voipStackTranslate(this._hass, text);
  }

  disconnectedCallback() {
    if (this._devicesRetryTimer) {
      clearTimeout(this._devicesRetryTimer);
      this._devicesRetryTimer = null;
    }
  }

  setConfig(config) {
    const normalised = normaliseCardConfig(config);
    this._config = normalised.config;
    this._render();
  }

  set hass(hass) {
    const languageChanged = String(this._hass?.language || "") !== String(hass?.language || "");
    this._hass = hass;
    if (hass && !this._devicesLoaded) this._loadDevices();
    if (languageChanged && this._els) this._els.form.computeLabel = schema => this._fieldLabel(schema.name);
    this._render();
  }

  _normaliseAudioMode(value) {
    return normaliseAudioMode(value);
  }

  _audioModeLabel(mode) {
    return audioModeLabel(mode);
  }

  async _loadDevices() {
    if (!this._hass || this._devicesLoaded || this._devicesLoading) return;
    if (!this._isVoipStackLoaded()) {
      this._scheduleLoadDevices();
      return;
    }
    this._devicesLoading = true;
    try {
      const result = await this._hass.connection.sendMessagePromise({
        type: "voip_stack/list_devices",
      });
      if (result?.devices) {
        this._devices = result.devices;
        this._devicesLoaded = true;
        this._render();
      }
    } catch (err) {
      if (this._isUnknownCommandError(err)) this._scheduleLoadDevices();
      else console.error("Failed to load devices:", err);
    } finally {
      this._devicesLoading = false;
    }
  }

  _isVoipStackLoaded() {
    const components = this._hass?.config?.components;
    return !Array.isArray(components) || components.includes("voip_stack");
  }

  _isUnknownCommandError(err) {
    const code = String(err?.code || err?.error || "");
    const message = String(err?.message || "");
    return code.includes("unknown_command") || message.includes("unknown command");
  }

  _scheduleLoadDevices() {
    if (this._devicesRetryTimer) return;
    this._devicesRetryTimer = setTimeout(() => {
      this._devicesRetryTimer = null;
      this._loadDevices();
    }, 2000);
  }

  _buildSkeleton() {
    this.replaceChildren();
    const wrap = document.createElement("div");
    wrap.style.padding = "16px";
    const form = document.createElement("ha-form");
    form.addEventListener("value-changed", event => this._formChanged(event));
    const modeInfo = document.createElement("p");
    const deviceInfo = document.createElement("p");
    for (const hint of [modeInfo, deviceInfo]) {
      hint.style.cssText = "color:var(--secondary-text-color);font-size:0.9em";
    }
    wrap.append(form, modeInfo, deviceInfo);
    this.appendChild(wrap);
    this._els = { form, modeInfo, deviceInfo };
  }

  _fieldLabel(name) {
    const mode = this._config.mode || this._config.card_mode || "esp_mirror";
    return this._t({
      mode: "Card Mode",
      device_id: mode === "ha_softphone" ? "SIP account" : "VoIP Device",
      name: "Card Name (optional)", title: "Title (optional)",
      show_extended_info: "Extended information",
      show_recording_controls: "Show recording controls",
    }[name] || name);
  }

  _formData() {
    return {
      mode: this._config.mode || this._config.card_mode || "esp_mirror",
      device_id: this._config.device_id || this._config.entity_id || "",
      name: this._config.name || "",
      title: this._config.title || this._config.name || "",
      show_extended_info: !!this._config.show_extended_info,
      show_recording_controls: !!this._config.show_recording_controls,
    };
  }

  _formChanged(event) {
    const values = event.detail?.value;
    if (!values || typeof values !== "object") return;
    const before = this._formData();
    if (values.mode && values.mode !== before.mode) {
      this._modeChanged(values.mode);
      return;
    }
    const config = { ...this._config };
    let changed = false;
    for (const key of Object.keys(before)) {
      if (key === "mode" || !Object.hasOwn(values, key) || values[key] === before[key]) continue;
      changed = true;
      if (values[key]) config[key] = values[key]; else delete config[key];
      if (key === "device_id") {
        delete config.entity_id;
        delete config.endpoint_id;
      }
    }
    if (changed) this._dispatchConfig(config);
  }

  _render() {
    if (!this._els) this._buildSkeleton();
    const els = this._els;
    const mode = this._config.mode || this._config.card_mode || "esp_mirror";
    const softphoneMode = mode === "ha_softphone";
    const phonebookMode = mode === "phonebook";
    const selectableDevices = this._devices.filter((device) =>
      softphoneMode
        ? this._isSoftphoneDevice(device) && device.can_control !== false && !!device.device_id
        : !this._isSoftphoneDevice(device));
    const configuredDeviceId = String(this._config.device_id || this._config.entity_id || "");
    const selectedDevice = selectableDevices.find(device => device.device_id === configuredDeviceId);
    const options = [{ value: "", label: this._t(softphoneMode ? "Preferred SIP account" : "-- Select device --") },
      ...selectableDevices.map(device => ({ value: device.device_id,
        label: softphoneMode
          ? `${device.name || device.endpoint_id || device.device_id}${device.extension ? ` (${device.extension})` : ""}`
          : `${device.name} (${this._audioModeLabel(device.audio_mode)})` }))];
    const configuredMissingPhone = configuredDeviceId && !selectedDevice;
    if (configuredMissingPhone) options.push({ value: configuredDeviceId, label: this._t("Missing phone") });
    const controlVisible = { field: "mode", operator: "not_eq", value: "phonebook" };
    els.form.hass = this._hass;
    els.form.computeLabel = schema => this._fieldLabel(schema.name);
    els.form.schema = [
      { name: "mode", selector: { select: { options: [
        { value: "esp_mirror", label: this._t("ESP mirror") },
        { value: "ha_softphone", label: this._t("SIP account") },
        { value: "phonebook", label: this._t("VoIP phonebook") },
      ] } } },
      { name: "device_id", visible: controlVisible, selector: { select: { options, mode: "dropdown" } } },
      { name: "name", visible: controlVisible, selector: { text: {} } },
      { name: "title", visible: { field: "mode", value: "phonebook" }, selector: { text: {} } },
      { name: "show_extended_info", visible: controlVisible, selector: { boolean: {} } },
      { name: "show_recording_controls", visible: { field: "mode", value: "ha_softphone" }, selector: { boolean: {} } },
    ];
    els.form.data = this._formData();
    els.modeInfo.textContent = this._t(phonebookMode
      ? "Scrollable view of the shared VoIP phonebook."
      : softphoneMode
        ? "Use any authorized SIP account. Leave unselected to use the preferred account."
        : "ESP mirror card: mirrors one ESP endpoint and presses that ESP's own call, answer and hangup controls.");
    els.deviceInfo.hidden = phonebookMode;
    els.deviceInfo.textContent = this._t(!this._devicesLoaded ? "Loading..."
      : configuredMissingPhone ? "The configured SIP account no longer exists. Select another account or use the preferred account."
      : !selectableDevices.length ? (softphoneMode ? "No SIP accounts found." : "No devices found")
      : softphoneMode ? "Leave unselected to use the preferred SIP account." : "Required for ESP mirror mode.");
  }

  _nameChanged(value) {
    const key = (this._config.mode || this._config.card_mode) === "phonebook"
      ? "title"
      : "name";
    this._valueChanged(key, value);
  }

  _isSoftphoneDevice(device) {
    return supportsHaClient(device);
  }

  _deviceChanged(deviceId) {
    const newConfig = { ...this._config };
    if (deviceId) {
      newConfig.device_id = deviceId;
      delete newConfig.entity_id;
      delete newConfig.endpoint_id;
    } else {
      delete newConfig.device_id;
      delete newConfig.entity_id;
      delete newConfig.endpoint_id;
    }
    this._dispatchConfig(newConfig);
  }

  _valueChanged(key, value) {
    const newConfig = { ...this._config };
    if (value) newConfig[key] = value;
    else delete newConfig[key];
    this._dispatchConfig(newConfig);
  }

  _modeChanged(value) {
    const newConfig = { ...this._config };
    if (value === "ha_softphone" || value === "phonebook") {
      newConfig.mode = value;
      delete newConfig.device_id;
      delete newConfig.entity_id;
      delete newConfig.endpoint_id;
      delete newConfig.target_device_id;
      if (value === "phonebook") delete newConfig.show_extended_info;
    } else {
      newConfig.mode = "esp_mirror";
      delete newConfig.card_mode;
      delete newConfig.target_device_id;
      delete newConfig.endpoint_id;
    }
    this._dispatchConfig(newConfig);
  }

  _boolChanged(key, checked) {
    const newConfig = { ...this._config };
    if (checked) newConfig[key] = true;
    else delete newConfig[key];
    this._dispatchConfig(newConfig);
  }

  _dispatchConfig(config) {
    this._config = config;
    this._render();
    this.dispatchEvent(new CustomEvent("config-changed", {
      detail: { config },
      bubbles: true,
      composed: true,
    }));
  }
}

if (!customElements.get("voip-stack-card-editor")) {
  customElements.define("voip-stack-card-editor", VoipStackCardEditor);
}
