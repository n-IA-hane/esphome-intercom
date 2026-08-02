const HA_SOFTPHONE_DEVICE_ID = "__voip_stack_ha_softphone__";
const DEFAULT_SOFTPHONE_ENDPOINT_ID = "default";
const WS_AUDIO = 1;
const WS_SUBSCRIBE_CALL_EVENTS = "voip_stack/subscribe_call_events";
const WS_SUBSCRIBE_HA_SOFTPHONE = "voip_stack/subscribe_ha_softphone_state";
const MODULE_VERSION = (() => {
  try {
    return new URL(import.meta.url).searchParams.get("v") || "dev";
  } catch (_) {
    return "dev";
  }
})();
const { RINGTONE_REPEAT_MS, playVoipRingtone } =
  await import(`./ringtone.js?v=${encodeURIComponent(MODULE_VERSION)}`);
const {
  desiredAudioPaths,
  normaliseAudioDirection,
  normaliseAudioMode,
  parsePcmFormat,
  resolveSessionFormats,
  sameAudioFormat,
} = await import(`./voip-stack-media-model.js?v=${encodeURIComponent(MODULE_VERSION)}`);
const {
  normaliseSoftphoneSelector,
  softphoneScopeKey,
  softphoneStateMatches,
} = await import(`./voip-stack-session-model.js?v=${encodeURIComponent(MODULE_VERSION)}`);
const CONTROL_ACK_TIMEOUT_MS = 3000;
const AUDIO_NEGOTIATION_TIMEOUT_MS = 3000;
const BUS_SUBSCRIBE_RETRY_MS = 2000;
const SOFTPHONE_MEDIA_SESSION_KEY = "voip_stack_owned_softphone_call";
const SOFTPHONE_MEDIA_SESSIONS_KEY = "voip_stack_owned_softphone_calls";
const MEDIA_CLIENT_GLOBAL_KEY = "__voipStackMediaClientId";
const MEDIA_CLIENT_SESSION_KEY = "voip_stack_media_client_id";
const MEDIA_RECONNECT_ATTEMPTS = 3;
const MEDIA_RECONNECT_DELAY_MS = 250;
const MEDIA_CLEANUP_TIMEOUT_MS = 750;
const MAX_AUDIO_WS_BUFFER_MS = 120;
const MIN_AUDIO_WS_BUFFER_FRAMES = 4;
const ACTIVE_SOFTPHONE_STATES = new Set([
  "calling",
  "remote_ringing",
  "ringing",
  "connecting",
  "in_call",
  "terminating",
]);
const TERMINAL_CALL_EVENT_TYPES = new Set([
  "ended",
  "missed",
  "declined",
  "failed",
]);

function settleWithin(promise, timeoutMs = MEDIA_CLEANUP_TIMEOUT_MS) {
  let timer;
  const schedule = globalThis.setTimeout || globalThis.window?.setTimeout;
  const cancel = globalThis.clearTimeout || globalThis.window?.clearTimeout;
  if (!schedule) return Promise.resolve(promise).catch(() => {});
  return Promise.race([
    Promise.resolve(promise).catch(() => {}),
    new Promise((resolve) => {
      timer = schedule(resolve, timeoutMs);
    }),
  ]).finally(() => {
    if (timer && cancel) cancel(timer);
  });
}

