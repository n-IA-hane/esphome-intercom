/** Browser-local presentation of an existing HA phone, independent of cards. */
const VERSION = new URL(import.meta.url).searchParams.get("v") || "dev";
const { voipStackEngine: engine } = await import(`./voip-stack-engine.js?v=${encodeURIComponent(VERSION)}`);
const { softphoneSnapshotSupersedes, terminalPeerLabel, formatCallDuration } = await import(`./voip-stack-card-model.js?v=${encodeURIComponent(VERSION)}`);
const { voipStackTranslate: translate } = await import(`./voip-stack-i18n.js?v=${encodeURIComponent(VERSION)}`);
const DIALOG = "voip-stack-phone-dialog";
const PREFERENCE = "voip_stack_popup_phone";
const ACTIVE = new Set(["ringing", "incoming", "calling", "in_call"]);
const activeCall = state => !!state?.call_id && ACTIVE.has(state.state) && state.enabled !== false;
const node = (tag, text = "") => Object.assign(document.createElement(tag), { textContent: text });

class VoipStackPhoneDialog extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    const style = node("style");
    style.textContent = `dialog { box-sizing:border-box; width:min(560px,calc(100vw - 24px));
      max-height:calc(100dvh - 24px); padding:12px; border:1px solid var(--divider-color);
      border-radius:var(--ha-card-border-radius,12px); color:var(--primary-text-color);
      background:var(--ha-card-background,var(--card-background-color,#fff)); }
      dialog::backdrop { background:rgba(0,0,0,.32); }
      header { display:flex; align-items:center; gap:8px; } h2 { flex:1; font-size:1.1rem; }
      button { min-width:44px; min-height:44px; cursor:pointer; font:inherit; }
      voip-stack-card { display:block; }`;
    this._dialog = node("dialog");
    const header = node("header");
    this._title = node("h2");
    this._title.id = "phone-title";
    this._dialog.setAttribute("aria-labelledby", this._title.id);
    const close = this._close = node("button", "×");
    close.type = "button";
    close.onclick = () => this.closeDialog();
    header.append(this._title, close);
    this._view = node("div");
    this._error = node("p");
    this._error.setAttribute("role", "alert");
    this._dialog.append(header, this._view, this._error);
    this.shadowRoot.append(style, this._dialog);
    this._dialog.addEventListener("cancel", event => { event.preventDefault(); this.closeDialog(); });
  }
  set hass(value) {
    this._hass = value;
    this._close.setAttribute("aria-label", translate(value, "Minimize call"));
    if (this._phone) this._phone.hass = value;
  }
  async showDialog({ presenter, callId }) {
    const generation = this._generation = (this._generation || 0) + 1;
    this._presenter = presenter;
    presenter.dialog = this;
    const current = () => generation === this._generation && presenter.state?.call_id === callId && activeCall(presenter.state);
    if (!current()) return;
    this._title.textContent = presenter.phone.name;
    this._error.textContent = "";
    try {
      await import(`./voip-stack-card.js?v=${encodeURIComponent(VERSION)}`);
      if (!current()) return;
      if (!this._phone) {
        this._phone = document.createElement("voip-stack-card");
        this._phone.setIncomingPopupMode(true);
        this._phone.setDashboardMode(true);
        this._phone.setConfig({ mode: "ha_softphone", device_id: presenter.phone.device_id });
        this._phone.hass = this._hass;
        this._view.append(this._phone);
      }
      this._phone.focusPhoneView();
      this._phone.consumePhoneAnswerLink();
      presenter.minimized = false;
      presenter.renderMini();
      // HA appends its persistent dialog after invoking showDialog().
      queueMicrotask(() => { if (current() && this.isConnected && !this._dialog.open) this._dialog.showModal(); });
    } catch (error) { this._error.textContent = translate(this._hass, error?.message || String(error)); }
  }
  closeDialog(minimize = true) {
    this._generation++;
    this._phone?.remove(); // The page engine retains media; this releases only the view/canvas lease.
    this._phone = null;
    this._dialog.close();
    if (this._presenter) { this._presenter.minimized = minimize; this._presenter.renderMini(); }
    this.dispatchEvent(new CustomEvent("dialog-closed", { detail: { dialog: DIALOG }, bubbles: true, composed: true }));
    return true;
  }
}

