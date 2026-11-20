const VIDEO_MODULE_VERSION = (() => {
  try {
    return new URL(import.meta.url).searchParams.get("v") || "dev";
  } catch (_) {
    return "dev";
  }
})();

const { VIDEO_ACCESS_UNIT, VIDEO_HEADER_BYTES, CODEC_SETUP_TIMEOUT_MS,
  codecWorkerUrl, supportedConfig } = await import(
  `./voip-stack-video-codecs.js?v=${encodeURIComponent(VIDEO_MODULE_VERSION)}`,
);
const MAX_PENDING_DECODE_BYTES = 8 * 1024 * 1024;
const MAX_PENDING_DECODE_FRAMES = 60;
const MAX_DECODE_QUEUE_FRAMES = 8;

// Own decoder, presentation and frame leases without a call or socket identity.
export class BrowserVideoReceiver {
  constructor({ sessionGeneration, receiveContract, readStats, notify,
    requestKeyFrame, onDisabled }) {
    this._sessionGeneration = sessionGeneration;
    this._receiveContract = receiveContract;
    this._readStats = readStats;
    this._notify = notify;
    this._requestKeyFrame = requestKeyFrame;
    this._onDisabled = onDisabled;
    this._workerUrl = codecWorkerUrl;
    this._receiverHasFrame = false;
    this._canvas = null;
    this._decoder = null;
    this._decoderWorker = null;
    this._decoderWorkerRequests = new Map();
    this._decoderWorkerRequestId = 0;
    this._pendingRenderedFrame = null;
    this._renderFrameHandle = 0;
    this._rtpTimestampBase = null;
    this._rtpTimestampLast = null;
    this._rtpTimestampTicks = 0;
    this._pendingDecode = [];
    this._pendingDecodeBytes = 0;
    this._dropUntilKeyFrame = false;
    this._jpegDecodePending = false;
    this._jpegQueuedBuffer = null;
    this._jpegDecodeToken = null;
    this._jpegDecodeErrorReported = 0;
    this._lastRenderedAt = 0;
    this._lastRenderedTimestamp = null;
    this._lastDecodedAt = 0;
    this._lastDecodedTimestamp = null;
  }

  setCanvas(canvas) {
    const next = canvas || null;
    if (next !== this._canvas) this._receiverHasFrame = false;
    this._canvas = next;
  }

  _resetMediaState(dropUntilKeyFrame) {
    this._rtpTimestampBase = null;
    this._rtpTimestampLast = null;
    this._rtpTimestampTicks = 0;
    this._lastRenderedAt = 0;
    this._lastRenderedTimestamp = null;
    this._lastDecodedAt = 0;
    this._lastDecodedTimestamp = null;
    this._pendingDecode = [];
    this._pendingDecodeBytes = 0;
    this._dropUntilKeyFrame = dropUntilKeyFrame;
    this._jpegDecodePending = false;
    this._jpegQueuedBuffer = null;
    this._jpegDecodeToken = null;
    this._jpegDecodeErrorReported = 0;
  }

