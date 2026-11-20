const VIDEO_MODULE_VERSION = (() => {
  try {
    return new URL(import.meta.url).searchParams.get("v") || "dev";
  } catch (_) {
    return "dev";
  }
})();

const [
  { cameraCaptureContract, cameraEncoderContract, jpegImagePlan },
  { VIDEO_ACCESS_UNIT, VIDEO_HEADER_BYTES, CODEC_SETUP_TIMEOUT_MS,
    codecWorkerUrl, supportedConfig, encoderCandidates, settleWithin },
] = await Promise.all([
  import(`./voip-stack-video-model.js?v=${encodeURIComponent(VIDEO_MODULE_VERSION)}`),
  import(`./voip-stack-video-codecs.js?v=${encodeURIComponent(VIDEO_MODULE_VERSION)}`),
]);
const MAX_VIDEO_ACCESS_UNIT_BYTES = 1024 * 1024;
const MAX_VIDEO_WS_BUFFER = 2 * 1024 * 1024;
const JPEG_CAMERA_QUALITY = 0.72;
const CAMERA_BITRATE = 600000;
const ENCODER_RATE_WINDOW_SECONDS = 2;
const ENCODER_RATE_MIN_WINDOW_SECONDS = 0.5;
const ENCODER_OVERSHOOT_FACTOR = 4;

class VideoFrameCadence {
  constructor() {
    this.reset();
  }

  reset() {
    this.period = 0;
    this.nextAt = null;
    this.lastAt = null;
  }

  // Admit a frame when zero is returned, otherwise report its remaining wait.
  // Keep phase across capture jitter. Like WebRTC's FramerateController, the
  // first target is half an interval ahead; a stall never banks extra frames.
  delay(now, framerate) {
    const period = Number.isFinite(framerate) && framerate > 0 ? 1 / framerate : 0;
    if (period !== this.period || (this.lastAt !== null && now < this.lastAt)) {
      this.period = period;
      this.nextAt = null;
    }
    this.lastAt = now;
    if (!period) return 0;
    if (this.nextAt === null || now - this.nextAt >= period) {
      this.nextAt = now + period / 2;
      return 0;
    }
    if (now < this.nextAt) return this.nextAt - now;
    this.nextAt += period;
    return 0;
  }
}

