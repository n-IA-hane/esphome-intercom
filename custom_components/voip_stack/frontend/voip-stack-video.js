const VIDEO_MODULE_VERSION = (() => {
  try {
    return new URL(import.meta.url).searchParams.get("v") || "dev";
  } catch (_) {
    return "dev";
  }
})();

const [
  { directionalVideoContract, emptyVideoStats },
  { BrowserVideoSender },
  { BrowserVideoReceiver },
  { CODEC_SETUP_TIMEOUT_MS },
] = await Promise.all([
  import(`./voip-stack-video-model.js?v=${encodeURIComponent(VIDEO_MODULE_VERSION)}`),
  import(`./voip-stack-video-sender.js?v=${encodeURIComponent(VIDEO_MODULE_VERSION)}`),
  import(`./voip-stack-video-receiver.js?v=${encodeURIComponent(VIDEO_MODULE_VERSION)}`),
  import(`./voip-stack-video-codecs.js?v=${encodeURIComponent(VIDEO_MODULE_VERSION)}`),
]);

export class VoipStackVideo extends EventTarget {
  constructor() {
    super();
    this._hass = null;
    this._clientId = "";
    this._ws = null;
    this._callId = "";
    this._endpointId = "default";
    this._active = false;
    this._canReceive = false;
    this._canSend = false;
    this._encoding = "H264";
    this._clockRate = 90000;
    this._negotiated = null;
    this._cameraAllowed = false;
    this._cameraEnabled = false;
    this._mediaUpdatePromise = Promise.resolve();
    this._generation = 0;
    this._stats = this._emptyStats();
    this._sender = new BrowserVideoSender({
      sessionGeneration: () => this._generation,
      canCapture: () => this._cameraEnabled && this._cameraAllowed,
      isActive: () => this._active,
      sendContract: () => this._mediaContract("send"),
      socket: () => this._ws,
      readStats: () => this._stats,
      notify: () => this._emit(),
      onSending: (sending) => { this._canSend = sending; },
      onTxEpoch: () => this._sendTxEpoch(),
    });
    this._receiver = new BrowserVideoReceiver({
      sessionGeneration: () => this._generation,
      receiveContract: () => this._mediaContract("receive"),
      readStats: () => this._stats,
      notify: () => this._emit(),
      requestKeyFrame: () => this._requestKeyFrame(),
      onDisabled: () => { this._canReceive = false; },
    });
  }

  _emptyStats() {
    return emptyVideoStats();
  }

  configure(hass, clientId = "") {
    this._hass = hass;
    if (clientId) this._clientId = String(clientId);
  }

  get active() {
    return this._active;
  }

  get callId() {
    return this._callId;
  }

  get visible() {
    // SDP only tells us that the peer may send video. Some PBXs advertise a
    // recvonly/sendrecv video line even for an audio-first call and never send
    // a frame. Keep the audio UI compact until an actual frame is rendered.
    return this._active && this._canReceive && this._receiver._receiverHasFrame;
  }

  get stats() {
    return { ...this._stats };
  }

  get canSend() {
    return this._cameraAllowed;
  }

  get cameraEnabled() {
    return this._cameraEnabled;
  }

  get cameraSettings() {
    return this._sender.cameraSettings;
  }

  setCameraDevicePreference(deviceId = "") {
    if (this._sender._encoder) return false;
    this._sender._cameraDeviceId = String(deviceId || "");
    return true;
  }

  async switchCamera(deviceId = "") {
    const selected = String(deviceId || "");
    if (!this._active || !this._cameraEnabled || !this._cameraAllowed || !this._negotiated) {
      this._sender._cameraDeviceId = selected;
      return {};
    }
    const currentId = String(this._sender._cameraSettings.deviceId || "");
    if (selected && selected === currentId) return this.cameraSettings;
    return this._sender._replaceSender(
      this._mediaContract("send").codec,
      this._generation,
      selected,
      true,
    );
  }

  async setCameraEnabled(enabled, endpointId = this._endpointId) {
    const endpoint = String(endpointId || "default").trim() || "default";
    const selected = Boolean(enabled);
    // This only reconciles the active media sender. Persistent intent belongs
    // to the logical HA phone and arrives in the backend state snapshot.
    if (this._active && endpoint !== this._endpointId) return;
    const changed = this._cameraEnabled !== selected;
    this._cameraEnabled = selected;
    if (!this._cameraEnabled) {
      // Engine state events cause the owning card to reconcile the same
      // authoritative snapshot.  A receive-only call therefore reaches this
      // method repeatedly with `false`; emitting again when no sender exists
      // feeds that state event straight back into reconciliation and can pin
      // the browser main thread.  Cleanup and notify only for a real change
      // or for resources that still need to be released.
      const hasSenderResources = Boolean(
        this._sender._cameraReader ||
        this._sender._encodeTask ||
        this._sender._encoder ||
        this._sender._cameraStream ||
        this._sender._jpegEncoderWorker ||
        this._sender._jpegEncodePending ||
        this._sender._jpegQueuedFrame ||
        this._canSend
      );
      if (!changed && !hasSenderResources) return;
      await this._sender._cleanupSender();
      this._emit();
      return;
    }
    if (!this._active || !this._cameraAllowed || !this._negotiated || this._sender._encoder) return;
    const generation = this._generation;
    await this._sender._ensureSender(this._mediaContract("send").codec, generation);
    if (
      generation === this._generation &&
      this._cameraEnabled &&
      this._sender._encoder?.state === "configured"
    ) {
      this._canSend = true;
      this._emit();
    }
  }

