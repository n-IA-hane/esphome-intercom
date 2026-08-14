const VIDEO_MODULE_VERSION = (() => {
  try {
    return new URL(import.meta.url).searchParams.get("v") || "dev";
  } catch (_) {
    return "dev";
  }
})();

const {
  cameraCaptureContract,
  cameraEncoderContract,
  directionalVideoContract,
  emptyVideoStats,
} = await import(
  `./voip-stack-video-model.js?v=${encodeURIComponent(VIDEO_MODULE_VERSION)}`
);

const VIDEO_ACCESS_UNIT = 1;
const VIDEO_HEADER_BYTES = 6;
const MAX_VIDEO_ACCESS_UNIT_BYTES = 1024 * 1024;
const MAX_VIDEO_WS_BUFFER = 2 * 1024 * 1024;
const MAX_PENDING_DECODE_BYTES = 8 * 1024 * 1024;
const MAX_PENDING_DECODE_FRAMES = 60;
const MAX_DECODE_QUEUE_FRAMES = 8;
const CODEC_SETUP_TIMEOUT_MS = 3000;
const MEDIA_CLEANUP_TIMEOUT_MS = 500;
const JPEG_CAMERA_QUALITY = 0.72;

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
    this._canvas = null;
    this._decoder = null;
    this._decoderWorker = null;
    this._decoderWorkerRequests = new Map();
    this._decoderWorkerRequestId = 0;
    this._pendingRenderedFrame = null;
    this._renderFrameHandle = 0;
    this._encoder = null;
    this._cameraStream = null;
    this._cameraReader = null;
    this._encodeTask = null;
    this._jpegEncoderWorker = null;
    this._jpegEncodePending = false;
    this._jpegQueuedFrame = null;
    this._forceCameraKeyFrame = true;
    this._sendDropUntilKeyFrame = false;
    this._encoding = "H264";
    this._clockRate = 90000;
    this._negotiated = null;
    this._cameraAllowed = false;
    this._cameraEnabled = false;
    this._rtpTimestampBase = null;
    this._rtpTimestampLast = null;
    this._rtpTimestampTicks = 0;
    this._encodedFrames = 0;
    this._pendingDecode = [];
    this._pendingDecodeBytes = 0;
    this._dropUntilKeyFrame = false;
    this._jpegDecodePending = false;
    this._jpegQueuedBuffer = null;
    this._jpegDecodeToken = null;
    this._jpegDecodeErrorReported = 0;
    this._mediaUpdatePromise = Promise.resolve();
    this._generation = 0;
    this._senderGeneration = 0;
    this._senderSerial = 0;
    this._senderSwitchGeneration = 0;
    this._senderSetupPromise = null;
    this._cameraDeviceId = "";
    this._cameraSettings = {};
    this._lastRenderedAt = 0;
    this._lastRenderedTimestamp = null;
    this._lastDecodedAt = 0;
    this._lastDecodedTimestamp = null;
    this._stats = this._emptyStats();
  }

  _emptyStats() {
    return emptyVideoStats();
  }

  _codecWorkerUrl() {
    const url = new URL("./voip-stack-video-worker.js", import.meta.url);
    try {
      url.search = new URL(import.meta.url).search;
    } catch (_) {}
    return url;
  }

  async _createJpegEncoder(
    width,
    height,
    generation,
    senderGeneration,
    isCurrent = () => this._senderIsCurrent(generation, senderGeneration),
  ) {
    if (typeof Worker === "undefined") {
      throw new Error("Browser cannot encode JPEG outside the UI thread");
    }
    const worker = new Worker(this._codecWorkerUrl(), { type: "module" });
    const requestId = 1;
    let resolveSetup;
    let rejectSetup;
    const setup = new Promise((resolve, reject) => {
      resolveSetup = resolve;
      rejectSetup = reject;
    });
    const encoder = {
      state: "configuring",
      kind: "jpeg",
      encodeQueueSize: 0,
      worker,
      close() {
        if (this.state === "closed") return;
        this.state = "closed";
        try { worker.postMessage({ type: "close" }); } catch (_) {}
        try { worker.terminate(); } catch (_) {}
      },
    };
    worker.onmessage = (event) => {
      const message = event.data || {};
      if (message.type === "reply" && Number(message.requestId || 0) === requestId) {
        if (message.ok) resolveSetup(message);
        else rejectSetup(new Error(message.error || "JPEG worker rejected setup"));
        return;
      }
      this._handleJpegEncoderWorkerMessage(
        worker,
        encoder,
        generation,
        senderGeneration,
        message,
      );
    };
    worker.onerror = (event) => {
      const error = new Error(
        event?.message || "JPEG camera worker failed",
      );
      if (encoder.state === "configuring") rejectSetup(error);
      if (
        this._jpegEncoderWorker === worker &&
        this._encoder === encoder &&
        generation === this._generation &&
        senderGeneration === this._senderGeneration
      ) {
        this._stats.dropped++;
        void this._cleanupSender();
        this._emit();
      }
    };
    const schedule = globalThis.setTimeout || globalThis.window?.setTimeout;
    const cancel = globalThis.clearTimeout || globalThis.window?.clearTimeout;
    const timer = schedule?.(
      () => rejectSetup(new Error("JPEG worker setup timed out")),
      CODEC_SETUP_TIMEOUT_MS,
    );
    try {
      worker.postMessage({
        type: "configure_jpeg_encoder",
        requestId,
        generation,
        width,
        height,
        quality: JPEG_CAMERA_QUALITY,
      });
      await setup;
      if (!isCurrent()) {
        throw new Error("SIP video session was superseded or camera transmission was disabled");
      }
      encoder.state = "configured";
      return encoder;
    } catch (error) {
      encoder.close();
      throw error;
    } finally {
      if (timer && cancel) cancel(timer);
    }
  }

  _handleJpegEncoderWorkerMessage(
    worker,
    encoder,
    generation,
    senderGeneration,
    message,
  ) {
    if (
      this._jpegEncoderWorker !== worker ||
      this._encoder !== encoder ||
      encoder.state !== "configured" ||
      generation !== this._generation ||
      senderGeneration !== this._senderGeneration ||
      Number(message.generation || 0) !== generation ||
      Number(message.senderGeneration || 0) !== senderGeneration
    ) return;
    if (message.type !== "jpeg_frame" && message.type !== "jpeg_error") return;

    encoder.encodeQueueSize = 0;
    this._jpegEncodePending = false;
    if (
      message.type === "jpeg_frame" &&
      message.buffer &&
      Number.isFinite(Number(message.buffer.byteLength))
    ) {
      this._sendEncodedAccessUnit(
        new Uint8Array(message.buffer),
        Number(message.timestamp || 0),
        true,
        generation,
        senderGeneration,
        encoder,
      );
      this._encodedFrames++;
    } else if (message.type === "jpeg_error") {
      this._stats.dropped++;
      this._emit();
    }

    const queued = this._jpegQueuedFrame;
    this._jpegQueuedFrame = null;
    if (queued) {
      this._dispatchJpegFrame(
        queued,
        worker,
        encoder,
        generation,
        senderGeneration,
      );
    }
  }

  _dispatchJpegFrame(frame, worker, encoder, generation, senderGeneration) {
    if (
      !this._senderIsCurrent(generation, senderGeneration) ||
      this._jpegEncoderWorker !== worker ||
      this._encoder !== encoder ||
      encoder.state !== "configured"
    ) {
      frame.close();
      return;
    }
    if (this._jpegEncodePending) {
      if (this._jpegQueuedFrame) {
        this._jpegQueuedFrame.close();
        this._stats.dropped++;
      }
      this._jpegQueuedFrame = frame;
      return;
    }
    this._jpegEncodePending = true;
    encoder.encodeQueueSize = 1;
    try {
      worker.postMessage({
        type: "encode_jpeg",
        generation,
        senderGeneration,
        timestamp: Number(frame.timestamp ?? performance.now() * 1000),
        frame,
      }, [frame]);
    } catch (error) {
      this._jpegEncodePending = false;
      encoder.encodeQueueSize = 0;
      frame.close();
      throw error;
    }
  }

  _createDecoderWorker(generation) {
    if (typeof Worker === "undefined") return null;
    const worker = new Worker(this._codecWorkerUrl(), { type: "module" });
    worker.onmessage = (event) => this._handleDecoderWorkerMessage(
      worker,
      generation,
      event.data || {},
    );
    worker.onerror = (event) => {
      if (this._decoderWorker !== worker || generation !== this._generation) return;
      this._stats.decode_errors++;
      this._dropUntilKeyFrame = true;
      this._requestKeyFrame();
      console.warn(`voip-stack-video: decoder worker failed (${event?.message || "unknown error"})`);
      this._emit();
    };
    this._decoderWorker = worker;
    return worker;
  }

  _handleDecoderWorkerMessage(worker, generation, message) {
    if (message.type === "reply") {
      const pending = this._decoderWorkerRequests.get(Number(message.requestId || 0));
      if (!pending) return;
      this._decoderWorkerRequests.delete(Number(message.requestId || 0));
      (globalThis.clearTimeout || globalThis.window?.clearTimeout)?.(pending.timer);
      if (message.ok) pending.resolve(message);
      else pending.reject(new Error(message.error || "decoder worker rejected request"));
      return;
    }
    if (
      this._decoderWorker !== worker ||
      generation !== this._generation ||
      Number(message.generation || 0) !== generation
    ) {
      message.frame?.close?.();
      message.bitmap?.close?.();
      return;
    }
    if (message.type === "frame" && message.frame) {
      this._queueDecodedFrame(message.frame);
      return;
    }
    if (message.type === "jpeg_bitmap" && message.bitmap) {
      this._jpegDecodePending = false;
      this._jpegDecodeToken = null;
      this._stats.received++;
      this._queueDecodedFrame({
        bitmap: message.bitmap,
        timestamp: Number(message.timestamp || 0),
        displayWidth: message.bitmap.width,
        displayHeight: message.bitmap.height,
        close: () => message.bitmap.close(),
      });
      if ((this._stats.received & 31) === 0) this._emit();
      const latest = this._jpegQueuedBuffer;
      this._jpegQueuedBuffer = null;
      if (latest) this._decodeJpegMessage(latest);
      return;
    }
    if (message.type === "decode_queue" && this._decoder) {
      this._decoder.decodeQueueSize = Math.max(0, Number(message.size || 0));
      return;
    }
    if (message.type === "decoder_error") {
      this._stats.decode_errors++;
      this._dropUntilKeyFrame = true;
      this._requestKeyFrame();
      this._emit();
    }
    if (message.type === "jpeg_decoder_error") {
      this._jpegDecodePending = false;
      this._jpegDecodeToken = null;
      const reported = Number(message.error_count);
      let delta = 0;
      if (Number.isSafeInteger(reported) && reported >= 0) {
        delta = Math.max(0, reported - this._jpegDecodeErrorReported);
        this._jpegDecodeErrorReported = Math.max(
          this._jpegDecodeErrorReported,
          reported,
        );
      } else {
        // Compatibility with an older cached worker during frontend rollout.
        delta = 1;
        this._jpegDecodeErrorReported++;
      }
      this._stats.decode_errors += delta;
      const latest = this._jpegQueuedBuffer;
      this._jpegQueuedBuffer = null;
      if (latest) this._decodeJpegMessage(latest);
      if (delta > 0) this._emit();
    }
  }

  _decoderWorkerRequest(worker, payload) {
    const requestId = ++this._decoderWorkerRequestId;
    return new Promise((resolve, reject) => {
      const schedule = globalThis.setTimeout || globalThis.window?.setTimeout;
      const timer = schedule(() => {
        this._decoderWorkerRequests.delete(requestId);
        reject(new Error("decoder worker setup timed out"));
      }, CODEC_SETUP_TIMEOUT_MS);
      this._decoderWorkerRequests.set(requestId, { resolve, reject, timer });
      worker.postMessage({ ...payload, requestId });
    });
  }

  async _setupDecoder(codec, generation) {
    const decoderConfig = await this._supportedConfig(VideoDecoder, {
      codec,
      optimizeForLatency: true,
    });
    if (generation !== this._generation) {
      throw new Error("SIP video session was superseded");
    }
    const worker = this._createDecoderWorker(generation);
    if (worker) {
      await this._decoderWorkerRequest(worker, {
        type: "configure_decoder",
        generation,
        config: decoderConfig,
      });
      if (generation !== this._generation || this._decoderWorker !== worker) {
        throw new Error("SIP video session was superseded");
      }
      // Lightweight proxy keeps the existing queue/backpressure contract.
      this._decoder = { state: "configured", decodeQueueSize: 0, worker };
      return;
    }

    // Compatibility fallback for older browsers. Current HA Companion builds
    // use the worker path so codec stalls cannot monopolise the UI thread.
    const support = await VideoDecoder.isConfigSupported(decoderConfig);
    if (generation !== this._generation) {
      throw new Error("SIP video session was superseded");
    }
    if (!support?.supported) throw new Error(`browser cannot decode ${codec}`);
    let decoder;
    decoder = new VideoDecoder({
      output: (frame) => {
        if (generation !== this._generation || this._decoder !== decoder) {
          frame.close();
          return;
        }
        this._queueDecodedFrame(frame);
      },
      error: () => {
        if (generation !== this._generation || this._decoder !== decoder) return;
        this._stats.decode_errors++;
        this._dropUntilKeyFrame = true;
        this._requestKeyFrame();
        this._emit();
      },
    });
    decoder.configure(support.config || decoderConfig);
    this._decoder = decoder;
  }

  async _setupJpegDecoder(generation) {
    if (typeof Worker === "undefined") {
      throw new Error("Browser cannot decode JPEG outside the UI thread");
    }
    const worker = this._createDecoderWorker(generation);
    if (!worker) {
      throw new Error("Browser cannot create the JPEG decoder worker");
    }
    this._jpegDecodeErrorReported = 0;
    await this._decoderWorkerRequest(worker, {
      type: "configure_jpeg_decoder",
      generation,
    });
    if (generation !== this._generation || this._decoderWorker !== worker) {
      throw new Error("SIP video session was superseded");
    }
    this._decoder = {
      state: "configured",
      kind: "jpeg",
      decodeQueueSize: 0,
      worker,
    };
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
    return this._active && this._canReceive && this._stats.rendered > 0;
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
    return { ...this._cameraSettings };
  }

  setCameraDevicePreference(deviceId = "") {
    if (this._encoder) return false;
    this._cameraDeviceId = String(deviceId || "");
    return true;
  }

  async switchCamera(deviceId = "") {
    const selected = String(deviceId || "");
    if (!this._active || !this._cameraEnabled || !this._cameraAllowed || !this._negotiated) {
      this._cameraDeviceId = selected;
      return {};
    }
    const currentId = String(this._cameraSettings.deviceId || "");
    if (selected && selected === currentId) return this.cameraSettings;
    return this._replaceSender(
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
        this._cameraReader ||
        this._encodeTask ||
        this._encoder ||
        this._cameraStream ||
        this._jpegEncoderWorker ||
        this._jpegEncodePending ||
        this._jpegQueuedFrame ||
        this._canSend
      );
      if (!changed && !hasSenderResources) return;
      await this._cleanupSender();
      this._emit();
      return;
    }
    if (!this._active || !this._cameraAllowed || !this._negotiated || this._encoder) return;
    const generation = this._generation;
    await this._ensureSender(this._mediaContract("send").codec, generation);
    if (
      generation === this._generation &&
      this._cameraEnabled &&
      this._encoder?.state === "configured"
    ) {
      this._canSend = true;
      this._emit();
    }
  }

  setCanvas(canvas) {
    this._canvas = canvas || null;
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
        this._decodeMessage(event.data);
      } else if (!this._decoder || this._decoder.state !== "configured") {
        this._bufferDecodeMessage(event.data);
      } else {
        this._decodeMessage(event.data);
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
    this._forceCameraKeyFrame = true;
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
    const generation = ++this._generation;
    const senderCleanup = this._cleanupSender();
    this._cleanupReceiver();
    this._active = false;
    await senderCleanup;
    if (this._ws !== ws || this._callId !== callId || generation !== this._generation) return;
    this._negotiated = negotiated;
    this._updateMediaSummary(negotiated);
    this._rtpTimestampBase = null;
    this._rtpTimestampLast = null;
    this._rtpTimestampTicks = 0;
    this._lastRenderedAt = 0;
    this._lastRenderedTimestamp = null;
    this._lastDecodedAt = 0;
    this._lastDecodedTimestamp = null;
    this._encodedFrames = 0;
    this._forceCameraKeyFrame = true;
    this._sendDropUntilKeyFrame = false;
    this._pendingDecode = [];
    this._pendingDecodeBytes = 0;
    this._dropUntilKeyFrame = true;
    this._jpegDecodePending = false;
    this._jpegQueuedBuffer = null;
    this._jpegDecodeToken = null;
    this._jpegDecodeErrorReported = 0;
    await this._setupCodecs(negotiated, generation);
    if (this._ws !== ws || this._callId !== callId || generation !== this._generation) return;
    this._active = Boolean(this._canReceive || this._canSend || this._cameraAllowed);
    this._emit();
  }

  async _setupCodecs(negotiated, generation) {
    if (generation !== this._generation) {
      throw new Error("SIP video session was superseded");
    }
    const receive = this._mediaContract("receive", negotiated);
    const send = this._mediaContract("send", negotiated);
    const failures = [];
    let usablePaths = 0;
    if (negotiated?.can_receive) {
      try {
        if (receive.encoding === "JPEG") {
          await this._setupJpegDecoder(generation);
          this._canReceive = true;
          this._active = true;
          usablePaths++;
          this._flushPendingDecode();
          this._emit();
        } else {
          if (typeof VideoDecoder === "undefined") {
            throw new Error("WebCodecs VideoDecoder is unavailable");
          }
          await this._setupDecoder(receive.codec, generation);
          this._canReceive = true;
          this._active = true;
          usablePaths++;
          this._flushPendingDecode();
          this._emit();
        }
      } catch (err) {
        if (generation !== this._generation) throw err;
        this._cleanupReceiver();
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
          await this._ensureSender(send.codec, generation);
          if (
            generation !== this._generation ||
            !this._cameraEnabled ||
            this._encoder?.state !== "configured"
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

  async _ensureSender(codec, generation) {
    if (this._encoder?.state === "configured") return;
    if (this._senderSetupPromise) return this._senderSetupPromise;
    return this._replaceSender(codec, generation, this._cameraDeviceId, false);
  }

  _senderIsCurrent(generation, senderGeneration) {
    return (
      generation === this._generation &&
      senderGeneration === this._senderGeneration &&
      this._cameraEnabled &&
      this._cameraAllowed
    );
  }

  _senderSetupIsCurrent(generation, switchGeneration) {
    return (
      generation === this._generation &&
      switchGeneration === this._senderSwitchGeneration &&
      this._cameraEnabled &&
      this._cameraAllowed
    );
  }

  _cameraCaptureContract(deviceId = this._cameraDeviceId) {
    const contract = cameraCaptureContract(this._mediaContract("send"));
    const selected = String(deviceId || "");
    if (selected) {
      contract.constraints = {
        ...contract.constraints,
        deviceId: { exact: selected },
      };
    }
    return contract;
  }

  async _replaceSender(codec, generation, deviceId, exact) {
    const switchGeneration = ++this._senderSwitchGeneration;
    const senderGeneration = ++this._senderSerial;
    let setup = this._prepareSender(
      codec,
      generation,
      senderGeneration,
      switchGeneration,
      deviceId,
    );
    this._senderSetupPromise = setup;
    let prepared = null;
    try {
      try {
        prepared = await setup;
      } catch (err) {
        if (exact || !deviceId || !this._senderSetupIsCurrent(generation, switchGeneration)) {
          throw err;
        }
        setup = this._prepareSender(
          codec,
          generation,
          senderGeneration,
          switchGeneration,
          "",
        );
        this._senderSetupPromise = setup;
        prepared = await setup;
      }
      if (!this._senderSetupIsCurrent(generation, switchGeneration)) {
        throw new Error("SIP video session was superseded or camera transmission was disabled");
      }
      const previous = this._takeSenderResources();
      this._senderGeneration = senderGeneration;
      this._cameraDeviceId = String(prepared.deviceId || deviceId || "");
      this._cameraSettings = { ...prepared.settings };
      this._cameraStream = prepared.stream;
      this._encoder = prepared.encoder;
      this._cameraReader = prepared.reader;
      this._jpegEncoderWorker = prepared.encoder.kind === "jpeg"
        ? prepared.encoder.worker
        : null;
      this._jpegEncodePending = false;
      this._jpegQueuedFrame = null;
      this._encodedFrames = 0;
      this._forceCameraKeyFrame = true;
      this._encodeTask = this._encodeCamera(
        prepared.framerate,
        prepared.reader,
        prepared.encoder,
        generation,
        senderGeneration,
      );
      prepared = null;
      this._canSend = true;
      this._sendTxEpoch();
      await this._disposeSenderResources(previous);
      this._emit();
      return this.cameraSettings;
    } finally {
      if (prepared) await this._disposeSenderResources(prepared);
      if (this._senderSetupPromise === setup) {
        this._senderSetupPromise = null;
      }
    }
  }

  async _prepareSender(
    codec,
    generation,
    senderGeneration,
    switchGeneration,
    deviceId,
  ) {
    const send = this._mediaContract("send");
    if (
      typeof MediaStreamTrackProcessor === "undefined" ||
      !navigator.mediaDevices?.getUserMedia ||
      (send.encoding === "JPEG" && typeof Worker === "undefined") ||
      (send.encoding !== "JPEG" && typeof VideoEncoder === "undefined")
    ) {
      throw new Error("Browser cannot capture negotiated SIP video");
    }
    let stream = null;
    let encoder = null;
    let reader = null;
    let prepared = false;
    try {
      const captureContract = this._cameraCaptureContract(deviceId);
      stream = await navigator.mediaDevices.getUserMedia({
        video: captureContract.constraints,
        audio: false,
      });
      if (!this._senderSetupIsCurrent(generation, switchGeneration)) {
        throw new Error("SIP video session was superseded or camera transmission was disabled");
      }
      const track = stream.getVideoTracks()[0];
      if (!track) throw new Error("No browser camera track available");
      const settings = track.getSettings();
      const {
        width,
        height,
        macroblocks,
        framerate,
      } = cameraEncoderContract(
        captureContract,
        settings,
        send.encoding,
      );
      if (macroblocks > captureContract.maxFs) {
        throw new Error("Browser camera exceeds the negotiated SIP video frame size");
      }
      if (send.encoding === "JPEG") {
        encoder = await this._createJpegEncoder(
          width,
          height,
          generation,
          senderGeneration,
          () => this._senderSetupIsCurrent(generation, switchGeneration),
        );
      } else {
        const encoderConfig = await this._supportedConfig(VideoEncoder, {
          codec,
          width,
          height,
          framerate,
          bitrate: 600000,
          ...(send.encoding === "H264" ? { avc: { format: "annexb" } } : {}),
        }, send.encoding === "H264");
        if (!this._senderSetupIsCurrent(generation, switchGeneration)) {
          throw new Error("SIP video session was superseded or camera transmission was disabled");
        }
        const support = await VideoEncoder.isConfigSupported(encoderConfig);
        if (!this._senderSetupIsCurrent(generation, switchGeneration)) {
          throw new Error("SIP video session was superseded or camera transmission was disabled");
        }
        if (!support?.supported) {
          throw new Error(`Browser cannot encode negotiated SIP video ${codec}`);
        }
        encoder = new VideoEncoder({
          output: (chunk) => this._sendEncodedChunk(
            chunk,
            generation,
            senderGeneration,
            encoder,
          ),
          error: () => {
            if (
              generation !== this._generation ||
              senderGeneration !== this._senderGeneration ||
              this._encoder !== encoder
            ) return;
            this._stats.dropped++;
            this._emit();
          },
        });
        encoder.configure(support.config || encoderConfig);
      }
      const processor = new MediaStreamTrackProcessor({ track });
      reader = processor.readable.getReader();
      if (!this._senderSetupIsCurrent(generation, switchGeneration)) {
        throw new Error("SIP video session was superseded or camera transmission was disabled");
      }
      prepared = true;
      return {
        stream,
        encoder,
        reader,
        framerate,
        deviceId: String(settings.deviceId || deviceId || ""),
        settings,
      };
    } finally {
      if (!prepared) {
        if (reader) await reader.cancel().catch(() => {});
        if (encoder && encoder.state !== "closed") encoder.close();
        if (stream) stream.getTracks().forEach((track) => track.stop());
      }
    }
  }

  async _supportedConfig(codecClass, base, realtimeHardware = false) {
    // Keep camera encoding off the Android main CPU when MediaCodec can
    // satisfy the exact negotiated profile. The software fallback uses
    // quality mode: WebCodecs realtime mode may discard encoder inputs, and
    // a frame_num gap in a dependent H.264 GOP is not recoverable by the P4
    // decoder until the next IDR. Our bounded encode queue still drops raw
    // frames before admission, which preserves a valid encoded dependency
    // chain without allowing latency to grow.
    const candidates = realtimeHardware
      ? [
          { ...base, hardwareAcceleration: "prefer-hardware", latencyMode: "realtime" },
          { ...base, hardwareAcceleration: "prefer-software", latencyMode: "quality" },
          { ...base, latencyMode: "quality" },
        ]
      : [
          { ...base, hardwareAcceleration: "prefer-hardware", latencyMode: "realtime" },
          { ...base, hardwareAcceleration: "prefer-software", latencyMode: "realtime" },
          { ...base, latencyMode: "realtime" },
        ];
    for (const candidate of candidates) {
      try {
        const support = await codecClass.isConfigSupported(candidate);
        if (support?.supported) return support.config || candidate;
      } catch (_) {}
    }
    return base;
  }

  async _encodeCamera(framerate, reader, encoder, generation, senderGeneration) {
    const keyInterval = Math.max(1, Math.round(framerate * 2));
    const minimumFrameIntervalMs = 1000 / Math.max(1, framerate);
    const minimumFrameIntervalUs = minimumFrameIntervalMs * 1000;
    let lastAcceptedSourceTimestamp = null;
    let nextAcceptedFrameAt = 0;
    try {
      while (
        this._senderIsCurrent(generation, senderGeneration) &&
        this._cameraReader === reader &&
        this._encoder === encoder &&
        encoder.state === "configured"
      ) {
        const { value, done } = await reader.read();
        let frame = value;
        if (done || !frame) break;
        try {
          if (
            !this._senderIsCurrent(generation, senderGeneration) ||
            this._cameraReader !== reader ||
            this._encoder !== encoder
          ) {
            break;
          }
          const sourceTimestamp = Number(frame.timestamp);
          if (Number.isFinite(sourceTimestamp) && sourceTimestamp >= 0) {
            if (
              lastAcceptedSourceTimestamp !== null &&
              sourceTimestamp >= lastAcceptedSourceTimestamp &&
              sourceTimestamp - lastAcceptedSourceTimestamp <
                minimumFrameIntervalUs
            ) {
              this._stats.dropped++;
              continue;
            }
            lastAcceptedSourceTimestamp = sourceTimestamp;
          } else {
            const now = performance.now();
            if (now < nextAcceptedFrameAt) {
              this._stats.dropped++;
              continue;
            }
            nextAcceptedFrameAt = now + minimumFrameIntervalMs;
          }
          // getUserMedia constraints are advisory on some browsers. Enforce
          // the negotiated SDP a=framerate envelope at the actual encoder
          // admission point without a timer or an idle polling task.
          if (encoder.kind === "jpeg") {
            this._dispatchJpegFrame(
              frame,
              encoder.worker,
              encoder,
              generation,
              senderGeneration,
            );
            // VideoFrame ownership is now either in the worker or in the
            // single latest-frame coalescing slot.
            frame = null;
          } else if (encoder.encodeQueueSize > 2) {
            this._stats.dropped++;
          } else {
            const keyFrame = this._forceCameraKeyFrame || this._encodedFrames % keyInterval === 0;
            encoder.encode(frame, { keyFrame });
            this._forceCameraKeyFrame = false;
            this._encodedFrames++;
          }
        } finally {
          frame?.close();
        }
      }
    } catch (_) {
      if (
        generation === this._generation &&
        senderGeneration === this._senderGeneration &&
        this._active
      ) this._stats.dropped++;
    } finally {
      // A camera track can end without an explicit card action (USB removal,
      // browser privacy revocation, laptop sleep). Do not keep advertising a
      // live browser sender or retain its stream/encoder after EOF.
      if (
        generation === this._generation &&
        senderGeneration === this._senderGeneration &&
        this._cameraReader === reader &&
        this._encoder === encoder
      ) {
        this._cameraReader = null;
        this._encoder = null;
        this._jpegEncoderWorker = null;
        this._jpegEncodePending = false;
        this._jpegQueuedFrame?.close();
        this._jpegQueuedFrame = null;
        this._cameraStream?.getTracks?.().forEach((track) => track.stop());
        this._cameraStream = null;
        this._encodeTask = null;
        this._canSend = false;
        if (encoder.state !== "closed") {
          try { encoder.close(); } catch (_) {}
        }
        this._emit();
      }
    }
  }

  _sendEncodedChunk(
    chunk,
    generation = this._generation,
    senderGeneration = this._senderGeneration,
    encoder = this._encoder,
  ) {
    const keyFrame = chunk.type === "key";
    if (chunk.byteLength > MAX_VIDEO_ACCESS_UNIT_BYTES) {
      this._stats.dropped++;
      this._sendDropUntilKeyFrame = true;
      this._forceCameraKeyFrame = true;
      return;
    }
    const ws = this._encodedAccessUnitSocket(
      keyFrame,
      generation,
      senderGeneration,
      encoder,
    );
    if (!ws) return;
    const payload = new Uint8Array(chunk.byteLength);
    chunk.copyTo(payload);
    this._writeEncodedAccessUnit(
      ws,
      payload,
      Number(chunk.timestamp || 0),
      keyFrame,
    );
  }

  _sendEncodedAccessUnit(
    payload,
    timestamp,
    keyFrame,
    generation = this._generation,
    senderGeneration = this._senderGeneration,
    encoder = this._encoder,
  ) {
    if (
      !payload ||
      payload.byteLength > MAX_VIDEO_ACCESS_UNIT_BYTES
    ) {
      this._stats.dropped++;
      this._sendDropUntilKeyFrame = true;
      this._forceCameraKeyFrame = true;
      return;
    }
    const ws = this._encodedAccessUnitSocket(
      keyFrame,
      generation,
      senderGeneration,
      encoder,
    );
    if (!ws) return;
    this._writeEncodedAccessUnit(ws, payload, timestamp, keyFrame);
  }

  _encodedAccessUnitSocket(keyFrame, generation, senderGeneration, encoder) {
    if (
      generation !== this._generation ||
      senderGeneration !== this._senderGeneration ||
      (encoder && this._encoder !== encoder)
    ) return null;
    const ws = this._ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return null;
    if (ws.bufferedAmount > MAX_VIDEO_WS_BUFFER) {
      this._stats.dropped++;
      this._sendDropUntilKeyFrame = true;
      this._forceCameraKeyFrame = true;
      return null;
    }
    if (this._sendDropUntilKeyFrame && !keyFrame) {
      this._stats.dropped++;
      this._forceCameraKeyFrame = true;
      return null;
    }
    if (keyFrame) this._sendDropUntilKeyFrame = false;
    return ws;
  }

  _writeEncodedAccessUnit(ws, payload, timestamp, keyFrame) {
    const frame = new Uint8Array(VIDEO_HEADER_BYTES + payload.byteLength);
    const view = new DataView(frame.buffer);
    frame[0] = VIDEO_ACCESS_UNIT;
    frame[1] = keyFrame ? 1 : 0;
    const rtpTimestamp = Math.round(
      Number(timestamp || 0) * this._mediaContract("send").clockRate / 1000000,
    ) >>> 0;
    view.setUint32(2, rtpTimestamp, false);
    frame.set(payload, VIDEO_HEADER_BYTES);
    ws.send(frame);
    this._stats.sent++;
    if ((this._stats.sent & 31) === 0) this._emit();
  }

  _decodeMessage(buffer) {
    if (this._mediaContract("receive").encoding === "JPEG") {
      this._decodeJpegMessage(buffer);
      return;
    }
    if (!this._decoder || this._decoder.state !== "configured") return;
    const bytes = new Uint8Array(buffer);
    if (bytes.byteLength <= VIDEO_HEADER_BYTES || bytes[0] !== VIDEO_ACCESS_UNIT) return;
    const keyFrame = Boolean(bytes[1] & 1);
    if (this._dropUntilKeyFrame && !keyFrame) {
      this._stats.dropped++;
      this._stats.dropped_decode_backpressure++;
      return;
    }
    if (!keyFrame && this._decoder.decodeQueueSize >= MAX_DECODE_QUEUE_FRAMES) {
      // Encoded delta frames are interdependent. Once latency pressure makes
      // us discard one, discard the rest of that GOP and ask the SIP sender
      // for a fresh key frame instead of growing WebCodecs' queue without a
      // bound or rendering a corrupted dependency chain.
      this._dropUntilKeyFrame = true;
      this._stats.dropped++;
      this._stats.dropped_decode_backpressure++;
      this._requestKeyFrame();
      return;
    }
    if (keyFrame) this._dropUntilKeyFrame = false;
    const rtpTimestamp = new DataView(buffer).getUint32(2, false);
    const timestamp = this._unwrapRtpTimestamp(rtpTimestamp);
    try {
      if (this._decoderWorker && this._decoder?.worker === this._decoderWorker) {
        this._decoder.decodeQueueSize++;
        this._decoderWorker.postMessage({
          type: "decode",
          generation: this._generation,
          keyFrame,
          timestamp,
          buffer,
          offset: VIDEO_HEADER_BYTES,
          length: bytes.byteLength - VIDEO_HEADER_BYTES,
        }, [buffer]);
      } else {
        this._decoder.decode(new EncodedVideoChunk({
          type: keyFrame ? "key" : "delta",
          timestamp,
          data: bytes.subarray(VIDEO_HEADER_BYTES),
        }));
      }
      this._stats.received++;
      if ((this._stats.received & 31) === 0) this._emit();
    } catch (_) {
      this._stats.decode_errors++;
      this._dropUntilKeyFrame = true;
      this._requestKeyFrame();
    }
  }

  _requestKeyFrame() {
    const ws = this._ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    try { ws.send(JSON.stringify({ type: "request_key_frame" })); } catch (_) {}
  }

  _decodeJpegMessage(buffer) {
    if (
      !this._decoderWorker ||
      this._decoder?.worker !== this._decoderWorker ||
      this._decoder?.kind !== "jpeg"
    ) return;
    const bytes = new Uint8Array(buffer);
    if (bytes.byteLength <= VIDEO_HEADER_BYTES || bytes[0] !== VIDEO_ACCESS_UNIT) return;
    if (this._jpegDecodePending) {
      this._jpegQueuedBuffer = buffer;
      this._stats.dropped++;
      this._stats.dropped_render_coalesce++;
      return;
    }
    const rtpTimestamp = new DataView(buffer).getUint32(2, false);
    const timestamp = this._unwrapRtpTimestamp(rtpTimestamp);
    const generation = this._generation;
    const worker = this._decoderWorker;
    const token = worker;
    this._jpegDecodePending = true;
    this._jpegDecodeToken = token;
    try {
      worker.postMessage({
        type: "decode_jpeg",
        generation,
        timestamp,
        buffer,
        offset: VIDEO_HEADER_BYTES,
        length: bytes.byteLength - VIDEO_HEADER_BYTES,
      }, [buffer]);
    } catch (_) {
      if (
        generation === this._generation &&
        this._jpegDecodeToken === token
      ) {
        this._jpegDecodePending = false;
        this._jpegDecodeToken = null;
        this._stats.decode_errors++;
      }
    }
  }

  _bufferDecodeMessage(buffer) {
    const bytes = new Uint8Array(buffer);
    if (bytes.byteLength <= VIDEO_HEADER_BYTES || bytes[0] !== VIDEO_ACCESS_UNIT) return;
    const keyFrame = Boolean(bytes[1] & 1);
    // A decoder can only join the stream from an IDR. Retain a bounded GOP
    // while WebCodecs and camera permission are being prepared, replacing it
    // whenever a newer key frame arrives.
    if (keyFrame) {
      this._pendingDecode = [];
      this._pendingDecodeBytes = 0;
    } else if (!this._pendingDecode.length) {
      return;
    }
    if (
      this._pendingDecode.length >= MAX_PENDING_DECODE_FRAMES ||
      this._pendingDecodeBytes + bytes.byteLength > MAX_PENDING_DECODE_BYTES
    ) {
      this._stats.dropped++;
      this._stats.dropped_pending_decode++;
      return;
    }
    this._pendingDecode.push(buffer);
    this._pendingDecodeBytes += bytes.byteLength;
  }

  _flushPendingDecode() {
    const pending = this._pendingDecode;
    this._pendingDecode = [];
    this._pendingDecodeBytes = 0;
    // An empty buffer does not prove decoder synchronisation. Keep dropping
    // deltas until a real key frame is observed after codec setup/update.
    if (!pending.length) return;
    this._dropUntilKeyFrame = false;
    for (const buffer of pending) this._decodeMessage(buffer);
  }

  _unwrapRtpTimestamp(value) {
    if (this._rtpTimestampBase === null) {
      this._rtpTimestampBase = value;
      this._rtpTimestampLast = value;
      this._rtpTimestampTicks = 0;
      return 0;
    }
    let delta = (value - this._rtpTimestampLast) >>> 0;
    if (delta >= 0x80000000) delta -= 0x100000000;
    this._rtpTimestampTicks += delta;
    this._rtpTimestampLast = value;
    return Math.round(
      this._rtpTimestampTicks * 1000000 / this._mediaContract("receive").clockRate,
    );
  }

  _queueDecodedFrame(frame) {
    if (!frame) return;
    const now = performance.now();
    const timestamp = Number(frame.timestamp || 0);
    if (this._lastRenderedTimestamp !== null && timestamp < this._lastRenderedTimestamp) {
      frame.close();
      this._stats.dropped++;
      this._stats.dropped_timestamp_regression++;
      return;
    }
    if (this._lastDecodedAt) {
      this._stats.max_arrival_gap_ms = Math.max(
        this._stats.max_arrival_gap_ms,
        Math.round(now - this._lastDecodedAt),
      );
    }
    if (this._lastDecodedTimestamp !== null && timestamp >= this._lastDecodedTimestamp) {
      this._stats.max_source_gap_ms = Math.max(
        this._stats.max_source_gap_ms,
        Math.round((timestamp - this._lastDecodedTimestamp) / 1000),
      );
    }
    this._lastDecodedAt = now;
    this._lastDecodedTimestamp = timestamp;
    if (!this._canvas) {
      frame.close();
      this._stats.dropped++;
      this._stats.dropped_no_canvas++;
      return;
    }
    if (this._pendingRenderedFrame) {
      this._pendingRenderedFrame.close();
      this._stats.dropped++;
      this._stats.dropped_render_coalesce++;
    }
    this._pendingRenderedFrame = frame;
    if (this._renderFrameHandle) return;
    const schedule = globalThis.requestAnimationFrame ||
      ((callback) => window.setTimeout(() => callback(performance.now()), 0));
    this._renderFrameHandle = schedule(() => {
      this._renderFrameHandle = 0;
      const latest = this._pendingRenderedFrame;
      this._pendingRenderedFrame = null;
      const timestamp = Number(latest?.timestamp || 0);
      if (!latest || !this._drawFrame(latest)) {
        if (latest) {
          this._stats.dropped++;
          this._stats.dropped_no_canvas++;
        }
        return;
      }
      const renderedAt = performance.now();
      if (this._lastRenderedAt) {
        const gap = Math.round(renderedAt - this._lastRenderedAt);
        this._stats.max_frame_gap_ms = Math.max(this._stats.max_frame_gap_ms, gap);
        if (gap > 100) this._stats.render_gaps_over_100_ms++;
        if (gap > 250) this._stats.render_gaps_over_250_ms++;
      }
      this._lastRenderedAt = renderedAt;
      this._lastRenderedTimestamp = timestamp;
      const firstRenderedFrame = this._stats.rendered === 0;
      this._stats.rendered++;
      if (firstRenderedFrame) this._emit();
    });
    return;
  }

  _drawFrame(frame) {
    try {
      const canvas = this._canvas;
      if (!canvas) return false;
      const width = frame.displayWidth || frame.codedWidth;
      const height = frame.displayHeight || frame.codedHeight;
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      const context = canvas.getContext("2d", { alpha: false });
      if (!context) return false;
      context.drawImage(frame.bitmap || frame, 0, 0, width, height);
      return true;
    } finally {
      frame.close();
    }
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
    const senderCleanup = this._cleanupSender();
    this._cleanupReceiver();
    if (generation === this._generation) {
      this._callId = "";
      this._endpointId = "default";
      this._active = false;
      this._canReceive = false;
      this._canSend = false;
      this._rtpTimestampBase = null;
      this._rtpTimestampLast = null;
      this._rtpTimestampTicks = 0;
      this._encodedFrames = 0;
      this._forceCameraKeyFrame = true;
      this._sendDropUntilKeyFrame = false;
      this._pendingDecode = [];
      this._pendingDecodeBytes = 0;
      this._dropUntilKeyFrame = false;
      this._jpegDecodePending = false;
      this._jpegQueuedBuffer = null;
      this._jpegDecodeToken = null;
      this._jpegDecodeErrorReported = 0;
      this._negotiated = null;
      this._cameraAllowed = false;
      this._lastRenderedAt = 0;
      this._lastRenderedTimestamp = null;
      this._lastDecodedAt = 0;
      this._lastDecodedTimestamp = null;
      this._emit();
    }
    await senderCleanup;
  }

  async _cleanupSender() {
    // Invalidate in-flight permission/config probes and detach all published
    // resources before the first await. A later call can then create its own
    // sender while this call finishes cancelling its old reader/task.
    this._senderSwitchGeneration++;
    this._senderGeneration = ++this._senderSerial;
    this._senderSetupPromise = null;
    const resources = this._takeSenderResources();
    this._cameraSettings = {};
    this._canSend = false;
    await this._disposeSenderResources(resources);
  }

  _takeSenderResources() {
    const reader = this._cameraReader;
    const encodeTask = this._encodeTask;
    const encoder = this._encoder;
    const stream = this._cameraStream;
    const queuedJpegFrame = this._jpegQueuedFrame;
    this._cameraReader = null;
    this._encodeTask = null;
    this._encoder = null;
    this._cameraStream = null;
    this._jpegEncoderWorker = null;
    this._jpegEncodePending = false;
    this._jpegQueuedFrame = null;
    return { reader, encodeTask, encoder, stream, queuedJpegFrame };
  }

  async _disposeSenderResources(resources = {}) {
    const { reader, encodeTask, encoder, stream, queuedJpegFrame } = resources;
    queuedJpegFrame?.close?.();
    let readerCancel = Promise.resolve();
    if (reader) {
      try {
        readerCancel = Promise.resolve(reader.cancel()).catch(() => {});
      } catch (_) {}
    }
    if (encoder && encoder.state !== "closed") {
      // JPEG proxy close() synchronously terminates its DedicatedWorker.
      try { encoder.close(); } catch (_) {}
    }
    if (stream) stream.getTracks().forEach((track) => track.stop());
    await settleWithin(Promise.all([
      readerCancel,
      encodeTask ? Promise.resolve(encodeTask).catch(() => {}) : Promise.resolve(),
    ]));
  }

  _cleanupReceiver() {
    const worker = this._decoderWorker;
    this._decoderWorker = null;
    if (worker) {
      try { worker.postMessage({ type: "close" }); } catch (_) {}
      try { worker.terminate(); } catch (_) {}
    }
    for (const pending of this._decoderWorkerRequests.values()) {
      (globalThis.clearTimeout || globalThis.window?.clearTimeout)?.(pending.timer);
      pending.reject(new Error("decoder worker closed"));
    }
    this._decoderWorkerRequests.clear();
    if (this._decoder && this._decoder.worker !== worker) {
      if (this._decoder.state !== "closed") this._decoder.close();
    }
    this._decoder = null;
    if (this._pendingRenderedFrame) {
      this._pendingRenderedFrame.close();
      this._pendingRenderedFrame = null;
    }
    if (this._renderFrameHandle) {
      const cancel = globalThis.cancelAnimationFrame || window.clearTimeout;
      cancel(this._renderFrameHandle);
      this._renderFrameHandle = 0;
    }
    this._pendingDecode = [];
    this._pendingDecodeBytes = 0;
    this._jpegDecodePending = false;
    this._jpegQueuedBuffer = null;
    this._jpegDecodeToken = null;
    this._jpegDecodeErrorReported = 0;
    this._canReceive = false;
  }
}