export class VoipStackPopup extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    const style = node("style");
    style.textContent = `:host { position:fixed; right:16px; bottom:max(16px,env(safe-area-inset-bottom)); z-index:5; }
      section { display:flex; align-items:center; gap:8px; padding:8px; border:1px solid var(--divider-color);
        box-sizing:border-box; max-width:calc(100vw - 32px);
        border-radius:var(--ha-card-border-radius,12px); color:var(--primary-text-color);
        background:var(--ha-card-background,var(--card-background-color,#fff)); box-shadow:var(--ha-card-box-shadow); }
      section[hidden] { display:none; } button { min-height:44px; min-width:44px; cursor:pointer; font:inherit; }
      .reopen { border:0; background:transparent; color:inherit; text-align:left; min-width:0; }
      .reopen span { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .hangup { border:0; border-radius:50%; background:#f44336; color:white; flex-shrink:0; }
      small { display:block; }`;
    this._mini = node("section");
    this._mini.hidden = true;
    this._reopen = node("button");
    this._reopen.type = "button";
    this._reopen.className = "reopen";
    this._caller = node("span");
    this._duration = node("small");
    this._reopen.append(this._caller, this._duration);
    this._reopen.onclick = () => this.open();
    this._hangup = node("button");
    this._hangup.type = "button";
    this._hangup.className = "hangup";
    const icon = node("ha-icon");
    icon.setAttribute("icon", "mdi:phone-hangup");
    this._hangup.append(icon);
    this._hangup.onclick = () => this.terminate();
    this._mini.append(this._reopen, this._hangup);
    this.shadowRoot.append(style, this._mini);
    this._ready = () => { if (this.binding) void this.resolveBinding(); };
    this._modalClosed = () => this.restoreMiniHost();
    this._answerLink = () => {
      const params = new URLSearchParams(location.search);
      if (["1", "true", "yes"].includes((params.get("voip_answer") || "").toLowerCase()) &&
          params.get("voip_endpoint") === this.binding?.endpoint_id &&
          params.get("voip_call_id") === this.state?.call_id &&
          this.state?.direction === "incoming" && ["ringing", "incoming"].includes(this.state.state)) this.open();
    };
    window.addEventListener("location-changed", this._answerLink);
    window.addEventListener("pageshow", this._answerLink);
    try { this.binding = JSON.parse(localStorage.getItem(PREFERENCE) || "null"); } catch (_) { this.binding = null; }
    if (!this.binding?.device_id || !this.binding?.endpoint_id) this.binding = null;
  }
  set hass(hass) {
    this._hass = hass;
    this._mini.setAttribute("aria-label", translate(hass, "Minimized call"));
    this._reopen.setAttribute("aria-label", translate(hass, "Reopen call"));
    this._hangup.setAttribute("aria-label", translate(hass, "Hang up call"));
    if (this.minimized && !this.isConnected) this.positionMini();
    if (this.dialog) this.dialog.hass = hass;
    if (this._connection === hass?.connection) { if (this.binding) engine.configure(hass); return; }
    this._connection?.removeEventListener?.("ready", this._ready);
    this._connection = hass?.connection;
    this._connection?.addEventListener?.("ready", this._ready);
    if (this.binding && this._connection) { engine.configure(hass); void this.resolveBinding(); }
  }
  async listPhones() {
    const result = await this._hass.callWS({ type: "voip_stack/list_devices" });
    return (result?.devices || []).filter(phone => phone.softphone === true && phone.device_id && phone.endpoint_id);
  }
  async resolveBinding() {
    const binding = this.binding;
    const connection = this._connection;
    try {
      const phones = await this.listPhones();
      if (binding !== this.binding || connection !== this._connection) return;
      const phone = phones.find(item => item.device_id === binding?.device_id && item.endpoint_id === binding?.endpoint_id);
      if (!phone) { this.release(); this.error = "The selected Home Assistant phone is unavailable."; return; }
      this.phone = phone;
      if (this._unsubscribe) return;
      engine.configure(this._hass);
      this._unsubscribe = engine.subscribeSoftphoneState(state => this.onState(state), { device_id: phone.device_id });
    } catch (error) { this.error = error?.message || String(error); }
  }
  bindPhone(phone, enabled) {
    const next = enabled ? { device_id: phone.device_id, endpoint_id: phone.endpoint_id } : null;
    if ((next?.endpoint_id !== this.binding?.endpoint_id) && activeCall(this.state)) {
      throw new Error("End the current call before changing the browser phone.");
    }
    // Persist only the existing phone identity, never an HA user/account relation.
    if (next) localStorage.setItem(PREFERENCE, JSON.stringify(next));
    else localStorage.removeItem(PREFERENCE);
    if (next?.endpoint_id === this.binding?.endpoint_id) { if (next) engine.unlockRingtone(); return; }
    this.release();
    this.binding = next;
    if (next) { this.phone = phone; engine.unlockRingtone(); void this.resolveBinding(); }
  }
  onState(state) {
    if (state.endpoint_id !== this.phone?.endpoint_id || state.device_id !== this.phone?.device_id) return;
    if (this.state && !softphoneSnapshotSupersedes(this.state, state)) return;
    this.state = state;
    const incoming = activeCall(state) && state.available !== false &&
      ["ringing", "incoming"].includes(state.state) && state.direction === "incoming";
    engine.setRingtoneRequest("voip-stack-global-popup", incoming, true);
    void engine.syncOwnedSoftphoneState(this.phone, state).catch(error => { this.error = error?.message || String(error); });
    if (!activeCall(state)) { this.dialog?.closeDialog(false); this.minimized = false; }
    else if (incoming && this._announcedCall !== state.call_id) { this._announcedCall = state.call_id; this.open(); }
    this.renderMini();
  }
  open() {
    if (!activeCall(this.state)) return;
    this._root.dispatchEvent(new CustomEvent("show-dialog", { detail: {
      dialogTag: DIALOG, dialogImport: () => Promise.resolve(),
      dialogParams: { presenter: this, callId: this.state.call_id },
    }, bubbles: true, composed: true }));
  }
  async terminate() {
    const state = this.state;
    if (!activeCall(state) || this._pending) return;
    this._pending = this._hangup.disabled = true;
    try { await engine.dashboardTerminate(["ringing", "incoming"].includes(state.state) ? "decline" : "hangup", this.phone, state); }
    catch (error) { this.error = error?.message || String(error); this._mini.title = translate(this._hass, this.error); }
    finally { this._pending = this._hangup.disabled = false; }
  }
  renderMini() {
    const visible = this.minimized && activeCall(this.state);
    this._mini.hidden = !visible;
    const update = () => {
      this._caller.textContent = terminalPeerLabel(this.state);
      const start = Number(this.state?.connected_at || 0);
      this._duration.textContent = start ? formatCallDuration(start) : translate(this._hass, "Incoming call");
    };
    if (visible) { this.positionMini(); update(); if (!this._timer) this._timer = setInterval(update, 1000); }
    else {
      this.restoreMiniHost();
      if (!activeCall(this.state)) this._returnModal = null;
      if (this._timer) { clearInterval(this._timer); this._timer = null; }
    }
  }
  positionMini() {
    // A node outside the top native modal is inert. Follow only the current
    // focus ancestry, not HA's private dialog state or an entire shadow tree.
    let focused = document.activeElement;
    while (focused?.shadowRoot?.activeElement) focused = focused.shadowRoot.activeElement;
    for (let item = focused; item; item = item.parentElement || item.getRootNode()?.host) {
      if (item.matches?.("dialog:modal") && !this.contains(item)) {
        if (this._modal === item) return;
        this.restoreMiniHost(); this._modal = this._returnModal = item;
        item.append(this); item.addEventListener("close", this._modalClosed, { once: true });
        return;
      }
    }
    // Reopening from the minicard can make that now-hidden button the native
    // dialog's previous focus. Retain only its still-open modal presentation anchor.
    if (this._returnModal?.matches("dialog:modal")) {
      if (this._modal === this._returnModal) return;
      this.restoreMiniHost(); this._modal = this._returnModal;
      this._modal.append(this); this._modal.addEventListener("close", this._modalClosed, { once: true });
      return;
    }
    this.restoreMiniHost();
  }
  restoreMiniHost() {
    this._modal?.removeEventListener("close", this._modalClosed);
    this._modal = null;
    if (this.parentNode !== this._root?.shadowRoot) this._root?.shadowRoot?.append(this);
  }
  release() {
    this._unsubscribe?.(); this._unsubscribe = null;
    engine.clearRingtoneRequest("voip-stack-global-popup");
    this.dialog?.closeDialog(false);
    this.state = null; this.minimized = false; this._announcedCall = ""; this.renderMini();
  }
}

