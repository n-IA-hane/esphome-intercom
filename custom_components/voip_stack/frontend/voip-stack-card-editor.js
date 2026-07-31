/** Home Assistant card editor for VoIP Stack. */

const VOIP_STACK_MODULE_VERSION = (() => {
  try {
    const raw = new URL(import.meta.url).searchParams.get("v") || "";
    return raw || "dev";
  } catch (_) {
    return "dev";
  }
})();
const {
  audioModeLabel,
  normaliseAudioMode,
} = await import(`./voip-stack-card-model.js?v=${encodeURIComponent(VOIP_STACK_MODULE_VERSION)}`);

const HA_SOFTPHONE_DEVICE_ID = "__voip_stack_ha_softphone__";
const DEFAULT_SOFTPHONE_ENDPOINT_ID = "default";

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
    if (this._hass && !this._devicesLoaded) this._loadDevices();
  }

  disconnectedCallback() {
    if (this._devicesRetryTimer) {
      clearTimeout(this._devicesRetryTimer);
      this._devicesRetryTimer = null;
    }
  }

  setConfig(config) {
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (hass && !this._devicesLoaded) this._loadDevices();
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

    const style = document.createElement("style");
    style.textContent = `
      .form-group { margin-bottom: 16px; }
      .form-group label { display: block; margin-bottom: 4px; font-weight: 500; color: var(--primary-text-color); }
      .form-group input, .form-group select {
        width: 100%; padding: 8px; border: 1px solid var(--divider-color, #ccc);
        border-radius: 4px; background: var(--card-background-color, white);
        color: var(--primary-text-color); font-size: 1em; box-sizing: border-box;
      }
      .checkbox-group label { display: flex; align-items: center; gap: 8px; }
      .checkbox-group input { width: auto; padding: 0; }
      .info { color: var(--secondary-text-color); font-size: 0.85em; margin-top: 8px; }
      .hidden { display: none; }
    `;
    this.appendChild(style);

    const wrap = document.createElement("div");
    wrap.style.padding = "16px";

    const modeGroup = document.createElement("div");
    modeGroup.className = "form-group";
    const modeLabel = document.createElement("label");
    modeLabel.textContent = "Card Mode";
    modeGroup.appendChild(modeLabel);
    const modeSelect = document.createElement("select");
    modeSelect.id = "mode-select";
    const mirrorOpt = document.createElement("option");
    mirrorOpt.value = "esp_mirror";
    mirrorOpt.textContent = "ESP mirror";
    const softphoneOpt = document.createElement("option");
    softphoneOpt.value = "ha_softphone";
    softphoneOpt.textContent = "Home Assistant softphone";
    const phonebookOpt = document.createElement("option");
    phonebookOpt.value = "phonebook";
    phonebookOpt.textContent = "VoIP phonebook";
    modeSelect.append(mirrorOpt, softphoneOpt, phonebookOpt);
    modeGroup.appendChild(modeSelect);
    const modeInfo = document.createElement("div");
    modeInfo.className = "info";
    modeGroup.appendChild(modeInfo);
    wrap.appendChild(modeGroup);

    const deviceGroup = document.createElement("div");
    deviceGroup.className = "form-group";
    const deviceLabel = document.createElement("label");
    deviceLabel.textContent = "VoIP Device";
    deviceGroup.appendChild(deviceLabel);
    const select = document.createElement("select");
    select.id = "entity-select";
    deviceGroup.appendChild(select);
    const deviceInfo = document.createElement("div");
    deviceInfo.className = "info";
    deviceGroup.appendChild(deviceInfo);
    wrap.appendChild(deviceGroup);

    const nameGroup = document.createElement("div");
    nameGroup.className = "form-group";
    const nameLabel = document.createElement("label");
    nameLabel.textContent = "Card Name (optional)";
    nameGroup.appendChild(nameLabel);
    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.id = "name-input";
    nameInput.placeholder = "No title";
    nameGroup.appendChild(nameInput);
    wrap.appendChild(nameGroup);

    const extendedInfoGroup = document.createElement("div");
    extendedInfoGroup.className = "form-group checkbox-group";
    const extendedInfoLabel = document.createElement("label");
    const extendedInfoInput = document.createElement("input");
    extendedInfoInput.type = "checkbox";
    extendedInfoInput.id = "show-extended-info-input";
    extendedInfoLabel.appendChild(extendedInfoInput);
    extendedInfoLabel.appendChild(
      document.createTextNode(" Extended information"),
    );
    extendedInfoGroup.appendChild(extendedInfoLabel);
    wrap.appendChild(extendedInfoGroup);

    this.appendChild(wrap);

    modeSelect.onchange = (event) => this._modeChanged(event.target.value);
    select.onchange = (event) => this._deviceChanged(event.target.value);
    nameInput.onchange = (event) => this._nameChanged(event.target.value);
    extendedInfoInput.onchange = (event) => {
      this._boolChanged("show_extended_info", event.target.checked);
    };

    this._els = {
      modeSelect,
      modeInfo,
      deviceGroup,
      deviceLabel,
      select,
      deviceInfo,
      nameGroup,
      nameLabel,
      nameInput,
      extendedInfoGroup,
      extendedInfoInput,
    };
  }

  _render() {
    if (!this._els) this._buildSkeleton();
    const els = this._els;
    const mode = this._config.mode || this._config.card_mode || "esp_mirror";
    const softphoneMode = mode === "ha_softphone";
    const phonebookMode = mode === "phonebook";
    els.modeSelect.value = phonebookMode
      ? "phonebook"
      : softphoneMode
        ? "ha_softphone"
        : "esp_mirror";
    els.modeInfo.textContent = phonebookMode
      ? "Scrollable view of the shared VoIP phonebook."
      : softphoneMode
        ? "Home Assistant phone: bind this card to one logical phone, or leave it unselected for the default phone."
        : "ESP mirror card: mirrors one ESP endpoint and presses that ESP's own call, answer and hangup controls.";
    els.deviceGroup.classList.toggle("hidden", phonebookMode);
    els.deviceLabel.textContent = softphoneMode
      ? "Home Assistant phone"
      : "VoIP Device";

    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = softphoneMode
      ? "Default Home Assistant softphone"
      : "-- Select device --";
    const newOptions = [placeholder];
    const selectableDevices = this._devices.filter((device) => softphoneMode
      ? this._isSoftphoneDevice(device) &&
        String(device.endpoint_id || "") !== DEFAULT_SOFTPHONE_ENDPOINT_ID &&
        String(device.device_id || "") !== HA_SOFTPHONE_DEVICE_ID
      : !this._isSoftphoneDevice(device));
    const configuredDeviceId = String(
      this._config.device_id || this._config.entity_id || "",
    );
    const configuredEndpointId = String(this._config.endpoint_id || "");
    const selectedDevice = selectableDevices.find((device) =>
      (configuredDeviceId && device.device_id === configuredDeviceId) ||
      (
        !configuredDeviceId &&
        configuredEndpointId &&
        device.endpoint_id === configuredEndpointId
      ));
    for (const device of selectableDevices) {
      const option = document.createElement("option");
      option.value = device.device_id;
      option.textContent = softphoneMode
        ? `${device.name || device.endpoint_id || device.device_id}${device.extension ? ` (${device.extension})` : ""}`
        : `${device.name} (${this._audioModeLabel(device.audio_mode)})`;
      if (selectedDevice === device) option.selected = true;
      newOptions.push(option);
    }
    const configuredMissingPhone = softphoneMode &&
      (
        configuredDeviceId ||
        (
          configuredEndpointId &&
          configuredEndpointId !== DEFAULT_SOFTPHONE_ENDPOINT_ID
        )
      ) &&
      !selectedDevice;
    if (configuredMissingPhone) {
      const missing = document.createElement("option");
      missing.value = configuredDeviceId ||
        `missing-endpoint:${configuredEndpointId}`;
      missing.textContent =
        `Missing phone: ${configuredEndpointId || configuredDeviceId}`;
      missing.selected = true;
      missing.disabled = true;
      newOptions.push(missing);
    }
    els.select.replaceChildren(...newOptions);

    if (!this._devicesLoaded) {
      els.deviceInfo.textContent = "Loading...";
    } else if (selectableDevices.length === 0) {
      els.deviceInfo.textContent = softphoneMode
        ? "No additional HA softphones found; the default phone will be used."
        : "No devices found";
    } else if (configuredMissingPhone) {
      els.deviceInfo.textContent =
        "The configured Home Assistant phone no longer exists. Select another phone or the default.";
    } else if (selectedDevice) {
      els.deviceInfo.textContent = softphoneMode
        ? `Endpoint: ${selectedDevice.endpoint_id || DEFAULT_SOFTPHONE_ENDPOINT_ID}`
        : `Audio: ${this._normaliseAudioMode(selectedDevice.audio_mode).replace("_", " ")}`;
    } else {
      els.deviceInfo.textContent = softphoneMode
        ? "Omit the selection to use the default Home Assistant phone."
        : "Required for ESP mirror mode.";
    }

    els.nameLabel.textContent = phonebookMode
      ? "Title (optional)"
      : "Card Name (optional)";
    els.nameInput.placeholder = "No title";
    els.nameInput.value = phonebookMode
      ? this._config.title || this._config.name || ""
      : this._config.name || "";
    els.extendedInfoGroup.classList.toggle("hidden", phonebookMode);
    els.extendedInfoInput.checked = !!this._config.show_extended_info;
  }

  _nameChanged(value) {
    const key = (this._config.mode || this._config.card_mode) === "phonebook"
      ? "title"
      : "name";
    this._valueChanged(key, value);
  }

  _isSoftphoneDevice(device) {
    const type = String(
      device?.endpoint_type || device?.type || device?.kind || "",
    ).toLowerCase();
    return !!device?.softphone || !!device?.endpoint_id &&
      ["browser", "ha_softphone", "home_assistant", "softphone"].includes(type);
  }

  _deviceChanged(deviceId) {
    const newConfig = { ...this._config };
    if (deviceId) {
      const selected = this._devices.find(
        (device) => device.device_id === deviceId,
      );
      newConfig.device_id = deviceId;
      delete newConfig.entity_id;
      if (selected?.endpoint_id) newConfig.endpoint_id = selected.endpoint_id;
      else delete newConfig.endpoint_id;
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
