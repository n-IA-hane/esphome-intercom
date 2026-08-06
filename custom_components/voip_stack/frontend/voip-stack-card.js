/**
 * VoIP Stack Card v2.0.0
 *
 * ESP cards mirror the ESPHome phone entities and send only button/contact
 * commands. HA softphone cards mirror backend-pushed SIP session state and own
 * the browser/app audio websocket for that HA call.
 *
 * Public SIP states -> Card UI:
 * - Idle       -> Show destination + Call button
 * - Calling    -> Show "Calling [dest]..." + Hangup
 * - Ringing    -> Show "Incoming [caller]" + Answer/Decline
 * - In Call  -> Show "In Call [peer]" + Hangup
 */

const VOIP_STACK_MODULE_VERSION = (() => {
  try {
    const raw = new URL(import.meta.url).searchParams.get("v") || "";
    return raw || "dev";
  } catch (_) {
    return "dev";
  }
})();
const VOIP_STACK_CARD_VERSION = VOIP_STACK_MODULE_VERSION.replace(/-\d+$/, "") || "dev";
await import(`./voip-phonebook-card.js?v=${encodeURIComponent(VOIP_STACK_MODULE_VERSION)}`);
const { voipStackEngine } = await import(`./voip-stack-engine.js?v=${encodeURIComponent(VOIP_STACK_MODULE_VERSION)}`);
const {
  audioModeLabel,
  formatListFromMetadata,
  formatCallDuration,
  formatEndReason,
  formatKnownReason,
  formatVideoFailureReason,
  normaliseAudioMode,
  normaliseCardConfig,
  normaliseTransport,
  reasonKey,
  softphoneSnapshotSupersedes,
  targetFromRosterEntry,
  terminalPeerLabel,
} = await import(`./voip-stack-card-model.js?v=${encodeURIComponent(VOIP_STACK_MODULE_VERSION)}`);
const {
  buildMainCardSkeleton,
  buildUnconfiguredCardSkeleton,
} = await import(`./voip-stack-card-view.js?v=${encodeURIComponent(VOIP_STACK_MODULE_VERSION)}`);
await import(`./voip-stack-card-editor.js?v=${encodeURIComponent(VOIP_STACK_MODULE_VERSION)}`);
const HANGUP_SERVICE_TIMEOUT_MS = 3000;

function settleServiceWithin(promise, timeoutMs, timeoutMessage) {
  let timer;
  const schedule = globalThis.setTimeout || globalThis.window?.setTimeout;
  const cancel = globalThis.clearTimeout || globalThis.window?.clearTimeout;
  if (!schedule) return Promise.resolve(promise);
  return Promise.race([
    Promise.resolve(promise),
    new Promise((_, reject) => {
      timer = schedule(
        () => reject(new Error(timeoutMessage)),
        timeoutMs,
      );
    }),
  ]).finally(() => {
    if (timer && cancel) cancel(timer);
  });
}


// Lazy gate for verbose logs. Errors and warnings always emit.
// Enable in the browser console with localStorage.voip_debug = "1".
const _ic_dbg = (() => {
  try { return localStorage.getItem("voip_debug") === "1"; }
  catch (_) { return false; }
})();
const _voip_log = {
  error: console.error.bind(console),
  warn: console.warn.bind(console),
  info: _ic_dbg ? console.info.bind(console) : () => {},
  debug: _ic_dbg ? console.debug.bind(console) : () => {},
};

class VoipStackCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });

    // UI transition states only
    this._starting = false;
    this._stopping = false;
    this._callOperationId = 0;
    this._softphoneSnapshot = null;
    this._activeSessionDeviceId = null;
    this._softphoneDnd = false;
    this._softphoneExtension = "";
    this._softphoneGroups = { ring_group: "", conference_group: "", conference_ring: false };
    this._softphoneTargetDeviceId = null;
    this._softphoneKeypadOpen = false;
    this._softphoneManualTarget = "";
    this._mirrorKeypadOpen = false;
    this._mirrorManualTarget = "";
    this._lastKnownMirrorDestination = "";
    this._mirroredConnectedPeer = "";
    this._softphoneStateLoaded = false;
    this._softphoneStateLoading = false;
    this._softphoneStateEpoch = 0;
    this._lifecycleGeneration = 0;

    this._cleanupTask = null;
    this._audioAttachTask = null;
    this._nativeCameraCard = null;
    this._nativeCameraEntityId = "";
    this._nativeCameraMountTask = null;
    this._nativeCameraMountGeneration = 0;

    // Device info
    this._activeDeviceInfo = null;
    this._resolvedDeviceId = null;
    this._deviceBindingsLoading = false;
    this._deviceBindingsRetryTimer = null;
    this._availableDevices = [];
    this._availableDevicesLoading = false;
    this._availableDevicesRetryTimer = null;
    this._rosterEntries = [];
    this._rosterSourceKey = null;
    this._softphoneTargetOptionsKey = null;

    // Entity IDs (discovered once)
    this._voipStateEntityId = null;
    this._transportEntityId = null;
    this._callerEntityId = null;
    this._destinationEntityId = null;
    this._lastReasonEntityId = null;
    this._previousButtonEntityId = null;
    this._nextButtonEntityId = null;
    this._callButtonEntityId = null;
    this._declineButtonEntityId = null;
    this._autoAnswerSwitchEntityId = null;
    this._dndSwitchEntityId = null;
    this._ringGroupsTextEntityId = null;
    this._conferenceGroupsTextEntityId = null;
    this._extensionTextEntityId = null;
    this._conferenceRingSwitchEntityId = null;
    this._startCallService = "";

    // Persistent error message (survives _render() DOM rebuild)
    this._errorMsg = "";

    // Auto-answer
    this._autoAnswer = false;
    this._autoAnswering = false;  // Prevents re-entry during auto-answer
    this._autoAnswerCallId = "";
    this._autoAnswerMicReady = false;
    this._autoAnswerPermissionPending = false;
    this._autoAnswerPermissionGeneration = 0;
    this._ringtoneEnabled = false;
    this._micAntiAliasEnabled = true;
    this._settingsOpen = false;
    this._ringtoneRequestKey = `voip-stack-card-${Math.random().toString(36).slice(2)}`;
    this._deepLinkAnswerConsumed = false;

    // ESP mirror cards keep a short local display copy of the ESP terminal
    // reason text sensor. HA softphone cards render terminal data directly
    // from the backend snapshot pushed on the event bus.
    this._lastEndInfo = null;          // {peer, reason, until_ms} | null
    this._lastSoftphoneTerminalKey = "";
    this._lastEndClearTimer = null;
    this._videoDurationTimer = null;
    this._unsubCallEvents = null;
    this._unsubSoftphoneState = null;

    // Static skeleton: built once per mode, then mutated via textContent/
    // hidden/className. Eliminates innerHTML interpolation of untrusted
    // strings (peer, destination, caller, decline reason).
    this._els = null;
    this._skeletonMode = null;  // 'main' | 'unconfigured' | null
    this._engineListener = () => {
      if (this._isSoftphoneController()) {
        const snapshot = this._softphoneSnapshot || {};
        this._ensureHaSoftphoneAudioPath(snapshot);
        this._maybeAutoAnswer(snapshot);
      }
      this._render();
    };
    this._engineErrorListener = (event) => {
      if (!this._isHaSoftphoneMode() || !this._isSoftphoneController()) return;
      const endpointId = this._getSoftphoneEndpointId();
      if (voipStackEngine.endpointId && voipStackEngine.endpointId !== endpointId) return;
      const detail = event?.detail;
      this._showError(
        typeof detail === "string"
          ? detail
          : detail?.message || String(detail || "Phone media error"),
      );
      this._render();
    };
    this._resizeObserver = new ResizeObserver(() => this._measureLayout());
  }

  connectedCallback() {
    this._lifecycleGeneration++;
    voipStackEngine.addEventListener("state", this._engineListener);
    voipStackEngine.addEventListener("error", this._engineErrorListener);
    voipStackEngine.addEventListener("video-error", this._engineErrorListener);
    this._observeLayout();
    if (this._hass) {
      this._subscribeBusEvents();
      if (this._isHaSoftphoneMode() && !this._softphoneStateLoaded) {
        this._loadSoftphoneState();
      }
    }
    this._render();
  }

  disconnectedCallback() {
    this._lifecycleGeneration++;
    this._resizeObserver.disconnect();
    if (this._unsubCallEvents) {
      this._unsubCallEvents();
      this._unsubCallEvents = null;
    }
    if (this._unsubSoftphoneState) {
      this._unsubSoftphoneState();
      this._unsubSoftphoneState = null;
    }
    if (this._lastEndClearTimer) {
      clearTimeout(this._lastEndClearTimer);
      this._lastEndClearTimer = null;
    }
    if (this._videoDurationTimer) {
      clearInterval(this._videoDurationTimer);
      this._videoDurationTimer = null;
    }
    if (this._availableDevicesRetryTimer) {
      clearTimeout(this._availableDevicesRetryTimer);
      this._availableDevicesRetryTimer = null;
    }
    if (this._deviceBindingsRetryTimer) {
      clearTimeout(this._deviceBindingsRetryTimer);
      this._deviceBindingsRetryTimer = null;
    }
    if (this._devicesRetryTimer) {
      clearTimeout(this._devicesRetryTimer);
      this._devicesRetryTimer = null;
    }
    voipStackEngine.removeEventListener("state", this._engineListener);
    voipStackEngine.removeEventListener("error", this._engineErrorListener);
    voipStackEngine.removeEventListener("video-error", this._engineErrorListener);
    voipStackEngine.clearRingtoneRequest(this._ringtoneRequestKey);
    voipStackEngine.releaseVideoCanvas(this);
    voipStackEngine.releaseSoftphoneController(this, this._softphoneRuntimeKey());
    this._clearNativeCameraCard();
  }

  async _subscribeBusEvents() {
    if (this._isHaSoftphoneMode()) {
      if (!this._unsubSoftphoneState) {
        this._unsubSoftphoneState = voipStackEngine.subscribeSoftphoneState(
          (state) => this._onSoftphoneState(state),
          this._softphoneSelector(),
        );
      }
      return;
    }
    if (!this._unsubCallEvents) {
      this._unsubCallEvents = voipStackEngine.subscribeCallEvents((e) => this._onCallEvent(e));
    }
  }

  _eventConcernsThisCard(payload) {
    const myId = this._activeDeviceInfo?.device_id || this._getConfigDeviceId();
    if (!myId || !payload) return false;
    if (this._isHaSoftphoneMode()) return true;
    const nameMatches = (value) => this._samePeerName(value, this._cardPeerName());
    if (payload.local_name || payload.peer_name || payload.caller || payload.callee) {
      if (nameMatches(payload.local_name) ||
          nameMatches(payload.peer_name) ||
          nameMatches(payload.caller) ||
          nameMatches(payload.callee)) {
        return true;
      }
    }
    return payload.source_device_id === myId
        || payload.dest_device_id === myId
        || payload.session_device_id === myId
        || payload.device_id === myId;
  }

  _normalPeerName(value) {
    return String(value || "")
      .normalize("NFKC")
      .trim()
      .toLocaleLowerCase()
      .replace(/[^\p{L}\p{N}]+/gu, "");
  }

  _samePeerName(a, b) {
    const aa = this._normalPeerName(a);
    const bb = this._normalPeerName(b);
    return !!aa && !!bb && aa === bb;
  }

  _cardPeerName() {
    if (this._isHaSoftphoneMode()) return this._getHaName();
    return this._activeDeviceInfo?.name || this.config?.name || "";
  }

  _onCallEvent(event) {
    const scope = (event?.data?.scope || "").toLowerCase();
    if (!this._isHaSoftphoneMode() && scope === "sip_bridge") {
      this._onMirroredBridgeStateEvent(event);
    }
  }

  _onSoftphoneState(state) {
    if (!this._isHaSoftphoneMode() || !state) return;
    if (!this._softphoneSnapshotMatches(state)) return;
    this._softphoneStateEpoch++;
    if (!this._applySoftphoneSnapshot(state)) return;
    this._ensureHaSoftphoneAudioPath(state);
    this._render();
  }

  _onMirroredBridgeStateEvent(event) {
    const data = event?.data || {};
    if (!this._eventConcernsThisCard(data)) return;
    const state = String(data.state || data.sip_state || "").toLowerCase();
    if (state === "in_call" || state === "answering") {
      this._mirroredConnectedPeer = terminalPeerLabel(data).trim();
      this._render();
      return;
    }
    if (["calling", "remote_ringing", "ringing", "incoming", "connecting"].includes(state)) {
      this._mirroredConnectedPeer = "";
      return;
    }
    if (!["idle", "busy", "declined", "cancelled", "media_incompatible", "transport_unreachable", "auth_required_unsupported", "error"].includes(state)) return;
    this._mirroredConnectedPeer = "";
    const reason = data.terminal_reason || data.reason || state;
    const peer = terminalPeerLabel(data);
    this._captureEndReason("terminal", reason, data.actor || "remote", peer);
    this._render();
  }

  _hasBrowserAudioPath() {
    const id = this._sessionDeviceId();
    const callId = this._sessionCallId();
    const endpointId = this._getSoftphoneEndpointId();
    return voipStackEngine.active &&
      (!endpointId || voipStackEngine.endpointId === endpointId) &&
      (!id || voipStackEngine.deviceId === id) &&
      (!callId || voipStackEngine.callId === callId);
  }

  _ownsSoftphoneMedia(snapshot = this._softphoneSnapshot || {}) {
    if (!this._isHaSoftphoneMode()) return false;
    const callId = String(snapshot.call_id || this._sessionCallId() || "");
    return voipStackEngine.ownsSoftphoneSession(callId, this._getSoftphoneEndpointId());
  }

  _softphoneSupportsVideo(snapshot = this._softphoneSnapshot || {}) {
    return Array.isArray(snapshot?.capabilities) &&
      snapshot.capabilities.some((item) => String(item).toLowerCase() === "video");
  }

  _otherPhoneOwnsBrowserMedia() {
    if (!this._isHaSoftphoneMode()) return false;
    const endpointId = this._getSoftphoneEndpointId();
    if (typeof voipStackEngine.hasOwnedSoftphoneSessionForOtherEndpoint === "function") {
      return voipStackEngine.hasOwnedSoftphoneSessionForOtherEndpoint(endpointId);
    }
    return Boolean(
      voipStackEngine.active && endpointId && voipStackEngine.endpointId !== endpointId,
    );
  }

  _isSoftphoneController() {
    return this.isConnected && this._isHaSoftphoneMode() &&
      voipStackEngine.claimSoftphoneController(this, this._softphoneRuntimeKey());
  }

  _maybeAutoAnswer(snapshot = {}) {
    if (
      !this._isSoftphoneController() ||
      snapshot.state !== "ringing" ||
      snapshot.direction !== "incoming" ||
      !this._autoAnswer ||
      !snapshot.call_id ||
      this._autoAnswerCallId === snapshot.call_id ||
      this._starting ||
      this._otherPhoneOwnsBrowserMedia()
    ) return;
    this._autoAnswering = true;
    this._autoAnswerCallId = snapshot.call_id;
    this._tryAutoAnswer({ callId: snapshot.call_id });
  }

  _markSoftphoneMediaOwner(callId) {
    const endpointId = this._getSoftphoneEndpointId();
    if (callId) voipStackEngine.claimSoftphoneSession(callId, endpointId);
    else voipStackEngine.releaseSoftphoneSession("", endpointId);
  }

  _cleanupAfterTerminalSession(snapshot = {}) {
    if (!this._isSoftphoneController()) return;
    const terminalCallId = String(snapshot.call_id || "");
    const endpointId = this._getSoftphoneEndpointId();
    const ownedCallId = String(voipStackEngine.softphoneCallIdFor(endpointId) || "");
    // A delayed initial-state read from a card that HA is replacing must not
    // tear down a newer call owned by the page-level engine.
    if (ownedCallId && terminalCallId && terminalCallId !== ownedCallId) {
      const activelyAttached = voipStackEngine.active &&
        voipStackEngine.endpointId === endpointId &&
        voipStackEngine.callId === ownedCallId;
      if (activelyAttached) return;
      voipStackEngine.releaseSoftphoneSession("", endpointId);
    }
    if (!this._autoAnswerCallId || this._autoAnswerCallId === terminalCallId) {
      this._autoAnswering = false;
      this._autoAnswerCallId = "";
    }
    this._starting = false;
    this._stopping = false;
    voipStackEngine.releaseSoftphoneSession(terminalCallId, endpointId);
    if (
      !voipStackEngine.active ||
      voipStackEngine.endpointId !== endpointId ||
      (terminalCallId && voipStackEngine.callId !== terminalCallId) ||
      this._cleanupTask
    ) return;
    this._cleanupTask = voipStackEngine.close("terminal")
      .catch((err) => console.warn("voip-stack-card: softphone cleanup failed", err))
      .finally(() => {
        this._cleanupTask = null;
        this._render();
      });
  }

  _normaliseSoftphoneSnapshot(payload = {}) {
    const endpointId = String(
      payload.endpoint_id || this._getSoftphoneEndpointId() || "",
    ).trim();
    const configuredDeviceId = String(this.config?.device_id || "").trim();
    const deviceId = String(
      payload.device_id || payload.endpoint_device_id || configuredDeviceId || "",
    ).trim();
    const state = String(payload.state || payload.sip_state || "idle").toLowerCase();
    const direction = String(payload.direction || "").toLowerCase();
    const peerName = terminalPeerLabel({ ...payload, direction }) || payload.contact || "";
    return {
      ...payload,
      endpoint_id: endpointId,
      device_id: deviceId,
      session_device_id: payload.session_device_id || deviceId,
      state,
      sip_state: String(payload.sip_state || state).toLowerCase(),
      direction,
      caller: payload.caller || "",
      callee: payload.callee || "",
      peer_name: peerName,
      call_id: payload.call_id || "",
      sequence: Number(payload.sequence || 0),
      revision: Number(payload.revision || 0),
      selected_tx_format: payload.selected_tx_format || payload.tx_format || "",
      selected_rx_format: payload.selected_rx_format || payload.rx_format || "",
      audio_mode: payload.audio_mode || "",
      audio_direction: String(payload.audio_direction || "sendrecv").toLowerCase(),
      audio_connection_held: !!payload.audio_connection_held,
      connected_at: Number(payload.connected_at || 0),
      debug_mode: !!payload.debug_mode,
      auto_answer: !!payload.auto_answer,
      send_video: !!payload.send_video,
      video_camera_send_enabled: !!payload.video_camera_send_enabled,
      video_requested: !!payload.video_requested,
      video_negotiated: !!payload.video_negotiated,
      video_status: String(payload.video_status || "inactive").toLowerCase(),
      video_failure_reason: String(payload.video_failure_reason || ""),
      capabilities: Array.isArray(payload.capabilities)
        ? payload.capabilities.map((item) => String(item).toLowerCase())
        : [],
      terminal_reason: payload.terminal_reason || payload.reason || "",
      extension: String(payload.extension || "").trim(),
      groups: payload.groups && typeof payload.groups === "object" ? payload.groups : {},
    };
  }

  _applySoftphoneSnapshot(payload = {}) {
    const snapshot = this._normaliseSoftphoneSnapshot(payload);
    const current = this._softphoneSnapshot;
    if (!softphoneSnapshotSupersedes(current, snapshot)) return false;
    this._softphoneSnapshot = snapshot;
    this._softphoneDnd = !!snapshot.dnd;
    this._autoAnswer = !!snapshot.auto_answer;
    this._softphoneExtension = snapshot.extension;
    this._softphoneGroups = {
      ring_group: String(snapshot.groups?.ring_group || "").trim(),
      conference_group: String(snapshot.groups?.conference_group || "").trim(),
      conference_ring: !!snapshot.groups?.conference_ring,
    };
    this._activeSessionDeviceId = snapshot.session_device_id || snapshot.device_id || "";
    const activePhoneState = ["calling", "remote_ringing", "ringing", "answering", "in_call", "connecting", "terminating"].includes(snapshot.state);
    if (activePhoneState) {
      this._lastSoftphoneTerminalKey = "";
      this._clearEndReason(false);
    } else {
      const terminalReason = String(snapshot.terminal_reason || "").trim();
      const terminalKey = terminalReason
        ? `${snapshot.call_id || "no-call"}|${snapshot.state}|${terminalReason}`
        : "";
      if (terminalKey && terminalKey !== this._lastSoftphoneTerminalKey) {
        this._lastSoftphoneTerminalKey = terminalKey;
        this._captureEndReason(
          snapshot.state,
          terminalReason,
          String(snapshot.origin || "").toLowerCase(),
          terminalPeerLabel(snapshot),
        );
      }
      this._cleanupAfterTerminalSession(snapshot);
    }
    this._maybeAutoAnswer(snapshot);
    if (this._isSoftphoneController()) this._maybeAnswerFromUrl();
    return true;
  }

  _ensureHaSoftphoneAudioPath(snapshot = {}) {
    if (!this._isSoftphoneController()) return;
    if (String(snapshot.state || "").toLowerCase() !== "in_call") return;
    // Hangup deliberately closes the local media socket before the backend
    // snapshot becomes idle. Engine cleanup emits synchronously, so without
    // this gate the still-in-call snapshot can start a second audio attach
    // while the first owner is being released (HTTP 409).
    if (this._stopping || voipStackEngine.mediaCleanupPending) return;
    const endpointId = this._getSoftphoneEndpointId();
    const callId = String(snapshot.call_id || "");
    if (!this._ownsSoftphoneMedia(snapshot) && !voipStackEngine.tryRecoverSoftphoneSession(
      callId,
      endpointId,
    )) return;
    // One browser tab has one microphone/output pipeline. Keep a concurrent
    // call on another logical phone visible, but never let its card oscillate
    // the shared media socket away from the endpoint already attached here.
    if (voipStackEngine.active && endpointId && voipStackEngine.endpointId !== endpointId) return;
    if (this._hasBrowserAudioPath()) {
      void voipStackEngine.reconcileSession(snapshot).catch((err) => {
        console.warn("voip-stack-card: failed to reconcile HA softphone media", err);
      });
      return;
    }
    if (this._starting || this._cleanupTask || this._audioAttachTask) return;
    const sessionDeviceId = snapshot.session_device_id || snapshot.device_id || this._getConfigDeviceId();
    const target = snapshot.target_device_id
      ? this._availableDevices.find(d => d.device_id === snapshot.target_device_id)
      : this._getSoftphoneTargetDevice();
    this._audioAttachTask = voipStackEngine.resumeSession(
      {
        ...(target || {}),
        device_id: sessionDeviceId,
        endpoint_id: snapshot.endpoint_id || this._getSoftphoneEndpointId(),
        audio_mode: snapshot.audio_mode || target?.audio_mode || "full_duplex",
        microphone_anti_alias: this._microphoneAntiAliasEnabled(),
        softphone: true,
      },
      sessionDeviceId,
      snapshot,
    ).catch((err) => {
      console.warn("voip-stack-card: failed to attach HA softphone audio", err);
      this._showError(err.message || String(err));
    }).finally(() => {
      this._audioAttachTask = null;
      this._render();
    });
  }

  _captureEndReason(kind, reason, origin, peerOverride = "") {
    const peer = peerOverride || this._getCallerName() || this._getDestination() || "";
    this._lastEndInfo = { kind, reason, origin, peer, until_ms: Date.now() + 5000 };
    if (this._lastEndClearTimer) clearTimeout(this._lastEndClearTimer);
    this._lastEndClearTimer = setTimeout(() => {
      this._lastEndInfo = null;
      this._lastEndClearTimer = null;
      this._render();
    }, 5000);
  }

  _syncVideoDurationTimer(active) {
    if (active && !this._videoDurationTimer) {
      this._videoDurationTimer = setInterval(() => this._render(), 1000);
    } else if (!active && this._videoDurationTimer) {
      clearInterval(this._videoDurationTimer);
      this._videoDurationTimer = null;
    }
  }

  _formatVideoCallDuration() {
    return formatCallDuration(this._softphoneSnapshot?.connected_at);
  }

  _clearEndReason(doRender = true) {
    if (this._lastEndClearTimer) {
      clearTimeout(this._lastEndClearTimer);
      this._lastEndClearTimer = null;
    }
    this._lastEndInfo = null;
    if (doRender) this._render();
  }

  _formatEndReason(info) {
    return formatEndReason(info);
  }

  _reasonKey(reason) {
    return reasonKey(reason);
  }

  _formatKnownReason(reason) {
    return formatKnownReason(reason);
  }

  _formatVideoFailureReason(reason) {
    return formatVideoFailureReason(reason);
  }

  setConfig(config) {
    const oldSelector = this.config?.entity_id || this.config?.device_id || "";
    const oldEndpointId = this._getSoftphoneEndpointId();
    const oldDeviceId = String(this.config?.device_id || "");
    const oldMode = this.config?.mode || this.config?.card_mode || "esp_mirror";
    const normalised = normaliseCardConfig(config);
    this.config = normalised.config;
    const newSelector = this.config?.entity_id || this.config?.device_id || "";
    const newMode = this.config?.mode || this.config?.card_mode || "esp_mirror";
    if (oldMode === "ha_softphone" && newMode !== "ha_softphone") {
      this._lifecycleGeneration++;
      this._callOperationId++;
      if (this._unsubSoftphoneState) {
        this._unsubSoftphoneState();
        this._unsubSoftphoneState = null;
      }
      voipStackEngine.releaseVideoCanvas(this);
      voipStackEngine.releaseSoftphoneController(
        this,
        oldEndpointId || (oldDeviceId ? `device:${oldDeviceId}` : "preferred"),
      );
    }
    if (oldSelector !== newSelector || oldMode !== newMode) {
      if (this._unsubSoftphoneState) {
        this._unsubSoftphoneState();
        this._unsubSoftphoneState = null;
      }
      voipStackEngine.releaseSoftphoneController(
        this,
        oldEndpointId || (oldDeviceId ? `device:${oldDeviceId}` : "preferred"),
      );
      this._resetDeviceBindings();
      this._softphoneStateLoaded = false;
      this._softphoneSnapshot = null;
      this._activeSessionDeviceId = null;
    }
    if (this._isPhonebookMode()) {
      this._render();
      return;
    }
    this._softphoneTargetDeviceId =
      this._loadSoftphoneTargetPreference() ||
      this._softphoneTargetDeviceId;
    // Ringtone and microphone filtering are browser preferences. Auto-answer
    // and camera intent come from the logical phone snapshot.
    const deviceId = this._phonePreferenceStorageId();
    if (deviceId) {
      try {
        this._ringtoneEnabled = localStorage.getItem(`voip_ringtone_${deviceId}`) === "true";
        this._micAntiAliasEnabled =
          localStorage.getItem(`voip_microphone_anti_alias_${deviceId}`) !==
          "false";
      } catch (_) {}
    }
    if (this._hass && this._isHaSoftphoneMode()) {
      if (this.isConnected) this._subscribeBusEvents();
      if (this.isConnected) this._loadSoftphoneState();
    }
    else if (this._hass) this._findEntityIds();
    this._render();
  }

  _resetDeviceBindings() {
    this._activeDeviceInfo = null;
    this._resolvedDeviceId = null;
    if (this._deviceBindingsRetryTimer) {
      clearTimeout(this._deviceBindingsRetryTimer);
      this._deviceBindingsRetryTimer = null;
    }
    for (const key of [
      "_voipStateEntityId", "_transportEntityId", "_callerEntityId",
      "_destinationEntityId", "_lastReasonEntityId", "_previousButtonEntityId",
      "_nextButtonEntityId", "_callButtonEntityId", "_declineButtonEntityId",
      "_autoAnswerSwitchEntityId", "_dndSwitchEntityId", "_ringGroupsTextEntityId",
      "_conferenceGroupsTextEntityId", "_extensionTextEntityId",
      "_conferenceRingSwitchEntityId",
    ]) this[key] = null;
    this._startCallService = "";
  }

  set hass(hass) {
    const oldHass = this._hass;
    this._hass = hass;
    if (this._nativeCameraCard) this._nativeCameraCard.hass = hass;
    if (this._isPhonebookMode()) {
      this._render();
      return;
    }
    voipStackEngine.configure(hass);

    // Devices populate the destination cycler.
    if (hass && this._availableDevices.length === 0) {
      this._loadAvailableDevices();
    }
    if (hass) {
      this._loadSharedRoster();
    }
    if (
      this.isConnected && hass && this._isHaSoftphoneMode() &&
      !this._softphoneStateLoaded
    ) {
      this._loadSoftphoneState();
    }

    // Discover entity IDs once
    if (hass && !this._voipStateEntityId) {
      this._findEntityIds();
    }

    // Subscribe to HA bus events once we have a hass.connection
    if (
      this.isConnected && hass && hass.connection &&
      (this._isHaSoftphoneMode() ? !this._unsubSoftphoneState : !this._unsubCallEvents)
    ) {
      this._subscribeBusEvents();
    }

    // Re-render when ESP state or destination changes
    if (hass) {
      let needsRender = false;
      let newEspState = null;
      let espStateChanged = false;
      let lastReasonChanged = false;

      // Check voip_state
      if (this._voipStateEntityId) {
        const stateEntity = hass.states[this._voipStateEntityId];
        const oldStateEntity = oldHass?.states?.[this._voipStateEntityId];
        newEspState = stateEntity?.state?.toLowerCase();
        if (stateEntity?.state !== oldStateEntity?.state) {
          needsRender = true;
          espStateChanged = true;
        }
      }

      // Check destination (drives contact-cycler label).
      if (this._destinationEntityId) {
        const destEntity = hass.states[this._destinationEntityId];
        const oldDestEntity = oldHass?.states?.[this._destinationEntityId];
        if (destEntity?.state !== oldDestEntity?.state) {
          needsRender = true;
        }
      }
      const rosterState = hass.states["sensor.voip_phonebook"];
      const oldRosterState = oldHass?.states?.["sensor.voip_phonebook"];
      if (
        rosterState?.attributes?.roster_json !== oldRosterState?.attributes?.roster_json ||
        rosterState?.attributes?.phonebook !== oldRosterState?.attributes?.phonebook
      ) {
        this._loadSharedRoster();
        needsRender = true;
      }

      if (this._transportEntityId) {
        const transportEntity = hass.states[this._transportEntityId];
        const oldTransportEntity = oldHass?.states?.[this._transportEntityId];
        if (transportEntity?.state !== oldTransportEntity?.state) {
          needsRender = true;
        }
      }

      if (this.config?.show_extended_info) {
        for (const device of this._availableDevices) {
          const transportEntityId = device?.entities?.voip_transport;
          if (!transportEntityId) continue;
          if (hass.states[transportEntityId]?.state !== oldHass?.states?.[transportEntityId]?.state) {
            needsRender = true;
            break;
          }
        }
      }

      for (const entityId of [
        this._autoAnswerSwitchEntityId,
        this._dndSwitchEntityId,
        this._ringGroupsTextEntityId,
        this._conferenceGroupsTextEntityId,
        this._conferenceRingSwitchEntityId,
      ]) {
        if (!entityId) continue;
        if (hass.states[entityId]?.state !== oldHass?.states?.[entityId]?.state) {
          needsRender = true;
        }
      }
      if (!this._isHaSoftphoneMode() && this._autoAnswerSwitchEntityId) {
        this._autoAnswer = String(hass.states[this._autoAnswerSwitchEntityId]?.state || "").toLowerCase() === "on";
      }

      // Check caller (for incoming call info)
      if (this._callerEntityId) {
        const callerEntity = hass.states[this._callerEntityId];
        const oldCallerEntity = oldHass?.states?.[this._callerEntityId];
        if (callerEntity?.state !== oldCallerEntity?.state) {
          needsRender = true;
        }
      }

      // Check terminal reason. For direct ESP-to-ESP calls HA is only
      // mirroring the source ESP, so the reason comes from the ESP's
      // voip_stack last-reason entity, not from a HA bridge event.
      if (this._lastReasonEntityId) {
        const reasonEntity = hass.states[this._lastReasonEntityId];
        const oldReasonEntity = oldHass?.states?.[this._lastReasonEntityId];
        if (reasonEntity?.state !== oldReasonEntity?.state) {
          needsRender = true;
          lastReasonChanged = true;
        }
      }

      // The HA session/audio websocket is authoritative for browser audio
      // teardown. The mirrored ESP state can briefly report idle during a
      // HA-originated call and must not close the page-level engine.
      if (espStateChanged && newEspState === "idle") {
        this._errorMsg = "";
        this._autoAnswering = false;
        if (!this._lastEndInfo) this._captureMirroredLastReason();
      } else if (lastReasonChanged && this._getEspState().toLowerCase() === "idle") {
        if (!this._lastEndInfo) this._captureMirroredLastReason();
      }

      // In ESP mirror mode, auto-answer mirrors the ESP smart Call button when
      // the ESP itself is ringing. HA softphone auto-answer is handled from
      // HA session events.
      if (
        espStateChanged &&
        !this._isHaSoftphoneMode() &&
        this._autoAnswer &&
        !this._autoAnswering &&
        !this._starting &&
        (newEspState === "ringing" || newEspState === "incoming")
      ) {
          this._autoAnswering = true;
          this._tryAutoAnswer();
      }

      if (needsRender) {
        this._render();
      }
    }
    return true;
  }

  _shouldAnswerFromUrl() {
    if (this._deepLinkAnswerConsumed) return false;
    try {
      const params = new URLSearchParams(window.location.search || "");
      const value = (params.get("voip_answer") || "").toLowerCase();
      if (!(value === "1" || value === "true" || value === "yes")) return false;
      const endpointId = String(params.get("voip_endpoint") || "").trim();
      const callId = String(params.get("voip_call_id") || "").trim();
      const currentEndpoint = this._getSoftphoneEndpointId();
      // Legacy links are deliberately scoped to the original master phone.
      // Additional phones require an explicit endpoint so two ringing kiosk
      // cards cannot race to consume one global URL parameter.
      if (!endpointId && currentEndpoint !== "default") return false;
      if (endpointId && endpointId !== currentEndpoint) return false;
      if (callId && callId !== String(this._softphoneSnapshot?.call_id || "")) return false;
      return true;
    } catch (_) {
      return false;
    }
  }

  _clearAnswerUrlParam() {
    try {
      const url = new URL(window.location.href);
      url.searchParams.delete("voip_answer");
      url.searchParams.delete("voip_endpoint");
      url.searchParams.delete("voip_call_id");
      window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
    } catch (_) {
      // Best effort only. Leaving the parameter is harmless because the local
      // consumed flag prevents repeated answers in this card instance.
    }
  }

  _maybeAnswerFromUrl(espState) {
    if (!this._shouldAnswerFromUrl()) return;
    if (this._autoAnswering || this._starting) return;
    if (!this._isHaSoftphoneMode()) return;
    const state = (espState || this._getEspState()).toLowerCase();
    if (state !== "ringing" && state !== "incoming") return;
    const snap = this._softphoneSnapshot || {};
    if (String(snap.direction || "").toLowerCase() !== "incoming") return;
    if (!snap.call_id) return;

    this._deepLinkAnswerConsumed = true;
    this._clearAnswerUrlParam();
    this._autoAnswering = true;
    this._tryAutoAnswer({
      callId: String(snap.call_id),
      requirePersistentPermission: false,
    });
  }

  _getConfigDeviceId() {
    if (this._isHaSoftphoneMode()) {
      return this.config?.device_id || this._softphoneSnapshot?.device_id || "";
    }
    return this._resolvedDeviceId || this._getConfigSelector();
  }

  _getConfigSelector() {
    return this.config?.entity_id || this.config?.device_id;
  }

  _getSoftphoneEndpointId() {
    if (!this._isHaSoftphoneMode()) return "";
    return String(this._softphoneSnapshot?.endpoint_id || "").trim();
  }

  _softphoneSelector() {
    const selector = {};
    const endpointId = this._getSoftphoneEndpointId();
    const deviceId = String(this.config?.device_id || "").trim();
    if (endpointId) selector.endpoint_id = endpointId;
    if (deviceId) selector.device_id = deviceId;
    return selector;
  }

  _softphoneRuntimeKey() {
    const configuredDevice = String(this.config?.device_id || "").trim();
    return configuredDevice
      ? `device:${configuredDevice}`
      : this._getSoftphoneEndpointId()
        ? `endpoint:${this._getSoftphoneEndpointId()}`
        : "preferred";
  }

  _softphoneSnapshotMatches(payload = {}) {
    const selector = this._softphoneSelector();
    const endpointId = String(payload.endpoint_id || "").trim();
    const deviceId = String(payload.device_id || payload.endpoint_device_id || "").trim();
    if (selector.endpoint_id) {
      return !!endpointId && endpointId === selector.endpoint_id;
    }
    if (selector.device_id) {
      return !!deviceId && selector.device_id === deviceId;
    }
    return !!endpointId && !!deviceId;
  }

  _softphoneRequestScope() {
    const scope = {};
    const endpointId = this._getSoftphoneEndpointId();
    const deviceId = String(this.config?.device_id || this._softphoneSnapshot?.device_id || "").trim();
    if (endpointId) scope.endpoint_id = endpointId;
    if (deviceId) scope.device_id = deviceId;
    return scope;
  }

  _softphoneServiceScope() {
    // endpoint_id is an internal card/WebSocket correlation key. Public HA
    // actions deliberately select a phone only through device_id.
    const deviceId = String(this._getConfigDeviceId() || "").trim();
    return deviceId ? { device_id: deviceId } : {};
  }

  _isHaSoftphoneMode() {
    return (this.config?.mode || this.config?.card_mode || "esp_mirror") === "ha_softphone";
  }

  _microphoneAntiAliasEnabled() {
    return this._micAntiAliasEnabled;
  }

  _canConfigureHaSoftphone() {
    return !this._isHaSoftphoneMode() || this._hass?.user?.is_admin === true;
  }

  _isPhonebookMode() {
    return (this.config?.mode || this.config?.card_mode || "esp_mirror") === "phonebook";
  }

  _phonePreferenceStorageId() {
    return this._isHaSoftphoneMode()
      ? (
          String(this.config?.device_id || "").trim() ||
          this._getSoftphoneEndpointId() ||
          "preferred"
        )
      : (this.config?.entity_id || this.config?.device_id);
  }

  _isIncomingSoftphoneRing(state) {
    const st = String(state || "").toLowerCase();
    return this._isHaSoftphoneMode() &&
      (st === "ringing" || st === "incoming") &&
      String(this._softphoneSnapshot?.direction || "").toLowerCase() === "incoming" &&
      !!this._softphoneSnapshot?.call_id;
  }

  _syncRingtoneRequest(state) {
    voipStackEngine.setRingtoneRequest(
      this._ringtoneRequestKey,
      this._isIncomingSoftphoneRing(state),
      this._ringtoneEnabled,
    );
  }

  _softphoneTargetStorageKey() {
    return `voip_softphone_target_${this._phonePreferenceStorageId() || "default"}`;
  }

  _loadSoftphoneTargetPreference() {
    try { return localStorage.getItem(this._softphoneTargetStorageKey()) || ""; }
    catch (_) { return ""; }
  }

  _saveSoftphoneTargetPreference(deviceId) {
    try {
      if (deviceId) localStorage.setItem(this._softphoneTargetStorageKey(), deviceId);
      else localStorage.removeItem(this._softphoneTargetStorageKey());
    } catch (_) {}
  }

  _sessionDeviceId() {
    if (this._isHaSoftphoneMode()) {
      return this._softphoneSnapshot?.session_device_id || this._getConfigDeviceId();
    }
    return this._activeSessionDeviceId || this._activeDeviceInfo?.device_id || this._getConfigDeviceId();
  }

  _sessionCallId() {
    if (this._isHaSoftphoneMode()) return this._softphoneSnapshot?.call_id || "";
    return "";
  }

  // Get current ESP state from entity
  _getEspState() {
    if (this._isHaSoftphoneMode()) return this._softphoneSnapshot?.state || "idle";
    if (!this._hass || !this._voipStateEntityId) return "unknown";
    const entity = this._hass.states[this._voipStateEntityId];
    return entity?.state || "unknown";
  }

  _isConfiguredSoftphone() {
    if (this._isHaSoftphoneMode()) return true;
    const device = this._activeDeviceInfo || this._availableDevices.find(d => this._deviceMatchesConfig(d));
    return !!device?.softphone;
  }

  _isEspUnavailable() {
    if (!this._hass) return false;

    const configuredDevice = this._availableDevices.find(d => this._deviceMatchesConfig(d));
    const stateEntityId =
      this._voipStateEntityId ||
      configuredDevice?.entities?.voip_state;
    if (stateEntityId) {
      const entity = this._hass.states[stateEntityId];
      if (!entity) return true;
      const state = String(entity.state || "").toLowerCase();
      return state === "unknown" || state === "unavailable";
    }

    const endpointEntityId = configuredDevice?.entities?.voip_endpoint;
    if (endpointEntityId) {
      const entity = this._hass.states[endpointEntityId];
      if (!entity) return true;
      const state = String(entity.state || "").toLowerCase();
      return state === "unknown" || state === "unavailable";
    }

    // Only fall back to endpoint discovery when no stable HA entity binding is
    // available. A bound state entity always wins so reconnects render on the
    // next hass update without waiting for list_devices to refresh.
    return !!(
      this._getConfigDeviceId() &&
      !configuredDevice &&
      !this._availableDevicesLoading &&
      this._availableDevices.length > 0
    );
  }

  // Get caller name from entity
  _getCallerName() {
    if (this._isHaSoftphoneMode()) {
      const snap = this._softphoneSnapshot || {};
      if (snap.direction === "incoming") return snap.caller || snap.peer_name || "";
      return snap.peer_name || snap.callee || "";
    }
    if (!this._hass || !this._callerEntityId) return "";
    const entity = this._hass.states[this._callerEntityId];
    const state = entity?.state;
    if (!state || state === "unknown" || state === "") return "";
    return state;
  }

  // The HA peer is identified by the instance friendly name (location_name).
  // The integration sensor prepends location_name as the first contact, and
  // voip_stack selects it by index, so the destination text shown by the
  // ESP equals location_name. Compare against this everywhere instead of the
  // hardcoded "Home Assistant" string literal.
  _getHaName() {
    if (this._isHaSoftphoneMode()) {
      return this._softphoneSnapshot?.name || this.config?.name ||
        this._hass?.config?.location_name || "Home Assistant";
    }
    return this._hass?.config?.location_name || "voip-stack";
  }

  // Get destination from entity
  _getDestination() {
    if (this._isHaSoftphoneMode()) {
      const snap = this._softphoneSnapshot || {};
      if (snap.state && snap.state !== "idle") {
        return snap.peer_name || snap.callee || snap.caller || this._getSoftphoneTargetDevice()?.name || "No endpoint";
      }
      return this._getSoftphoneTargetDevice()?.name || "No endpoint";
    }
    if (!this._hass || !this._destinationEntityId) return this._getHaName();
    const entity = this._hass.states[this._destinationEntityId];
    return entity?.state || this._getHaName();
  }

  _contactCyclerDestination(destination) {
    if (this._isHaSoftphoneMode()) return destination;
    if (!this._lastEndInfo) this._lastKnownMirrorDestination = destination;
    return this._lastEndInfo ? this._lastKnownMirrorDestination || destination : destination;
  }

  _softphoneTargets() {
    const targets = [];
    const ownEndpointId = this._getSoftphoneEndpointId();
    for (const entry of this._rosterEntries || []) {
      if (!entry || entry.enabled === false) continue;
      const metadata = entry.metadata || {};
      if (
        String(metadata.endpoint_kind || "").trim().toLowerCase() === "sip_account" &&
        metadata.registered !== true
      ) continue;
      const entryEndpointId = String(metadata.endpoint_id || "").trim();
      if (
        metadata.local_ha &&
        !!entryEndpointId &&
        entryEndpointId === ownEndpointId
      ) continue;
      const target = this._targetFromRosterEntry(entry);
      if (!target.device_id || !target.name) continue;
      targets.push(target);
    }
    return targets;
  }

  _availableSoftphoneGroups(groupType) {
    const groups = [];
    for (const entry of this._rosterEntries || []) {
      if (!entry || entry.enabled === false) continue;
      if (String(entry.metadata?.group_type || "") !== groupType) continue;
      const name = entry.name || entry.id;
      if (name) groups.push(name);
    }
    return groups;
  }

  _getSoftphoneTargetDevice() {
    const targets = this._softphoneTargets();
    if (targets.length === 0) return null;
    const wanted = this._softphoneTargetDeviceId;
    return targets.find(d => d.device_id === wanted) || targets[0];
  }

  _activeSoftphonePeerDevice() {
    if (!this._isHaSoftphoneMode()) return null;
    const snapshot = this._softphoneSnapshot || {};
    const targetDeviceId = String(snapshot.target_device_id || "").trim();
    if (targetDeviceId) {
      const target = this._availableDevices.find(
        (device) => String(device?.device_id || "") === targetDeviceId,
      );
      if (target) return target;
      const rosterTarget = this._softphoneTargets().find(
        (device) => String(device?.device_id || "") === targetDeviceId,
      );
      if (rosterTarget) return rosterTarget;
    }

    const peerName = String(
      terminalPeerLabel(snapshot),
    ).trim();
    if (peerName) {
      const rosterTarget = this._softphoneTargets().find(
        (device) => this._samePeerName(device?.name, peerName),
      );
      if (rosterTarget) return rosterTarget;
      const target = this._availableDevices.find(
        (device) => this._samePeerName(device?.name, peerName),
      );
      if (target) return target;
    }
    return this._getSoftphoneTargetDevice();
  }

  _activeNativeCameraEntityId(espState) {
    if (
      !this._isHaSoftphoneMode() ||
      String(espState || "").trim().toLowerCase() !== "in_call"
    ) return "";
    const peer = this._activeSoftphonePeerDevice();
    if (!peer) return "";
    const capabilities = this._formatListFromMetadata(peer.capabilities)
      .map((value) => value.toLowerCase());
    if (
      String(peer.sip_video_codec || "").trim() ||
      capabilities.includes("video")
    ) return "";
    const entityId = String(peer.camera_entity_id || "").trim();
    if (!entityId.startsWith("camera.")) return "";
    // ESPHome cameras do not publish an initial image state. After an HA
    // restart their state machine can therefore remain "unavailable" until
    // the first standard camera stream request arrives. Mount the native HA
    // camera card whenever the entity exists so that request can bootstrap it.
    if (!this._hass?.states?.[entityId]) return "";
    return entityId;
  }

  _clearNativeCameraCard() {
    this._nativeCameraMountGeneration++;
    this._nativeCameraEntityId = "";
    this._nativeCameraCard = null;
    this._nativeCameraMountTask = null;
    const host = this._els?.nativeCameraHost;
    if (host) {
      host.hidden = true;
      host.replaceChildren();
    }
  }

  _syncNativeCameraCard(entityId) {
    const host = this._els?.nativeCameraHost;
    if (!host) return false;
    const wanted = String(entityId || "").trim();
    if (!wanted) {
      if (this._nativeCameraEntityId || this._nativeCameraCard || this._nativeCameraMountTask) {
        this._clearNativeCameraCard();
      } else {
        host.hidden = true;
      }
      return false;
    }

    host.hidden = false;
    if (wanted === this._nativeCameraEntityId) {
      if (this._nativeCameraCard) {
        this._nativeCameraCard.hass = this._hass;
        if (this._nativeCameraCard.parentNode !== host) {
          host.replaceChildren(this._nativeCameraCard);
        }
      }
      return true;
    }

    const generation = ++this._nativeCameraMountGeneration;
    this._nativeCameraEntityId = wanted;
    this._nativeCameraCard = null;
    host.replaceChildren();
    this._nativeCameraMountTask = (async () => {
      const helpers = await window.loadCardHelpers();
      const card = helpers.createCardElement({
        type: "picture-entity",
        entity: wanted,
        camera_image: wanted,
        camera_view: "live",
        show_name: false,
        show_state: false,
        tap_action: { action: "none" },
        hold_action: { action: "none" },
      });
      if (
        generation !== this._nativeCameraMountGeneration ||
        wanted !== this._nativeCameraEntityId ||
        !this.isConnected
      ) return;
      card.classList.add("native-camera-card");
      card.hass = this._hass;
      this._nativeCameraCard = card;
      host.replaceChildren(card);
    })().catch((err) => {
      if (generation !== this._nativeCameraMountGeneration) return;
      this._nativeCameraEntityId = "";
      host.hidden = true;
      console.warn("voip-stack-card: failed to mount native HA camera", err);
    }).finally(() => {
      if (generation === this._nativeCameraMountGeneration) {
        this._nativeCameraMountTask = null;
      }
    });
    return true;
  }

  _loadSharedRoster() {
    const attr = this._hass?.states?.["sensor.voip_phonebook"]?.attributes || {};
    const raw = attr.roster_json || "";
    const phonebook = attr.phonebook || "";
    const sourceKey = `${raw}\u0000${phonebook}`;
    // Home Assistant assigns `hass` to every Lovelace card for every global
    // state update. Re-parsing the full shared roster on each assignment made
    // bursts of SIP/ESP entity updates visibly stall softphone controls while
    // an outbound call moved through calling/ringing. Only rebuild when the
    // actual phonebook payload changes.
    if (this._rosterSourceKey === sourceKey) return false;
    this._rosterSourceKey = sourceKey;
    let contacts = [];
    if (raw) {
      try {
        const parsed = JSON.parse(raw);
        contacts = Array.isArray(parsed) ? parsed : (Array.isArray(parsed?.contacts) ? parsed.contacts : []);
      } catch (err) {
        console.error("Invalid voip roster_json:", err);
      }
    }
    const rosterEntries = [];
    for (const entry of contacts) {
      if (!entry || typeof entry !== "object") continue;
      const id = String(entry.id || entry.name || "").trim();
      if (!id) continue;
      rosterEntries.push({
        id,
        name: String(entry.name || entry.id || "").trim(),
        address: String(entry.address || entry.host || "").trim(),
        sip_uri: String(entry.sip_uri || "").trim(),
        extension: String(entry.extension || "").trim(),
        number: String(entry.number || "").trim(),
        port: Number(entry.port || 0),
        ha_bridge: !!entry.ha_bridge,
        enabled: entry.enabled !== false,
        metadata: entry.metadata && typeof entry.metadata === "object" ? entry.metadata : {},
      });
    }
    this._rosterEntries = rosterEntries;
    if (this._isHaSoftphoneMode() && !this._getSoftphoneTargetDevice()) {
      this._softphoneTargetDeviceId = this._softphoneTargets()[0]?.device_id || null;
    }
    return true;
  }

  _formatListFromMetadata(value) {
    return formatListFromMetadata(value);
  }

  _targetFromRosterEntry(entry) {
    return targetFromRosterEntry(entry);
  }

  _normaliseTransport(value) {
    return normaliseTransport(value);
  }

  _transportFromEntity(entityId) {
    if (!this._hass || !entityId) return "";
    return this._normaliseTransport(this._hass.states[entityId]?.state);
  }

  _deviceMatchesConfig(device) {
    const deviceId = this._getConfigDeviceId();
    return !!device && !!deviceId && device.device_id === deviceId;
  }

  _normaliseAudioMode(value) {
    return normaliseAudioMode(value);
  }

  _audioModeLabel(mode) {
    return audioModeLabel(mode);
  }

  _getOwnTransport() {
    const direct = this._transportFromEntity(this._transportEntityId);
    if (direct) return direct;
    const device = this._activeDeviceInfo || this._availableDevices.find(d => this._deviceMatchesConfig(d));
    return this._transportFromEntity(device?.entities?.voip_transport) ||
           this._normaliseTransport(device?.sip_transport);
  }

  _getOwnAudioMode() {
    const device = this._activeDeviceInfo || this._availableDevices.find(d => this._deviceMatchesConfig(d));
    return this._normaliseAudioMode(device?.audio_mode);
  }

  _formatHeaderTitle(baseName) {
    const name = String(baseName || "").trim();
    if (!name) return "";
    if (!this.config?.show_extended_info) return name;
    const transport = this._getOwnTransport();
    const mode = this._audioModeLabel(this._getOwnAudioMode());
    return transport ? `${name} - ${transport}/${mode}` : `${name} - ${mode}`;
  }

  _isHaName(name) {
    return String(name || "").trim().toLowerCase() === String(this._getHaName() || "").trim().toLowerCase();
  }

  _isSoftphoneContext() {
    return this._isHaSoftphoneMode();
  }

  async _pressEspButton(entityId, label) {
    if (!entityId) throw new Error(`${label} button not available`);
    await this._hass.callService("button", "press", { entity_id: entityId });
  }

  _entityState(entityId) {
    if (!entityId) return "";
    const state = this._hass?.states?.[entityId]?.state || "";
    return state === "unknown" || state === "unavailable" ? "" : state;
  }

  async _setSwitchEntity(entityId, enabled) {
    if (!entityId) throw new Error("Switch entity not available");
    await this._hass.callService("switch", enabled ? "turn_on" : "turn_off", { entity_id: entityId });
  }

  async _setTextEntity(entityId, value) {
    if (!entityId) throw new Error("Text entity not available");
    await this._hass.callService("text", "set_value", {
      entity_id: entityId,
      value: String(value || "").trim(),
    });
  }

  _getLastReason() {
    if (!this._hass || !this._lastReasonEntityId) return "";
    const entity = this._hass.states[this._lastReasonEntityId];
    const value = entity?.state || "";
    return value === "unknown" || value === "unavailable" ? "" : value;
  }

  _captureMirroredLastReason() {
    const reason = this._getLastReason();
    if (!reason) return;
    const reasonKey = this._reasonKey(reason);
    // Mirror mode shows the ESP terminal reason as-is. If the card is a
    // HA/browser softphone, terminal direction comes from call_event instead.
    this._captureEndReason(
      "terminal",
      reason,
      reasonKey === "local_hangup" ? "self" : "remote",
    );
  }

  async _findEntityIds() {
    if (!this._hass) return;
    if (this._isHaSoftphoneMode()) return;
    if (this._deviceBindingsLoading || this._deviceBindingsRetryTimer) return;

    const expectedSelector = this._getConfigSelector();
    this._deviceBindingsLoading = true;
    try {
      const deviceInfo = await this._getDeviceInfo();
      if (this._isHaSoftphoneMode() || expectedSelector !== this._getConfigSelector()) return;
      const configDeviceId = this._getConfigDeviceId();
      const targetDeviceId = deviceInfo?.device_id || configDeviceId;
      if (!targetDeviceId) return;

      // Use entities mapping from backend
      if (deviceInfo?.entities && typeof deviceInfo.entities === "object") {
        const e = deviceInfo.entities;
        this._voipStateEntityId = e.voip_state || null;
        this._transportEntityId = e.voip_transport || null;
        this._callerEntityId = e.incoming_caller || null;
        this._destinationEntityId = e.destination || null;
        this._lastReasonEntityId = e.last_reason || null;
        this._previousButtonEntityId = e.previous || null;
        this._nextButtonEntityId = e.next || null;
        this._callButtonEntityId = e.call || null;
        this._declineButtonEntityId = e.decline || null;
        this._autoAnswerSwitchEntityId = e.auto_answer || null;
        this._dndSwitchEntityId = e.dnd || null;
        this._extensionTextEntityId = e.voip_extension || null;
        this._ringGroupsTextEntityId = e.voip_ring_groups || null;
        this._conferenceGroupsTextEntityId = e.voip_conference_groups || null;
        this._conferenceRingSwitchEntityId = e.voip_conference_ring || null;
        this._startCallService = e.start_call_service || "";
        this._render();
        return;
      }

      // Fallback: entity registry
      try {
        const registryResult = await this._hass.callWS({
          type: "config/entity_registry/list_for_display",
        });
        const registry = Array.isArray(registryResult)
          ? registryResult
          : registryResult?.entities;
        if (
          !Array.isArray(registry) ||
          this._isHaSoftphoneMode() ||
          expectedSelector !== this._getConfigSelector()
        ) return;

        for (const entity of registry) {
          const registryDeviceId = entity.di || entity.device_id;
          if (registryDeviceId !== targetDeviceId) continue;
          const id = entity.ei || entity.entity_id;
          if (!id) continue;
          if (id.includes("voip_state")) this._voipStateEntityId = id;
          else if (id.includes("voip_transport")) this._transportEntityId = id;
          else if (id.includes("caller")) this._callerEntityId = id;
          else if (id.includes("destination")) this._destinationEntityId = id;
          else if (id.includes("voip_last_reason") || id.includes("last_reason") || id.includes("end_reason")) this._lastReasonEntityId = id;
          else if (id.startsWith("button.") && id.includes("previous")) this._previousButtonEntityId = id;
          else if (id.startsWith("button.") && id.includes("next")) this._nextButtonEntityId = id;
          else if (id.startsWith("button.") && id.includes("call") && !id.includes("decline")) this._callButtonEntityId = id;
          else if (id.startsWith("button.") && id.includes("decline")) this._declineButtonEntityId = id;
          else if (id.startsWith("switch.") && id.includes("auto_answer")) this._autoAnswerSwitchEntityId = id;
          else if (id.startsWith("switch.") && (id.includes("do_not_disturb") || id.includes("_dnd"))) this._dndSwitchEntityId = id;
          else if (id.startsWith("text.") && id.includes("voip_extension")) this._extensionTextEntityId = id;
          else if (id.startsWith("text.") && id.includes("voip_ring_groups")) this._ringGroupsTextEntityId = id;
          else if (id.startsWith("text.") && id.includes("voip_conference_groups")) this._conferenceGroupsTextEntityId = id;
          else if (id.startsWith("switch.") && id.includes("voip_conference_ring")) this._conferenceRingSwitchEntityId = id;
        }
        this._render();
      } catch (err) {
        console.error("Entity discovery failed:", err);
      }
    } finally {
      this._deviceBindingsLoading = false;
      if (
        this.isConnected &&
        !this._voipStateEntityId &&
        !this._isHaSoftphoneMode() &&
        expectedSelector === this._getConfigSelector()
      ) this._scheduleDeviceBindingsLoad();
    }
  }

  _scheduleDeviceBindingsLoad() {
    if (this._deviceBindingsRetryTimer) return;
    this._deviceBindingsRetryTimer = setTimeout(() => {
      this._deviceBindingsRetryTimer = null;
      this._findEntityIds();
    }, 2000);
  }

  async _loadAvailableDevices() {
    if (!this._hass || this._availableDevicesLoading) return;
    if (!this._isVoipStackLoaded()) {
      this._scheduleAvailableDevicesLoad();
      return;
    }
    this._availableDevicesLoading = true;
    try {
      const result = await this._hass.connection.sendMessagePromise({
        type: "voip_stack/list_devices",
      });
      if (result?.devices) {
        this._availableDevices = result.devices;
        this._render();
      }
    } catch (err) {
      if (this._isUnknownCommandError(err)) this._scheduleAvailableDevicesLoad();
      else console.error("Failed to load devices:", err);
    } finally {
      this._availableDevicesLoading = false;
    }
  }

  _isVoipStackLoaded() {
    const components = this._hass?.config?.components;
    return !Array.isArray(components) || components.includes("voip_stack");
  }

  _isUnknownCommandError(err) {
    const code = String(err?.code || err?.error || "").toLowerCase();
    const message = String(err?.message || "").toLowerCase();
    return code.includes("unknown_command") || code.includes("invalid_format") ||
      message.includes("unknown command") || message.includes("extra keys") ||
      message.includes("not allowed");
  }

  _scheduleAvailableDevicesLoad() {
    if (this._availableDevicesRetryTimer) return;
    this._availableDevicesRetryTimer = setTimeout(() => {
      this._availableDevicesRetryTimer = null;
      this._loadAvailableDevices();
    }, 2000);
  }

  _render() {
    if (this._isPhonebookMode()) {
      this._renderPhonebook();
      return;
    }
    const customName = String(this.config?.name || "").trim();
    const name = customName;
    const deviceId = this._getConfigDeviceId();

    if (!deviceId) {
      voipStackEngine.clearRingtoneRequest(this._ringtoneRequestKey);
      this._renderUnconfigured(name);
      return;
    }

    if (this._skeletonMode !== "main") {
      this._buildSkeletonMain();
      this._skeletonMode = "main";
    }
    const els = this._els;

    const espState = this._getEspState();
    const destination = this._getDestination();
    const caller = this._getCallerName();

    let statusText = "";
    let statusReason = "";
    let statusClass = "idle";
    let showAnswer = false;
    let showHangup = false;
    let showCall = false;
    const buttonDisabled = this._starting || this._stopping;
    const softphoneEnabled =
      !this._isHaSoftphoneMode() || this._softphoneSnapshot?.enabled !== false;

    let espDeviceName = this._activeDeviceInfo?.name;
    if (!espDeviceName && deviceId) {
      const device = this._availableDevices.find(d =>
        this._deviceMatchesConfig(d)
      );
      espDeviceName = device?.name;
    }
    const displayName = customName;
    espDeviceName = espDeviceName || displayName;

    if (!this._isHaSoftphoneMode() && this._isEspUnavailable()) {
      els.headerName.textContent = this._formatHeaderTitle(displayName);
      els.header.hidden = !displayName;
      els.destRow.hidden = true;
      els.offlinePanel.hidden = false;
      els.answerBtn.hidden = true;
      els.declineBtn.hidden = true;
      els.hangupBtn.hidden = true;
      els.callBtn.hidden = true;
      els.placeholderBtn.hidden = true;
      els.autoAnswerRow.hidden = true;
      els.statusIndicator.className = "status-indicator unavailable";
      els.statusText.textContent = "ESP unavailable";
      els.statusReason.textContent = "Device is offline";
      els.statusReason.hidden = false;
      els.stats.textContent = "";
      els.err.textContent = "";
      voipStackEngine.clearRingtoneRequest(this._ringtoneRequestKey);
      return;
    }
    els.offlinePanel.hidden = true;

    switch (espState.toLowerCase()) {
      case "idle":
        if (!softphoneEnabled) {
          statusText = "Phone unavailable";
          statusReason = "This logical phone is disabled or has been removed.";
          statusClass = "unavailable";
        } else if (this._isHaSoftphoneMode() && this._softphoneDnd) {
          statusText = "Do Not Disturb";
          statusReason = "Incoming calls to Home Assistant are declined.";
          statusClass = "idle";
          showCall = true;
        } else if (this._isHaSoftphoneMode() && this._lastEndInfo) {
          const peerLabel = this._lastEndInfo.peer ? ` with ${this._lastEndInfo.peer}` : "";
          statusText = `Call${peerLabel} ended.`;
          statusReason = `Reason: ${this._formatEndReason(this._lastEndInfo)}`;
          statusClass = "idle";
          showCall = true;
        } else if (!this._isHaSoftphoneMode() && this._lastEndInfo) {
          const reasonLabel = this._formatEndReason(this._lastEndInfo);
          const peerLabel = this._lastEndInfo.peer ? ` with ${this._lastEndInfo.peer}` : "";
          statusText = `Call${peerLabel} ended.`;
          statusReason = `Reason: ${reasonLabel}`;
          statusClass = "idle";
          showCall = true;
        } else {
          statusText = "Ready";
          statusClass = "idle";
          showCall = true;
        }
        break;
      case "calling":
      case "connecting":
      case "remote_ringing":
        statusText = espState.toLowerCase() === "remote_ringing"
          ? `${destination} is ringing...`
          : `Calling ${destination}...`;
        statusClass = espState.toLowerCase() === "remote_ringing" ? "ringing" : "transitioning";
        showHangup = true;
        break;
      case "ringing":
      case "incoming":
        statusText = `Incoming: ${caller || "Unknown"}`;
        statusClass = "ringing";
        showAnswer = true;
        break;
      case "answering":
        statusText = `Answering ${caller || destination || "call"}...`;
        statusClass = "transitioning";
        showHangup = true;
        break;
      case "terminating":
        statusText = "Ending call...";
        statusClass = "transitioning";
        break;
      case "in_call":
        statusText = `In Call: ${
          (!this._isHaSoftphoneMode() && this._mirroredConnectedPeer) ||
          caller || destination || "Active"
        }`;
        statusClass = "in_call";
        showHangup = true;
        break;
      default:
        statusText = espState;
        statusClass = "idle";
        showCall = true;
    }

    if (this._starting) {
      statusText = "Connecting...";
      showCall = false;
      showAnswer = false;
      showHangup = true;
    }
    if (this._stopping) statusText = "Ending call...";
    const videoFailureReason = this._formatVideoFailureReason(
      this._softphoneSnapshot?.video_failure_reason,
    );
    if (
      !statusReason &&
      this._isHaSoftphoneMode() &&
      !!this.config?.show_extended_info &&
      ["degraded", "failed", "rejected"].includes(
        String(this._softphoneSnapshot?.video_status || "").toLowerCase(),
      ) &&
      videoFailureReason
    ) {
      statusReason = `Video unavailable: ${videoFailureReason}`;
    }
    this._syncRingtoneRequest(espState);

    els.headerName.textContent = this._formatHeaderTitle(displayName);
    els.header.hidden = !displayName;

    // ESP cards mirror the ESP contact cycler. The optional keypad keeps its
    // own manual buffer and calls the ESPHome start_call service directly.
    const softphoneMode = this._isHaSoftphoneMode();
    const ownsVideoCanvas = softphoneMode &&
      espState.toLowerCase() === "in_call" &&
      voipStackEngine.endpointId === this._getSoftphoneEndpointId() &&
      this._isSoftphoneController()
      ? voipStackEngine.claimVideoCanvas(this, els.videoCanvas, this._getSoftphoneEndpointId())
      : (voipStackEngine.releaseVideoCanvas(this), false);
    const videoVisible = ownsVideoCanvas &&
      espState.toLowerCase() === "in_call" &&
      voipStackEngine.videoVisible;
    const nativeCameraVisible = !videoVisible && this._syncNativeCameraCard(
      this._activeNativeCameraEntityId(espState),
    );
    const visualMediaVisible = videoVisible || nativeCameraVisible;
    els.card.classList.toggle("video-active", visualMediaVisible);
    els.videoCanvas.hidden = !videoVisible;
    els.videoShade.hidden = !visualMediaVisible;
    this._syncVideoDurationTimer(visualMediaVisible);
    if (els.hangupPeer) {
      const normalizedState = espState.toLowerCase();
      els.hangupState.textContent = this._stopping
        ? "Ending"
        : (this._starting || ["calling", "connecting"].includes(normalizedState))
          ? "Calling"
          : normalizedState === "remote_ringing"
            ? "Ringing"
            : normalizedState === "answering"
              ? "Answering"
              : normalizedState === "terminating"
                ? "Ending"
            : "In call";
      els.hangupPeer.textContent = caller || destination || "Active call";
      els.hangupDuration.textContent = this._formatVideoCallDuration();
    }
    const keypadOpen = this._keypadOpen();
    els.destRow.hidden = !showCall || keypadOpen;
    els.destValue.textContent = this._contactCyclerDestination(destination);
    if (els.destSelect) {
      els.destSelect.hidden = !softphoneMode || keypadOpen;
      els.destValueWrap.classList.toggle("selecting", softphoneMode && !keypadOpen);
      this._renderSoftphoneDestinationSelect(els.destSelect);
    }
    if (els.keypadPanel) {
      els.keypadPanel.hidden = !(showCall && keypadOpen);
      els.keypadInput.value = this._manualTarget();
      for (const btn of Object.values(els.keypadKeys || {})) {
        btn.disabled = buttonDisabled;
      }
    }
    const hideContactCycler = softphoneMode || keypadOpen;
    els.prevBtn.disabled = buttonDisabled || hideContactCycler;
    els.nextBtn.disabled = buttonDisabled || hideContactCycler;
    els.prevBtn.hidden = hideContactCycler;
    els.nextBtn.hidden = hideContactCycler;
    els.prevBtn.style.display = hideContactCycler ? "none" : "";
    els.nextBtn.style.display = hideContactCycler ? "none" : "";

    const browserMediaBusy = softphoneMode && this._otherPhoneOwnsBrowserMedia();
    if (browserMediaBusy && (showAnswer || showCall) && !statusReason) {
      statusReason = "This browser is already handling another phone call.";
    }

    // Action buttons: exactly one set visible at a time.
    els.answerBtn.hidden = !showAnswer;
    els.declineBtn.hidden = !showAnswer;
    els.hangupBtn.hidden = !showHangup;
    els.callBtn.hidden = !showCall;
    els.placeholderBtn.hidden = showAnswer || showHangup || showCall;
    els.answerBtn.disabled = buttonDisabled || browserMediaBusy;
    els.declineBtn.disabled = buttonDisabled;
    // Cancelling an outbound INVITE has priority over the still-pending start
    // request. In particular, a trunk call may remain in CALLING until SIP
    // timer B expires, so `_starting` must never lock out Hangup.
    els.hangupBtn.disabled = this._stopping;
    els.callBtn.disabled = buttonDisabled || browserMediaBusy;

    // Status
    els.statusIndicator.className = "status-indicator " + statusClass;
    els.statusText.textContent = statusText;
    els.statusReason.textContent = statusReason;
    els.statusReason.hidden = !statusReason;

    // Runtime options are idle-only and live behind a compact settings panel.
    // During ringing/in_call the card shows only call actions, so toggles
    // cannot be changed mid-call.
    const showRuntimeOptions = showCall && !this._starting && !this._stopping;
    const showSettingsPanel = showRuntimeOptions && this._settingsOpen;
    const canUseKeypad = softphoneMode || !!this._startCallService;
    els.runtimeControls.hidden = !showRuntimeOptions;
    els.keypadBtn.hidden = !(showRuntimeOptions && canUseKeypad);
    els.keypadBtn.textContent = keypadOpen ? "Contacts" : "Keypad";
    els.keypadBtn.setAttribute("aria-expanded", String(showCall && keypadOpen));
    els.settingsBtn.hidden = !showRuntimeOptions;
    els.settingsPanel.hidden = !showSettingsPanel;
    els.settingsBtn.setAttribute("aria-expanded", String(showSettingsPanel));
    const autoAnswerAvailable = softphoneMode || !!this._autoAnswerSwitchEntityId;
    els.autoAnswerRow.hidden = !(showSettingsPanel && autoAnswerAvailable);
    els.autoAnswerCheckbox.checked = softphoneMode
      ? !!this._autoAnswer
      : this._entityState(this._autoAnswerSwitchEntityId).toLowerCase() === "on";
    if (els.ringtoneRow) {
      els.ringtoneRow.hidden = !(showSettingsPanel && this._isHaSoftphoneMode());
      els.ringtoneCheckbox.checked = !!this._ringtoneEnabled;
    }
    if (els.microphoneAntiAliasRow) {
      els.microphoneAntiAliasRow.hidden =
        !(showSettingsPanel && this._isHaSoftphoneMode());
      els.microphoneAntiAliasCheckbox.checked = this._micAntiAliasEnabled;
    }
    if (els.videoCameraRow) {
      const cameraAvailable = softphoneMode &&
        this._softphoneSupportsVideo() &&
        !!this._softphoneSnapshot?.video_camera_send_enabled;
      els.videoCameraRow.hidden = !(showSettingsPanel && cameraAvailable);
      els.videoCameraCheckbox.checked = !!this._softphoneSnapshot?.send_video;
    }
    if (els.dndRow) {
      const dndAvailable = softphoneMode || !!this._dndSwitchEntityId;
      els.dndRow.hidden = !(showSettingsPanel && dndAvailable);
      els.dndCheckbox.checked = softphoneMode
        ? !!this._softphoneDnd
        : this._entityState(this._dndSwitchEntityId).toLowerCase() === "on";
    }
    if (els.softphoneGroupsPanel) {
      const showGroups = showSettingsPanel && this._canConfigureHaSoftphone() && (
        softphoneMode ||
        !!this._ringGroupsTextEntityId ||
        !!this._conferenceGroupsTextEntityId ||
        !!this._conferenceRingSwitchEntityId
      );
      els.softphoneGroupsPanel.hidden = !showGroups;
      if (showGroups) this._renderGroupControls();
    }

    // Stats line: diagnostics stay out of the video plane. In video mode they are a
    // compact, single-line item in the bottom call bar and only appear when
    // the card explicitly opted into Extended information.
    const debugMode = !!this._softphoneSnapshot?.debug_mode;
    const statsText = this._isHaSoftphoneMode() &&
      this._hasBrowserAudioPath() && debugMode
      ? voipStackEngine.statsText()
      : "";
    const showVideoStats = videoVisible &&
      !!this.config?.show_extended_info &&
      !!statsText;
    if (els.hangupStats) {
      els.hangupStats.hidden = !showVideoStats;
      els.hangupStats.textContent = showVideoStats ? statsText : "";
      els.hangupStats.title = showVideoStats ? statsText : "";
    }
    if (!videoVisible && statsText) {
      els.stats.textContent = statsText;
    } else {
      els.stats.textContent = "";
    }

    // Error
    els.err.textContent = this._errorMsg;
  }

  _renderUnconfigured(name) {
    if (this._skeletonMode !== "unconfigured") {
      this._buildSkeletonUnconfigured();
      this._skeletonMode = "unconfigured";
    }
    this._els.headerName.textContent = name;
    this._els.header.hidden = !name;
    this._observeLayout();
  }

  _observeLayout() {
    const card = this.shadowRoot?.querySelector("ha-card");
    if (!card) return;
    this._resizeObserver.disconnect();
    this._resizeObserver.observe(card);
    this._measureLayout();
  }

  _measureLayout() {
    const card = this.shadowRoot?.querySelector("ha-card");
    if (!card) return;
    const width = card.clientWidth;
    const height = card.clientHeight;
    const buttonSize = Math.max(58, Math.min(136, Math.round((height - 100) * 0.36), Math.round(width * 0.32)));
    const spacing = Math.max(4, Math.min(16, Math.round(Math.min(width / 24, height / 28))));
    card.style.setProperty("--voip-button-size", `${buttonSize}px`);
    card.style.setProperty("--voip-small-button-size", `${Math.max(52, Math.round(buttonSize * 0.8))}px`);
    card.style.setProperty("--voip-fluid-space", `${spacing}px`);
    card.classList.toggle("layout-narrow", width < 350);
    card.classList.toggle("layout-compact", height < 360);
    card.classList.toggle("layout-short", height < 285);
  }

  _buildSkeletonMain() {
    buildMainCardSkeleton.call(this, VOIP_STACK_CARD_VERSION);
  }

  _buildSkeletonUnconfigured() {
    buildUnconfiguredCardSkeleton.call(this, VOIP_STACK_CARD_VERSION);
  }

  _renderPhonebook() {
    if (this._skeletonMode !== "phonebook") {
      this.shadowRoot.replaceChildren();
      const view = document.createElement("voip-stack-phonebook-view");
      this.shadowRoot.appendChild(view);
      this._phonebookView = view;
      this._skeletonMode = "phonebook";
    }
    const phonebookConfig = {
      entity: this.config?.entity || "sensor.voip_phonebook",
      title: String(this.config?.title || this.config?.name || "").trim(),
      empty_text: this.config?.empty_text || "No contacts available.",
      show_disabled: !!this.config?.show_disabled,
    };
    const configKey = JSON.stringify(phonebookConfig);
    if (configKey !== this._phonebookConfigKey) {
      this._phonebookView.setConfig(phonebookConfig);
      this._phonebookConfigKey = configKey;
    }
    if (this._hass) this._phonebookView.hass = this._hass;
  }

  _attachEventHandlers() {
    const els = this._els;
    if (!els) return;
    if (els.keypadBtn) els.keypadBtn.onclick = () => this._toggleKeypad();
    if (els.keypadInput) els.keypadInput.oninput = (event) => this._setManualTarget(event.target.value);
    if (els.keypadKeys) {
      for (const [key, btn] of Object.entries(els.keypadKeys)) {
        btn.onclick = () => this._pressKeypadKey(key);
      }
    }
    if (els.settingsBtn) els.settingsBtn.onclick = () => this._toggleSettings();
    els.autoAnswerCheckbox.onchange = () => this._toggleAutoAnswer();
    if (els.dndCheckbox) els.dndCheckbox.onchange = () => this._toggleDnd();
    if (els.ringtoneCheckbox) els.ringtoneCheckbox.onchange = () => this._toggleRingtone();
    if (els.microphoneAntiAliasCheckbox) {
      els.microphoneAntiAliasCheckbox.onchange = () =>
        this._toggleMicrophoneAntiAlias();
    }
    if (els.videoCameraCheckbox) {
      els.videoCameraCheckbox.onchange = (event) => this._toggleVideoCamera(event.target.checked);
    }
    if (els.extensionInput) {
      els.extensionInput.onchange = (event) => this._setExtensionSetting(event.target.value);
      els.extensionInput.onkeydown = (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          event.currentTarget.blur();
        }
      };
    }
    if (els.ringGroupInput) els.ringGroupInput.onchange = (event) => this._setGroupSetting("ring_group", event.target.value);
    if (els.conferenceGroupInput) els.conferenceGroupInput.onchange = (event) => this._setGroupSetting("conference_group", event.target.value);
    if (els.conferenceRingCheckbox) els.conferenceRingCheckbox.onchange = (event) => this._setGroupSetting("conference_ring", event.target.checked);
    els.callBtn.onclick = () => this._startCall();
    els.hangupBtn.onclick = () => this._hangup();
    els.answerBtn.onclick = () => this._answer();
    els.declineBtn.onclick = () => this._decline();
    els.prevBtn.onclick = () => this._prevContact();
    els.nextBtn.onclick = () => this._nextContact();
    if (els.destSelect) {
      els.destSelect.onchange = (event) => this._setSoftphoneTarget(event.target.value);
    }
  }

  _renderSoftphoneDestinationSelect(select) {
    const targets = this._softphoneTargets();
    const current = this._getSoftphoneTargetDevice();
    const optionsKey = JSON.stringify({
      selected: current?.device_id || "",
      targets: targets.map((device) => [device.device_id, device.name || device.device_id]),
    });
    select.disabled = this._starting || this._stopping || targets.length === 0;
    // Active SIP state transitions may render several times in a fraction of
    // a second. The destination list is hidden then and normally unchanged;
    // rebuilding all <option> nodes needlessly invalidates Lovelace layout.
    if (this._softphoneTargetOptionsKey === optionsKey) return;
    this._softphoneTargetOptionsKey = optionsKey;
    const options = [];
    if (targets.length === 0) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "No endpoints";
      options.push(opt);
    } else {
      for (const device of targets) {
        const opt = document.createElement("option");
        opt.value = device.device_id;
        opt.textContent = device.name || device.device_id;
        if (device.device_id === current?.device_id) opt.selected = true;
        options.push(opt);
      }
    }
    select.replaceChildren(...options);
  }

  _setSoftphoneTarget(deviceId) {
    this._softphoneTargetDeviceId = deviceId || null;
    this._saveSoftphoneTargetPreference(this._softphoneTargetDeviceId);
    this._render();
  }

  _keypadOpen() {
    return this._isHaSoftphoneMode() ? this._softphoneKeypadOpen : this._mirrorKeypadOpen;
  }

  _manualTarget() {
    return this._isHaSoftphoneMode() ? this._softphoneManualTarget : this._mirrorManualTarget;
  }

  _setManualTarget(value) {
    const clean = String(value || "").replace(/[\r\n]/g, "").trimStart();
    if (this._isHaSoftphoneMode()) this._softphoneManualTarget = clean;
    else this._mirrorManualTarget = clean;
  }

  _toggleKeypad() {
    if (!this._isHaSoftphoneMode() && !this._startCallService) return;
    if (this._isHaSoftphoneMode()) {
      this._softphoneKeypadOpen = !this._softphoneKeypadOpen;
    } else {
      this._mirrorKeypadOpen = !this._mirrorKeypadOpen;
      if (this._mirrorKeypadOpen) this._mirrorManualTarget = "";
    }
    if (this._keypadOpen()) this._settingsOpen = false;
    this._render();
    if (this._keypadOpen()) {
      requestAnimationFrame(() => this._els?.keypadInput?.focus());
    }
  }

  _pressKeypadKey(key) {
    if (key === "Clear") {
      this._setManualTarget("");
    } else if (key === "⌫") {
      this._setManualTarget(this._manualTarget().slice(0, -1));
    } else {
      this._setManualTarget(this._manualTarget() + key);
    }
    if (this._els?.keypadInput) {
      this._els.keypadInput.value = this._manualTarget();
      this._els.keypadInput.focus();
    }
  }

  async _prevContact() {
    if (this._isHaSoftphoneMode()) {
      return;
    }
    if (this._previousButtonEntityId) {
      await this._hass.callService("button", "press", { entity_id: this._previousButtonEntityId });
    }
  }

  async _nextContact() {
    if (this._isHaSoftphoneMode()) {
      return;
    }
    if (this._nextButtonEntityId) {
      await this._hass.callService("button", "press", { entity_id: this._nextButtonEntityId });
    }
  }

  async _startCall() {
    if (this._starting || this._stopping) return;
    const softphoneAction = this._isHaSoftphoneMode();
    const mediaIntentToken = softphoneAction ? {} : null;
    if (
      softphoneAction &&
      (
        this._otherPhoneOwnsBrowserMedia() ||
        !voipStackEngine.tryAcquireMediaIntent(
          this._getSoftphoneEndpointId(),
          mediaIntentToken,
        )
      )
    ) {
      this._showError("This browser is already handling another phone call.");
      return;
    }
    const operationId = ++this._callOperationId;
    this._starting = true;
    this._errorMsg = "";
    this._render();

    try {
      const deviceInfo = await this._getDeviceInfo();
      if (operationId !== this._callOperationId) return;
      if (softphoneAction) {
        await this._startHaSoftphoneCall(deviceInfo, operationId);
        return;
      }
      if (deviceInfo?.softphone) {
        throw new Error("Set card mode to Home Assistant softphone to call from HA");
      }
      if (!deviceInfo?.host) throw new Error("Device not available");
      this._activeDeviceInfo = deviceInfo;
      if (this._mirrorKeypadOpen) {
        const manualTarget = this._mirrorManualTarget.trim();
        if (!manualTarget) throw new Error("No destination entered");
        if (!this._startCallService) throw new Error("ESP start_call service not available");
        const [domain, service] = this._startCallService.split(".", 2);
        if (!domain || !service) throw new Error("Invalid ESP start_call service");
        await this._hass.callService(domain, service, { dest: manualTarget });
      } else {
        await this._pressEspButton(this._callButtonEntityId, "Call");
      }
    } catch (err) {
      if (operationId !== this._callOperationId) return;
      this._showError(err.message || String(err));
      if (
        softphoneAction &&
        voipStackEngine.endpointId === this._getSoftphoneEndpointId() &&
        (!voipStackEngine.callId || voipStackEngine.callId === this._sessionCallId())
      ) await voipStackEngine.close("start_error");
      else await this._cleanup();
    } finally {
      if (mediaIntentToken) voipStackEngine.releaseMediaIntent(mediaIntentToken);
      if (operationId === this._callOperationId) {
        this._starting = false;
        this._ensureHaSoftphoneAudioPath(this._softphoneSnapshot || {});
        this._render();
      }
    }
  }

  async _startHaSoftphoneCall(softphoneInfo, operationId) {
    const manualTarget = this._softphoneKeypadOpen ? this._softphoneManualTarget.trim() : "";
    const target = manualTarget
      ? {
          device_id: `manual:${manualTarget}`,
          name: manualTarget,
          audio_mode: "full_duplex",
          manual: true,
        }
      : this._getSoftphoneTargetDevice();
    if (!target?.name && !target?.device_id) {
      throw new Error("No endpoint available");
    }
    const callee = manualTarget || target.name || this._getDestination();

    const scope = this._softphoneRequestScope();
    const sessionInfo = {
      ...(softphoneInfo || {}),
      ...scope,
      device_id: scope.device_id || "",
      name: this._getHaName(),
      audio_mode: target.audio_mode || "full_duplex",
      microphone_anti_alias: this._microphoneAntiAliasEnabled(),
      softphone: true,
    };
    this._activeDeviceInfo = sessionInfo;
    let sendVideo = Boolean(
      this._softphoneSupportsVideo() &&
      this._softphoneSnapshot?.video_camera_send_enabled &&
      this._softphoneSnapshot?.send_video
    );
    if (sendVideo) {
      sendVideo = await voipStackEngine.prepareVideoCameraPermission({
        endpointId: this._getSoftphoneEndpointId(),
      });
    }
    if (operationId !== this._callOperationId) return;
    const reply = await voipStackEngine.startHaSoftphone(target, sessionInfo, {
      ...scope,
      callee,
      sendVideo,
      // Card recreation and dashboard navigation do not cancel a SIP call:
      // media ownership lives in the page-level engine.  Only a newer user
      // operation on this card may supersede the start transaction.
      shouldAbort: () => operationId !== this._callOperationId,
    });
    if (reply && !reply.superseded && operationId === this._callOperationId) {
      this._applySoftphoneSnapshot(reply);
    }
  }

  async _answer(options = {}) {
    if (this._starting || this._stopping) return;
    const softphoneAction = this._isHaSoftphoneMode();
    const callId = softphoneAction
      ? String(options.callId || this._sessionCallId() || "")
      : "";
    if (softphoneAction && !callId) return;
    const mediaIntentToken = softphoneAction ? {} : null;
    if (
      softphoneAction &&
      (
        this._otherPhoneOwnsBrowserMedia() ||
        !voipStackEngine.tryAcquireMediaIntent(
          this._getSoftphoneEndpointId(),
          mediaIntentToken,
        )
      )
    ) {
      this._showError("This browser is already handling another phone call.");
      return;
    }
    const operationId = ++this._callOperationId;
    this._starting = true;
    this._errorMsg = "";
    this._render();
    let claimedSoftphoneMedia = false;

    try {
      const deviceInfo = await this._getDeviceInfo();
      if (operationId !== this._callOperationId) return;
      if (!deviceInfo?.device_id) throw new Error("Device not found");
      this._activeDeviceInfo = deviceInfo;
      if (softphoneAction) {
        const snapshotState = String(this._softphoneSnapshot?.state || "").toLowerCase();
        if (
          this._sessionCallId() !== callId ||
          !["ringing", "incoming"].includes(snapshotState)
        ) return;
        const wantsVideo = Boolean(
          this._softphoneSupportsVideo() &&
          this._softphoneSnapshot?.video_camera_send_enabled &&
          this._softphoneSnapshot?.send_video
        );
        // A peer such as Wildix commonly establishes audio first and adds
        // video with an in-dialog re-INVITE.  A manual answer must preserve
        // the user's existing Send Camera choice for that later offer; auto
        // answer still supplies its separately preflighted permission.
        const sendVideo = wantsVideo
          ? typeof options.videoPermission === "boolean"
            ? options.videoPermission
            : await voipStackEngine.prepareVideoCameraPermission({
                endpointId: this._getSoftphoneEndpointId(),
              })
          : false;
        if (
          operationId !== this._callOperationId ||
          this._sessionCallId() !== callId ||
          !["ringing", "incoming"].includes(
            String(this._softphoneSnapshot?.state || "").toLowerCase()
          )
        ) return;
        this._activeDeviceInfo = {
          ...(deviceInfo || {}),
          ...this._softphoneRequestScope(),
          device_id: this._getConfigDeviceId(),
          softphone: true,
        };
        const alreadyOwned = voipStackEngine.ownsSoftphoneSession(
          callId,
          this._getSoftphoneEndpointId(),
        );
        this._markSoftphoneMediaOwner(callId);
        claimedSoftphoneMedia = !alreadyOwned;
        await this._hass.callService("voip_stack", "answer", {
          ...this._softphoneServiceScope(),
          call_id: callId,
          send_video: sendVideo,
          media_client_id: voipStackEngine.mediaClientId,
        });
        // The page-level engine, not this transient Lovelace element, owns
        // the media session.  If HA recreates the card while the service call
        // is in flight, the replacement adopts the authoritative backend
        // state; the detached element must never compensate with a hangup.
        return;
      }

      await this._pressEspButton(this._callButtonEntityId, "Call");
    } catch (err) {
      if (operationId !== this._callOperationId) return;
      this._showError(err.message || String(err));
      const endpointId = this._getSoftphoneEndpointId();
      if (
        claimedSoftphoneMedia &&
        voipStackEngine.ownsSoftphoneSession(callId, endpointId) &&
        !(
          voipStackEngine.active &&
          voipStackEngine.endpointId === endpointId &&
          voipStackEngine.callId === callId
        )
      ) {
        voipStackEngine.releaseSoftphoneSession(callId, endpointId);
      }
    } finally {
      if (mediaIntentToken) voipStackEngine.releaseMediaIntent(mediaIntentToken);
      if (operationId === this._callOperationId) {
        this._starting = false;
        this._ensureHaSoftphoneAudioPath(this._softphoneSnapshot || {});
        this._render();
      }
    }
  }

  async _decline() {
    if (this._stopping) return;
    const softphoneAction = this._isHaSoftphoneMode();
    const callId = softphoneAction ? String(this._sessionCallId() || "") : "";
    if (softphoneAction && !callId) return;
    const operationId = ++this._callOperationId;
    this._starting = false;
    this._stopping = true;
    this._errorMsg = "";
    this._render();

    try {
      const deviceInfo = await this._getDeviceInfo();
      if (operationId !== this._callOperationId) return;
      if (!deviceInfo?.device_id) throw new Error("Device not found");
      if (softphoneAction) {
        if (this._sessionCallId() !== callId) return;
        await this._hass.callService("voip_stack", "decline", {
          ...this._softphoneServiceScope(),
          call_id: callId,
          status: 603,
          reason: "Decline",
          decline_reason: "declined",
        });
      } else {
        await this._pressEspButton(this._declineButtonEntityId, "Decline");
      }
    } catch (err) {
      if (operationId !== this._callOperationId) return;
      this._showError(err.message || String(err));
    } finally {
      if (operationId === this._callOperationId) {
        this._stopping = false;
        if (softphoneAction) await this._loadSoftphoneState();
        this._render();
      }
    }
  }

  async _hangup() {
    if (this._stopping) return;
    const wasSoftphone = this._isSoftphoneContext();
    const operationId = ++this._callOperationId;
    const callId = wasSoftphone ? String(this._sessionCallId() || "") : "";
    this._starting = false;
    this._stopping = true;
    this._errorMsg = "";
    if (wasSoftphone && callId) {
      // Shed camera/decode/WebSocket work before waiting on HA call control.
      // The engine keeps the SIP/audio claim, so a rejected request remains
      // visible and Hangup can be retried.
      void voipStackEngine.suspendVideoForHangup(
        callId,
        this._getSoftphoneEndpointId(),
      );
    }
    this._render();
    let hangupSucceeded = false;

    try {
      const deviceInfo = this._activeDeviceInfo || await this._getDeviceInfo();
      if (!deviceInfo?.device_id) {
        throw new Error("Device not found");
      }
      this._activeDeviceInfo = deviceInfo;

      if (wasSoftphone) {
        await settleServiceWithin(
          this._hass.callService("voip_stack", "hangup", {
            ...this._softphoneServiceScope(),
            call_id: callId,
          }),
          HANGUP_SERVICE_TIMEOUT_MS,
          "Hangup request timed out; you can retry.",
        );
      } else {
        // Mirror mode: Hangup is the ESP's Decline button. Firmware maps
        // decline during in_call to stop(), and idle is a no-op.
        await this._pressEspButton(this._declineButtonEntityId, "Decline");
      }
      hangupSucceeded = true;
    } catch (err) {
      console.error("Hangup error:", err);
      this._showError(err.message || String(err));
    }

    if (wasSoftphone && hangupSucceeded) {
      const endpointId = this._getSoftphoneEndpointId();
      const ownedCallId = String(voipStackEngine.softphoneCallIdFor(endpointId) || "");
      if (!ownedCallId || ownedCallId === callId) {
        // Relinquish the authoritative call before close() emits its local
        // IDLE transition. Otherwise the controller listener can reconcile
        // the still-live backend snapshot and immediately reattach call A
        // while an intentional hangup is tearing it down.
        voipStackEngine.releaseSoftphoneSession(callId, endpointId);
        if (
          voipStackEngine.endpointId === endpointId &&
          (!callId || voipStackEngine.callId === callId)
        ) void voipStackEngine.close("hangup");
      }
      else voipStackEngine.releaseSoftphoneSession(callId, endpointId);
      if (operationId === this._callOperationId) await this._loadSoftphoneState();
    } else if (wasSoftphone && operationId === this._callOperationId) {
      // The service may not have reached HA, or its reply may have been lost.
      // Keep the local media claim until the authoritative snapshot settles it
      // so the user can retry Hangup instead of becoming a silent spectator.
      await this._loadSoftphoneState();
    }

    if (operationId === this._callOperationId) {
      this._stopping = false;
      this._render();
    }
  }

  async _cleanup() {
    const wasSoftphone = this._isSoftphoneContext();
    const callId = wasSoftphone ? String(this._sessionCallId() || "") : "";
    const endpointId = wasSoftphone ? this._getSoftphoneEndpointId() : "";
    if (
      callId &&
      (
        (voipStackEngine.endpointId === endpointId && voipStackEngine.callId === callId) ||
        voipStackEngine.ownsSoftphoneSession(callId, endpointId)
      )
    ) {
      voipStackEngine.releaseSoftphoneSession(callId, endpointId);
      if (voipStackEngine.endpointId === endpointId && voipStackEngine.callId === callId) {
        await voipStackEngine.close("card_cleanup");
      }
    }
    this._activeDeviceInfo = null;
    if (wasSoftphone) {
      this._softphoneSnapshot = null;
      this._activeSessionDeviceId = null;
    }
  }

  async _tryAutoAnswer(options = {}) {
    const requirePersistentPermission = options.requirePersistentPermission !== false;
    const softphoneAction = this._isHaSoftphoneMode();
    const lifecycleGeneration = this._lifecycleGeneration;
    const callId = softphoneAction
      ? String(options.callId || this._sessionCallId() || "")
      : "";
    // Check if browser has persistent mic permission
    try {
      const audioDirection = String(
        this._softphoneSnapshot?.audio_direction || "sendrecv"
      ).toLowerCase();
      const needsMicrophone = !softphoneAction ||
        ["sendonly", "sendrecv"].includes(audioDirection);
      if (
        requirePersistentPermission &&
        needsMicrophone &&
        !this._autoAnswerMicReady
      ) {
        if (!navigator.permissions?.query) {
          _voip_log.info("voip: auto-answer skipped, persistent mic permission unavailable");
          return;
        }
        const perm = await navigator.permissions.query({ name: "microphone" });
        if (lifecycleGeneration !== this._lifecycleGeneration) return;
        if (perm.state !== "granted") {
          _voip_log.info("voip: auto-answer skipped, mic permission not persistent");
          return;
        }
        this._autoAnswerMicReady = true;
      }
      let videoPermission;
      if (
        softphoneAction &&
        this._softphoneSupportsVideo() &&
        this._softphoneSnapshot?.video_offered &&
        this._softphoneSnapshot?.video_camera_send_enabled &&
        this._softphoneSnapshot?.send_video
      ) {
        videoPermission = await voipStackEngine.prepareVideoCameraPermission({
          persistentOnly: true,
          endpointId: this._getSoftphoneEndpointId(),
        });
        if (lifecycleGeneration !== this._lifecycleGeneration) return;
      }
      if (softphoneAction) {
        if (
          lifecycleGeneration !== this._lifecycleGeneration ||
          !this._isSoftphoneController()
        ) return;
        const state = String(this._softphoneSnapshot?.state || "").toLowerCase();
        if (
          !callId ||
          this._sessionCallId() !== callId ||
          !["ringing", "incoming"].includes(state)
        ) return;
      }
      // permissions.query not available or permission granted: try answering
      _voip_log.info("voip: auto-answering call");
      await this._answer({ callId, videoPermission });
    } catch (e) {
      console.warn("voip: auto-answer failed", e);
    } finally {
      if (!softphoneAction || !callId || this._autoAnswerCallId === callId) {
        this._autoAnswering = false;
        this._autoAnswerCallId = "";
      }
      if (this.isConnected) this._render();
    }
  }

  async _toggleAutoAnswer() {
    this._settingsOpen = true;
    if (!this._isHaSoftphoneMode() && this._autoAnswerSwitchEntityId) {
      const next = this._entityState(this._autoAnswerSwitchEntityId).toLowerCase() !== "on";
      this._autoAnswer = next;
      this._render();
      try {
        await this._setSwitchEntity(this._autoAnswerSwitchEntityId, next);
      } catch (err) {
        this._autoAnswer = !next;
        this._showError(err.message || String(err));
      }
      this._render();
      return;
    }
    const permissionGeneration = ++this._autoAnswerPermissionGeneration;
    if (this._autoAnswer || this._autoAnswerPermissionPending) {
      this._autoAnswerPermissionPending = false;
      this._autoAnswerMicReady = false;
      const previous = this._autoAnswer;
      this._autoAnswer = false;
      this._render();
      try {
        await this._hass.callService("voip_stack", "set_auto_answer", {
          ...this._softphoneServiceScope(),
          auto_answer: false,
        });
      } catch (err) {
        this._autoAnswer = previous;
        this._showError(err.message || String(err));
      }
      this._render();
      return;
    }

    // A HA browser phone normally answers with sendrecv audio. Acquire and
    // release the microphone while this user gesture is active, then enable
    // Auto Answer only after the browser proved that future media attachment
    // can succeed. The remote phone selected in the dialer is irrelevant.
    if (this._isHaSoftphoneMode()) {
      if (!navigator.mediaDevices?.getUserMedia) {
        this._showError("Auto Answer requires browser microphone access.");
        this._render();
        return;
      }
      this._autoAnswerPermissionPending = true;
      this._render();
      let stream = null;
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        if (permissionGeneration !== this._autoAnswerPermissionGeneration) return;
        const microphoneReady = Boolean(stream?.getAudioTracks?.()[0]);
        if (!microphoneReady) {
          throw new Error("The browser did not provide a microphone track.");
        }
        this._autoAnswerMicReady = true;
        await this._hass.callService("voip_stack", "set_auto_answer", {
          ...this._softphoneServiceScope(),
          auto_answer: true,
        });
        if (permissionGeneration !== this._autoAnswerPermissionGeneration) return;
        this._autoAnswer = true;
        _voip_log.info("voip: mic permission granted for auto-answer");
      } catch (err) {
        this._autoAnswerMicReady = false;
        this._autoAnswer = false;
        this._showError(
          `Auto Answer was not enabled: ${err?.message || String(err)}`,
        );
      } finally {
        for (const track of stream?.getTracks?.() || []) track.stop();
        if (permissionGeneration === this._autoAnswerPermissionGeneration) {
          this._autoAnswerPermissionPending = false;
        }
      }
      if (this._autoAnswer) {
        this._maybeAutoAnswer(this._softphoneSnapshot || {});
      }
      this._render();
      return;
    }

    this._autoAnswer = true;
    this._render();
  }

  _toggleSettings() {
    this._settingsOpen = !this._settingsOpen;
    this._render();
  }

  _toggleRingtone() {
    this._settingsOpen = true;
    this._ringtoneEnabled = !this._ringtoneEnabled;
    const deviceId = this._phonePreferenceStorageId();
    if (deviceId) {
      try {
        localStorage.setItem(`voip_ringtone_${deviceId}`, this._ringtoneEnabled.toString());
      } catch (_) {}
    }
    if (this._ringtoneEnabled) voipStackEngine.unlockRingtone();
    this._syncRingtoneRequest(this._getEspState());
    this._render();
  }

  _toggleMicrophoneAntiAlias() {
    this._settingsOpen = true;
    this._micAntiAliasEnabled = !this._micAntiAliasEnabled;
    const deviceId = this._phonePreferenceStorageId();
    if (deviceId) {
      try {
        localStorage.setItem(
          `voip_microphone_anti_alias_${deviceId}`,
          this._micAntiAliasEnabled.toString(),
        );
      } catch (_) {}
    }
    this._render();
  }

  async _toggleVideoCamera(enabled) {
    this._settingsOpen = true;
    const next = Boolean(enabled);
    const previous = !!this._softphoneSnapshot?.send_video;
    if (this._softphoneSnapshot) this._softphoneSnapshot.send_video = next;
    this._render();
    try {
      if (next) {
        const permitted = await voipStackEngine.prepareVideoCameraPermission({
          endpointId: this._getSoftphoneEndpointId(),
        });
        if (!permitted) throw new Error("Browser camera access was not granted.");
      }
      await this._hass.callService("voip_stack", "set_send_video", {
        ...this._softphoneServiceScope(),
        send_video: next,
      });
    } catch (err) {
      if (this._softphoneSnapshot) this._softphoneSnapshot.send_video = previous;
      this._showError(err.message || String(err));
    }
    this._render();
  }

  async _toggleDnd() {
    this._settingsOpen = true;
    const espMode = !this._isHaSoftphoneMode();
    const next = espMode
      ? this._entityState(this._dndSwitchEntityId).toLowerCase() !== "on"
      : !this._softphoneDnd;
    if (espMode) {
      this._render();
      try {
        await this._setSwitchEntity(this._dndSwitchEntityId, next);
      } catch (err) {
        this._showError(err.message || String(err));
      }
      this._render();
      return;
    }
    this._softphoneDnd = next;
    this._render();
    try {
      await this._hass.callService("voip_stack", "set_dnd", {
        ...this._softphoneServiceScope(),
        dnd: next,
      });
      await this._loadSoftphoneState();
    } catch (err) {
      this._softphoneDnd = !next;
      this._showError(err.message || String(err));
    }
    this._render();
  }

  _populateGroupSuggestions(input, datalist, groups, selected) {
    if (!input) return;
    const current = String(selected || "").trim();
    const options = [...groups];
    const wanted = JSON.stringify(options);
    if (datalist && datalist.dataset.options !== wanted) {
      datalist.replaceChildren(
        ...options.map(name => {
          const option = document.createElement("option");
          option.value = name;
          option.textContent = name;
          return option;
        })
      );
      datalist.dataset.options = wanted;
    }
    if (input.value !== current) input.value = current;
  }

  _renderGroupControls() {
    const els = this._els || {};
    const ringGroups = this._availableSoftphoneGroups("ring");
    const conferenceGroups = this._availableSoftphoneGroups("conference");
    const softphoneMode = this._isHaSoftphoneMode();
    const extension = softphoneMode ? this._softphoneExtension : this._entityState(this._extensionTextEntityId);
    const ringGroup = softphoneMode ? this._softphoneGroups.ring_group : this._entityState(this._ringGroupsTextEntityId);
    const conferenceGroup = softphoneMode ? this._softphoneGroups.conference_group : this._entityState(this._conferenceGroupsTextEntityId);
    const conferenceRing = softphoneMode
      ? !!this._softphoneGroups.conference_ring
      : this._entityState(this._conferenceRingSwitchEntityId).toLowerCase() === "on";
    if (els.extensionRow) els.extensionRow.hidden = !softphoneMode && !this._extensionTextEntityId;
    if (els.extensionInput) {
      els.extensionInput.disabled = !softphoneMode && !this._extensionTextEntityId;
      if (els.extensionInput.value !== extension) els.extensionInput.value = extension;
    }
    if (els.ringGroupInput) els.ringGroupInput.disabled = !softphoneMode && !this._ringGroupsTextEntityId;
    if (els.conferenceGroupInput) els.conferenceGroupInput.disabled = !softphoneMode && !this._conferenceGroupsTextEntityId;
    this._populateGroupSuggestions(els.ringGroupInput, els.ringGroupOptions, ringGroups, ringGroup);
    this._populateGroupSuggestions(els.conferenceGroupInput, els.conferenceGroupOptions, conferenceGroups, conferenceGroup);
    if (els.conferenceRingCheckbox) {
      els.conferenceRingCheckbox.checked = conferenceRing;
      els.conferenceRingCheckbox.disabled =
        !conferenceGroup || (!softphoneMode && !this._conferenceRingSwitchEntityId);
    }
  }

  async _setGroupSetting(key, value) {
    if (this._isHaSoftphoneMode()) {
      await this._setHaSoftphoneSettings({ [key]: value });
      return;
    }
    this._settingsOpen = true;
    try {
      if (key === "ring_group") {
        await this._setTextEntity(this._ringGroupsTextEntityId, value);
      } else if (key === "conference_group") {
        await this._setTextEntity(this._conferenceGroupsTextEntityId, value);
        if (!String(value || "").trim() && this._conferenceRingSwitchEntityId) {
          await this._setSwitchEntity(this._conferenceRingSwitchEntityId, false);
        }
      } else if (key === "conference_ring") {
        await this._setSwitchEntity(this._conferenceRingSwitchEntityId, !!value);
      }
    } catch (err) {
      this._showError(err.message || String(err));
    }
    this._render();
  }

  async _setExtensionSetting(value) {
    if (this._isHaSoftphoneMode()) {
      await this._setHaSoftphoneSettings({ extension: value });
      return;
    }
    this._settingsOpen = true;
    try {
      await this._setTextEntity(this._extensionTextEntityId, value);
    } catch (err) {
      this._showError(err.message || String(err));
    }
    this._render();
  }

  async _setHaSoftphoneSettings(patch) {
    if (!this._isHaSoftphoneMode() || !this._hass?.connection) return;
    if (!this._canConfigureHaSoftphone()) {
      this._showError("Administrator privileges are required to configure this phone.");
      return;
    }
    this._settingsOpen = true;
    const previousExtension = this._softphoneExtension;
    const previousGroups = { ...this._softphoneGroups };
    if (Object.prototype.hasOwnProperty.call(patch, "extension")) {
      this._softphoneExtension = String(patch.extension || "").trim();
    }
    const { extension: _extension, ...groupPatch } = patch;
    this._softphoneGroups = { ...previousGroups, ...groupPatch };
    if (!this._softphoneGroups.conference_group) this._softphoneGroups.conference_ring = false;
    this._render();
    try {
      await this._hass.callService("voip_stack", "set_ha_softphone_settings", {
        ...this._softphoneServiceScope(),
        extension: this._softphoneExtension,
        ring_group: this._softphoneGroups.ring_group,
        conference_group: this._softphoneGroups.conference_group,
        conference_ring: !!this._softphoneGroups.conference_ring,
      });
      await this._loadSoftphoneState();
    } catch (err) {
      this._softphoneExtension = previousExtension;
      this._softphoneGroups = previousGroups;
      this._showError(err.message || String(err));
    }
    this._render();
  }

  async _loadSoftphoneState() {
    if (!this._hass?.connection || this._softphoneStateLoading) return;
    const connection = this._hass.connection;
    const requestEpoch = this._softphoneStateEpoch;
    const lifecycleGeneration = this._lifecycleGeneration;
    this._softphoneStateLoading = true;
    try {
      const request = {
        type: "voip_stack/ha_softphone_state",
        ...this._softphoneRequestScope(),
      };
      const result = await connection.sendMessagePromise(request);
      if (!this._isHaSoftphoneMode() || this._hass?.connection !== connection) return;
      if (lifecycleGeneration !== this._lifecycleGeneration) return;
      if (this._softphoneStateEpoch !== requestEpoch) return;
      const snapshot = result || { state: "idle" };
      if (!this._softphoneSnapshotMatches(snapshot)) return;
      if (!this._applySoftphoneSnapshot(snapshot)) return;
      // The WebSocket subscription can publish its initial state before HA
      // recreates this card. A direct state load must therefore drive the same
      // media attachment path, especially after an in-call page reload.
      this._ensureHaSoftphoneAudioPath(this._softphoneSnapshot || snapshot);
      this._softphoneStateLoaded = true;
    } catch (err) {
      if (!this._isUnknownCommandError(err)) console.warn("voip: failed loading HA softphone state", err);
    } finally {
      this._softphoneStateLoading = false;
      this._render();
    }
  }

  _cycleSoftphoneTarget(delta) {
    const targets = this._softphoneTargets();
    if (targets.length === 0) return;
    const current = this._getSoftphoneTargetDevice();
    const idx = Math.max(0, targets.findIndex(d => d.device_id === current?.device_id));
    const next = targets[(idx + delta + targets.length) % targets.length];
    this._softphoneTargetDeviceId = next.device_id;
    this._render();
  }

  async _getDeviceInfo() {
    try {
      if (this._isHaSoftphoneMode()) {
        const scope = this._softphoneRequestScope();
        return {
          ...scope,
          device_id: scope.device_id || "",
          name: this._getHaName(),
          audio_mode: "full_duplex",
          softphone: true,
        };
      }
      const expectedSelector = this._getConfigSelector();
      const result = await this._hass.connection.sendMessagePromise({
        type: "voip_stack/resolve_device",
        device_id: expectedSelector,
      });
      if (this._isHaSoftphoneMode() || expectedSelector !== this._getConfigSelector()) return null;
      if (result?.device?.device_id) this._resolvedDeviceId = result.device.device_id;
      return result?.device || null;
    } catch (err) {
      if (!this._isUnknownCommandError(err)) console.error("Failed to get device info:", err);
    }
    return null;
  }

  _showError(msg) {
    this._errorMsg = msg || "";
    if (this._els?.err) this._els.err.textContent = this._errorMsg;
  }

  getGridOptions() {
    return this._isPhonebookMode()
      ? { columns: 12, rows: 7, min_columns: 4, min_rows: 3, max_rows: 8 }
      : { columns: 12, rows: 7, min_columns: 6, min_rows: 4, max_rows: 8 };
  }

  getCardSize() { return 7; }

  static getConfigElement() {
    return document.createElement("voip-stack-card-editor");
  }

  static getStubConfig() {
    return {};
  }
}