/** Supported HA custom panel, deliberately absent from the sidebar. */
export class VoipStackPopupSettings extends HTMLElement {
  constructor() {
    super(); this.attachShadow({ mode: "open" });
    const style = node("style");
    style.textContent = `:host { display:block; padding:24px; color:var(--primary-text-color); }
      form { max-width:560px; } label { display:block; margin:16px 0; }
      select,button { min-height:44px; font:inherit; } select { width:100%; }
      p { line-height:1.5; } .error { color:var(--error-color); }`;
    this._title = node("h1");
    this._form = node("form");
    const selectLabel = node("label");
    this._selectText = node("span");
    this._select = node("select"); selectLabel.append(this._selectText, this._select);
    const enabledLabel = node("label");
    this._enabled = node("input"); this._enabled.type = "checkbox";
    this._enabledText = node("span");
    enabledLabel.append(this._enabled, this._enabledText);
    this._notice = node("p");
    this._save = node("button"); this._save.type = "submit";
    this._message = node("p"); this._message.setAttribute("role", "status");
    this._form.append(selectLabel, enabledLabel, this._notice, this._save, this._message);
    this.shadowRoot.append(style, this._title, this._form);
    this._form.onsubmit = event => {
      event.preventDefault();
      const phone = this._phones?.find(item => item.device_id === this._select.value);
      try {
        if (this._enabled.checked && !phone) throw new Error("Select an existing Home Assistant phone.");
        globalThis.__voipStackPopup.bindPhone(phone, this._enabled.checked);
        this._message.className = ""; this._message.textContent = translate(this._hass, "Saved");
      } catch (error) { this._message.className = "error"; this._message.textContent = translate(this._hass, error?.message || String(error)); }
    };
  }
  set hass(value) {
    this._hass = value;
    this._title.textContent = translate(value, "Call popups");
    this._selectText.textContent = translate(value, "Home Assistant phone");
    this._enabledText.textContent = ` ${translate(value, "Receive call popups on this device")}`;
    this._notice.textContent = translate(value, "Uses an existing softphone without a card. This choice is saved in this app/browser. X, Esc and Back minimize an active call. Audio still requires browser permission.");
    this._save.textContent = translate(value, "Save");
    if (this._select.options[0]) this._select.options[0].textContent = translate(value, "-- Select phone --");
    if (this._connection === value?.connection) return;
    this._connection = value?.connection;
    if (this._connection) void this.load();
  }
  async load() {
    try {
      const presenter = await presenterReady;
      presenter.hass = this._hass;
      this._phones = await presenter.listPhones();
      const endpoint = new URL(location.href).searchParams.get("endpoint_id");
      const selected = this._phones.find(item => item.endpoint_id === endpoint) ||
        this._phones.find(item => item.device_id === presenter.binding?.device_id);
      this._select.replaceChildren(new Option(translate(this._hass, "-- Select phone --"), ""), ...this._phones.map(phone => new Option(phone.name, phone.device_id)));
      this._select.value = selected?.device_id || "";
      this._enabled.checked = !!selected && presenter.binding?.device_id === selected.device_id;
    } catch (error) { this._message.className = "error"; this._message.textContent = translate(this._hass, error?.message || String(error)); }
  }
}

for (const [tag, type] of [[DIALOG, VoipStackPhoneDialog], ["voip-stack-popup", VoipStackPopup], ["voip-stack-popup-settings", VoipStackPopupSettings]]) {
  if (!customElements.get(tag)) customElements.define(tag, type);
}
async function bootstrap() {
  await customElements.whenDefined("home-assistant");
  const attach = async root => {
    await root.updateComplete;
    if (globalThis.__voipStackPopup) return globalThis.__voipStackPopup;
    if (!root.shadowRoot || typeof root.provideHass !== "function") throw new Error("The HA frontend context provider is unavailable.");
    const host = document.createElement("voip-stack-popup"); host._root = root;
    globalThis.__voipStackPopup = host; root.shadowRoot.append(host); root.provideHass(host);
    return host;
  };
  const root = document.querySelector("home-assistant");
  if (root) return attach(root);
  return new Promise((resolve, reject) => {
    const observer = new MutationObserver(() => {
      const root = document.querySelector("home-assistant");
      if (root) { observer.disconnect(); attach(root).then(resolve, reject); }
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
  });
}
const presenterReady = bootstrap();
presenterReady.catch(error => console.warn("VoIP popup is unavailable:", error.message));