// Use the same reader contract when this context has no track processor.
// Each pending read arms at most one callback; a slow encoder cannot build
// a queue of captured frames or keep an otherwise idle timer running.
function createVideoElementFrameReader(stream, framerate, track) {
  let target = {};
  const video = document.createElement("video");
  video.muted = true;
  video.playsInline = true;
  video.setAttribute("playsinline", "");
  video.setAttribute("muted", "");
  Object.assign(video.style, {
    position: "fixed", width: "1px", height: "1px", opacity: "0",
    pointerEvents: "none", left: "0", top: "0",
  });
  video.srcObject = stream;
  document.body.appendChild(video);
  const canvas = document.createElement("canvas");
  const context2d = canvas.getContext("2d", { alpha: false });
  let cancelled = false, waiter = null, callbackId = null, timer = null;
  let rejectStartup = null, lastTime = -1, lastTimestamp = -1;
  const intervalMs = Math.max(10, Math.round(1000 / Math.max(1, framerate || 15)));
  const useRvfc = typeof video.requestVideoFrameCallback === "function";
  const finish = (result) => {
    const resolve = waiter;
    waiter = null;
    resolve?.(result);
  };
  const emit = (mediaTime) => {
    if (track.readyState === "ended") { cleanup(); return; }
    if (cancelled || !waiter || !(video.readyState >= 2)) return;
    try {
      const seconds = Number.isFinite(mediaTime) ? mediaTime : video.currentTime;
      // Zero is a valid first media timestamp. Mixing it with performance.now()
      // made the next frame jump backwards to a different clock origin.
      const candidate = Number.isFinite(seconds) && seconds >= 0
        ? Math.round(seconds * 1e6) : Math.max(0, lastTimestamp + intervalMs * 1000);
      const timestamp = Math.max(lastTimestamp + 1, candidate);
      const sourceWidth = video.videoWidth, sourceHeight = video.videoHeight;
      if (!(sourceWidth > 0 && sourceHeight > 0) || !context2d) return;
      const width = Math.max(2, (Number(target.width) || sourceWidth) & ~1);
      const height = Math.max(2, (Number(target.height) || sourceHeight) & ~1);
      if (canvas.width !== width) canvas.width = width;
      if (canvas.height !== height) canvas.height = height;
      // drawImage honours display orientation. Letterbox a later rotation or
      // aspect change instead of stretching it into the negotiated geometry.
      const scale = Math.min(width / sourceWidth, height / sourceHeight);
      const drawnWidth = sourceWidth * scale, drawnHeight = sourceHeight * scale;
      context2d.fillStyle = "black";
      context2d.fillRect(0, 0, width, height);
      context2d.drawImage(video, (width - drawnWidth) / 2, (height - drawnHeight) / 2, drawnWidth, drawnHeight);
      const frame = new VideoFrame(canvas, { timestamp });
      lastTimestamp = timestamp;
      finish({ value: frame, done: false });
    } catch (_) { /* Retry only while the existing read is pending. */ }
  };
  const onVideoFrame = (_now, metadata) => {
    callbackId = null;
    emit(metadata?.mediaTime);
    arm();
  };
  const onTimer = () => {
    timer = null;
    const time = video.currentTime;
    if (time !== lastTime) { lastTime = time; emit(time); }
    arm();
  };
  const arm = () => {
    if (track.readyState === "ended") { cleanup(); return; }
    if (cancelled || !waiter || callbackId !== null || timer !== null) return;
    if (useRvfc) callbackId = video.requestVideoFrameCallback(onVideoFrame);
    else timer = setTimeout(onTimer, intervalMs);
  };
  const cleanup = () => {
    if (cancelled) return;
    cancelled = true;
    track.removeEventListener("ended", cleanup);
    rejectStartup?.(new Error("Camera frame reader cancelled"));
    if (timer !== null) clearTimeout(timer);
    timer = null;
    if (callbackId !== null) {
      try { video.cancelVideoFrameCallback?.(callbackId); } catch (_) {}
      callbackId = null;
    }
    try { video.pause(); } catch (_) {}
    video.srcObject = null;
    try { video.remove(); } catch (_) {}
    finish({ done: true });
  };
  track.addEventListener("ended", cleanup);
  return {
    async start() {
      let timeout;
      try {
        if (track.readyState === "ended") cleanup();
        if (cancelled) throw new Error("Camera frame reader cancelled");
        await Promise.race([
          video.play(),
          new Promise((_resolve, reject) => {
            rejectStartup = reject;
            timeout = setTimeout(() => reject(new Error("Camera playback did not start")), CODEC_SETUP_TIMEOUT_MS);
          }),
        ]);
        if (cancelled) throw new Error("Camera frame reader cancelled");
      } catch (error) { cleanup(); throw error; }
      finally { clearTimeout(timeout); rejectStartup = null; }
    },
    get size() {
      return video.videoWidth > 0 && video.videoHeight > 0
        ? { width: video.videoWidth, height: video.videoHeight } : null;
    },
    setTarget(next) { target = next || {}; },
    read() {
      if (track.readyState === "ended") cleanup();
      if (cancelled) return Promise.resolve({ done: true });
      if (waiter) return Promise.reject(new Error("Camera frame read already pending"));
      return new Promise((resolve) => { waiter = resolve; arm(); });
    },
    cancel() { cleanup(); return Promise.resolve(); },
  };
}

// Some Android hardware encoders ignore the configured bitrate on camera
// frames and send many times the target, which backs slower network paths up.
// Flag the encoder as soon as its delta frames exceed 4 times the target,
// since everything sent before the switch delays live video.
class EncoderRateGuard {
  constructor(targetBitrate) {
    this.targetBitrate = targetBitrate;
    // Bits per second of delta frames when the encoder was flagged.
    this.measuredBitrate = null;
    this.overTarget = false;
    this.done = false;
    this.deltaBytes = 0;
    this.startUs = null;
  }

