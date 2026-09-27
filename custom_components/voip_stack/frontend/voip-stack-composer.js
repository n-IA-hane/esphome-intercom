const version = new URL(import.meta.url).searchParams.get("v") || "dev";
const { voipStackEngine } = await import(`./voip-stack-engine.js?v=${encodeURIComponent(version)}`);
const { voipStackTranslate } = await import(`./voip-stack-i18n.js?v=${encodeURIComponent(version)}`);

/** Persistent HA dialog: minimizing keeps the existing phone/media owner alive. */
class VoipStackComposer extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._phones = [];
    this._selected = "";
    this._loading = null;
    this._dialog = document.createElement("ha-dialog");
    this._dialog.setAttribute("header-title", "VoIP Stack");
    this._dialog.setAttribute("width", "medium");
    this._dialog.addEventListener("closed", () => this._closed());
    this._form = document.createElement("ha-form");
    this._form.addEventListener("value-changed", (event) => {
      if (this._phone?.callActive) return;
      this._select(event.detail.value.caller);
    });
    this._error = document.createElement("ha-alert");
    this._error.setAttribute("alert-type", "error");
    this._error.hidden = true;
    this._phone = document.createElement("voip-stack-card");
    this._dialog.append(this._form, this._error, this._phone);
    this.shadowRoot.append(this._dialog);
    this._sync = () => this._renderForm();
  }

  connectedCallback() {
    voipStackEngine.addEventListener("state", this._sync);
  }

  disconnectedCallback() {
    voipStackEngine.removeEventListener("state", this._sync);
  }

  set hass(hass) {
    this._hass = hass;
    this._form.hass = hass;
    if (this._selected) this._phone.hass = hass;
    this._renderForm();
    if (this._open && !this._selected && !this._loading) void this.showDialog();
  }

  async showDialog() {
    this._dialog.open = true;
    this._open = true;
    if (!this._hass || this._loading || this._phone.callActive) return;
    this._loading = this._loadPhones();
    try { await this._loading; } finally { this._loading = null; }
  }

  closeDialog() {
    this._dialog.open = false;
    this._closed();
    return true;
  }

  _closed() {
    if (!this._open) return;
    this._open = false;
    this.dispatchEvent(new CustomEvent("dialog-closed", {
      detail: { dialog: "voip-stack-composer" }, bubbles: true, composed: true,
    }));
  }

  async _loadPhones() {
    try {
      const result = await this._hass.callWS({ type: "voip_stack/list_devices" });
      const external = this._hass.auth.external;
      this._nativeContext = external?.config?.nativeCalls === 1
        ? await external.sendMessage({ type: "call/context" }) : null;
      this._phones = (result.devices || []).filter((phone) =>
        phone.endpoint_type === "browser" || phone.endpoint_type === "esphome" ||
        (phone.endpoint_type === "companion" && phone.mobile_device_id === this._nativeContext?.deviceId));
      const selected = this._phones.find((phone) => phone.device_id === this._selected)
        || (this._nativeContext
          ? this._phones.find((phone) => phone.endpoint_type === "companion" && phone.mobile_device_id === this._nativeContext.deviceId)
          : this._phones.find((phone) => phone.endpoint_id === "default"));
      if (selected) this._select(selected.device_id);
      else this._showError("The default Home Assistant phone is unavailable. Choose a calling phone.");
      this._renderForm();
    } catch (error) { this._showError(error.message || String(error)); }
  }

  _select(deviceId) {
    const phone = this._phones.find((item) => item.device_id === deviceId);
    if (!phone || this._phone.callActive) return;
    this._selected = deviceId;
    this._error.hidden = true;
    this._phone.nativeCallContext = phone.endpoint_type === "companion" ? this._nativeContext : null;
    this._phone.setConfig({ type: "custom:voip-stack-card", mode: phone.endpoint_type === "esphome" ? "esp_mirror" : "ha_softphone", device_id: deviceId });
    this._phone.presentation = "composer";
    this._phone.hass = this._hass;
    this._renderForm();
  }

  _renderForm() {
    if (!this._hass) return;
    this._form.computeLabel = () => voipStackTranslate(this._hass, "Calling phone");
    this._form.data = { caller: this._selected };
    this._form.disabled = !!this._phone.callActive;
    this._form.schema = [{ name: "caller", required: true, selector: {
      select: { mode: "dropdown", options: this._phones.map((phone) => ({
        value: phone.device_id, label: phone.name,
      })) },
    } }];
  }

  _showError(message) {
    this._error.textContent = voipStackTranslate(this._hass, message);
    this._error.hidden = false;
  }
}

if (!customElements.get("voip-stack-composer")) customElements.define("voip-stack-composer", VoipStackComposer);