  _createDecoderWorker(generation) {
    if (typeof Worker === "undefined") return null;
    const worker = new Worker(this._workerUrl(), { type: "module" });
    worker.onmessage = (event) => this._handleDecoderWorkerMessage(
      worker,
      generation,
      event.data || {},
    );
    worker.onerror = (event) => {
      if (this._decoderWorker !== worker || generation !== this._sessionGeneration()) return;
      this._readStats().decode_errors++;
      this._dropUntilKeyFrame = true;
      this._requestKeyFrame();
      console.warn(`voip-stack-video: decoder worker failed (${event?.message || "unknown error"})`);
      this._notify();
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
      generation !== this._sessionGeneration() ||
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
      this._readStats().received++;
      this._queueDecodedFrame({
        bitmap: message.bitmap,
        timestamp: Number(message.timestamp || 0),
        displayWidth: message.bitmap.width,
        displayHeight: message.bitmap.height,
        close: () => message.bitmap.close(),
      });
      if ((this._readStats().received & 31) === 0) this._notify();
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
      this._readStats().decode_errors++;
      // Closing a native decoder aborts every queued chunk. The worker waits
      // for a key frame before recreating it, under the same generation.
      if (this._decoder) this._decoder.decodeQueueSize = 0;
      this._dropUntilKeyFrame = true;
      this._requestKeyFrame();
      this._notify();
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
      this._readStats().decode_errors += delta;
      const latest = this._jpegQueuedBuffer;
      this._jpegQueuedBuffer = null;
      if (latest) this._decodeJpegMessage(latest);
      if (delta > 0) this._notify();
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
    const decoderConfig = await supportedConfig(VideoDecoder, {
      codec,
      optimizeForLatency: true,
    });
    if (generation !== this._sessionGeneration()) {
      throw new Error("SIP video session was superseded");
    }
    const worker = this._createDecoderWorker(generation);
    if (worker) {
      await this._decoderWorkerRequest(worker, {
        type: "configure_decoder",
        generation,
        config: decoderConfig,
      });
      if (generation !== this._sessionGeneration() || this._decoderWorker !== worker) {
        throw new Error("SIP video session was superseded");
      }
      // Lightweight proxy keeps the existing queue/backpressure contract.
      this._decoder = { state: "configured", decodeQueueSize: 0, worker };
      return;
    }

    // Compatibility fallback for older browsers. Current HA Companion builds
    // use the worker path so codec stalls cannot monopolise the UI thread.
    const support = await VideoDecoder.isConfigSupported(decoderConfig);
    if (generation !== this._sessionGeneration()) {
      throw new Error("SIP video session was superseded");
    }
    if (!support?.supported) throw new Error(`browser cannot decode ${codec}`);
    let decoder;
    decoder = new VideoDecoder({
      output: (frame) => {
        if (generation !== this._sessionGeneration() || this._decoder !== decoder) {
          frame.close();
          return;
        }
        this._queueDecodedFrame(frame);
      },
      error: () => {
        if (generation !== this._sessionGeneration() || this._decoder !== decoder) return;
        this._readStats().decode_errors++;
        this._dropUntilKeyFrame = true;
        this._requestKeyFrame();
        this._notify();
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
    if (generation !== this._sessionGeneration() || this._decoderWorker !== worker) {
      throw new Error("SIP video session was superseded");
    }
    this._decoder = {
      state: "configured",
      kind: "jpeg",
      decodeQueueSize: 0,
      worker,
    };
  }

  _decodeMessage(buffer) {
    if (this._receiveContract().encoding === "JPEG") {
      this._decodeJpegMessage(buffer);
      return;
    }
    if (!this._decoder || this._decoder.state !== "configured") return;
    const bytes = new Uint8Array(buffer);
    if (bytes.byteLength <= VIDEO_HEADER_BYTES || bytes[0] !== VIDEO_ACCESS_UNIT) return;
    const keyFrame = Boolean(bytes[1] & 1);
    if (this._dropUntilKeyFrame && !keyFrame) {
      this._readStats().dropped++;
      this._readStats().dropped_decode_backpressure++;
      return;
    }
    if (!keyFrame && this._decoder.decodeQueueSize >= MAX_DECODE_QUEUE_FRAMES) {
      // Encoded delta frames are interdependent. Once latency pressure makes
      // us discard one, discard the rest of that GOP and ask the SIP sender
      // for a fresh key frame instead of growing WebCodecs' queue without a
      // bound or rendering a corrupted dependency chain.
      this._dropUntilKeyFrame = true;
      this._readStats().dropped++;
      this._readStats().dropped_decode_backpressure++;
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
          generation: this._sessionGeneration(),
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
      this._readStats().received++;
      if ((this._readStats().received & 31) === 0) this._notify();
    } catch (_) {
      this._readStats().decode_errors++;
      this._dropUntilKeyFrame = true;
      this._requestKeyFrame();
    }
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
      this._readStats().dropped++;
      this._readStats().dropped_render_coalesce++;
      return;
    }
    const rtpTimestamp = new DataView(buffer).getUint32(2, false);
    const timestamp = this._unwrapRtpTimestamp(rtpTimestamp);
    const generation = this._sessionGeneration();
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
        generation === this._sessionGeneration() &&
        this._jpegDecodeToken === token
      ) {
        this._jpegDecodePending = false;
        this._jpegDecodeToken = null;
        this._readStats().decode_errors++;
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
      this._readStats().dropped++;
      this._readStats().dropped_pending_decode++;
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
      this._rtpTimestampTicks * 1000000 / this._receiveContract().clockRate,
    );
  }

  _queueDecodedFrame(frame) {
    if (!frame) return;
    const now = performance.now();
    const timestamp = Number(frame.timestamp || 0);
    if (this._lastRenderedTimestamp !== null && timestamp < this._lastRenderedTimestamp) {
      frame.close();
      this._readStats().dropped++;
      this._readStats().dropped_timestamp_regression++;
      return;
    }
    if (this._lastDecodedAt) {
      this._readStats().max_arrival_gap_ms = Math.max(
        this._readStats().max_arrival_gap_ms,
        Math.round(now - this._lastDecodedAt),
      );
    }
    if (this._lastDecodedTimestamp !== null && timestamp >= this._lastDecodedTimestamp) {
      this._readStats().max_source_gap_ms = Math.max(
        this._readStats().max_source_gap_ms,
        Math.round((timestamp - this._lastDecodedTimestamp) / 1000),
      );
    }
    this._lastDecodedAt = now;
    this._lastDecodedTimestamp = timestamp;
    if (!this._canvas) {
      frame.close();
      this._readStats().dropped++;
      this._readStats().dropped_no_canvas++;
      return;
    }
    if (this._pendingRenderedFrame) {
      this._pendingRenderedFrame.close();
      this._readStats().dropped++;
      this._readStats().dropped_render_coalesce++;
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
          this._readStats().dropped++;
          this._readStats().dropped_no_canvas++;
        }
        return;
      }
      const renderedAt = performance.now();
      if (this._lastRenderedAt) {
        const gap = Math.round(renderedAt - this._lastRenderedAt);
        this._readStats().max_frame_gap_ms = Math.max(this._readStats().max_frame_gap_ms, gap);
        if (gap > 100) this._readStats().render_gaps_over_100_ms++;
        if (gap > 250) this._readStats().render_gaps_over_250_ms++;
      }
      this._lastRenderedAt = renderedAt;
      this._lastRenderedTimestamp = timestamp;
      const firstRenderedFrame = !this._receiverHasFrame;
      this._receiverHasFrame = true;
      this._readStats().rendered++;
      if (firstRenderedFrame) this._notify();
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

  _cleanupReceiver() {
    this._receiverHasFrame = false;
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
    this._onDisabled();
  }}