  // Process each encoded chunk until the encoder is flagged or the window ends.
  addChunk(chunk) {
    if (this.done) return;
    const timestamp = Number(chunk.timestamp || 0);
    if (this.startUs === null) this.startUs = timestamp;
    const elapsedSeconds = (timestamp - this.startUs) / 1000000;
    // Key frames are large on any encoder, so only monitor delta frames.
    if (chunk.type !== "key") this.deltaBytes += chunk.byteLength;
    // Trigger only once delta frames total at least 0.5s worth at 4 times the
    // target, so a normal encoder's first few frames can't trip it.
    const windowSeconds = Math.max(elapsedSeconds, ENCODER_RATE_MIN_WINDOW_SECONDS);
    const allowedBytes =
      this.targetBitrate / 8 * ENCODER_OVERSHOOT_FACTOR * windowSeconds;
    if (this.deltaBytes > allowedBytes) {
      // Log the rate actually sent, not the rate over the minimum window.
      this.measuredBitrate = this.deltaBytes * 8 / Math.max(elapsedSeconds, 0.001);
      this.overTarget = true;
      this.done = true;
    } else if (elapsedSeconds >= ENCODER_RATE_WINDOW_SECONDS) {
      this.done = true;
    }
  }
}

// Own camera resources only; session identity, policy and transport stay outside.
export class BrowserVideoSender {
  constructor({ sessionGeneration, canCapture, isActive, sendContract, socket,
    readStats, notify, onSending, onTxEpoch }) {
    this._sessionGeneration = sessionGeneration;
    this._canCapture = canCapture;
    this._isActive = isActive;
    this._sendContract = sendContract;
    this._socket = socket;
    this._readStats = readStats;
    this._notify = notify;
    this._onSending = onSending;
    this._onTxEpoch = onTxEpoch;
    this._workerUrl = codecWorkerUrl;
    this._encoder = null;
    this._cameraStream = null;
    this._cameraReader = null;
    this._encodeTask = null;
    this._jpegEncoderWorker = null;
    this._jpegEncodePending = false;
    this._jpegQueuedFrame = null;
    this._forceCameraKeyFrame = true;
    this._sendDropUntilKeyFrame = false;
    this._encodedFrames = 0;
    this._senderGeneration = 0;
    this._senderSerial = 0;
    this._senderSwitchGeneration = 0;
    this._senderSetupPromise = null;
    this._cameraDeviceId = "";
    this._cameraSettings = {};
  }

  get cameraSettings() {
    return { ...this._cameraSettings };
  }