  setCanvas(canvas) {
    this._receiver.setCanvas(canvas);
  }

  _emit() {
    const send = this._mediaContract("send");
    const receive = this._mediaContract("receive");
    this.dispatchEvent(new CustomEvent("state", {
      detail: {
        active: this._active,
        visible: this.visible,
        can_receive: this._canReceive,
        can_send: this._canSend,
        camera_available: this._cameraAllowed,
        camera_enabled: this._cameraEnabled,
        encoding: this._encoding,
        send_encoding: send.encoding,
        receive_encoding: receive.encoding,
        send_clock_rate: send.clockRate,
        receive_clock_rate: receive.clockRate,
        send_payload_type: send.payloadType,
        receive_payload_type: receive.payloadType,
        call_id: this._callId,
        endpoint_id: this._endpointId,
        stats: this.stats,
      },
    }));
  }

  async _wsUrl(callId, endpointId = "default") {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const path = `/api/voip_stack/video_ws?endpoint_id=${encodeURIComponent(endpointId || "default")}&call_id=${encodeURIComponent(callId)}&client_id=${encodeURIComponent(this._clientId)}`;
    const signed = await this._hass.callWS({ type: "auth/sign_path", path });
    return `${proto}//${window.location.host}${signed.path || path}`;
  }

  async start(statePayload) {
    const callId = String(statePayload?.call_id || "");
    const endpointId = String(statePayload?.endpoint_id || "default");
    if (!statePayload?.video_active || !callId) {
      await this.close();
      return false;
    }
    if (
      this._ws?.readyState === WebSocket.OPEN &&
      this._callId === callId &&
      this._endpointId === endpointId
    ) return true;
    await this.close();
    if (!window.isSecureContext) {
      throw new Error("SIP video requires a secure browser context");
    }
    const generation = ++this._generation;
    this._callId = callId;
    this._endpointId = endpointId;
    this._cameraEnabled = Boolean(statePayload?.send_video);
    this._stats = this._emptyStats();
    const url = await this._wsUrl(callId, endpointId);
    if (
      generation !== this._generation ||
      this._callId !== callId ||
      this._endpointId !== endpointId
    ) return false;
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    this._ws = ws;
    let helloResolve;
    let helloReject;
    const hello = new Promise((resolve, reject) => {
      helloResolve = resolve;
      helloReject = reject;
    });
    // The socket can close while start() is still awaiting OPEN. Mark the
    // parallel hello Promise as observed now; Promise.race below still sees
    // its rejection once OPEN has succeeded.
    void hello.catch(() => {});
    ws.onmessage = (event) => {
      if (this._ws !== ws) return;
      if (typeof event.data === "string") {
        try {
          const payload = JSON.parse(event.data);
          if (payload.error) {
            helloReject(new Error(payload.error));
          } else if (this._handleEncoderControl(payload)) {
          } else if (payload.type === "media_update") {
            this._enqueueMediaUpdate(payload, ws, callId);
          } else {
            helloResolve(payload);
          }
        } catch (err) {
          helloReject(err);
        }
        return;
      }
      if (this._mediaContract("receive").encoding === "JPEG" && this._canReceive) {
        this._receiver._decodeMessage(event.data);
      } else if (!this._receiver._decoder || this._receiver._decoder.state !== "configured") {
        this._receiver._bufferDecodeMessage(event.data);
      } else {
        this._receiver._decodeMessage(event.data);
      }
    };
    let openedReject;
    const opened = new Promise((resolve, reject) => {
      openedReject = reject;
      ws.onopen = resolve;
      ws.onerror = () => {
        const error = new Error("SIP video WebSocket failed");
        reject(error);
        helloReject(error);
      };
    });
    ws.onclose = () => {
      const error = new Error("SIP video WebSocket closed before negotiation");
      openedReject(error);
      helloReject(error);
      if (this._ws !== ws) return;
      this._ws = null;
      const cleanupGeneration = ++this._generation;
      void this._cleanupMedia(cleanupGeneration);
    };
    try {
      await Promise.race([
        opened,
        new Promise((_, reject) => {
          const schedule = globalThis.setTimeout || globalThis.window?.setTimeout;
          schedule?.(
            () => reject(new Error("SIP video WebSocket open timed out")),
            CODEC_SETUP_TIMEOUT_MS,
          );
        }),
      ]);
      const negotiated = await Promise.race([
        hello,
        new Promise((_, reject) => window.setTimeout(
          () => reject(new Error("SIP video negotiation timed out")),
          3000,
        )),
      ]);
      if (!this._isCurrent(generation, ws, callId)) return false;
      this._negotiated = negotiated;
      this._updateMediaSummary(negotiated);
      await this._setupCodecs(negotiated, generation);
      if (!this._isCurrent(generation, ws, callId)) return false;
      this._active = true;
      this._emit();
      return true;
    } catch (err) {
      if (!this._isCurrent(generation, ws, callId)) return false;
      await this.close();
      throw err;
    }
  }

