// Physical browser audio resources. Call identity and policy stay in the engine.
const MODULE_VERSION = (() => {
  try {
    return new URL(import.meta.url).searchParams.get("v") || "dev";
  } catch (_) {
    return "dev";
  }
})();
const { exactDeviceConstraint } = await import(`./voip-stack-media-devices.js?v=${encodeURIComponent(MODULE_VERSION)}`);
const WS_AUDIO = 1;
const MAX_AUDIO_WS_BUFFER_MS = 120;
const MIN_AUDIO_WS_BUFFER_FRAMES = 4;
const MEDIA_CLEANUP_TIMEOUT_MS = 750;

export class AudioSetupSupersededError extends Error {
  constructor() {
    super("Audio setup superseded");
  }
}

export function settleWithin(promise, timeoutMs = MEDIA_CLEANUP_TIMEOUT_MS) {
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

export class BrowserAudioSession {
  constructor({ getPreference, captureEnabled, onCapture, onPlaybackStats, onLevel }) {
    this._getPreference = getPreference;
    this._captureEnabled = captureEnabled;
    this._onCapture = onCapture;
    this._onPlaybackStats = onPlaybackStats;
    this._onLevel = onLevel;
    this.ready = false;
    this.prepared = null;
    this._setupGeneration = 0;
    this._microphoneSwitchGeneration = 0;
    this._outputSwitchGeneration = 0;
    this.audioContext = null;
    this.mediaStream = null;
    this.captureNode = null;
    this.captureSink = null;
    this.source = null;
    this.playbackNode = null;
    this.playbackAnalyser = null;
    this.txFormat = null;
    this.rxFormat = null;
    this.level = 0;
    this._levelFrame = 0;
    this._frameBuffer = null;
  }

  supportsOutputSelection(context = this.audioContext) {
    if (typeof context?.setSinkId === "function") return true;
    const Ctor = window.AudioContext || window.webkitAudioContext;
    return typeof Ctor?.prototype?.setSinkId === "function";
  }

  _audioCaptureConstraints(deviceId, processing = true) {
    const base = processing
      ? { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
      : {};
    return exactDeviceConstraint(base, deviceId);
  }

  async _openAudioInput(deviceId, { exact = false, processing = true } = {}) {
    const selected = String(deviceId || "");
    try {
      return await navigator.mediaDevices.getUserMedia({
        audio: selected ? this._audioCaptureConstraints(selected, processing) : processing ? this._audioCaptureConstraints("", true) : true,
      });
    } catch (err) {
      if (exact || !selected) throw err;
      return navigator.mediaDevices.getUserMedia({
        audio: processing ? this._audioCaptureConstraints("", true) : true,
      });
    }
  }

  async probeMicrophonePermission() {
    if (!navigator.mediaDevices?.getUserMedia) return false;
    let stream = null;
    try {
      stream = await this._openAudioInput(
        this._getPreference("audioinput"), { processing: false },
      );
      return Boolean(stream?.getAudioTracks?.()[0]);
    } catch (err) {
      console.warn("voip-stack-engine: microphone permission unavailable", err);
      return false;
    } finally {
      for (const track of stream?.getTracks?.() || []) track.stop();
    }
  }

  async selectMicrophone(deviceId, commitSelection) {
    const generation = ++this._microphoneSwitchGeneration;
    const context = this.audioContext;
    const captureNode = this.captureNode;
    const stream = await this._openAudioInput(deviceId, { exact: true, processing: true });
    const track = stream?.getAudioTracks?.()[0];
    if (!track) {
      stream?.getTracks?.().forEach((item) => item.stop());
      throw new Error("No microphone audio track is available.");
    }
    const settings = track.getSettings?.() || {};
    this.applyMicrophoneEnabled(stream);
    if (
      generation !== this._microphoneSwitchGeneration ||
      context !== this.audioContext ||
      captureNode !== this.captureNode
    ) {
      stream.getTracks().forEach((item) => item.stop());
      throw new Error("Microphone selection was superseded");
    }
    if (!context || !captureNode) {
      stream.getTracks().forEach((item) => item.stop());
    } else {
      const source = context.createMediaStreamSource(stream);
      source.connect(captureNode);
      const previousSource = this.source;
      const previousStream = this.mediaStream;
      this.source = source;
      this.mediaStream = stream;
      try { previousSource?.disconnect(); } catch (_) {}
      previousStream?.getTracks?.().forEach((item) => item.stop());
    }
    return commitSelection(settings);
  }

  async selectAudioOutput(deviceId, commitSelection) {
    const generation = ++this._outputSwitchGeneration;
    let context = this.audioContext;
    let selected = deviceId;
    if (selected && typeof navigator.mediaDevices?.selectAudioOutput === "function") {
      selected = String((await navigator.mediaDevices.selectAudioOutput({
        deviceId: selected,
      }))?.deviceId || selected);
    }
    if (generation !== this._outputSwitchGeneration || context !== this.audioContext) {
      throw new Error("Audio output selection was superseded");
    }
    let temporary = false;
    if (!context) {
      context = this._createAudioContext();
      temporary = true;
    }
    try {
      if (typeof context.setSinkId !== "function") {
        if (selected) throw new Error("This browser controls audio output through the operating system.");
      } else {
        await context.setSinkId(selected);
      }
      if (generation !== this._outputSwitchGeneration || (!temporary && context !== this.audioContext)) {
        throw new Error("Audio output selection was superseded");
      }
      return await commitSelection(selected);
    } finally {
      if (temporary) await settleWithin(context.close());
    }
  }

  async _loadAudioWorklets(context, { capture, playback }) {
    const modules = [];
    if (capture) modules.push("voip-stack-processor.js");
    if (playback) modules.push("voip-stack-playback-processor.js");
    await Promise.all(modules.map((name) => context.audioWorklet.addModule(
      `/voip-stack/${name}?v=${encodeURIComponent(MODULE_VERSION)}`,
    )));
  }

  async prepare({ needsMicrophone = true } = {}) {
    if (window.isSecureContext === false) {
      throw new Error("Browser audio requires Home Assistant over HTTPS or another secure context.");
    }
    const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextCtor || typeof globalThis.AudioWorkletNode !== "function") {
      throw new Error("This browser does not provide the Web Audio features required for calls.");
    }
    if (needsMicrophone && !navigator.mediaDevices?.getUserMedia) {
      throw new Error("Microphone access is unavailable in this browser or Home Assistant app.");
    }
    await this.discardPrepared();
    const resources = {
      audioContext: null, mediaStream: null, source: null, captureSink: null, readinessNode: null,
      ready: false, cancel: null,
    };
    this.prepared = resources;
    const assertCurrent = () => {
      if (this.prepared !== resources) throw new Error("Audio preparation cancelled");
    };
    try {
      if (needsMicrophone) {
        const stream = await this._openAudioInput(
          this._getPreference("audioinput"), { processing: true },
        );
        if (this.prepared !== resources) {
          for (const track of stream?.getTracks?.() || []) track.stop();
          assertCurrent();
        }
        resources.mediaStream = stream;
        if (!stream?.getAudioTracks?.()[0]) throw new Error("No microphone audio track is available.");
      }
      // Open output after capture selects the native communication audio route.
      const context = resources.audioContext = this._createAudioContext();
      if (context.state === "suspended") await context.resume();
      assertCurrent();
      const output = this._getPreference("audiooutput");
      if (output && typeof context.setSinkId === "function") {
        await context.setSinkId(output).catch(() => context.setSinkId(""));
        assertCurrent();
      }
      if (resources.mediaStream) {
        resources.source = context.createMediaStreamSource(resources.mediaStream);
        resources.captureSink = context.createGain();
        resources.captureSink.gain.value = 0;
        resources.source.connect(resources.captureSink).connect(context.destination);
      }
      // Load processors before INVITE/answer so immediate RTP does not wait on downloads.
      await this._loadAudioWorklets(context, { capture: needsMicrophone, playback: true });
      assertCurrent();
      // A running context and a short BufferSource's ended event can precede
      // processing of the live microphone graph. Admit SIP only after that
      // graph has reached the audio rendering thread, without choosing a
      // fabricated PCM format before negotiation.
      const probe = resources.readinessNode = new AudioWorkletNode(
        context, "voip-stack-audio-ready", {
          numberOfInputs: needsMicrophone ? 1 : 0,
          outputChannelCount: [1],
          processorOptions: { needsMicrophone },
        },
      );
      resources.source?.connect(probe);
      probe.connect(context.destination);
      try {
        await new Promise((resolve, reject) => {
          let previousClock = null;
          resources.cancel = () => reject(new Error("Audio preparation cancelled"));
          probe.port.onmessage = (event) => {
            if (event.data?.type !== "ready") return;
            // The initial render can prefill a small hardware buffer before
            // the device clock actually starts. Wait for its output timestamp,
            // or a fresh render round-trip when that public API is absent.
            const timestamp = context.getOutputTimestamp?.();
            const outputStarted = timestamp
              ? timestamp.performanceTime > 0 && timestamp.contextTime > 0
              : previousClock !== null && context.currentTime > previousClock;
            if (outputStarted) resolve();
            else {
              previousClock = context.currentTime;
              probe.port.postMessage({ type: "check" });
            }
          };
          probe.onprocessorerror = () => reject(new Error("Browser audio preparation failed"));
        });
      } finally {
        resources.cancel = null;
        probe.port.onmessage = null;
        probe.onprocessorerror = null;
      }
      assertCurrent();
      resources.ready = true;
      return true;
    } catch (err) {
      if (this.prepared === resources) await this.discardPrepared();
      throw err;
    }
  }

  async discardPrepared() {
    const resources = this.prepared;
    this.prepared = null;
    if (!resources) return;
    resources.cancel?.();
    await this.dispose(resources);
  }

  _createAudioContext() {
    const Ctor = window.AudioContext || window.webkitAudioContext;
    return new Ctor();
  }

  send(buffer, socket, canSend, muted) {
    if (!socket || socket.readyState !== WebSocket.OPEN || !canSend) return 0;
    const bytes = new Uint8Array(buffer);
    if (!bytes.byteLength) return 0;
    const bytesPerSample = this.txFormat?.pcmFormat === "s16le" ? 2 :
      this.txFormat?.pcmFormat === "s24le" ? 3 : 4;
    const bytesPerSecond = Number(this.txFormat?.sampleRate || 0) *
      Number(this.txFormat?.channels || 0) * bytesPerSample;
    const maxBufferedBytes = Math.max(
      bytes.byteLength * MIN_AUDIO_WS_BUFFER_FRAMES,
      Math.ceil(bytesPerSecond * MAX_AUDIO_WS_BUFFER_MS / 1000),
    );
    if (socket.bufferedAmount >= maxBufferedBytes) {
      return -1;
    }
    if (!this._frameBuffer || this._frameBuffer.byteLength !== bytes.byteLength + 1) {
      this._frameBuffer = new Uint8Array(bytes.byteLength + 1);
    }
    const frame = this._frameBuffer;
    frame[0] = WS_AUDIO;
    // Keep the PCM clock while silencing frames queued before the user muted.
    if (muted) frame.fill(0, 1);
    else frame.set(bytes, 1);
    socket.send(frame);
    return 1;
  }

  receive(buffer, canReceive) {
    const raw = new Uint8Array(buffer);
    if (raw[0] !== WS_AUDIO || raw.byteLength < 2 || !this.playbackNode || !canReceive) return false;
    this.playbackNode.port.postMessage({ type: "audio", buffer, byteOffset: 1 }, [buffer]);
    return true;
  }

  resumeRemoteSilence() {
    this.playbackNode?.port.postMessage({ type: "remote_silence_resume" });
  }

  _emitAudioLevel(level) {
    const next = Math.max(0, Math.min(1, Number(level) || 0));
    if (Math.abs(next - this.level) < 0.005) return;
    this.level = next;
    this._onLevel(next);
  }

  _startAudioLevel(analyser) {
    this._stopAudioLevel();
    if (!analyser) return;
    const schedule = globalThis.requestAnimationFrame || window.requestAnimationFrame;
    if (typeof schedule !== "function") return;
    analyser.fftSize = 256;
    analyser.smoothingTimeConstant = 0.5;
    const samples = new Float32Array(analyser.fftSize);
    let lastSampleAt = 0;
    let smoothed = 0;
    const tick = (timestamp = performance.now()) => {
      if (this.playbackAnalyser !== analyser) return;
      if (timestamp - lastSampleAt >= 50) {
        lastSampleAt = timestamp;
        analyser.getFloatTimeDomainData(samples);
        let power = 0;
        for (const sample of samples) power += sample * sample;
        const rms = Math.sqrt(power / samples.length);
        const decibels = rms > 0 ? 20 * Math.log10(rms) : -100;
        const target = rms < 0.003
          ? 0
          : Math.max(0, Math.min(1, (decibels + 50) / 35));
        smoothed += (target - smoothed) * (target > smoothed ? 0.4 : 0.16);
        this._emitAudioLevel(smoothed < 0.01 ? 0 : smoothed);
      }
      this._levelFrame = schedule(tick);
    };
    this._levelFrame = schedule(tick);
  }

  _stopAudioLevel() {
    const cancel = globalThis.cancelAnimationFrame || window.cancelAnimationFrame;
    if (this._levelFrame && typeof cancel === "function") {
      cancel(this._levelFrame);
    }
    this._levelFrame = 0;
    this._emitAudioLevel(0);
  }

  async setup({ capture, playback, formats, microphoneAntiAlias }, {
    assertCurrent: assertOwnerCurrent, activate,
  }) {
    const setupGeneration = ++this._setupGeneration;
    const prepared = this.prepared?.ready ? this.prepared : null;
    if (prepared) this.prepared = null;
    const resources = {
      audioContext: prepared?.audioContext || null,
      mediaStream: prepared?.mediaStream || null,
      captureNode: null,
      captureSink: prepared?.captureSink || null,
      source: prepared?.source || null,
      playbackNode: null,
      playbackAnalyser: null,
      readinessNode: prepared?.readinessNode || null,
    };
    const assertCurrent = () => {
      if (setupGeneration !== this._setupGeneration) {
        throw new AudioSetupSupersededError();
      }
      assertOwnerCurrent();
    };

    try {
      assertCurrent();
      if (capture || playback) {
        resources.audioContext ||= this._createAudioContext();
        if (resources.audioContext.state === "suspended") {
          await resources.audioContext.resume();
          assertCurrent();
        }
        const output = this._getPreference("audiooutput");
        if (output && resources.audioContext.sinkId !== output && typeof resources.audioContext.setSinkId === "function") {
          try {
            await resources.audioContext.setSinkId(output);
            assertCurrent();
          } catch (_) {
            await resources.audioContext.setSinkId("").catch(() => {});
          }
        }
      }

      if (!capture && resources.mediaStream) {
        resources.source?.disconnect();
        resources.captureSink?.disconnect();
        resources.source = null;
        resources.captureSink = null;
        for (const track of resources.mediaStream.getTracks()) track.stop();
        resources.mediaStream = null;
      }
      if (!capture && !playback && resources.audioContext) {
        await resources.audioContext.close();
        resources.audioContext = null;
      }
      if (capture || playback) {
        await this._loadAudioWorklets(resources.audioContext, {
          capture: capture && !prepared?.mediaStream,
          playback: playback && !prepared,
        });
        assertCurrent();
      }
      if (capture) {
        resources.mediaStream ||= await this._openAudioInput(
          this._getPreference("audioinput"),
          { processing: true },
        );
        assertCurrent();
        resources.source ||= resources.audioContext.createMediaStreamSource(resources.mediaStream);
        resources.captureNode = new AudioWorkletNode(resources.audioContext, "voip-stack-processor", {
          processorOptions: {
            format: formats.tx,
            antiAlias: microphoneAntiAlias,
          },
        });
        resources.captureNode.port.onmessage = (event) => {
          if (
            this.captureNode === resources.captureNode &&
            event.data?.type === "audio"
          ) this._onCapture(event.data.buffer);
        };
        resources.source.connect(resources.captureNode);
        resources.captureSink ||= resources.audioContext.createGain();
        resources.captureSink.gain.value = 0;
        resources.captureNode.connect(resources.captureSink);
        if (prepared?.captureSink) {
          // Keep the prepared input owner alive while the negotiated capture
          // node is attached. Remove only its old muted bypass afterwards.
          resources.source.disconnect(resources.captureSink);
        } else {
          resources.captureSink.connect(resources.audioContext.destination);
        }
      }

      if (playback) {
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
            this.playbackNode !== resources.playbackNode ||
            event.data?.type !== "stats"
          ) return;
          this._onPlaybackStats(event.data);
        };
        if (typeof resources.audioContext.createAnalyser === "function") {
          resources.playbackAnalyser = resources.audioContext.createAnalyser();
          resources.playbackNode
            .connect(resources.playbackAnalyser)
            .connect(resources.audioContext.destination);
        } else {
          resources.playbackNode.connect(resources.audioContext.destination);
        }
      }
      assertCurrent();
      // Keep preparation's graph alive until both negotiated paths are wired.
      if (resources.readinessNode) {
        resources.readinessNode.port.postMessage({ type: "stop" });
        try { resources.source?.disconnect(resources.readinessNode); } catch (_) {}
        resources.readinessNode.disconnect();
        resources.readinessNode = null;
      }
      const previous = this._takeAudioResources();
      this.txFormat = formats.tx;
      this.rxFormat = formats.rx;
      this.audioContext = resources.audioContext;
      this.mediaStream = resources.mediaStream;
      this.captureNode = resources.captureNode;
      this.captureSink = resources.captureSink;
      this.source = resources.source;
      this.playbackNode = resources.playbackNode;
      this.playbackAnalyser = resources.playbackAnalyser;
      this.ready = true;
      // The engine publishes its selected policy before capture can run.
      activate();
      this._startAudioLevel(resources.playbackAnalyser);
      await this.dispose(previous);
    } catch (err) {
      await this.dispose(resources);
      throw err;
    }
  }

  applyMicrophoneEnabled(stream = this.mediaStream) {
    const enabled = this._captureEnabled();
    let changed = false;
    for (const track of stream?.getAudioTracks?.() || []) {
      if (track.enabled !== enabled) {
        track.enabled = enabled;
        changed = true;
      }
    }
    return changed;
  }

  detach() {
    this._setupGeneration++;
    this._microphoneSwitchGeneration++;
    this._outputSwitchGeneration++;
    this.ready = false;
    return this._takeAudioResources();
  }

  _takeAudioResources() {
    this._stopAudioLevel();
    const resources = {
      captureNode: this.captureNode,
      captureSink: this.captureSink,
      source: this.source,
      mediaStream: this.mediaStream,
      playbackNode: this.playbackNode,
      playbackAnalyser: this.playbackAnalyser,
      audioContext: this.audioContext,
    };
    this.captureNode = null;
    this.captureSink = null;
    this.source = null;
    this.mediaStream = null;
    this.playbackNode = null;
    this.playbackAnalyser = null;
    this.audioContext = null;
    this._frameBuffer = null;
    return resources;
  }

  async dispose(resources = {}) {
    if (resources.readinessNode) {
      resources.readinessNode.port.onmessage = null;
      resources.readinessNode.onprocessorerror = null;
      try { resources.readinessNode.port.postMessage({ type: "stop" }); } catch (_) {}
      try { resources.readinessNode.disconnect(); } catch (_) {}
    }
    try { resources.captureNode?.disconnect(); } catch (_) {}
    try { resources.captureSink?.disconnect(); } catch (_) {}
    try { resources.source?.disconnect(); } catch (_) {}
    try { resources.mediaStream?.getTracks?.().forEach((track) => track.stop()); } catch (_) {}
    try { resources.playbackNode?.disconnect(); } catch (_) {}
    try { resources.playbackAnalyser?.disconnect(); } catch (_) {}
    if (resources.audioContext) {
      await settleWithin(resources.audioContext.close());
    }
  }

}