  _resetMediaState() {
    this._encodedFrames = 0;
    this._forceCameraKeyFrame = true;
    this._sendDropUntilKeyFrame = false;
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
    const worker = new Worker(this._workerUrl(), { type: "module" });
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
      imageAttributes: this._sendContract().imageAttributes,
      sourceGeometry: "",
      imagePlan: null,
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
        generation === this._sessionGeneration() &&
        senderGeneration === this._senderGeneration
      ) {
        this._readStats().dropped++;
        void this._cleanupSender();
        this._notify();
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
      generation !== this._sessionGeneration() ||
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
      this._readStats().dropped++;
      this._notify();
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
        this._readStats().dropped++;
      }
      this._jpegQueuedFrame = frame;
      return;
    }
    const width = Number(frame.displayWidth || frame.codedWidth || 0);
    const height = Number(frame.displayHeight || frame.codedHeight || 0);
    const geometry = `${width}x${height}`;
    if (encoder.sourceGeometry !== geometry) {
      encoder.sourceGeometry = geometry;
      encoder.imagePlan = jpegImagePlan(width, height, encoder.imageAttributes);
    }
    if (encoder.imageAttributes?.recv && !encoder.imageAttributes.recv.wildcard && !encoder.imagePlan) {
      frame.close();
      this._readStats().dropped++;
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
        imagePlan: encoder.imagePlan,
        frame,
      }, [frame]);
    } catch (error) {
      this._jpegEncodePending = false;
      encoder.encodeQueueSize = 0;
      frame.close();
      throw error;
    }
  }

  async _ensureSender(codec, generation) {
    if (this._encoder?.state === "configured") return;
    if (this._senderSetupPromise) return this._senderSetupPromise;
    return this._replaceSender(codec, generation, this._cameraDeviceId, false);
  }

  _senderIsCurrent(generation, senderGeneration) {
    return (
      generation === this._sessionGeneration() &&
      senderGeneration === this._senderGeneration &&
      this._canCapture()
    );
  }

  _senderSetupIsCurrent(generation, switchGeneration) {
    return (
      generation === this._sessionGeneration() &&
      switchGeneration === this._senderSwitchGeneration &&
      this._canCapture()
    );
  }

  _cameraCaptureContract(deviceId = this._cameraDeviceId) {
    const contract = cameraCaptureContract(this._sendContract());
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
    this._senderSetupReader?.cancel();
    this._senderSetupReader = null;
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
        prepared.maxFramerate,
      );
      prepared = null;
      this._onSending(true);
      this._onTxEpoch();
      await this._disposeSenderResources(previous);
      this._notify();
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
    const send = this._sendContract();
    const hasTrackProcessor = typeof MediaStreamTrackProcessor !== "undefined";
    const hasVideoElementCapture =
      typeof VideoFrame !== "undefined" &&
      typeof document !== "undefined" &&
      typeof document.createElement === "function";
    if (
      !(hasTrackProcessor || hasVideoElementCapture) ||
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
      let settings = track.getSettings();
      if (!hasTrackProcessor) {
        reader = createVideoElementFrameReader(
          stream,
          Number(settings.frameRate) || 15,
          track,
        );
        this._senderSetupReader = reader;
        await reader.start();
        // getSettings() can report the unrotated sensor size while the video
        // element (and therefore our frames) is upright. Configure the encoder
        // with the orientation we will actually feed it, otherwise WebCodecs
        // rescales the frames and the picture looks stretched.
        const size = reader.size;
        if (size) settings = { ...settings, ...size };
      }
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
        const baseConfig = {
          codec,
          width,
          height,
          framerate,
          bitrate: CAMERA_BITRATE,
          ...(send.encoding === "H264" ? { avc: { format: "annexb" } } : {}),
        };
        const encoderConfig = await supportedConfig(
          VideoEncoder,
          baseConfig,
          send.encoding === "H264",
        );
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
        const configured = support.config || encoderConfig;
        let rateGuard = configured.hardwareAcceleration === "prefer-software"
          ? null
          : new EncoderRateGuard(CAMERA_BITRATE);
        encoder = new VideoEncoder({
          output: (chunk) => {
            rateGuard?.addChunk(chunk);
            if (rateGuard?.overTarget) {
              void this._fallBackToSoftwareEncoder(
                encoder,
                encoderCandidates(baseConfig, send.encoding === "H264")[1],
                rateGuard.measuredBitrate,
                generation,
                senderGeneration,
              );
              rateGuard = null;
            }
            this._sendEncodedChunk(chunk, generation, senderGeneration, encoder);
          },
          error: () => {
            if (
              generation !== this._sessionGeneration() ||
              senderGeneration !== this._senderGeneration ||
              this._encoder !== encoder
            ) return;
            this._readStats().dropped++;
            this._notify();
          },
        });
        encoder.configure(configured);
      }
      if (hasTrackProcessor) {
        const processor = new MediaStreamTrackProcessor({ track });
        reader = processor.readable.getReader();
      } else {
        reader.setTarget({ width, height });
      }
      if (!this._senderSetupIsCurrent(generation, switchGeneration)) {
        throw new Error("SIP video session was superseded or camera transmission was disabled");
      }
      prepared = true;
      return {
        stream,
        encoder,
        reader,
        framerate,
        maxFramerate: Math.min(
          captureContract.maxFr,
          captureContract.maxMbps / Math.max(1, macroblocks),
        ),
        deviceId: String(settings.deviceId || deviceId || ""),
        settings,
      };
    } finally {
      if (this._senderSetupReader === reader) this._senderSetupReader = null;
      if (!prepared) {
        if (reader) await reader.cancel().catch(() => {});
        if (encoder && encoder.state !== "closed") encoder.close();
        if (stream) stream.getTracks().forEach((track) => track.stop());
      }
    }
  }

  // Reconfigure a camera encoder that EncoderRateGuard caught far over its
  // bitrate. The next frame is a key frame, so receivers resync cleanly.
  async _fallBackToSoftwareEncoder(encoder, config, bitrate, generation, senderGeneration) {
    console.warn(
      `voip-stack-video: camera encoder sent ${Math.round(bitrate / 1000)} kbit/s ` +
      `for a ${CAMERA_BITRATE / 1000} kbit/s target, switching to software encoding`,
    );
    try {
      const support = await VideoEncoder.isConfigSupported(config);
      if (
        !support?.supported ||
        !this._senderIsCurrent(generation, senderGeneration) ||
        this._encoder !== encoder ||
        encoder.state !== "configured"
      ) return;
      encoder.configure(support.config || config);
      this._forceCameraKeyFrame = true;
    } catch (_) {
      // Keep the hardware encoder: too much video beats no video.
    }
  }

  async _encodeCamera(
    framerate, reader, encoder, generation, senderGeneration, maxFramerate = framerate,
  ) {
    const keyInterval = Math.max(1, Math.round(framerate * 2));
    const cadence = new VideoFrameCadence();
    let usingSourceClock = null;
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
          const sourceClock = Number.isFinite(sourceTimestamp) && sourceTimestamp >= 0;
          if (sourceClock !== usingSourceClock) {
            cadence.reset();
            usingSourceClock = sourceClock;
          }
          const now = sourceClock ? sourceTimestamp / 1000000 : performance.now() / 1000;
          // Track FPS is an encoder/GOP estimate. Only the negotiated codec
          // envelope limits admission; an absent setting is not a hidden cap.
          if (cadence.delay(now, maxFramerate) > 0) {
            this._readStats().dropped++;
            continue;
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
            this._readStats().dropped++;
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
        generation === this._sessionGeneration() &&
        senderGeneration === this._senderGeneration &&
        this._isActive()
      ) this._readStats().dropped++;
    } finally {
      // A camera track can end without an explicit card action (USB removal,
      // browser privacy revocation, laptop sleep). Do not keep advertising a
      // live browser sender or retain its stream/encoder after EOF.
      if (
        generation === this._sessionGeneration() &&
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
        this._onSending(false);
        if (encoder.state !== "closed") {
          try { encoder.close(); } catch (_) {}
        }
        this._notify();
      }
    }
  }

  _sendEncodedChunk(
    chunk,
    generation = this._sessionGeneration(),
    senderGeneration = this._senderGeneration,
    encoder = this._encoder,
  ) {
    const keyFrame = chunk.type === "key";
    if (chunk.byteLength > MAX_VIDEO_ACCESS_UNIT_BYTES) {
      this._readStats().dropped++;
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
    generation = this._sessionGeneration(),
    senderGeneration = this._senderGeneration,
    encoder = this._encoder,
  ) {
    if (
      !payload ||
      payload.byteLength > MAX_VIDEO_ACCESS_UNIT_BYTES
    ) {
      this._readStats().dropped++;
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
      generation !== this._sessionGeneration() ||
      senderGeneration !== this._senderGeneration ||
      (encoder && this._encoder !== encoder)
    ) return null;
    const ws = this._socket();
    if (!ws || ws.readyState !== WebSocket.OPEN) return null;
    if (ws.bufferedAmount > MAX_VIDEO_WS_BUFFER) {
      this._readStats().dropped++;
      this._sendDropUntilKeyFrame = true;
      this._forceCameraKeyFrame = true;
      return null;
    }
    if (this._sendDropUntilKeyFrame && !keyFrame) {
      this._readStats().dropped++;
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
      Number(timestamp || 0) * this._sendContract().clockRate / 1000000,
    ) >>> 0;
    view.setUint32(2, rtpTimestamp, false);
    frame.set(payload, VIDEO_HEADER_BYTES);
    ws.send(frame);
    this._readStats().sent++;
    if ((this._readStats().sent & 31) === 0) this._notify();
  }

  async _cleanupSender() {
    // Invalidate in-flight permission/config probes and detach all published
    // resources before the first await. A later call can then create its own
    // sender while this call finishes cancelling its old reader/task.
    this._senderSwitchGeneration++;
    this._senderGeneration = ++this._senderSerial;
    this._senderSetupPromise = null;
    this._senderSetupReader?.cancel();
    this._senderSetupReader = null;
    const resources = this._takeSenderResources();
    this._cameraSettings = {};
    this._onSending(false);
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

}