  _isCurrent(generation, ws, callId) {
    return generation === this._generation && this._ws === ws && this._callId === callId;
  }

  _mediaContract(direction, negotiated = this._negotiated) {
    return directionalVideoContract(negotiated, direction);
  }

  _updateMediaSummary(negotiated) {
    const primaryDirection = negotiated?.can_receive === false && negotiated?.can_send
      ? "send"
      : "receive";
    this._encoding = this._mediaContract(primaryDirection, negotiated).encoding;
    this._clockRate = this._mediaContract("receive", negotiated).clockRate;
  }

  _handleEncoderControl(payload) {
    if (payload?.type !== "force_key_frame") return false;
    this._sender._forceCameraKeyFrame = true;
    this._emit();
    return true;
  }

  _sendTxEpoch() {
    const ws = this._ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: "tx_epoch" }));
  }

  _enqueueMediaUpdate(payload, ws, callId) {
    this._mediaUpdatePromise = this._mediaUpdatePromise
      .then(() => this._applyMediaUpdate(payload, ws, callId))
      .catch((err) => {
        if (this._ws === ws) {
          console.warn(`voip-stack-video: media update failed (${err?.message || String(err)})`);
        }
      });
  }

  async _applyMediaUpdate(negotiated, ws, callId) {
    if (this._ws !== ws || this._callId !== callId) return;
    if (negotiated?.restart_required) {
      // The server cannot replace an FFmpeg/direct RTP topology underneath a
      // live owner. Release the old media socket first, then reconnect using
      // the normal ownership handoff and the newly committed SDP generation.
      const expectedClosedGeneration = this._generation + 1;
      const endpointId = this._endpointId;
      await this.close();
      if (
        this._generation !== expectedClosedGeneration ||
        this._ws !== null ||
        this._callId
      ) return false;
      return this.start({
        ...negotiated,
        call_id: callId,
        endpoint_id: endpointId,
        video_active: true,
      });
    }
    const codecContract = (direction, payload) => {
      const contract = this._mediaContract(direction, payload);
      // The format token repeats SDP direction; capabilities below own that
      // change. Retain every codec, RTP profile and image constraint.
      return { ...contract, format: contract.format.replace(
        /;direction=(sendrecv|sendonly|recvonly|inactive)(?=;|$)/g, "") };
    };
    const sameContract = this._negotiated && ["send", "receive"].every((direction) =>
      JSON.stringify(codecContract(direction, this._negotiated)) ===
      JSON.stringify(codecContract(direction, negotiated)));
    const directionChanged = this._negotiated && (
      Boolean(this._negotiated.can_receive) !== Boolean(negotiated.can_receive) ||
      Boolean(this._negotiated.can_send) !== Boolean(negotiated.can_send));
    const sameGeneration = this._negotiated?.media_generation === negotiated.media_generation;
    if (this._active && sameContract && (directionChanged || sameGeneration) &&
        (negotiated.can_receive || negotiated.can_send)) {
      // A direction change does not invalidate the other direction's codec
      // callbacks, camera track or decoder reference chain.
      const generation = this._generation;
      const preserveReceive = Boolean(negotiated.can_receive &&
        this._negotiated.can_receive && this._canReceive &&
        this._receiver._decoder?.state === "configured");
      if (!preserveReceive && this._negotiated.can_receive) {
        this._receiver._cleanupReceiver();
        this._receiver._resetMediaState(true);
      }
      if (!negotiated.can_send && this._negotiated.can_send) {
        await this._sender._cleanupSender();
      }
      if (this._ws !== ws || this._callId !== callId || generation !== this._generation) return;
      this._negotiated = negotiated;
      this._updateMediaSummary(negotiated);
      await this._setupCodecs(negotiated, generation, { preserveReceive });
      if (this._ws !== ws || this._callId !== callId || generation !== this._generation) return;
      this._active = Boolean(this._canReceive || this._canSend || this._cameraAllowed);
      this._emit();
      return;
    }
    const generation = ++this._generation;
    const senderCleanup = this._sender._cleanupSender();
    this._receiver._cleanupReceiver();
    this._active = false;
    await senderCleanup;
    if (this._ws !== ws || this._callId !== callId || generation !== this._generation) return;
    this._negotiated = negotiated;
    this._updateMediaSummary(negotiated);
    this._receiver._resetMediaState(true);
    this._sender._resetMediaState();
    await this._setupCodecs(negotiated, generation);
    if (this._ws !== ws || this._callId !== callId || generation !== this._generation) return;
    this._active = Boolean(this._canReceive || this._canSend || this._cameraAllowed);
    this._emit();
  }

  async _setupCodecs(negotiated, generation, { preserveReceive = false } = {}) {
    if (generation !== this._generation) {
      throw new Error("SIP video session was superseded");
    }
    const receive = this._mediaContract("receive", negotiated);
    const send = this._mediaContract("send", negotiated);
    const failures = [];
    let usablePaths = preserveReceive ? 1 : 0;
    if (negotiated?.can_receive && !preserveReceive) {
      try {
        if (receive.encoding === "JPEG") {
          await this._receiver._setupJpegDecoder(generation);
          this._canReceive = true;
          this._active = true;
          usablePaths++;
          this._receiver._flushPendingDecode();
          this._emit();
        } else {
          if (typeof VideoDecoder === "undefined") {
            throw new Error("WebCodecs VideoDecoder is unavailable");
          }
          await this._receiver._setupDecoder(receive.codec, generation);
          this._canReceive = true;
          this._active = true;
          usablePaths++;
          this._receiver._flushPendingDecode();
          this._emit();
        }
      } catch (err) {
        if (generation !== this._generation) throw err;
        this._receiver._cleanupReceiver();
        failures.push(`receive: ${err?.message || String(err)}`);
      }
    }
    if (generation !== this._generation) {
      throw new Error("SIP video session was superseded");
    }
    this._cameraAllowed = Boolean(negotiated?.can_send);
    if (this._cameraAllowed && this._cameraEnabled) {
      const setupSender = async () => {
        try {
          await this._sender._ensureSender(send.codec, generation);
          if (
            generation !== this._generation ||
            !this._cameraEnabled ||
            this._sender._encoder?.state !== "configured"
          ) return "superseded";
          this._canSend = true;
          this._active = true;
          this._emit();
          return "";
        } catch (err) {
          if (generation !== this._generation) return "superseded";
          return `send: ${err?.message || String(err)}`;
        }
      };
      if (usablePaths) {
        // A camera permission prompt may remain open indefinitely. Incoming
        // video is already usable, so expose it immediately and let the
        // independent send direction finish in the background.
        void setupSender().then((failure) => {
          if (failure && failure !== "superseded" && generation === this._generation) {
            console.warn(`voip-stack-video: partial media support (${failure})`);
          }
        });
      } else {
        const failure = await setupSender();
        if (failure) failures.push(failure);
        else usablePaths++;
      }
    } else if (this._cameraAllowed) {
      // A send-only dialog may wait for an explicit user camera choice. Keep
      // the authenticated media attachment alive without prompting on load.
      usablePaths++;
    }
    if (!usablePaths) {
      throw new Error(failures.join("; ") || "No negotiated SIP video direction is usable");
    }
    if (failures.length) {
      // Video directions are independent in RFC 3264. Camera permission or
      // encoder failure must not hide a valid incoming door-station stream,
      // and a decoder limitation must not tear down a valid outgoing stream.
      console.warn(`voip-stack-video: partial media support (${failures.join("; ")})`);
    }
  }

  _requestKeyFrame() {
    const ws = this._ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    try { ws.send(JSON.stringify({ type: "request_key_frame" })); } catch (_) {}
  }

  async close() {
    const generation = ++this._generation;
    // Do not let a camera prompt or codec probe from an old dialog serialize
    // media updates belonging to the next WebSocket/call.
    this._mediaUpdatePromise = Promise.resolve();
    const ws = this._ws;
    this._ws = null;
    if (ws && [WebSocket.OPEN, WebSocket.CONNECTING].includes(ws.readyState)) {
      try { ws.close(); } catch (_) {}
    }
    await this._cleanupMedia(generation);
  }

  async _cleanupMedia(generation = this._generation) {
    const senderCleanup = this._sender._cleanupSender();
    this._receiver._cleanupReceiver();
    if (generation === this._generation) {
      this._callId = "";
      this._endpointId = "default";
      this._active = false;
      this._canReceive = false;
      this._canSend = false;
      this._receiver._resetMediaState(false);
      this._sender._resetMediaState();
      this._negotiated = null;
      this._cameraAllowed = false;
      this._emit();
    }
    await senderCleanup;
  }


}
