const VERSION = (() => { try { return new URL(import.meta.url).searchParams.get("v") || "dev"; } catch (_) { return "dev"; } })();
const { normaliseDashboardConfig, dashboardTranslate } = await import(`./voip-stack-dashboard-model.js?v=${encodeURIComponent(VERSION)}`);
const { voipStackTranslate } = await import(`./voip-stack-i18n.js?v=${encodeURIComponent(VERSION)}`);

const { ensureNativeForm } = await import(`./voip-stack-form.js?v=${encodeURIComponent(VERSION)}`);

class VoipStackDashboardCardEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._devices = [];
    this._connection = null;
    this._loading = false;
    this._loaded = false;
    this._error = "";
  }

  connectedCallback() { void ensureNativeForm().then(() => this._render()); }

  _t(text) { return dashboardTranslate(this._hass?.language, text, (value) => voipStackTranslate(this._hass, value)); }
  setConfig(config) { this._config = normaliseDashboardConfig(config); this._render(); }
  set hass(hass) {
    const changed = this._hass?.connection !== hass?.connection;
    const languageChanged = this._hass?.language !== hass?.language;
    this._hass = hass;
    if (changed) { this._loaded = false; this._error = ""; this._connection = hass?.connection; }
    if (!this._loaded && !this._loading && !this._error) this._load();
    if (changed || languageChanged || !this.shadowRoot.childElementCount) this._render();
  }

  async _load() {
    const connection = this._hass?.connection;
    if (!connection?.sendMessagePromise || this._loading) return;
    this._loading = true;
    try {
      const response = await connection.sendMessagePromise({ type: "voip_stack/list_devices" });
      if (this._hass?.connection !== connection) return;
      this._devices = (response?.devices || []).filter((device) => {
        const kind = String(device.kind || device.endpoint_type || (device.softphone ? "browser" : "esphome"));
        return device.device_id && ["browser", "ha_softphone", "sip_account", "esphome"].includes(kind);
      });
      this._loaded = true;
      this._error = "";
    } catch (error) {
      if (this._hass?.connection === connection) this._error = error?.message || String(error);
    } finally { this._loading = false; this._render(); }
  }

  _changed(key, value) {
    const config = { ...this._config };
    if (value) config[key] = value; else delete config[key];
    this._config = config;
    this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
  }

  _formChanged(event) {
    const values = event.detail?.value;
    if (!values || typeof values !== "object") return;
    const config = { ...this._config };
    let changed = false;
    for (const key of ["device_id", "title"]) {
      if (!Object.hasOwn(values, key) || values[key] === (this._config[key] || "")) continue;
      changed = true;
      if (values[key]) config[key] = values[key]; else delete config[key];
    }
    if (!changed) return;
    this._config = config;
    this._render();
    this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
  }

  _render() {
    const root = this.shadowRoot;
    if (!this._form) {
      const form = this._form = document.createElement("ha-form");
      form.style.cssText = "display:block;padding:14px";
      form.addEventListener("value-changed", event => this._formChanged(event));
      const hint = this._hint = document.createElement("p");
      hint.style.cssText = "color:var(--error-color);padding:0 14px";
      const retry = this._retry = document.createElement("ha-button");
      retry.textContent = this._t("Reload phones");
      retry.addEventListener("click", () => { this._error = ""; this._load(); });
      root.append(form, hint, retry);
    }
    const options = [{ value: "", label: this._t("Choose a phone") },
      ...this._devices.map(device => ({ value: device.device_id,
        label: `${device.name || device.device_id}${device.extension ? ` (${device.extension})` : ""}` }))];
    const selected = this._config.device_id || "";
    if (selected && !this._devices.some(device => device.device_id === selected)) {
      options.push({ value: selected, label: this._t("Missing phone") });
    }
    this._form.hass = this._hass;
    this._form.computeLabel = schema => this._t(schema.name === "device_id" ? "Calling phone" : "Title (optional)");
    this._form.schema = [
      { name: "device_id", required: true, selector: { select: { options, mode: "dropdown" } } },
      { name: "title", selector: { text: {} } },
    ];
    this._form.data = { device_id: selected, title: this._config.title || "" };
    this._hint.hidden = !this._error;
    this._hint.textContent = this._error;
    this._retry.hidden = !this._error;
  }

}

if (!customElements.get("voip-stack-dashboard-card-editor")) {
  customElements.define("voip-stack-dashboard-card-editor", VoipStackDashboardCardEditor);
}
