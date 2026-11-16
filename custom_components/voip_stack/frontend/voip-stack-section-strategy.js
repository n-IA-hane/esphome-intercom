/** Native HA section strategy, declaring existing SIP accounts by stable ID. */
const VERSION = (() => { try { return new URL(import.meta.url).searchParams.get("v") || "dev"; } catch (_) { return "dev"; } })();
const [{ supportsHaClient }, { voipStackTranslate }, { ensureNativeForm }] = await Promise.all([
  import(`./voip-stack-card-model.js?v=${encodeURIComponent(VERSION)}`),
  import(`./voip-stack-i18n.js?v=${encodeURIComponent(VERSION)}`),
  import(`./voip-stack-form.js?v=${encodeURIComponent(VERSION)}`),
]);

async function accounts(hass) {
  const result = await hass.callWS({ type: "voip_stack/list_devices" });
  return (result?.devices || []).filter(phone => supportsHaClient(phone) && phone.endpoint_id && phone.device_id);
}

class VoipStackSectionStrategy extends HTMLElement {
  static configRequired = true;

  static getStubConfig() { return { type: "custom:voip-stack", account_ids: [] }; }

  static async getConfigElement() {
    await ensureNativeForm();
    return document.createElement("voip-stack-section-strategy-editor");
  }

  static async generate(config, hass) {
    const ids = config.account_ids || [];
    if (!Array.isArray(ids) || ids.some(id => typeof id !== "string" || !id)) {
      throw new Error("Choose existing SIP accounts.");
    }
    const byId = new Map((await accounts(hass)).map(phone => [phone.endpoint_id, phone]));
    const cards = [];
    let missing = false;
    for (const id of new Set(ids)) {
      const phone = byId.get(id);
      if (!phone) { missing = true; continue; }
      cards.push({ type: "custom:voip-stack-card", mode: "ha_softphone", device_id: phone.device_id });
    }
    if (!ids.length || missing) cards.push({ type: "markdown", content: voipStackTranslate(hass,
      !ids.length ? "Choose existing SIP accounts to display in this section."
        : "A selected SIP account is unavailable. Edit this section to update the selection.") });
    return { type: "grid", cards };
  }
}

class VoipStackSectionStrategyEditor extends HTMLElement {
  constructor() { super(); this._config = {}; this._accounts = []; this._connection = null; }

  connectedCallback() { void ensureNativeForm().then(() => this._render()); }
  setConfig(config) { this._config = { ...config }; this._render(); }
  set hass(hass) {
    this._hass = hass;
    const connection = hass?.connection;
    if (connection && connection !== this._connection) {
      this._connection = connection;
      void accounts(hass).then(phones => {
        if (this._hass?.connection !== connection) return;
        this._accounts = phones;
        this._error = "";
        this._render();
      }).catch(() => { if (this._hass?.connection === connection) { this._error = "Unable to load SIP accounts."; this._render(); } });
    }
    this._render();
  }

  _render() {
    if (!this._form) {
      const form = this._form = document.createElement("ha-form");
      form.addEventListener("value-changed", event => {
        const ids = event.detail?.value?.account_ids;
        if (!Array.isArray(ids) || ids.some(id => typeof id !== "string" || !id)) return;
        this._config = { ...this._config, type: "custom:voip-stack", account_ids: [...new Set(ids)] };
        this.dispatchEvent(new CustomEvent("config-changed", { detail: { config: this._config }, bubbles: true, composed: true }));
      });
      const hint = this._hint = document.createElement("p");
      this.append(form, hint);
    }
    const selected = Array.isArray(this._config.account_ids) ? this._config.account_ids : [];
    const options = this._accounts.map(phone => ({ value: phone.endpoint_id,
      label: `${phone.name || "SIP account"}${phone.extension ? ` (${phone.extension})` : ""}` }));
    for (const id of selected) if (!options.some(option => option.value === id)) {
      options.push({ value: id, label: voipStackTranslate(this._hass, "Missing phone") });
    }
    this._form.hass = this._hass;
    this._form.computeLabel = () => voipStackTranslate(this._hass, "SIP accounts");
    this._form.schema = [{ name: "account_ids", required: true,
      selector: { select: { options, multiple: true, mode: "dropdown" } } }];
    this._form.data = { account_ids: selected };
    this._hint.textContent = this._error ? voipStackTranslate(this._hass, this._error) : "";
  }
}

if (!customElements.get("ll-strategy-section-voip-stack")) {
  customElements.define("ll-strategy-section-voip-stack", VoipStackSectionStrategy);
}
if (!customElements.get("voip-stack-section-strategy-editor")) {
  customElements.define("voip-stack-section-strategy-editor", VoipStackSectionStrategyEditor);
}
window.customStrategies = window.customStrategies || [];
if (!window.customStrategies.some(strategy => strategy.type === "voip-stack" && strategy.strategyType === "section")) {
  window.customStrategies.push({ type: "voip-stack", strategyType: "section", name: "VoIP Stack",
    description: "Existing SIP accounts and their phone controls.",
    documentationURL: "https://github.com/n-IA-hane/esphome-intercom/blob/dev/docs/USER_GUIDE.md" });
}
