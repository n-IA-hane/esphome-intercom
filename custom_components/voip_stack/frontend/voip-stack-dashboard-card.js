/** Operator dashboard: a view and command surface over the shared phone engine. */
const VERSION = (() => { try { return new URL(import.meta.url).searchParams.get("v") || "dev"; } catch (_) { return "dev"; } })();
const { voipStackEngine } = await import(`./voip-stack-engine.js?v=${encodeURIComponent(VERSION)}`);
const { VoipEntityIds } = await import(`./voip-stack-entity-ids.js?v=${encodeURIComponent(VERSION)}`);
const { voipStackTranslate } = await import(`./voip-stack-i18n.js?v=${encodeURIComponent(VERSION)}`);
const { softphoneSnapshotSupersedes } = await import(`./voip-stack-card-model.js?v=${encodeURIComponent(VERSION)}`);
const {
  DashboardPhoneStore, normaliseDashboardConfig, parseDashboardRoster, ownerDeviceInfo,
  dashboardSections, phonePresentation, distinctCalls, dashboardCanCall,
  dashboardPickupCandidate, dashboardColumnCount, dashboardTranslate, dashboardIconFor, dashboardTypeFor,
} = await import(`./voip-stack-dashboard-model.js?v=${encodeURIComponent(VERSION)}`);
const { buildDashboardSkeleton, buildDashboardTile, dashboardElement, dashboardIcon } = await import(`./voip-stack-dashboard-view.js?v=${encodeURIComponent(VERSION)}`);
await import(`./voip-stack-dashboard-card-editor.js?v=${encodeURIComponent(VERSION)}`);

class VoipStackDashboardCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._store = new DashboardPhoneStore();
    this._entityIds = new VoipEntityIds();
    this._devices = [];
    this._devicesRevision = 0;
    this._roster = [];
    this._rosterRaw = null;
    this._tiles = new Map();
    this._tileData = new Map();
    this._epoch = 0;
    this._pending = "";
    this._error = "";
    this._ownerSnapshot = null;
    this._ownerSelector = "";
    this._syncKey = "";
    this._syncPending = false;
    this._phonePresentation = {video:false,settings:false};
    this._videoFocusCallId = "";
    this._engineListener = () => {
      this._syncOwner(); this._render();
      const owner = this._owner();
      if (owner?.softphone && voipStackEngine.videoVisible && voipStackEngine.endpointId === owner.endpoint_id &&
          this._videoFocusCallId !== voipStackEngine.callId) {
        this._videoFocusCallId = voipStackEngine.callId;
        const otherDashboard = voipStackEngine.videoCanvasOwner?.hasAttribute?.("data-dashboard-phone") &&
          voipStackEngine.videoCanvasOwner !== this._phoneView;
        if (!otherDashboard) this._phoneView?.focusPhoneView?.();
      }
    };
    this._resizeObserver = new ResizeObserver(() => this._measure());
  }

  _t(text) { return dashboardTranslate(this._hass?.language, text, (value) => voipStackTranslate(this._hass, value)); }

  setConfig(config) {
    const value = normaliseDashboardConfig(config);
    if (value.device_id !== this._config.device_id) {
      this._epoch++;
      this._releaseOwner();
      this._ownerSnapshot = null;
      this._phonePresentation = {video:false,settings:false};
      this._videoFocusCallId = "";
      this._pending = "";
      this._error = "";
      this._els?.dialog.close();
    }
    this._config = value;
    this._configurePhoneView();
    this._renderKey = "";
    if (this.isConnected) this._bindOwner();
    this._render();
  }

  set hass(hass) {
    const languageChanged = this._hass?.language !== hass?.language;
    this._hass = hass;
    if (this._phoneView) this._phoneView.hass = hass;
    if (languageChanged && this._els) {
      this._resizeObserver.disconnect(); this._els = null; this._tiles.clear(); this._renderKey = "";
    }
    this._entityIds.update(hass, () => { this._readRoster(); this._render(); });
    if (this.isConnected) this._bind();
    this._readRoster();
    this._render();
  }

  connectedCallback() {
    this._epoch++;
    voipStackEngine.addEventListener("state", this._engineListener);
    if (this._els) {
      this._resizeObserver.observe(this._els.card);
      this._measure();
    }
    this._bind(); this._render();
  }

  disconnectedCallback() {
    this._epoch++;
    this._resizeObserver.disconnect();
    this._unsubPhones?.(); this._unsubPhones = null;
    this._releaseOwner();
    voipStackEngine.removeEventListener("state", this._engineListener);
    this._els?.dialog.close();
    // The shared page-level engine keeps an owned call and its media alive.
  }

  _bind() {
    const connection = this._hass?.connection;
    if (!connection) return;
    if (connection !== this._connection) {
      this._epoch++;
      this._connection = connection;
      this._store.reset(); this._devices = []; this._devicesLoaded = false;
      this._unsubPhones?.(); this._unsubPhones = null;
      this._releaseOwner(); this._ownerSnapshot = null;
    }
    voipStackEngine.configure(this._hass);
    if (!this._unsubPhones) {
      this._unsubPhones = voipStackEngine.subscribePhoneStates((event) => {
        if (!this.isConnected) return;
        if (this._store.apply(event)) { this._bindOwner(); this._render(); }
      });
    }
    if (!this._devicesLoaded && this._deviceRequestConnection !== connection) this._loadDevices(connection);
    this._bindOwner();
  }

  async _loadDevices(connection) {
    this._deviceRequestConnection = connection;
    try {
      const result = await connection.sendMessagePromise({ type: "voip_stack/list_devices" });
      if (this._hass?.connection !== connection) return;
      this._devices = result?.devices || [];
      this._devicesLoaded = true; this._devicesRevision++;
      this._configurePhoneView(); this._bindOwner(); this._render();
    } catch (error) {
      if (this._hass?.connection === connection) { this._error = error?.message || String(error); this._render(); }
    }
  }

  _readRoster() {
    const raw = this._hass?.states?.[this._entityIds.phonebook]?.attributes?.roster_json;
    if (raw === this._rosterRaw) return;
    this._rosterRaw = raw; this._roster = parseDashboardRoster(raw);
  }

  _owner() { return ownerDeviceInfo(this._devices, this._store.phones, this._config.device_id); }

  _bindOwner() {
    const owner = this._owner();
    const selector = owner?.softphone ? owner.device_id : "";
    if (selector === this._ownerSelector) return;
    this._releaseOwner();
    if (!selector || !this.isConnected) return;
    this._ownerSelector = selector;
    const epoch = this._epoch;
    this._ownerPushEpoch = 0;
    const deliver = (state) => {
      if (!this.isConnected || epoch !== this._epoch || this._ownerSelector !== selector) return;
      if (state?.device_id && state.device_id !== selector) return;
      if (owner.endpoint_id && state?.endpoint_id && owner.endpoint_id !== state.endpoint_id) return;
      if (this._ownerSnapshot && !softphoneSnapshotSupersedes(this._ownerSnapshot, state)) return;
      this._ownerSnapshot = state;
      this._syncOwner(); this._render();
    };
    this._unsubOwner = voipStackEngine.subscribeSoftphoneState((state) => {
      this._ownerPushEpoch++;
      deliver(state);
    }, { device_id: selector });
    const readEpoch = this._ownerPushEpoch;
    this._hass.callWS({ type: "voip_stack/ha_softphone_state", device_id: selector,
      media_client_id: voipStackEngine.mediaClientId }).then((state) => {
      if (this._ownerPushEpoch === readEpoch) deliver(state);
    }).catch((error) => {
      if (epoch === this._epoch && this._ownerSelector === selector) {
        this._error = error?.message || String(error); this._render();
      }
    });
  }

  _releaseOwner() {
    this._unsubOwner?.(); this._unsubOwner = null; this._ownerSelector = ""; this._syncKey = "";
  }

  _syncOwner() {
    const owner = this._owner(); const state = this._ownerSnapshot;
    if (!this.isConnected || !owner?.softphone || !state || this._syncPending) return;
    const key = JSON.stringify([state, voipStackEngine.active, voipStackEngine.callId, voipStackEngine.endpointId]);
    if (key === this._syncKey) return;
    this._syncKey = key; this._syncPending = true;
    const epoch = this._epoch;
    Promise.resolve().then(() => voipStackEngine.dashboardSyncOwner(owner, state)).catch((error) => {
      if (epoch === this._epoch) { this._syncKey = ""; this._error = error?.message || String(error); this._render(); }
    }).finally(() => { this._syncPending = false; });
  }

  _measure() {
    if (!this._els) return;
    const width = this._els.card.clientWidth;
    this._els.card.style.setProperty("--dash-columns", dashboardColumnCount(width));
    this.classList.toggle("compact", width < 900);
    this.classList.toggle("narrow", width < 520);
  }

  _render() {
    if (!this.shadowRoot) return;
    if (!this._els) {
      this._els = buildDashboardSkeleton(this.shadowRoot, (text) => this._t(text), {
        command: (action) => this._ownerCommand(action), options: () => this._openOptions(), ownerDetails: () => this._openDetails(this._ownerTileKey()),
      });
      this._resizeObserver.observe(this._els.card);
      this._ensurePhoneView();
      this._measure();
    }
    const key = JSON.stringify([this._store.revision, this._store.ready, this._store.error, this._rosterRaw,
      this._devicesRevision, this._config, this._ownerSnapshot, this._pending, this._error,
      voipStackEngine.active, voipStackEngine.callId, voipStackEngine.endpointId]);
    if (key === this._renderKey) return;
    this._renderKey = key;
    const e = this._els; const owner = this._owner();
    const customTitle = this._config.title?.trim();
    e.title.textContent = customTitle || owner?.name || "";
    e.ownerName.textContent = owner
      ? customTitle ? [owner.name, owner.extension].filter(Boolean).join(" · ") : owner.extension || ""
      : this._t("Select the calling phone in the card editor.");
    e.ownerName.hidden = !e.ownerName.textContent;
    const notice = this._error || this._store.error || (!this._store.ready ? this._t("Waiting for phone state") : "");
    e.notice.textContent = notice; e.notice.hidden = !notice;
    e.notice.classList.toggle("error", !!(this._error || this._store.error));
    const sections = dashboardSections(this._store.phones, this._roster, this._config.device_id);
    this._ensurePhoneView();
    this._renderPhoneSurface();
    this._renderHeader(owner ? { ...owner, icon: sections.owner?.icon, metadata: sections.owner?.metadata } : null);
    this._tileData.clear();
    // Header details need the owner metadata, independently of visible tiles.
    if (sections.owner) this._tileData.set(sections.owner.tile_key, sections.owner);
    const retained = new Set();
    for (const sectionName of ["internals", "contacts"]) {
      const values = sections[sectionName]; const section = e.sections[sectionName];
      section.heading.textContent = `${this._t(section.label)} (${values.length})`; section.empty.hidden = !!values.length;
      let previous = null;
      for (const value of values) {
        const tileKey = value.tile_key; retained.add(tileKey); this._tileData.set(tileKey, value);
        let tile = this._tiles.get(tileKey);
        if (!tile) {
          tile = buildDashboardTile(tileKey, { select: (id) => this._selectTile(id), pickup: (id) => this._pickup(id) });
          this._tiles.set(tileKey, tile);
        }
        const presentation = JSON.stringify([value, owner?.device_id, owner?.state,
          owner?.availability, this._store.ready, this._pending, this._hass?.language]);
        if (tile.presentation !== presentation) {
          this._renderTile(tile, value, owner);
          tile.presentation = presentation;
        }
        const expected = previous ? previous.nextSibling : section.grid.firstChild;
        if (tile.root !== expected) section.grid.insertBefore(tile.root, expected);
        previous = tile.root;
      }
    }
    for (const [id, tile] of this._tiles) if (!retained.has(id)) { tile.root.remove(); this._tiles.delete(id); }
    if (e.dialog.open) this._renderDetails();
  }

  _configurePhoneView() {
    if (!this._phoneView) return;
    const owner = this._owner();
    const config = {...this._config, type:"custom:voip-stack-card", mode:owner?.softphone ? "ha_softphone" : "esp_mirror"};
    const key = JSON.stringify(config);
    if (key !== this._phoneConfigKey) { this._phoneConfigKey = key; this._phoneView.setConfig(config); }
    if (this._hass) this._phoneView.hass = this._hass;
  }

  _ensurePhoneView() {
    if (!this.isConnected || !this._els) return;
    if (this._phoneView) {
      if (this._phoneView.parentElement !== this._els.phoneHost) this._els.phoneHost.append(this._phoneView);
      this._configurePhoneView();
      return;
    }
    if (this._phoneViewPromise) return;
    this._phoneViewPromise = customElements.whenDefined("voip-stack-card").then(() => {
      if (!this.isConnected || !this._els) return;
      const phone = document.createElement("voip-stack-card");
      phone.setAttribute("data-dashboard-phone", "");
      this._phoneView = phone;
      phone.setDashboardMode?.(true);
      phone.addEventListener("voip-presentation-changed", event => {
        this._phonePresentation = event.detail;
        this._renderPhoneSurface();
      });
      this._configurePhoneView();
      this._els.phoneHost.append(phone);
      this._renderPhoneSurface();
    }).finally(() => { this._phoneViewPromise = null; });
  }

  _renderPhoneSurface() {
    if (!this._els) return;
    const {video,settings} = this._owner() ? this._phonePresentation : {video:false,settings:false};
    this._els.phoneHost.hidden = !(video || settings);
    this._els.scroller.hidden = !!(video || settings);
    this._els.card.classList.toggle("video-mode", !!video);
    this._els.options.disabled = !this._phoneView;
    this._els.options.setAttribute("aria-expanded", String(!!settings));
  }

  _openOptions() {
    this._phoneView?.openPhoneOptions?.();
  }

  _renderHeader(owner) {
    const e = this._els; const value = owner ? phonePresentation(owner) : null; const call = value?.primary;
    e.ownerIcon.setAttribute("icon", owner ? dashboardIconFor(owner) : "mdi:phone");
    e.ownerCall.hidden = !call;
    if (!call) return;
    const incoming = call.direction === "incoming" && call.state === "early";
    e.ownerCall.className = `owner-call ${incoming ? "ringing" : "busy"}`;
    e.callIcon.setAttribute("icon", incoming ? "mdi:phone-incoming" : call.held ? "mdi:phone-paused" : "mdi:phone-in-talk");
    e.callLabel.textContent = this._t(incoming ? "Incoming call" : call.held ? "On hold" : value.label);
    e.callPeer.textContent = call.peer_name || owner.name || "";
    const peer = this._store.phones.get(call.peer_endpoint_id);
    const caller = this._ownerSnapshot?.caller || "";
    e.callNumber.textContent = peer?.extension || (/^[+\d*# ()-]+$/.test(caller) ? caller : "");
    e.callNumber.hidden = !e.callNumber.textContent;
    const offeredVideo = !!owner?.softphone && !!this._ownerSnapshot?.video_offered;
    e.answerVideo.hidden = !incoming || !offeredVideo;
    e.answer.querySelector("span").textContent = this._t(offeredVideo ? "Answer audio only" : "Answer");
    e.answer.hidden = !incoming; e.decline.hidden = !incoming; e.hangup.hidden = incoming;
    e.moreCalls.hidden = value.calls.length < 2;
    for (const button of [e.answerVideo, e.answer, e.decline, e.hangup, e.moreCalls]) button.disabled = !!this._pending;
  }

  _renderTile(tile, phone, owner) {
    const state = phonePresentation(phone);
    tile.root.className = `tile ${state.visual}${phone.owner ? " owner" : ""}${phone.kind === "contact" ? " contact" : ""}${phone.dnd ? " dnd" : ""}${this._store.ready ? "" : " stale"}`;
    tile.name.textContent = phone.name || ""; tile.extension.textContent = phone.extension || phone.number || "";
    tile.extension.hidden = !tile.extension.textContent;
    const typeLabel = this._t(dashboardTypeFor(phone));
    tile.icon.setAttribute("icon", dashboardIconFor(phone));
    const offline = ["offline", "unavailable"].includes(phone.availability);
    tile.availability.hidden = phone.kind === "contact" || (!offline && phone.availability !== "available");
    tile.availability.className = `availability-icon${offline ? " offline" : ""}`;
    tile.availability.setAttribute("icon", offline ? "mdi:close-circle-outline" : "mdi:check-circle");
    tile.availability.title = this._t(offline ? "Offline" : "Online");
    tile.owner.textContent = this._t("Your phone"); tile.owner.hidden = !phone.owner;
    tile.mail.hidden = !phone.mailbox?.waiting; tile.mailCount.textContent = phone.mailbox?.new ? String(phone.mailbox.new) : "";
    tile.mail.setAttribute("aria-label", `${this._t("Voicemail")}: ${phone.mailbox?.new || 0}`);
    tile.callCount.textContent = String(state.calls.length); tile.callCount.hidden = state.calls.length < 2;
    tile.ringingBell.hidden = state.visual !== "ringing" || !!phone.owner;
    tile.status.hidden = phone.kind === "contact";
    tile.statusCopy.textContent = `${this._t(state.label)}${state.peer && state.visual !== "idle" && state.visual !== "offline" ? `${state.label === "In call with" ? " " : ": "}${state.peer}` : ""}`;
    tile.dot.hidden = state.visual === "ringing" || state.visual === "busy";
    tile.held.hidden = !state.held.length && !phone.dnd;
    tile.held.querySelector("ha-icon").setAttribute("icon", state.held.length ? "mdi:pause-circle" : "mdi:minus-circle");
    tile.heldCopy.textContent = state.held.length ? state.held.map((call) => `${call.peer_name || ""} ${this._t("On hold").toLowerCase()}`.trim()).join(", ") : "DND";
    tile.attended.hidden = !state.attended; tile.attendedCopy.textContent = this._t("Attended transfer");
    const candidate = dashboardPickupCandidate(owner, phone, this._store.ready && !this._pending);
    const eligible = state.calls.filter((call) => call.pickup_available);
    tile.actions.hidden = phone.owner || phone.state !== "ringing" || !eligible.length;
    tile.pickup.disabled = !!this._pending || !this._store.ready || owner?.state !== "idle" || !owner;
    tile.pickupLabel.textContent = this._t(eligible.length > 1 ? "Choose call" : "Pick up");
    tile.pickup.setAttribute("aria-label", `${tile.pickupLabel.textContent}: ${phone.name}`);
    const canCall = dashboardCanCall(owner, phone, this._store.ready && !this._pending);
    const description = [phone.owner ? this._t("Your phone") : "", phone.mailbox?.waiting ? `${this._t("Voicemail")}: ${phone.mailbox.new || 0}` : ""].filter(Boolean).join(". ");
    tile.main.setAttribute("aria-label", `${phone.name || ""}${phone.extension ? `, ${phone.extension}` : ""}. ${typeLabel}. ${tile.statusCopy.textContent || this._t("Unknown")}.${description ? ` ${description}.` : ""} ${this._t(canCall ? "Call" : "Call details")}`);
    tile.main.title = `${typeLabel}, ${this._t(canCall ? "Call" : "Call details")}`;
    tile.main.disabled = !!this._pending;
    tile.pickup.dataset.callId = candidate?.id || "";
  }

  _ownerTileKey() { const owner = this._owner(); return owner ? `endpoint:${owner.endpoint_id}` : ""; }

  _selectTile(key) {
    const phone = this._tileData.get(key);
    if (dashboardCanCall(this._owner(), phone, this._store.ready && !this._pending)) this._runAction("call", phone);
    else this._openDetails(key);
  }

  _pickup(key, explicitCall = null) {
    const phone = this._tileData.get(key); const owner = this._owner();
    const candidate = explicitCall || dashboardPickupCandidate(owner, phone, this._store.ready && !this._pending);
    if (!candidate) { this._openDetails(key); return; }
    this._runAction("pickup", phone, candidate);
  }

  _ownerCommand(action, explicitCall = null) {
    const owner = this._owner(); const call = explicitCall || phonePresentation(owner).primary;
    if (!call || !owner) return;
    this._runAction(action, owner, call);
  }

  _callSnapshot(owner, call) {
    const sameCall = this._ownerSnapshot?.call_id === call.id;
    return { ...(sameCall ? this._ownerSnapshot : {}), endpoint_id: owner.endpoint_id, device_id: owner.device_id,
      call_id: call.id, generation: call.generation, state: call.state === "early" ? "ringing" : owner.state,
      peer_name: call.peer_name, direction: call.direction };
  }

  async _runAction(action, target, call = null) {
    if (this._pending) return;
    const owner = this._owner();
    if (!owner) return;
    if (action === "call" && !dashboardCanCall(owner, target, this._store.ready)) return;
    if (action === "pickup" && (!this._store.ready || owner.state !== "idle" || !call?.pickup_available)) return;
    const epoch = this._epoch;
    this._pending = action; this._error = ""; this._render();
    try {
      this._phoneView?.focusPhoneView?.();
      if (action === "call") await voipStackEngine.dashboardStartCall(owner, target);
      else if (action === "pickup") await voipStackEngine.dashboardPickup(owner, target, call);
      else if (action === "answer" || action === "answer_video") {
        if (owner.softphone && this._phoneView?.answerPhoneCall) {
          await this._phoneView.answerPhoneCall({video:action === "answer_video"});
        } else {
          await voipStackEngine.dashboardAnswer(owner, this._callSnapshot(owner, call));
        }
      }
      else await voipStackEngine.dashboardTerminate(action, owner, this._callSnapshot(owner, call));
    } catch (error) {
      if (epoch === this._epoch) this._error = error?.message || String(error);
    } finally {
      if (epoch === this._epoch) {
        this._pending = ""; this._syncKey = "";
        this._syncOwner(); this._render();
      }
    }
  }

  _openDetails(key) {
    if (!this._tileData.has(key)) return;
    this._detailKey = key; this._renderDetails();
    if (!this._els.dialog.open) this._els.dialog.showModal();
  }

  _renderDetails() {
    const phone = this._tileData.get(this._detailKey); const e = this._els;
    e.detailsTitle.textContent = phone?.name || this._t("Call details");
    e.detailsSummary.textContent = [phone?.extension || phone?.number, this._t(phonePresentation(phone).label)].filter(Boolean).join(" · ");
    e.detailsRows.replaceChildren();
    const owner = this._owner(); const calls = distinctCalls(phone);
    if (!calls.length) {
      e.detailsRows.append(dashboardElement("p", "empty", this._t(phonePresentation(phone).label || "Unknown")));
      return;
    }
    for (const call of calls) {
      const row = dashboardElement("div", "details-row"); row.append(dashboardElement("div", "details-peer", call.peer_name || ""));
      row.append(dashboardElement("div", "details-state", this._t(call.held ? "On hold" : call.state === "confirmed" ? "Connected" : call.direction === "incoming" && call.state === "early" ? "Incoming call" : "Calling")));
      const actions = dashboardElement("div", "details-actions");
      const addAction = (label, icon, handler) => {
        const button = dashboardElement("button", "action"); button.type = "button"; button.disabled = !!this._pending;
        button.append(dashboardIcon(icon), dashboardElement("span", "", this._t(label))); button.onclick = handler; actions.append(button);
      };
      if (phone.owner) {
        if (call.direction === "incoming" && call.state === "early") {
          const offeredVideo = owner?.softphone && this._ownerSnapshot?.call_id === call.id && this._ownerSnapshot?.video_offered;
          if (offeredVideo) addAction("Answer with video", "mdi:video", () => this._ownerCommand("answer_video", call));
          addAction(offeredVideo ? "Answer audio only" : "Answer", "mdi:phone", () => this._ownerCommand("answer", call));
          addAction("Decline", "mdi:phone-hangup", () => this._ownerCommand("decline", call));
        } else addAction("Hang up", "mdi:phone-hangup", () => this._ownerCommand("hangup", call));
      } else if (call.pickup_available && owner?.state === "idle" && this._store.ready) {
        addAction("Pick up", "mdi:phone", () => this._pickup(this._detailKey, call));
      }
      row.append(actions); e.detailsRows.append(row);
    }
  }

  getGridOptions() { return { columns: 12, rows: 8, min_columns: 4, min_rows: 4 }; }
  getCardSize() { return 8; }
  static getConfigElement() { return document.createElement("voip-stack-dashboard-card-editor"); }
  static getStubConfig() { return { device_id: "" }; }
}

if (!customElements.get("voip-stack-dashboard-card")) customElements.define("voip-stack-dashboard-card", VoipStackDashboardCard);
window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "voip-stack-dashboard-card")) {
  window.customCards.push({ type: "voip-stack-dashboard-card", name: "VoIP Dashboard", description: "Extension status, contacts and call controls for one phone", preview: true });
}