function mediaClientInstanceId() {
  try {
    const existing = String(globalThis[MEDIA_CLIENT_GLOBAL_KEY] || "");
    if (/^[A-Za-z0-9][A-Za-z0-9._~-]{15,127}$/.test(existing)) return existing;
  } catch (_) {}
  try {
    const existing = String(sessionStorage.getItem(MEDIA_CLIENT_SESSION_KEY) || "");
    if (/^[A-Za-z0-9][A-Za-z0-9._~-]{15,127}$/.test(existing)) {
      try { globalThis[MEDIA_CLIENT_GLOBAL_KEY] = existing; } catch (_) {}
      return existing;
    }
  } catch (_) {}
  let generated = "";
  try {
    generated = globalThis.crypto?.randomUUID?.() || "";
  } catch (_) {}
  if (!generated) {
    // This token is collision resistance, not authentication: Home
    // Assistant signs the complete path including it before WebSocket use.
    generated = `tab-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
  }
  // Keep the media identity across a reload of this browsing context. The
  // backend pins every call to this value, so another device cannot race the
  // originating page and steal its microphone, camera, or RTP sockets.
  try { globalThis[MEDIA_CLIENT_GLOBAL_KEY] = generated; } catch (_) {}
  try { sessionStorage.setItem(MEDIA_CLIENT_SESSION_KEY, generated); } catch (_) {}
  return generated;
}

class VoipStackEngine extends EventTarget {
  constructor() {
    super();
    this._hass = null;
    this._ws = null;
    this._state = "IDLE";
    this._deviceId = "";
    this._endpointId = DEFAULT_SOFTPHONE_ENDPOINT_ID;
    this._callId = "";
    this._audioMode = "full_duplex";
    this._microphoneAntiAlias = true;
    this._audioDirection = "sendrecv";
    this._txFormat = null;
    this._rxFormat = null;
    this._lastSessionPayload = null;
    this._audioReady = false;
    this._audioSetupGeneration = 0;
    this._mediaStream = null;
    this._audioContext = null;
    this._captureNode = null;
    this._captureSink = null;
    this._source = null;
    this._playbackNode = null;
    this._stats = { sent: 0, received: 0, tx_dropped: 0, buffered_frames: 0, frames_drop: 0, underruns: 0 };
    this._busConnection = null;
    this._busUnsub = null;
    this._softphoneBusUnsub = null;
    this._busSubscribePending = false;
    this._softphoneBusSubscribePending = false;
    this._busSubscribeRetryTimer = null;
    this._callSubscribers = new Set();
    this._softphoneSubscribers = new Set();
    this._lastEvents = new Map();
    this._lastSoftphoneState = null;
    this._softphoneSubscriberSelectors = new Map();
    this._lastSoftphoneStates = new Map();
    this._softphoneScopeSubscriptions = new Map();
    this._controlWaiter = null;
    this._connectPromise = null;
    this._connectGeneration = 0;
    this._sessionAttachKey = "";
    this._sessionAttachPromise = null;
    this._mediaClientId = mediaClientInstanceId();
    this._mediaIntent = null;
    this._mediaRecoveryAttempts = new Set();
    // Media ownership belongs to the page-level engine, not to one Lovelace
    // element. Home Assistant may recreate a card while an outbound call is
    // ringing; the replacement must still be able to attach that call's media.
    try {
      this._ownedSoftphoneCallId = sessionStorage.getItem(SOFTPHONE_MEDIA_SESSION_KEY) || "";
      const stored = JSON.parse(sessionStorage.getItem(SOFTPHONE_MEDIA_SESSIONS_KEY) || "{}");
      this._ownedSoftphoneCalls = new Map(
        Object.entries(stored)
          .filter(([endpointId, callId]) => endpointId && callId)
          .map(([endpointId, callId]) => [String(endpointId), String(callId)]),
      );
      if (this._ownedSoftphoneCallId && !this._ownedSoftphoneCalls.has(DEFAULT_SOFTPHONE_ENDPOINT_ID)) {
        this._ownedSoftphoneCalls.set(DEFAULT_SOFTPHONE_ENDPOINT_ID, this._ownedSoftphoneCallId);
      }
    } catch (_) {
      this._ownedSoftphoneCallId = "";
      this._ownedSoftphoneCalls = new Map();
    }
    this._ringtoneRequests = new Map();
    this._ringtoneContext = null;
    this._ringtoneTimer = null;
    this._audioFrameBuffer = null;
    this._video = null;
    this._videoLoadPromise = null;
    this._videoCanvas = null;
    this._videoCanvasOwner = null;
    this._videoCanvasEndpointId = "";
    this._softphoneController = null;
    this._softphoneControllers = new Map();
    this._videoAttachGeneration = 0;
    this._videoAttachPromise = null;
    this._videoAttachCallId = "";
    this._videoHangupSuspendedKey = "";
    this._mediaCleanupPromise = null;
    this._ownedSessionReconcilePromise = null;
    this._pageHiding = false;

    window.addEventListener("pagehide", () => {
      this._pageHiding = true;
      this._ringtoneRequests.clear();
      this._stopRingtone();
      void this.close("pagehide");
    });
    window.addEventListener("pageshow", () => {
      this._pageHiding = false;
      this._emit();
    });
  }

  configure(hass) {
    this._hass = hass;
    if (this._video) this._video.configure(hass, this._mediaClientId);
    void this.reconcileOwnedSoftphoneSessions();
    const conn = hass?.connection || null;
    if (!conn) return;
    if (conn !== this._busConnection) {
      if (this._busUnsub) this._busUnsub();
      if (this._softphoneBusUnsub) this._softphoneBusUnsub();
      this._busUnsub = null;
      this._softphoneBusUnsub = null;
      this._busSubscribePending = false;
      this._softphoneBusSubscribePending = false;
      for (const record of this._softphoneScopeSubscriptions.values()) {
        try { record.unsub?.(); } catch (_) {}
        record.unsub = null;
        record.pending = false;
        record.invalid = false;
      }
      if (this._busSubscribeRetryTimer) clearTimeout(this._busSubscribeRetryTimer);
      this._busSubscribeRetryTimer = null;
      this._busConnection = conn;
    }
    this._ensureBusSubscriptions(conn);
  }

  _scheduleBusSubscriptionRetry(conn) {
    if (this._busConnection !== conn || this._busSubscribeRetryTimer) return;
    this._busSubscribeRetryTimer = setTimeout(() => {
      this._busSubscribeRetryTimer = null;
      if (this._busConnection === conn) this._ensureBusSubscriptions(conn);
    }, BUS_SUBSCRIBE_RETRY_MS);
  }

  _ensureBusSubscriptions(conn) {
    if (this._busConnection !== conn) return;
    if (!this._busUnsub && !this._busSubscribePending) {
      this._busSubscribePending = true;
      conn.subscribeMessage((event) => this._onBusEvent(event), { type: WS_SUBSCRIBE_CALL_EVENTS })
      .then((unsub) => {
        if (this._busConnection === conn) this._busUnsub = unsub;
        else unsub();
      })
      .catch((err) => {
        console.warn("voip-stack-engine: call_event subscription failed", err);
        this._scheduleBusSubscriptionRetry(conn);
      })
      .finally(() => {
        if (this._busConnection === conn) this._busSubscribePending = false;
      });
    }
    for (const record of this._softphoneScopeSubscriptions.values()) {
      this._ensureSoftphoneScopeSubscription(conn, record);
    }
  }

  _normaliseSoftphoneSelector(selector = {}) {
    return normaliseSoftphoneSelector(selector);
  }

  _softphoneScopeKey(selector = {}) {
    return softphoneScopeKey(selector);
  }

  _softphoneStateMatches(state, selector = {}, subscriptionSelector = null) {
    return softphoneStateMatches(state, selector, subscriptionSelector);
  }

  _isLegacySchemaError(err) {
    const code = String(err?.code || err?.error || "").toLowerCase();
    const message = String(err?.message || err || "").toLowerCase();
    return code.includes("invalid_format") || code.includes("unknown_command") ||
      message.includes("extra keys") || message.includes("not allowed") ||
      message.includes("unknown command");
  }

  _isUnknownEndpointError(err) {
    const code = String(err?.code || err?.error || "").toLowerCase();
    const message = String(err?.message || err || "").toLowerCase();
    return code.includes("unknown_endpoint") ||
      message.includes("unknown phone endpoint") ||
      message.includes("unknown endpoint");
  }

  _ensureSoftphoneScopeSubscription(conn, record) {
    if (!record || record.unsub || record.pending || record.invalid || this._busConnection !== conn) return;
    record.pending = true;
    const request = { type: WS_SUBSCRIBE_HA_SOFTPHONE };
    if (record.selector.endpoint_id) request.endpoint_id = record.selector.endpoint_id;
    if (record.selector.device_id) request.device_id = record.selector.device_id;
    const subscribe = (message, legacy = false) => conn.subscribeMessage(
      (event) => this._onSoftphoneState(event, record.selector),
      message,
    ).catch((err) => {
      if (!legacy && record.selector.endpoint_id === DEFAULT_SOFTPHONE_ENDPOINT_ID && this._isLegacySchemaError(err)) {
        return subscribe({ type: WS_SUBSCRIBE_HA_SOFTPHONE }, true);
      }
      throw err;
    });
    subscribe(request).then((unsub) => {
      if (this._busConnection === conn && this._softphoneScopeSubscriptions.get(record.key) === record) {
        record.unsub = unsub;
        if (record.selector.endpoint_id === DEFAULT_SOFTPHONE_ENDPOINT_ID) this._softphoneBusUnsub = unsub;
      } else {
        unsub();
      }
    }).catch((err) => {
      if (this._isUnknownEndpointError(err)) {
        record.invalid = true;
        this._onSoftphoneState({
          endpoint_id: record.selector.endpoint_id || "",
          device_id: record.selector.device_id || "",
          state: "unavailable",
          sip_state: "unavailable",
          availability: "unavailable",
          terminal_reason: "unknown_endpoint",
          subscription_error: "unknown_endpoint",
        }, record.selector);
        return;
      }
      console.warn("voip-stack-engine: HA softphone subscription failed", err);
      this._scheduleBusSubscriptionRetry(conn);
    }).finally(() => {
      record.pending = false;
      if (record.selector.endpoint_id === DEFAULT_SOFTPHONE_ENDPOINT_ID) {
        this._softphoneBusSubscribePending = false;
      }
    });
    if (record.selector.endpoint_id === DEFAULT_SOFTPHONE_ENDPOINT_ID) {
      this._softphoneBusSubscribePending = true;
    }
  }

  get active() {
    return this._state !== "IDLE";
  }

  get deviceId() {
    return this._deviceId;
  }

  get callId() {
    return this._callId;
  }

  get endpointId() {
    return this._endpointId;
  }

  get mediaCleanupPending() {
    return this._mediaCleanupPromise !== null;
  }

  _persistOwnedSoftphoneCalls() {
    const legacy = this._ownedSoftphoneCalls.get(DEFAULT_SOFTPHONE_ENDPOINT_ID) || "";
    this._ownedSoftphoneCallId = legacy;
    try {
      const entries = Object.fromEntries(this._ownedSoftphoneCalls);
      if (Object.keys(entries).length) {
        sessionStorage.setItem(SOFTPHONE_MEDIA_SESSIONS_KEY, JSON.stringify(entries));
      } else {
        sessionStorage.removeItem(SOFTPHONE_MEDIA_SESSIONS_KEY);
      }
      if (legacy) sessionStorage.setItem(SOFTPHONE_MEDIA_SESSION_KEY, legacy);
      else sessionStorage.removeItem(SOFTPHONE_MEDIA_SESSION_KEY);
    } catch (_) {}
  }

  claimSoftphoneSession(callId, endpointId = DEFAULT_SOFTPHONE_ENDPOINT_ID) {
    const endpoint = String(endpointId || DEFAULT_SOFTPHONE_ENDPOINT_ID);
    const wanted = String(callId || "");
    if (wanted) this._ownedSoftphoneCalls.set(endpoint, wanted);
    else this._ownedSoftphoneCalls.delete(endpoint);
    this._persistOwnedSoftphoneCalls();
  }

  ownsSoftphoneSession(callId, endpointId = DEFAULT_SOFTPHONE_ENDPOINT_ID) {
    const wanted = String(callId || "");
    return !!wanted && wanted === this._ownedSoftphoneCalls.get(String(endpointId || DEFAULT_SOFTPHONE_ENDPOINT_ID));
  }

  get softphoneCallId() {
    return this._ownedSoftphoneCallId;
  }

  softphoneCallIdFor(endpointId = DEFAULT_SOFTPHONE_ENDPOINT_ID) {
    return this._ownedSoftphoneCalls.get(String(endpointId || DEFAULT_SOFTPHONE_ENDPOINT_ID)) || "";
  }

  hasOwnedSoftphoneSessionForOtherEndpoint(
    endpointId = DEFAULT_SOFTPHONE_ENDPOINT_ID,
  ) {
    const selected = String(endpointId || DEFAULT_SOFTPHONE_ENDPOINT_ID);
    for (const [candidate, callId] of this._ownedSoftphoneCalls) {
      if (candidate !== selected && callId) return true;
    }
    return Boolean(
      this.active && this._endpointId && this._endpointId !== selected,
    );
  }

  tryAcquireMediaIntent(
    endpointId = DEFAULT_SOFTPHONE_ENDPOINT_ID,
    token = null,
  ) {
    if (!token) return false;
    const selected = String(endpointId || DEFAULT_SOFTPHONE_ENDPOINT_ID);
    if (this._mediaIntent) {
      return this._mediaIntent.token === token;
    }
    if (this.hasOwnedSoftphoneSessionForOtherEndpoint(selected)) return false;
    this._mediaIntent = { endpointId: selected, token };
    return true;
  }

  releaseMediaIntent(token) {
    if (!token || this._mediaIntent?.token !== token) return false;
    this._mediaIntent = null;
    return true;
  }

  releaseSoftphoneSession(callId = "", endpointId = DEFAULT_SOFTPHONE_ENDPOINT_ID) {
    const endpoint = String(endpointId || DEFAULT_SOFTPHONE_ENDPOINT_ID);
    const wanted = String(callId || "");
    if (!wanted || wanted === this._ownedSoftphoneCalls.get(endpoint)) {
      this._ownedSoftphoneCalls.delete(endpoint);
      this._persistOwnedSoftphoneCalls();
    }
  }

  async reconcileOwnedSoftphoneSessions() {
    if (
      this._ownedSessionReconcilePromise ||
      !this._hass?.callWS ||
      !this._ownedSoftphoneCalls.size
    ) return this._ownedSessionReconcilePromise;
    const claims = [...this._ownedSoftphoneCalls.entries()];
    const reconcile = (async () => {
      let changed = false;
      await Promise.all(claims.map(async ([endpointId, callId]) => {
        let state;
        try {
          state = await this._hass.callWS({
            type: "voip_stack/ha_softphone_state",
            endpoint_id: endpointId,
          });
        } catch (_) {
          // A network/config reload failure cannot prove the claim stale.
          return;
        }
        if (this._ownedSoftphoneCalls.get(endpointId) !== callId) return;
        const currentCallId = String(state?.call_id || "");
        const currentState = String(state?.state || "").toLowerCase();
        if (
          currentCallId === callId &&
          ACTIVE_SOFTPHONE_STATES.has(currentState)
        ) return;
        this.releaseSoftphoneSession(callId, endpointId);
        changed = true;
        if (
          this.active &&
          this._endpointId === endpointId &&
          this._callId === callId
        ) {
          await this.close("backend_session_ended");
        }
      }));
      if (changed) this._emit();
    })();
    this._ownedSessionReconcilePromise = reconcile;
    try {
      await reconcile;
    } finally {
      if (this._ownedSessionReconcilePromise === reconcile) {
        this._ownedSessionReconcilePromise = null;
      }
    }
  }

  async _mediaAttachState(callId, endpointId) {
    if (!this._hass?.callWS) return null;
    try {
      return await this._hass.callWS({
        type: "voip_stack/ha_softphone_state",
        endpoint_id: String(endpointId || DEFAULT_SOFTPHONE_ENDPOINT_ID),
        media_client_id: this._mediaClientId,
      });
    } catch (_) {
      // Failure to inspect ownership is not proof of an expected conflict.
      // In that case the normal media WebSocket error remains user-visible.
      return null;
    }
  }

  _mediaAttachNoLongerBelongsHere(snapshot, callId) {
    if (!snapshot) return false;
    const currentCallId = String(snapshot.call_id || "");
    const currentState = String(snapshot.state || "").toLowerCase();
    return (
      currentCallId !== String(callId || "") ||
      !ACTIVE_SOFTPHONE_STATES.has(currentState)
    );
  }

  _mediaAttachOwnedByOther(snapshot, callId) {
    if (!snapshot) return false;
    return (
      String(snapshot.media_owner || "") === "other" &&
      String(snapshot.call_id || "") === String(callId || "") &&
      ACTIVE_SOFTPHONE_STATES.has(String(snapshot.state || "").toLowerCase())
    );
  }

  tryRecoverSoftphoneSession(callId, endpointId = DEFAULT_SOFTPHONE_ENDPOINT_ID) {
    const wanted = String(callId || "").trim();
    const endpoint = String(endpointId || DEFAULT_SOFTPHONE_ENDPOINT_ID).trim() ||
      DEFAULT_SOFTPHONE_ENDPOINT_ID;
    if (!wanted || this._pageHiding) return false;
    if (
      this.active &&
      this._endpointId === endpoint &&
      this._callId === wanted
    ) return true;
    const attemptKey = `${endpoint}|${wanted}`;
    if (this._mediaRecoveryAttempts.has(attemptKey)) {
      return this.ownsSoftphoneSession(wanted, endpoint);
    }
    if (this.hasOwnedSoftphoneSessionForOtherEndpoint(endpoint)) return false;
    if (this._mediaRecoveryAttempts.size >= 256) {
      this._mediaRecoveryAttempts.delete(this._mediaRecoveryAttempts.values().next().value);
    }
    this._mediaRecoveryAttempts.add(attemptKey);
    this.claimSoftphoneSession(wanted, endpoint);
    return true;
  }

  claimSoftphoneController(owner, endpointId = DEFAULT_SOFTPHONE_ENDPOINT_ID) {
    if (!owner || owner.isConnected === false) return false;
    const endpoint = String(endpointId || DEFAULT_SOFTPHONE_ENDPOINT_ID);
    const controller = this._softphoneControllers.get(endpoint);
    if (controller && controller !== owner) return false;
    if (!controller) this._softphoneControllers.set(endpoint, owner);
    if (endpoint === DEFAULT_SOFTPHONE_ENDPOINT_ID) this._softphoneController = owner;
    return true;
  }

  releaseSoftphoneController(owner, endpointId = DEFAULT_SOFTPHONE_ENDPOINT_ID) {
    const endpoint = String(endpointId || DEFAULT_SOFTPHONE_ENDPOINT_ID);
    if (!owner || this._softphoneControllers.get(endpoint) !== owner) return false;
    this._softphoneControllers.delete(endpoint);
    if (endpoint === DEFAULT_SOFTPHONE_ENDPOINT_ID) this._softphoneController = null;
    this._emit();
    return true;
  }

  get stats() {
    return { ...this._stats, video: this._video?.stats || {} };
  }

  get videoActive() {
    return Boolean(this._video?.active);
  }

  get videoVisible() {
    return Boolean(this._video?.visible);
  }

  setVideoCanvas(canvas) {
    this._videoCanvas = canvas || null;
    if (this._video) this._video.setCanvas(this._videoCanvas);
  }

  claimVideoCanvas(owner, canvas, endpointId = DEFAULT_SOFTPHONE_ENDPOINT_ID) {
    if (!owner || owner.isConnected === false || !canvas) return false;
    const endpoint = String(endpointId || DEFAULT_SOFTPHONE_ENDPOINT_ID);
    if (
      this._videoCanvasOwner &&
      this._videoCanvasOwner !== owner &&
      this._videoCanvasEndpointId === endpoint
    ) return false;
    if (
      this._videoCanvasOwner &&
      this._videoCanvasOwner !== owner &&
      this._endpointId !== endpoint
    ) return false;
    this._videoCanvasOwner = owner;
    this._videoCanvasEndpointId = endpoint;
    this.setVideoCanvas(canvas);
    return true;
  }

  releaseVideoCanvas(owner) {
    if (!owner || this._videoCanvasOwner !== owner) return false;
    this._videoCanvasOwner = null;
    this._videoCanvasEndpointId = "";
    this.setVideoCanvas(null);
    return true;
  }

  get videoCanSend() {
    return Boolean(this._video?.canSend);
  }

  suspendVideoForHangup(
    callId,
    endpointId = DEFAULT_SOFTPHONE_ENDPOINT_ID,
  ) {
    const wantedCallId = String(callId || "");
    const endpoint = String(endpointId || DEFAULT_SOFTPHONE_ENDPOINT_ID);
    if (!wantedCallId) return Promise.resolve();
    if (
      this._callId &&
      (this._callId !== wantedCallId || this._endpointId !== endpoint)
    ) return Promise.resolve();
    if (
      this._video?.callId &&
      this._video.callId !== wantedCallId
    ) return Promise.resolve();
    this._videoHangupSuspendedKey = `${endpoint}|${wantedCallId}`;
    this._videoAttachGeneration++;
    this._videoAttachPromise = null;
    this._videoAttachCallId = "";
    // close() synchronously detaches the WebSocket, camera and decoder workers
    // before its first await. SIP/audio ownership remains authoritative so a
    // failed service request can still be retried.
    return this._video ? this._video.close() : Promise.resolve();
  }

  async prepareVideoCameraPermission({
    persistentOnly = false,
    endpointId: _endpointId = DEFAULT_SOFTPHONE_ENDPOINT_ID,
  } = {}) {
    if (!navigator.mediaDevices?.getUserMedia) return false;
    if (persistentOnly) {
      if (!navigator.permissions?.query) return false;
      try {
        const permission = await navigator.permissions.query({ name: "camera" });
        if (permission.state === "granted") return true;
        return false;
      } catch (_) {
        return false;
      }
    }
    // A manual call is allowed to request camera permission.  In particular,
    // Android/iOS companion WebViews may report `denied` or an unsupported
    // state through the Permissions API while getUserMedia() can still open
    // the camera through the native application permission.  The real media
    // acquisition is authoritative here, as it was before multi-phone
    // per-endpoint camera preferences were introduced.
    let stream = null;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: {
          width: { ideal: 640, max: 1280 },
          height: { ideal: 360, max: 720 },
          frameRate: { ideal: 15, max: 20 },
        },
        audio: false,
      });
      return Boolean(stream?.getVideoTracks?.()[0]);
    } catch (err) {
      console.warn("voip-stack-engine: camera permission unavailable; continuing receive-only", err);
      return false;
    } finally {
      for (const track of stream?.getTracks?.() || []) track.stop();
    }
  }

  async _loadVideo() {
    if (this._video) return this._video;
    if (!this._videoLoadPromise) {
      this._videoLoadPromise = import(`./voip-stack-video.js?v=${encodeURIComponent(MODULE_VERSION)}`)
        .then(({ VoipStackVideo }) => {
          const video = new VoipStackVideo();
          if (this._hass) video.configure(this._hass, this._mediaClientId);
          video.setCanvas(this._videoCanvas);
          video.addEventListener("state", () => this._emit());
          this._video = video;
          return video;
        })
        .finally(() => { this._videoLoadPromise = null; });
    }
    return this._videoLoadPromise;
  }

  statsText() {
    if (!this.active) return "";
    const video = this._video?.active
      ? ` | Video TX: ${this._video.stats.sent} RX: ${this._video.stats.received} Render: ${this._video.stats.rendered || 0} Drop: ${this._video.stats.dropped} Gap: ${Math.round(this._video.stats.max_frame_gap_ms || 0)}ms Src: ${Math.round(this._video.stats.max_source_gap_ms || 0)}ms Arr: ${Math.round(this._video.stats.max_arrival_gap_ms || 0)}ms Playout: ${Math.round(this._video.stats.playout_ms || 0)}ms`
      : "";
    return `Sent: ${this._stats.sent} | Recv: ${this._stats.received} | TxDrop: ${this._stats.tx_dropped || 0} | Buf: ${this._stats.buffered_frames} | Und: ${this._stats.underruns || 0}${video}`;
  }

  _emit() {
    this.dispatchEvent(new CustomEvent("state", {
      detail: {
        state: this._state,
        device_id: this._deviceId,
        endpoint_id: this._endpointId,
        stats: { ...this._stats },
      },
    }));
  }

  _setState(state) {
    const target = String(state || "").toUpperCase();
    if (target === this._state) return;
    this._state = target;
    this._emit();
  }

  _forceIdle() {
    this._state = "IDLE";
    if (!this._pageHiding) this._emit();
  }

  _onBusEvent(event) {
    const data = event?.data;
    if (!data) return;
    const eventType = String(data.type || "").toLowerCase();
    const eventState = String(data.state || "").toLowerCase();
    const terminal = TERMINAL_CALL_EVENT_TYPES.has(eventType) ||
      (!ACTIVE_SOFTPHONE_STATES.has(eventState) &&
        ["idle", "ended", "cancelled", "timeout", "failed", "declined", "busy"]
          .includes(eventState));
    const terminalCallId = String(data.call_id || "");
    const terminalEndpointId = String(data.endpoint_id || "");
    if (terminal && terminalCallId) {
      let released = false;
      for (const [endpointId, callId] of [...this._ownedSoftphoneCalls]) {
        if (
          callId !== terminalCallId ||
          (terminalEndpointId && endpointId !== terminalEndpointId)
        ) continue;
        this.releaseSoftphoneSession(callId, endpointId);
        released = true;
        if (
          this.active &&
          this._endpointId === endpointId &&
          this._callId === callId
        ) void this.close("terminal_event");
      }
      if (released) this._emit();
    }
    const scope = (data.scope || "").toLowerCase();
    for (const id of [data.device_id, data.source_device_id, data.dest_device_id, data.session_device_id]) {
      if (!id) continue;
      const key = `${id}|${scope}`;
      if (!this._lastEvents.has(key) && this._lastEvents.size >= 256) {
        this._lastEvents.delete(this._lastEvents.keys().next().value);
      }
      this._lastEvents.set(key, event);
    }
    for (const cb of this._callSubscribers) {
      try { cb(event); } catch (err) { console.error("voip-stack-engine subscriber", err); }
    }
  }

  subscribeCallEvents(cb) {
    this._callSubscribers.add(cb);
    for (const event of this._lastEvents.values()) {
      try { cb(event); } catch (err) { console.error("voip-stack-engine replay", err); }
    }
    return () => this._callSubscribers.delete(cb);
  }

  _onSoftphoneState(state, subscriptionSelector = null) {
    if (!state) return;
    const endpointId = String(state.endpoint_id || subscriptionSelector?.endpoint_id || "").trim();
    const deviceId = String(state.device_id || state.endpoint_device_id || subscriptionSelector?.device_id || "").trim();
    const key = endpointId ? `endpoint:${endpointId}` : deviceId ? `device:${deviceId}` : "endpoint:default";
    const snapshot = endpointId && !state.endpoint_id ? { ...state, endpoint_id: endpointId } : state;
    this._lastSoftphoneState = snapshot;
    this._lastSoftphoneStates.set(key, snapshot);
    for (const cb of this._softphoneSubscribers) {
      const selector = this._softphoneSubscriberSelectors.get(cb) || {};
      // The same logical endpoint can temporarily have both an endpoint-id
      // subscription and a legacy device-id subscription (for example while
      // an old dashboard and a newly configured card coexist). Dispatch only
      // through the subscription owned by this callback so a single backend
      // event cannot be delivered twice via overlapping selectors.
      if (
        subscriptionSelector &&
        this._softphoneScopeKey(selector) !== this._softphoneScopeKey(subscriptionSelector)
      ) continue;
      if (!this._softphoneStateMatches(snapshot, selector, subscriptionSelector)) continue;
      try { cb(snapshot); } catch (err) { console.error("voip-stack-engine softphone subscriber", err); }
    }
  }

  subscribeSoftphoneState(cb, selector = {}) {
    const normalised = this._normaliseSoftphoneSelector(selector);
    const key = this._softphoneScopeKey(normalised);
    this._softphoneSubscribers.add(cb);
    this._softphoneSubscriberSelectors.set(cb, normalised);
    let record = this._softphoneScopeSubscriptions.get(key);
    if (!record) {
      record = {
        key,
        selector: normalised,
        refs: 0,
        unsub: null,
        pending: false,
        invalid: false,
      };
      this._softphoneScopeSubscriptions.set(key, record);
    }
    record.refs++;
    if (this._busConnection) this._ensureSoftphoneScopeSubscription(this._busConnection, record);
    for (const state of this._lastSoftphoneStates.values()) {
      if (!this._softphoneStateMatches(state, normalised)) continue;
      try { cb(state); } catch (err) { console.error("voip-stack-engine softphone replay", err); }
    }
    return () => {
      this._softphoneSubscribers.delete(cb);
      this._softphoneSubscriberSelectors.delete(cb);
      record.refs = Math.max(0, record.refs - 1);
      if (record.refs) return;
      try { record.unsub?.(); } catch (_) {}
      if (this._softphoneScopeSubscriptions.get(key) === record) {
        this._softphoneScopeSubscriptions.delete(key);
      }
      if (normalised.endpoint_id === DEFAULT_SOFTPHONE_ENDPOINT_ID) {
        this._softphoneBusUnsub = null;
        this._softphoneBusSubscribePending = false;
      }
    };
  }

  setRingtoneRequest(key, active, enabled) {
    if (!key) return;
    const shouldRing = !!active && !!enabled;
    if (shouldRing) this._ringtoneRequests.set(key, true);
    else this._ringtoneRequests.delete(key);
    this._syncRingtone();
  }

  clearRingtoneRequest(key) {
    if (!key) return;
    this._ringtoneRequests.delete(key);
    this._syncRingtone();
  }

  unlockRingtone() {
    this._ensureRingtoneContext()
      ?.resume()
      .catch((err) => console.warn("voip-stack-engine: ringtone unlock failed", err));
  }

  _syncRingtone() {
    if (this._ringtoneRequests.size > 0) this._startRingtone();
    else this._stopRingtone();
  }

  _ensureRingtoneContext() {
    if (this._ringtoneContext) return this._ringtoneContext;
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) return null;
    this._ringtoneContext = new Ctor();
    return this._ringtoneContext;
  }

  _startRingtone() {
    if (this._ringtoneTimer !== null) return;
    const ctx = this._ensureRingtoneContext();
    if (!ctx) return;
    ctx.resume().catch((err) => console.warn("voip-stack-engine: ringtone resume failed", err));
    const tick = () => playVoipRingtone(ctx);
    tick();
    this._ringtoneTimer = window.setInterval(tick, RINGTONE_REPEAT_MS);
  }

  _stopRingtone() {
    if (this._ringtoneTimer !== null) {
      window.clearInterval(this._ringtoneTimer);
      this._ringtoneTimer = null;
    }
  }

  async _wsUrl(deviceId, callId, endpointId = DEFAULT_SOFTPHONE_ENDPOINT_ID) {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const path = `/api/voip_stack/ws?device_id=${encodeURIComponent(deviceId)}&endpoint_id=${encodeURIComponent(endpointId || DEFAULT_SOFTPHONE_ENDPOINT_ID)}&call_id=${encodeURIComponent(callId)}&client_id=${encodeURIComponent(this._mediaClientId)}`;
    const signed = await this._hass.callWS({ type: "auth/sign_path", path });
    return `${proto}//${window.location.host}${signed.path || path}`;
  }

  async _connect(deviceId, callId = "", endpointId = DEFAULT_SOFTPHONE_ENDPOINT_ID) {
    const wantedCallId = String(callId || "");
    const wantedEndpointId = String(endpointId || DEFAULT_SOFTPHONE_ENDPOINT_ID);
    if (
      this._ws &&
      this._deviceId === deviceId &&
      this._endpointId === wantedEndpointId &&
      this._callId === wantedCallId &&
      this._ws.readyState === WebSocket.OPEN
    ) return this._lastSessionPayload;
    if (
      this._connectPromise &&
      this._deviceId === deviceId &&
      this._endpointId === wantedEndpointId &&
      this._callId === wantedCallId &&
      this._ws &&
      this._ws.readyState === WebSocket.CONNECTING
    ) {
      return this._connectPromise;
    }
    const connectGeneration = ++this._connectGeneration;
    await this.close("switch", true, true);
    if (connectGeneration !== this._connectGeneration) {
      throw new Error("Audio WebSocket superseded before connect");
    }
    this._deviceId = deviceId;
    this._endpointId = wantedEndpointId;
    this._callId = wantedCallId;
    this._lastSessionPayload = null;
    const wsUrl = await this._wsUrl(deviceId, wantedCallId, wantedEndpointId);
    if (
      connectGeneration !== this._connectGeneration ||
      this._deviceId !== deviceId ||
      this._endpointId !== wantedEndpointId ||
      this._callId !== wantedCallId
    ) {
      throw new Error("Audio WebSocket superseded before connect");
    }
    const ws = new WebSocket(wsUrl);
    this._ws = ws;
    ws.binaryType = "arraybuffer";
    let helloResolve;
    let helloReject;
    let helloSettled = false;
    const settleHello = (method, value) => {
      if (helloSettled) return;
      helloSettled = true;
      method(value);
    };
    const hello = new Promise((resolve, reject) => {
      helloResolve = resolve;
      helloReject = reject;
    });
    // onclose can reject this before OPEN has completed. Observe it now so a
    // losing connect generation never creates an unhandled rejection.
    void hello.catch(() => {});
    ws.onmessage = (event) => {
      if (this._ws !== ws) return;
      if (typeof event.data === "string") {
        try {
          const payload = JSON.parse(event.data);
          if (payload?.error) {
            settleHello(helloReject, new Error(payload.error));
          } else if (payload?.tx_format || payload?.selected_tx_format) {
            settleHello(helloResolve, payload);
          }
        } catch (_) {}
      }
      this._handleMessage(event);
    };
    let opened = false;
    const openedPromise = new Promise((resolve, reject) => {
      ws.onopen = () => {
        opened = true;
        if (this._ws === ws) resolve();
        else {
          try { ws.close(); } catch (_) {}
          reject(new Error("Audio WebSocket superseded"));
        }
      };
      ws.onerror = () => {
        const error = new Error("Audio WebSocket failed");
        settleHello(helloReject, error);
        reject(error);
      };
      ws.onclose = () => {
        const error = new Error(
          opened
            ? "Audio WebSocket closed before negotiation"
            : "Audio WebSocket closed before opening",
        );
        settleHello(helloReject, error);
        if (!opened) reject(error);
        if (this._ws !== ws) return;
        this._ws = null;
        void this._cleanupAudio("ws_close");
      };
    });
    const connectPromise = (async () => {
      await openedPromise;
      const negotiated = await Promise.race([
        hello,
        new Promise((_, reject) => window.setTimeout(
          () => reject(new Error("Audio WebSocket negotiation timed out")),
          AUDIO_NEGOTIATION_TIMEOUT_MS,
        )),
      ]);
      if (
        connectGeneration !== this._connectGeneration ||
        this._ws !== ws ||
        this._deviceId !== deviceId ||
        this._endpointId !== wantedEndpointId ||
        this._callId !== wantedCallId
      ) {
        throw new Error("Audio WebSocket superseded during negotiation");
      }
      return negotiated;
    })();
    this._connectPromise = connectPromise;
    try {
      return await connectPromise;
    } catch (err) {
      if (this._ws === ws) {
        this._ws = null;
        this._callId = "";
        try { ws.close(); } catch (_) {}
      }
      throw err;
    } finally {
      if (this._connectPromise === connectPromise) this._connectPromise = null;
    }
  }

  _sendControl(payload, waitForReply = false, acceptReply = null) {
    if (!this._ws || this._ws.readyState !== WebSocket.OPEN) {
      return waitForReply ? Promise.resolve(null) : null;
    }
    if (!waitForReply) {
      this._ws.send(JSON.stringify(payload));
      return null;
    }
    if (this._controlWaiter) {
      this._controlWaiter.resolve(null);
      this._controlWaiter = null;
    }
    const promise = new Promise((resolve) => {
      const timer = window.setTimeout(() => {
        if (this._controlWaiter?.resolve === resolve) this._controlWaiter = null;
        resolve(null);
      }, CONTROL_ACK_TIMEOUT_MS);
      this._controlWaiter = { resolve, timer, acceptReply };
    });
    this._ws.send(JSON.stringify(payload));
    return promise;
  }

  _resolveControlWaiter(msg) {
    if (!this._controlWaiter) return;
    const waiter = this._controlWaiter;
    if (waiter.acceptReply && !waiter.acceptReply(msg)) return;
    this._controlWaiter = null;
    window.clearTimeout(waiter.timer);
    waiter.resolve(msg);
  }

  _isTerminalControlReply(msg) {
    return !!msg?.error || ["in_call", "idle", "error"].includes(String(msg?.state || "").toLowerCase());
  }

  _sendAudio(buffer) {
    if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
    if (!this._canSendAudio()) return;
    const bytes = new Uint8Array(buffer);
    if (!bytes.byteLength) return;
    const bytesPerSample = this._txFormat?.pcmFormat === "s16le" ? 2 :
      this._txFormat?.pcmFormat === "s24le" ? 3 : 4;
    const bytesPerSecond = Number(this._txFormat?.sampleRate || 0) *
      Number(this._txFormat?.channels || 0) * bytesPerSample;
    const maxBufferedBytes = Math.max(
      bytes.byteLength * MIN_AUDIO_WS_BUFFER_FRAMES,
      Math.ceil(bytesPerSecond * MAX_AUDIO_WS_BUFFER_MS / 1000),
    );
    if (this._ws.bufferedAmount >= maxBufferedBytes) {
      this._stats.tx_dropped++;
      if ((this._stats.tx_dropped & 31) === 1) this._emit();
      return;
    }
    if (!this._audioFrameBuffer || this._audioFrameBuffer.byteLength !== bytes.byteLength + 1) {
      this._audioFrameBuffer = new Uint8Array(bytes.byteLength + 1);
    }
    const frame = this._audioFrameBuffer;
    frame[0] = WS_AUDIO;
    frame.set(bytes, 1);
    this._ws.send(frame);
    this._stats.sent++;
    if ((this._stats.sent & 31) === 0) this._emit();
  }

  _handleMessage(event) {
    if (typeof event.data === "string") {
      try {
        const msg = JSON.parse(event.data);
        if (msg.tx_format || msg.rx_format || msg.audio_direction) {
          this._lastSessionPayload = { ...(this._lastSessionPayload || {}), ...msg };
        }
        if (msg.audio_direction) {
          if (this._audioReady) void this._reconcileAudioMedia(msg);
          else this._audioDirection = this._normaliseAudioDirection(msg.audio_direction);
        } else if (this._audioReady && (msg.tx_format || msg.rx_format)) {
          void this._reconcileAudioMedia(msg);
        }
        if (msg.state) this._setState(String(msg.state).toUpperCase());
        if (msg.error) this.dispatchEvent(new CustomEvent("error", { detail: msg.error }));
        this._resolveControlWaiter(msg);
      } catch (_) {}
      return;
    }
    const raw = new Uint8Array(event.data);
    if (
      raw[0] !== WS_AUDIO ||
      raw.byteLength < 2 ||
      !this._playbackNode ||
      !this._canReceiveAudio()
    ) return;
    this._playbackNode.port.postMessage({ type: "audio", buffer: event.data, byteOffset: 1 }, [event.data]);
    this._stats.received++;
    if ((this._stats.received & 31) === 0) this._emit();
  }

  _createAudioContext() {
    const Ctor = window.AudioContext || window.webkitAudioContext;
    return new Ctor();
  }

  _parseFormat(token, label = "audio format") {
    return parsePcmFormat(token, label);
  }

  _resolveSessionFormats(negotiated = null) {
    return resolveSessionFormats(negotiated);
  }

  async _setupAudio(deviceInfo, negotiated = null, attachKey = "") {
    const audioMode = this._normaliseAudioMode(deviceInfo?.audio_mode);
    const audioDirection = this._normaliseAudioDirection(negotiated?.audio_direction);
    const formats = this._resolveSessionFormats(negotiated);
    const microphoneAntiAlias = deviceInfo?.microphone_anti_alias !== false;
    const { capture, playback } = this._desiredAudioPaths(audioMode, audioDirection);
    const setupGeneration = ++this._audioSetupGeneration;
    const expectedCallId = this._callId;
    const resources = {
      audioContext: null,
      mediaStream: null,
      captureNode: null,
      captureSink: null,
      source: null,
      playbackNode: null,
    };
    const assertCurrent = () => {
      if (
        setupGeneration !== this._audioSetupGeneration ||
        this._callId !== expectedCallId
      ) {
        throw new Error("Audio setup superseded");
      }
      if (attachKey && this._sessionAttachKey !== attachKey) {
        throw new Error("Audio setup superseded");
      }
    };

    try {
      assertCurrent();
      if (capture || playback) {
        resources.audioContext = this._createAudioContext();
        if (resources.audioContext.state === "suspended") {
          await resources.audioContext.resume();
          assertCurrent();
        }
      }

      if (capture) {
        resources.mediaStream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
        assertCurrent();
        await resources.audioContext.audioWorklet.addModule(
          `/voip-stack/voip-stack-processor.js?v=${encodeURIComponent(MODULE_VERSION)}`,
        );
        assertCurrent();
        resources.source = resources.audioContext.createMediaStreamSource(resources.mediaStream);
        resources.captureNode = new AudioWorkletNode(resources.audioContext, "voip-stack-processor", {
          processorOptions: {
            format: formats.tx,
            antiAlias: microphoneAntiAlias,
          },
        });
        resources.captureNode.port.onmessage = (event) => {
          if (
            this._captureNode === resources.captureNode &&
            event.data?.type === "audio"
          ) this._sendAudio(event.data.buffer);
        };
        resources.source.connect(resources.captureNode);
        resources.captureSink = resources.audioContext.createGain();
        resources.captureSink.gain.value = 0;
        resources.captureNode
          .connect(resources.captureSink)
          .connect(resources.audioContext.destination);
      }

      if (playback) {
        await resources.audioContext.audioWorklet.addModule(
          `/voip-stack/voip-stack-playback-processor.js?v=${encodeURIComponent(MODULE_VERSION)}`,
        );
        assertCurrent();
        resources.playbackNode = new AudioWorkletNode(
          resources.audioContext,
          "voip-stack-playback-processor",
          {
            outputChannelCount: [formats.rx.channels],
            processorOptions: { format: formats.rx },
          },
        );
        resources.playbackNode.port.onmessage = (event) => {
          if (
            this._playbackNode !== resources.playbackNode ||
            event.data?.type !== "stats"
          ) return;
          this._stats = { ...this._stats, ...event.data };
          this._emit();
        };
        resources.playbackNode.connect(resources.audioContext.destination);
      }
      assertCurrent();
      const previous = this._takeAudioResources();
      this._audioMode = audioMode;
      this._microphoneAntiAlias = microphoneAntiAlias;
      this._audioDirection = audioDirection;
      this._txFormat = formats.tx;
      this._rxFormat = formats.rx;
      this._audioContext = resources.audioContext;
      this._mediaStream = resources.mediaStream;
      this._captureNode = resources.captureNode;
      this._captureSink = resources.captureSink;
      this._source = resources.source;
      this._playbackNode = resources.playbackNode;
      this._audioReady = true;
      this._applyAudioDirection(audioDirection);
      await this._disposeAudioResources(previous);
    } catch (err) {
      await this._disposeAudioResources(resources);
      throw err;
    }
  }

  _normaliseAudioMode(value) {
    return normaliseAudioMode(value);
  }

  _normaliseAudioDirection(value) {
    return normaliseAudioDirection(value);
  }

  _desiredAudioPaths(audioMode, audioDirection) {
    return desiredAudioPaths(audioMode, audioDirection);
  }

  _sameAudioFormat(left, right) {
    return sameAudioFormat(left, right);
  }

  async _reconcileAudioMedia(update = {}) {
    if (!this._audioReady || !this._callId) return;
    const negotiated = { ...(this._lastSessionPayload || {}), ...(update || {}) };
    if (update?.tx_format || update?.rx_format) this._lastSessionPayload = negotiated;
    const direction = this._normaliseAudioDirection(
      negotiated.audio_direction || this._audioDirection,
    );
    const formats = this._resolveSessionFormats(negotiated);
    const desired = this._desiredAudioPaths(this._audioMode, direction);
    if (
      this._sameAudioFormat(formats.tx, this._txFormat) &&
      this._sameAudioFormat(formats.rx, this._rxFormat) &&
      desired.capture === Boolean(this._captureNode) &&
      desired.playback === Boolean(this._playbackNode)
    ) {
      this._applyAudioDirection(direction);
      return;
    }
    try {
      // Preserve the capture preference when a re-INVITE rebuilds the worklet.
      // Falling back to an implicit default here would make one call change
      // behavior after an otherwise unrelated media renegotiation.
      await this._setupAudio(
        {
          audio_mode: this._audioMode,
          microphone_anti_alias: this._microphoneAntiAlias,
        },
        { ...negotiated, audio_direction: direction },
      );
    } catch (err) {
      if (String(err?.message || err).includes("superseded")) return;
      console.warn("voip-stack-engine: audio media update failed", err);
      this.dispatchEvent(new CustomEvent("error", {
        detail: `Audio media update failed: ${err?.message || String(err)}`,
      }));
    }
  }

  _canSendAudio() {
    return ["sendrecv", "sendonly"].includes(this._audioDirection);
  }

  _canReceiveAudio() {
    return ["sendrecv", "recvonly"].includes(this._audioDirection);
  }

  _applyAudioDirection(value) {
    const nextDirection = this._normaliseAudioDirection(value);
    let changed = nextDirection !== this._audioDirection;
    this._audioDirection = nextDirection;
    const enabled = this._canSendAudio();
    for (const track of this._mediaStream?.getAudioTracks?.() || []) {
      if (track.enabled !== enabled) {
        track.enabled = enabled;
        changed = true;
      }
    }
    // Engine state listeners reconcile the authoritative call snapshot. An
    // unconditional event here made an unchanged reconciliation call itself
    // synchronously until Chrome exhausted the JavaScript stack, most often
    // while Hangup was cleaning up an active media pipeline.
    if (changed) this._emit();
  }

  async _setupAudioOrAbort(deviceId, deviceInfo, reply, attachKey = "", endpointId = DEFAULT_SOFTPHONE_ENDPOINT_ID) {
    let connected = false;
    const callId = String(reply?.call_id || "");
    try {
      const beforeAttach = await this._mediaAttachState(callId, endpointId);
      if (this._mediaAttachOwnedByOther(beforeAttach, callId)) {
        // Only the browser that originated or answered the call may attach
        // media. A spectator can observe call state but must discard its
        // optimistic local claim before it opens microphone/camera sockets.
        this.releaseSoftphoneSession(callId, endpointId);
        return false;
      }
      if (this._mediaAttachNoLongerBelongsHere(beforeAttach, callId)) {
        this.releaseSoftphoneSession(callId, endpointId);
        return false;
      }
      let negotiated = null;
      for (let attempt = 0; attempt < MEDIA_RECONNECT_ATTEMPTS; attempt++) {
        try {
          negotiated = await this._connect(deviceId, callId, endpointId);
          break;
        } catch (err) {
          const superseded = attachKey && this._sessionAttachKey !== attachKey;
          const retryable =
            !this._pageHiding &&
            !superseded &&
            this.ownsSoftphoneSession(callId, endpointId) &&
            attempt + 1 < MEDIA_RECONNECT_ATTEMPTS;
          if (!retryable) throw err;
          await new Promise((resolve) => window.setTimeout(
            resolve,
            MEDIA_RECONNECT_DELAY_MS * (attempt + 1),
          ));
        }
      }
      connected = true;
      await this._setupAudio(
        deviceInfo,
        { ...(reply || {}), ...(negotiated || {}) },
        attachKey,
      );
      if (attachKey && this._sessionAttachKey !== attachKey) {
        return false;
      }
      // A re-INVITE may land while getUserMedia or the worklet module is
      // pending. The WebSocket handler records it while the pipeline is not
      // yet publishable; reconcile once after the atomic initial commit.
      await this._reconcileAudioMedia(this._lastSessionPayload || {});
      if (attachKey && this._sessionAttachKey !== attachKey) return false;
      return true;
    } catch (err) {
      if (attachKey && this._sessionAttachKey !== attachKey) {
        return false;
      }
      // A failed HTTP/WebSocket ownership claim (for example, a 409 while a
      // newer card takes over) never established a media path and therefore
      // must not terminate the live SIP dialog.  Once the socket opened, a
      // real browser media-format/setup failure is still fatal for this leg.
      if (!connected) {
        const afterFailure = await this._mediaAttachState(callId, endpointId);
        if (this._mediaAttachOwnedByOther(afterFailure, callId)) {
          // The ownership race happened between preflight and WebSocket
          // admission. `_connect()` already discarded its losing socket.
          this.releaseSoftphoneSession(callId, endpointId);
          return false;
        }
        const expectedTerminalRace =
          this._mediaAttachNoLongerBelongsHere(afterFailure, callId);
        this.releaseSoftphoneSession(callId, endpointId);
        await this.close("media_attach_conflict");
        this._forceIdle();
        if (expectedTerminalRace) return false;
        console.error("voip-stack-engine: audio setup failed", err);
        this.dispatchEvent(new CustomEvent("error", {
          detail: "Call media is active in another tab or could not be attached.",
        }));
        return false;
      }
      console.error("voip-stack-engine: audio setup failed", err);
      this.dispatchEvent(new CustomEvent("error", { detail: err?.message || String(err) }));
      if (
        (deviceId === HA_SOFTPHONE_DEVICE_ID || !!endpointId) &&
        this._deviceId === deviceId &&
        this._endpointId === endpointId &&
        this._callId === callId &&
        this._hass
      ) {
        await this._hass.callService("voip_stack", "hangup", {
          call_id: callId,
          device_id: deviceId,
          reason: "media_incompatible",
        }).catch(() => {});
      }
      await this.close("audio_setup_failed");
      this._setState("ERROR");
      return false;
    }
  }

  async startHaSoftphone(target, softphoneInfo, context = {}) {
    await this._waitForMediaCleanup();
    this._resetStats();
    let endpointId = String(context.endpoint_id || softphoneInfo?.endpoint_id || "").trim();
    let deviceId = String(context.device_id || softphoneInfo?.device_id || "").trim();
    if (!endpointId && !deviceId) endpointId = DEFAULT_SOFTPHONE_ENDPOINT_ID;
    if (!deviceId && endpointId === DEFAULT_SOFTPHONE_ENDPOINT_ID) deviceId = HA_SOFTPHONE_DEVICE_ID;
    const request = {
      type: "call_service",
      domain: "voip_stack",
      service: "call",
      return_response: true,
      service_data: {
        destination: context.callee || target.name || target.device_id || "",
        call_id: context.call_id || "",
        send_video: Boolean(context.sendVideo),
        media_client_id: this._mediaClientId,
      },
    };
    if (deviceId) request.service_data.device_id = deviceId;
    const serviceReply = await this._hass.callWS(request);
    const reply = serviceReply?.response || serviceReply || {};
    endpointId = String(reply?.endpoint_id || endpointId || DEFAULT_SOFTPHONE_ENDPOINT_ID);
    deviceId = String(reply?.device_id || deviceId || HA_SOFTPHONE_DEVICE_ID);
    const state = String(reply?.state || "").toLowerCase();
    const callId = String(reply?.call_id || "");
    if (typeof context.shouldAbort === "function" && context.shouldAbort()) {
      if (
        callId &&
        ["calling", "connecting", "remote_ringing", "ringing", "in_call"].includes(state)
      ) {
        await this._hass.callService("voip_stack", "hangup", {
          call_id: callId,
          device_id: deviceId,
          reason: "superseded",
        }).catch(() => {});
      }
      return { ...(reply || {}), superseded: true };
    }
    if (!["calling", "connecting", "remote_ringing", "ringing", "in_call"].includes(state)) {
      this._setState("IDLE");
      return reply;
    }
    this.claimSoftphoneSession(callId, endpointId);
    if (state === "in_call") {
      const mediaInfo = {
        ...(softphoneInfo || {}),
        ...(target || {}),
        device_id: deviceId,
        endpoint_id: endpointId,
        audio_mode: target?.audio_mode || softphoneInfo?.audio_mode || "full_duplex",
      };
      await this.resumeSession(mediaInfo, deviceId, { ...(reply || {}), endpoint_id: endpointId });
    }
    return reply;
  }

  get mediaClientId() {
    return this._mediaClientId;
  }

  async resumeSession(deviceInfo, sessionDeviceId, statePayload) {
    if (this._pageHiding) return;
    const state = String(statePayload?.state || "").toLowerCase();
    if (state !== "in_call") return;
    const deviceId = sessionDeviceId || statePayload?.session_device_id || statePayload?.device_id || this._deviceId;
    const endpointId = String(statePayload?.endpoint_id || deviceInfo?.endpoint_id || DEFAULT_SOFTPHONE_ENDPOINT_ID);
    if (!deviceId) return;
    // Endpoint + Call-ID identify the logical browser media leg. The device
    // metadata can legitimately settle from a fallback ID to the registered
    // HA device after answer; treating that metadata update as a new attach
    // tears down a healthy RTP/WebSocket pipeline and loses the first video
    // keyframe.
    const attachKey = `${endpointId}|${statePayload?.call_id || ""}`;
    if (this._sessionAttachPromise && this._sessionAttachKey === attachKey) {
      return this._sessionAttachPromise;
    }
    this._sessionAttachKey = attachKey;
    // Defer the actual attach until after `_sessionAttachPromise` has been
    // installed below. `_connect()` synchronously emits an intermediate
    // engine state while tearing down an older pipeline; a card listener may
    // re-enter `resumeSession()` from that event. Starting this async body
    // immediately left a small window where the re-entrant call could not see
    // the in-flight promise and superseded the very same audio setup.
    const attachPromise = Promise.resolve().then(() => {
      if (this._sessionAttachKey !== attachKey) return;
      return this._resumeSessionLocked(deviceInfo, deviceId, { ...statePayload, endpoint_id: endpointId }, attachKey);
    });
    const trackedPromise = attachPromise.finally(() => {
      if (this._sessionAttachPromise !== trackedPromise) return;
      this._sessionAttachPromise = null;
      if (this._sessionAttachKey === attachKey) this._sessionAttachKey = "";
    });
    this._sessionAttachPromise = trackedPromise;
    return this._sessionAttachPromise;
  }

  async reconcileSession(statePayload) {
    const callId = String(statePayload?.call_id || "");
    if (
      String(statePayload?.state || "").toLowerCase() !== "in_call" ||
      !callId ||
      this._callId !== callId
    ) return;
    if (statePayload.audio_direction) {
      this._lastSessionPayload = { ...(this._lastSessionPayload || {}), ...statePayload };
      if (this._audioReady) await this._reconcileAudioMedia(statePayload);
      else this._audioDirection = this._normaliseAudioDirection(statePayload.audio_direction);
    }
    await this._ensureVideo(statePayload);
  }

  async _resumeSessionLocked(deviceInfo, deviceId, statePayload, attachKey) {
    await this._waitForMediaCleanup();
    if (attachKey && this._sessionAttachKey !== attachKey) return;
    const callId = String(statePayload?.call_id || "");
    const endpointId = String(statePayload?.endpoint_id || deviceInfo?.endpoint_id || DEFAULT_SOFTPHONE_ENDPOINT_ID);
    if (
      this._ws &&
      this._endpointId === endpointId &&
      this._callId === callId &&
      this._ws.readyState === WebSocket.OPEN &&
      this._audioReady
    ) {
      this._setState("IN_CALL");
      void this._ensureVideo(statePayload);
      return;
    }
    this._resetStats();
    if (!await this._setupAudioOrAbort(
      deviceId,
      { ...(deviceInfo || {}), device_id: deviceId },
      statePayload,
      attachKey,
      endpointId,
    )) return;
    if (this._sessionAttachKey !== attachKey) return;
    this._setState("IN_CALL");
    // Video is optional and directionally independent from the audio call.
    // In particular, a real browser may leave getUserMedia pending while it
    // asks for camera permission. Never make audio attachment or call control
    // wait for that prompt.
    void this._ensureVideo(statePayload);
  }

  async _ensureVideo(statePayload) {
    const wantedCallId = String(statePayload?.call_id || "");
    const wantedEndpoint = String(
      statePayload?.endpoint_id || this._endpointId ||
      DEFAULT_SOFTPHONE_ENDPOINT_ID,
    );
    const wantedVideoKey = `${wantedEndpoint}|${wantedCallId}`;
    if (
      this._videoHangupSuspendedKey &&
      this._videoHangupSuspendedKey !== wantedVideoKey
    ) {
      this._videoHangupSuspendedKey = "";
    }
    if (
      wantedCallId &&
      this._videoHangupSuspendedKey === wantedVideoKey
    ) {
      if (
        this._video &&
        (this._video.active || this._video.callId === wantedCallId)
      ) await this._video.close();
      return;
    }
    if (!statePayload?.video_active) {
      if (wantedCallId && this._callId !== wantedCallId) return;
      this._videoAttachGeneration++;
      this._videoAttachPromise = null;
      this._videoAttachCallId = "";
      this._videoHangupSuspendedKey = "";
      if (
        this._video &&
        (this._video.active || this._video.callId) &&
        (!wantedCallId || !this._video.callId || this._video.callId === wantedCallId)
      ) await this._video.close();
      return;
    }

    if (!wantedCallId || this._callId !== wantedCallId) return;
    const intentGeneration = this._videoAttachGeneration;

    const video = await this._loadVideo();
    if (
      intentGeneration !== this._videoAttachGeneration ||
      !wantedCallId ||
      this._callId !== wantedCallId
    ) return;

    if (video.active && video.callId === wantedCallId) {
      await video.setCameraEnabled(
        Boolean(statePayload?.send_video),
        String(statePayload?.endpoint_id || this._endpointId || DEFAULT_SOFTPHONE_ENDPOINT_ID),
      );
      return;
    }
    if (this._videoAttachPromise && this._videoAttachCallId === wantedCallId) {
      await this._videoAttachPromise;
      return;
    }

    const generation = ++this._videoAttachGeneration;
    this._videoAttachCallId = wantedCallId;
    const attach = (async () => {
      try {
        await video.start(statePayload);
        if (
          generation !== this._videoAttachGeneration ||
          !wantedCallId ||
          this._callId !== wantedCallId
        ) {
          if (video.callId === wantedCallId) await video.close();
        }
      } catch (err) {
        if (generation !== this._videoAttachGeneration) return;
        console.warn("voip-stack-engine: optional SIP video setup failed", err);
        this.dispatchEvent(new CustomEvent("video-error", { detail: err?.message || String(err) }));
      }
    })();
    this._videoAttachPromise = attach;
    try {
      await attach;
    } finally {
      if (this._videoAttachPromise === attach) {
        this._videoAttachPromise = null;
        this._videoAttachCallId = "";
      }
    }
  }

  _resetStats() {
    this._stats = { sent: 0, received: 0, tx_dropped: 0, buffered_frames: 0, frames_drop: 0, underruns: 0 };
  }

  async close(_reason = "", preserveAttach = false, preserveConnect = false) {
    const previousCleanup = this._mediaCleanupPromise;
    let finishCleanup;
    const currentCleanup = new Promise((resolve) => { finishCleanup = resolve; });
    this._mediaCleanupPromise = currentCleanup;
    try {
      if (!preserveConnect) this._connectGeneration++;
      this._videoAttachGeneration++;
      this._videoAttachPromise = null;
      this._videoAttachCallId = "";
      this._videoHangupSuspendedKey = "";
      if (!preserveAttach) this._sessionAttachKey = "";
      const ws = this._ws;
      this._ws = null;
      if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        try { ws.close(); } catch (_) {}
      }
      this._callId = "";
      // New sessions wait on the page-level cleanup gate and cannot overlap
      // camera/encoder/AudioContext destruction from the previous call.
      const audioCleanup = this._cleanupAudio("close");
      const videoCleanup = this._video ? this._video.close() : Promise.resolve();
      await settleWithin(Promise.allSettled([
        previousCleanup || Promise.resolve(),
        audioCleanup,
        videoCleanup,
      ]));
    } finally {
      finishCleanup();
      if (this._mediaCleanupPromise === currentCleanup) this._mediaCleanupPromise = null;
    }
  }

  async _waitForMediaCleanup() {
    const cleanup = this._mediaCleanupPromise;
    if (cleanup) await cleanup.catch(() => {});
  }

  _detachAudioResources() {
    this._audioSetupGeneration++;
    this._audioReady = false;
    if (this._controlWaiter) {
      window.clearTimeout(this._controlWaiter.timer);
      this._controlWaiter.resolve(null);
      this._controlWaiter = null;
    }
    return this._takeAudioResources();
  }

  _takeAudioResources() {
    const resources = {
      captureNode: this._captureNode,
      captureSink: this._captureSink,
      source: this._source,
      mediaStream: this._mediaStream,
      playbackNode: this._playbackNode,
      audioContext: this._audioContext,
    };
    this._captureNode = null;
    this._captureSink = null;
    this._source = null;
    this._mediaStream = null;
    this._playbackNode = null;
    this._audioContext = null;
    this._audioFrameBuffer = null;
    return resources;
  }

  async _disposeAudioResources(resources = {}) {
    try { resources.captureNode?.disconnect(); } catch (_) {}
    try { resources.captureSink?.disconnect(); } catch (_) {}
    try { resources.source?.disconnect(); } catch (_) {}
    try { resources.mediaStream?.getTracks?.().forEach((track) => track.stop()); } catch (_) {}
    try { resources.playbackNode?.disconnect(); } catch (_) {}
    if (resources.audioContext) {
      await settleWithin(resources.audioContext.close());
    }
  }

  async _cleanupAudio(_reason) {
    const resources = this._detachAudioResources();
    this._forceIdle();
    await this._disposeAudioResources(resources);
  }
}

export const voipStackEngine = globalThis.__voipStackEngine ||
  (globalThis.__voipStackEngine = new VoipStackEngine());
