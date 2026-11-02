const VERSION = (() => { try { return new URL(import.meta.url).searchParams.get("v") || "dev"; } catch (_) { return "dev"; } })();
const { normaliseDashboardConfig, dashboardTranslate } = await import(`./voip-stack-dashboard-model.js?v=${encodeURIComponent(VERSION)}`);
const { voipStackTranslate } = await import(`./voip-stack-i18n.js?v=${encodeURIComponent(VERSION)}`);

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
        return device.device_id && ["browser", "ha_softphone", "esphome"].includes(kind);
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

  _render() {
    const root = this.shadowRoot;
    root.replaceChildren();
    const style = document.createElement("style");
    style.textContent = `:host{display:block;padding:14px;color:var(--primary-text-color)}label{display:block;margin:0 0 7px;font-weight:500}select,input{box-sizing:border-box;width:100%;font:inherit;padding:11px;border:1px solid var(--divider-color);border-radius:7px;background:var(--card-background-color);color:var(--primary-text-color)}.field+.field{margin-top:18px}.hint{font-size:13px;color:var(--secondary-text-color);margin-top:9px}.error{color:var(--error-color)}button{font:inherit;min-height:44px;margin-top:12px;background:transparent;color:var(--primary-color);border:1px solid var(--divider-color);border-radius:6px;padding:8px 12px}option{background-color:Canvas;color:CanvasText}`;
    root.append(style);
    const field = (label, id) => {
      const row = document.createElement("div"); row.className = "field";
      const title = document.createElement("label"); title.htmlFor = id; title.textContent = this._t(label); row.append(title); root.append(row); return row;
    };
    const ownerField = field("Calling phone", "owner-select");
    const select = document.createElement("select"); select.id = "owner-select"; select.required = true;
    const empty = document.createElement("option"); empty.value = ""; empty.textContent = this._t("Choose a phone"); select.append(empty);
    for (const device of this._devices) {
      const option = document.createElement("option"); option.value = device.device_id;
      option.textContent = `${device.name || device.device_id}${device.extension ? ` (${device.extension})` : ""}`;
      select.append(option);
    }
    const selected = this._config.device_id || "";
    if (selected && !this._devices.some((device) => device.device_id === selected)) {
      const missing = document.createElement("option"); missing.value = selected; missing.disabled = true;
      missing.textContent = `${this._t("Missing phone")}: ${selected}`; select.append(missing);
    }
    select.value = selected; select.onchange = () => this._changed("device_id", select.value); ownerField.append(select);
    const titleField = field("Title (optional)", "dashboard-title"); const input = document.createElement("input");
    input.id = "dashboard-title"; input.type = "text"; input.value = this._config.title || "";
    input.onchange = () => this._changed("title", input.value); titleField.append(input);
    if (this._error) {
      const error = document.createElement("p"); error.className = "hint error"; error.textContent = this._error; root.append(error);
      const retry = document.createElement("button"); retry.type = "button"; retry.textContent = this._t("Reload phones"); retry.onclick = () => { this._error = ""; this._load(); }; root.append(retry);
    }
  }
}

if (!customElements.get("voip-stack-dashboard-card-editor")) {
  customElements.define("voip-stack-dashboard-card-editor", VoipStackDashboardCardEditor);
}