// Idempotent define so HMR / re-installs don't throw.
if (!customElements.get("voip-stack-card")) {
  customElements.define("voip-stack-card", VoipStackCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some(card => card.type === "voip-stack-card")) {
  window.customCards.push({
    type: "voip-stack-card",
    name: "VoIP Stack Card",
    description: "ESP SIP phone mirror and HA SIP softphone controls",
    preview: true,
    getEntitySuggestion: (hass, entityId) => {
      const registryEntry = hass?.entities?.[entityId];
      const state = hass?.states?.[entityId];
      const deviceId = String(registryEntry?.device_id || "").trim();
      const device = deviceId ? hass?.devices?.[deviceId] : null;
      const deviceModel = String(device?.model || "").trim().toLowerCase();
      const endpointId = String(state?.attributes?.endpoint_id || "").trim();
      let endpointKind = String(state?.attributes?.endpoint_kind || "")
        .trim()
        .toLowerCase();
      const isPrimaryPhoneSensor =
        registryEntry?.translation_key === "phone_endpoint_call_state";
      // The migrated default browser phone predates translated per-phone
      // entities. Its call-state sensor has no translation_key, so recognise
      // only the fully-qualified combination observed in HA's registries.
      const isDefaultBrowserCallState =
        !registryEntry?.translation_key &&
        endpointKind === "browser" &&
        !!endpointId &&
        deviceModel === "home assistant softphone";
      if (
        !String(entityId || "").startsWith("sensor.") ||
        registryEntry?.platform !== "voip_stack" ||
        !deviceId ||
        (!isPrimaryPhoneSensor && !isDefaultBrowserCallState)
      ) return null;

      // A freshly-created entity can still be unavailable when the picker is
      // opened. Infer only device models whose meaning is unambiguous; never
      // perform a WebSocket lookup from this synchronous picker callback.
      if (!endpointKind) {
        if (deviceModel === "home assistant softphone") endpointKind = "browser";
        else if (deviceModel === "sip account") endpointKind = "sip_account";
        else if (deviceModel) endpointKind = "esphome";
      }
      if (!["browser", "esphome"].includes(endpointKind)) return null;
      return {
        config: {
          type: "custom:voip-stack-card",
          mode: endpointKind === "browser" ? "ha_softphone" : "esp_mirror",
          device_id: deviceId,
        },
      };
    },
  });
}
