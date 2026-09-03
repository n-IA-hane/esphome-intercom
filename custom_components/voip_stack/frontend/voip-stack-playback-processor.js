const PCM_FORMATS = Object.freeze(["s16le", "s24le", "s24le_in_s32", "s32le"]);
const FRAME_MS = Object.freeze([10, 16, 20, 32]);
const BUFFER_CAPACITY_SECONDS = 0.32;
const MIN_START_LATENCY_MS = 80;
const MAX_START_LATENCY_MS = 160;
const JITTER_SAFETY_MULTIPLIER = 4;
const STABLE_DECAY_SECONDS = 12;
const PLC_DECAY_PER_SAMPLE = 0.9997;
const CLOCK_RECOVERY_DEADBAND_FRAMES = 1;
const CLOCK_RECOVERY_PROPORTIONAL_GAIN = 0.001;
const CLOCK_RECOVERY_MAX_RATE = 0.005;
const SILENT_RECOVERY_MAX_RATE = 0.01;
const CLOCK_RECOVERY_SMOOTHING = 0.01;

function normaliseFormat(value) {
  if (!value) throw new Error("playback worklet requires negotiated PCM format");
  const sampleRate = Number(value.sampleRate);
  const frameMs = Number(value.frameMs);
  const channels = Number(value.channels);
  const pcmFormat = value.pcmFormat;
  if (!Number.isFinite(sampleRate) || !Number.isFinite(frameMs) || !Number.isFinite(channels)) {
    throw new Error("playback worklet PCM format has invalid numeric fields");
  }
  if (!PCM_FORMATS.includes(pcmFormat)) throw new Error(`playback worklet unsupported PCM format ${pcmFormat}`);
  if (![1, 2].includes(channels)) throw new Error(`playback worklet unsupported channel count ${channels}`);
  if (!FRAME_MS.includes(frameMs)) throw new Error(`playback worklet unsupported frame_ms ${frameMs}`);
  if ((sampleRate * frameMs) % 1000 !== 0) throw new Error("playback worklet PCM format does not form whole frames");
  const bytesPerSample = pcmFormat === "s16le" ? 2 : pcmFormat === "s24le" ? 3 : 4;
  return {
    sampleRate,
    frameMs,
    channels,
    pcmFormat,
    bytesPerSample,
    frameSamples: Math.floor((sampleRate * frameMs) / 1000),
  };
}

class VoipPlaybackProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this._format = normaliseFormat(options?.processorOptions?.format);
    this._contextFrameSamples = Math.max(1, Math.round(this._format.frameSamples * sampleRate / this._format.sampleRate));
    this._capacityFrames = Math.max(8, Math.ceil((BUFFER_CAPACITY_SECONDS * 1000) / this._format.frameMs));
    this._minStartFrames = Math.max(2, Math.ceil(MIN_START_LATENCY_MS / this._format.frameMs));
    this._maxStartFrames = Math.max(
      this._minStartFrames,
      Math.ceil(MAX_START_LATENCY_MS / this._format.frameMs),
    );
    this._dropFrames = this._maxStartFrames + 1;
    this._ring = new Float32Array(this._contextFrameSamples * this._format.channels * this._capacityFrames);
    this._read = 0;
    this._write = 0;
    this._available = 0;
    this._readFraction = 0;
    this._playbackRate = 1;
    this._started = false;
    this._framesIn = 0;
    this._framesOut = 0;
    this._framesDrop = 0;
    this._underruns = 0;
    this._lastStats = 0;
    this._targetStartFrames = Math.min(this._maxStartFrames, this._minStartFrames);
    this._lastUnderrun = 0;
    this._lastOutput = new Float32Array(this._format.channels);
    this._concealmentGain = 0;
    this._lastArrivalTime = 0;
    this._arrivalJitterMs = 0;
    this._adaptiveStartFrames = this._minStartFrames;
    this._maxArrivalGapMs = 0;
    this._arrivalGapsOver40Ms = 0;
    this._lastDeliveryTimeMs = 0;
    this._maxDeliveryGapMs = 0;
    this._levelPower = 0;
    this._levelSamples = 0;
    this._previousInput = new Float32Array(this._format.channels);
    this._hasPreviousInput = false;
    this._playbackReady = false;
    this._generation = 0;
    this._timelineReady = false;
    this._playoutTimestamp = 0;
    this._lateDiscard = 0;
    this._timelineGaps = 0;
    this._inUnderrun = false;
    this._lastSilent = true;

    const receive = (event) => {
      const data = event.data;
      if (data?.type === "audio" && data.buffer) {
        this._push(
          data.buffer,
          data.byteOffset || 0,
          data.arrivalMs,
          data.generation,
          data.sequence,
          data.timestamp,
        );
      } else if (data?.type === "bind_media_port" && data.port) {
        data.port.onmessage = receive;
        data.port.start?.();
      } else if (data?.type === "configure_timeline") {
        this._resetTimeline(Number(data.generation || 0) >>> 0);
      }
    };
    this.port.onmessage = receive;
  }

  _resetTimeline(generation) {
    this._generation = generation;
    this._timelineReady = false;
    this._playoutTimestamp = 0;
    this._read = 0;
    this._write = 0;
    this._available = 0;
    this._readFraction = 0;
    this._started = false;
    this._inUnderrun = false;
    this._playbackRate = 1;
    this._targetStartFrames = Math.min(this._maxStartFrames, this._minStartFrames);
    this._lastArrivalTime = 0;
    this._arrivalJitterMs = 0;
    this._adaptiveStartFrames = this._minStartFrames;
    this._hasPreviousInput = false;
  }

  _timestampDelta(value, reference) {
    const delta = ((Number(value) >>> 0) - (Number(reference) >>> 0)) >>> 0;
    return delta > 0x7fffffff ? delta - 0x100000000 : delta;
  }

  _decode(view, sampleIndex) {
    const offset = sampleIndex * this._format.bytesPerSample;
    if (this._format.pcmFormat === "s16le") return view.getInt16(offset, true) / 32768;
    if (this._format.pcmFormat === "s24le") {
      let v = view.getUint8(offset) | (view.getUint8(offset + 1) << 8) | (view.getUint8(offset + 2) << 16);
      if (v & 0x800000) v |= 0xff000000;
      return v / 8388608;
    }
    if (this._format.pcmFormat === "s24le_in_s32") return view.getInt32(offset, true) / 8388608;
    return view.getInt32(offset, true) / 2147483648;
  }

  _appendSilence(sourceSamples) {
    const samples = Math.round(
      sourceSamples * sampleRate / this._format.sampleRate,
    ) * this._format.channels;
    if (samples <= 0) return true;
    if (samples > this._ring.length - this._available) return false;
    const first = Math.min(samples, this._ring.length - this._write);
    this._ring.fill(0, this._write, this._write + first);
    if (first < samples) this._ring.fill(0, 0, samples - first);
    this._write = (this._write + samples) % this._ring.length;
    this._available += samples;
    return true;
  }

  _push(buffer, byteOffset = 0, arrivalMs, generation = this._generation, _sequence, timestamp) {
    const frameBytes = this._format.frameSamples * this._format.channels * this._format.bytesPerSample;
    if (byteOffset < 0 || buffer.byteLength - byteOffset !== frameBytes) return;
    if ((Number(generation) >>> 0) !== this._generation) return;
    const frameTimestamp = timestamp === undefined
      ? (this._timelineReady
        ? (Math.round(this._playoutTimestamp) + Math.round(
          this._available / this._format.channels * this._format.sampleRate / sampleRate,
        )) >>> 0
        : 0)
      : Number(timestamp) >>> 0;
    if (!this._timelineReady) {
      this._timelineReady = true;
      this._playoutTimestamp = frameTimestamp;
    } else {
      const queuedContextSamples = this._available / this._format.channels;
      const queuedSourceSamples = Math.round(
        queuedContextSamples * this._format.sampleRate / sampleRate,
      );
      const expectedTimestamp = (
        Math.round(this._playoutTimestamp) + queuedSourceSamples
      ) >>> 0;
      const delta = this._timestampDelta(frameTimestamp, expectedTimestamp);
      if (delta < -this._format.frameSamples / 2) {
        this._lateDiscard++;
        this._targetStartFrames = Math.min(
          this._maxStartFrames,
          this._targetStartFrames + 1,
        );
        return;
      }
      if (delta > this._format.frameSamples / 2) {
        this._timelineGaps += Math.max(
          1,
          Math.round(delta / this._format.frameSamples),
        );
        if (!this._appendSilence(delta)) {
          this._read = 0;
          this._write = 0;
          this._available = 0;
          this._readFraction = 0;
          this._started = false;
          this._playoutTimestamp = frameTimestamp;
          this._hasPreviousInput = false;
        }
      }
    }
    const frameSamples = this._contextFrameSamples * this._format.channels;
    this._updateArrivalJitter(arrivalMs);
    const deliveryTimeMs = currentTime * 1000;
    if (this._lastDeliveryTimeMs > 0) {
      const deliveryGapMs = deliveryTimeMs - this._lastDeliveryTimeMs;
      this._maxDeliveryGapMs = Math.max(this._maxDeliveryGapMs, deliveryGapMs);
    }
    this._lastDeliveryTimeMs = deliveryTimeMs;
    if (this._available >= frameSamples * this._dropFrames) {
      const queuedFrames = Math.floor(this._available / frameSamples);
      const framesToDrop = Math.max(1, queuedFrames - this._maxStartFrames);
      const samplesToDrop = framesToDrop * frameSamples;
      this._read = (this._read + samplesToDrop) % this._ring.length;
      this._available -= samplesToDrop;
      this._framesDrop += framesToDrop;
      this._playoutTimestamp = (
        this._playoutTimestamp + framesToDrop * this._format.frameSamples
      ) % 0x100000000;
    }
    const view = new DataView(buffer, byteOffset, frameBytes);
    if (!this._hasPreviousInput) {
      for (let ch = 0; ch < this._format.channels; ch++) {
        this._previousInput[ch] = this._decode(view, ch);
      }
      this._hasPreviousInput = true;
    }
    for (let i = 0; i < this._contextFrameSamples; i++) {
      // Keep one source sample across RTP frames. Resampling each frame in
      // isolation clamps its final interpolation to the last sample and then
      // jumps at the next frame, which is audible on 8 kHz G.711 calls.
      const srcPos = i * this._format.frameSamples / this._contextFrameSamples;
      const base = Math.floor(srcPos);
      const frac = srcPos - base;
      for (let ch = 0; ch < this._format.channels; ch++) {
        const a = base === 0
          ? this._previousInput[ch]
          : this._decode(view, (base - 1) * this._format.channels + ch);
        const b = this._decode(view, base * this._format.channels + ch);
        this._ring[this._write] = a + (b - a) * frac;
        this._write = (this._write + 1) % this._ring.length;
      }
    }
    for (let ch = 0; ch < this._format.channels; ch++) {
      this._previousInput[ch] = this._decode(
        view,
        (this._format.frameSamples - 1) * this._format.channels + ch,
      );
    }
    this._available += this._contextFrameSamples * this._format.channels;
    this._framesIn++;
    if (!this._started && this._available >= frameSamples * this._targetStartFrames) {
      this._started = true;
    }
  }

  _updateArrivalJitter(arrivalMs) {
    const now = Number(arrivalMs);
    if (!Number.isFinite(now) || now <= 0) return;
    if (this._lastArrivalTime > 0) {
      const arrivalGap = now - this._lastArrivalTime;
      this._maxArrivalGapMs = Math.max(this._maxArrivalGapMs, arrivalGap);
      if (arrivalGap >= 40) this._arrivalGapsOver40Ms++;
      const deviation = Math.abs(arrivalGap - this._format.frameMs);
      this._arrivalJitterMs += (deviation - this._arrivalJitterMs) / 16;
      this._adaptiveStartFrames = Math.min(
        this._maxStartFrames,
        Math.max(
          this._minStartFrames,
          Math.ceil(
            (MIN_START_LATENCY_MS + this._arrivalJitterMs * JITTER_SAFETY_MULTIPLIER)
              / this._format.frameMs,
          ),
        ),
      );
      this._targetStartFrames = Math.max(
        this._targetStartFrames,
        this._adaptiveStartFrames,
      );
    }
    this._lastArrivalTime = now;
  }

  process(_inputs, outputs) {
    const channels = outputs?.[0] || [];
    if (!channels.length) return true;

    if (!this._playbackReady) {
      this._playbackReady = true;
      this.port.postMessage({ type: "playback_ready" });
    }
    const availableFrameSamples = this._available / this._format.channels;
    const targetFrameSamples = this._targetStartFrames * this._contextFrameSamples;
    const errorFrames = (availableFrameSamples - targetFrameSamples) / this._contextFrameSamples;
    const controlledError = Math.abs(errorFrames) <= CLOCK_RECOVERY_DEADBAND_FRAMES
      ? 0
      : errorFrames - Math.sign(errorFrames) * CLOCK_RECOVERY_DEADBAND_FRAMES;
    const recoveryLimit = this._lastSilent
      ? SILENT_RECOVERY_MAX_RATE
      : CLOCK_RECOVERY_MAX_RATE;
    const correction = Math.max(
      -recoveryLimit,
      Math.min(recoveryLimit, controlledError * CLOCK_RECOVERY_PROPORTIONAL_GAIN),
    );
    const desiredRate = 1 + correction;
    if (
      (this._playbackRate > 1 && errorFrames <= CLOCK_RECOVERY_DEADBAND_FRAMES) ||
      (this._playbackRate < 1 && errorFrames >= -CLOCK_RECOVERY_DEADBAND_FRAMES)
    ) {
      this._playbackRate = 1;
    } else {
      this._playbackRate += (desiredRate - this._playbackRate) * CLOCK_RECOVERY_SMOOTHING;
    }
    let underrunThisQuantum = false;
    for (let i = 0; i < channels[0].length; i++) {
      if (!this._started) {
        for (let ch = 0; ch < channels.length; ch++) {
          const out = channels[ch];
          out[i] = this._inUnderrun
            ? this._lastOutput[Math.min(ch, this._format.channels - 1)] * this._concealmentGain
            : 0;
        }
        if (this._inUnderrun) this._concealmentGain *= PLC_DECAY_PER_SAMPLE;
        continue;
      }
      if (this._available < this._format.channels) {
        if (!underrunThisQuantum) {
          underrunThisQuantum = true;
          if (!this._inUnderrun) {
            this._inUnderrun = true;
            this._underruns++;
            this._lastUnderrun = currentTime;
            this._targetStartFrames = Math.min(
              this._maxStartFrames,
              this._targetStartFrames + 2,
            );
          }
          // Match the receive-buffer behaviour used by established SIP
          // clients: after an underflow, emit concealment while filling the
          // same bounded buffer again. Advancing the media timestamp while
          // empty makes every subsequently received frame look late and
          // prevents recovery from a transport burst.
          this._started = false;
        }
        for (let ch = 0; ch < channels.length; ch++) {
          channels[ch][i] = this._lastOutput[Math.min(ch, this._format.channels - 1)] * this._concealmentGain;
        }
        this._concealmentGain *= PLC_DECAY_PER_SAMPLE;
        continue;
      }
      this._inUnderrun = false;
      const nextFrameOffset = this._available >= this._format.channels * 2
        ? this._format.channels
        : 0;
      for (let ch = 0; ch < channels.length; ch++) {
        const inputChannel = Math.min(ch, this._format.channels - 1);
        const currentSample = this._ring[(this._read + inputChannel) % this._ring.length];
        const nextSample = this._ring[
          (this._read + nextFrameOffset + inputChannel) % this._ring.length
        ];
        const sample = currentSample + (nextSample - currentSample) * this._readFraction;
        channels[ch][i] = sample;
        this._lastOutput[inputChannel] = sample;
        if (ch === 0) {
          this._levelPower += sample * sample;
          this._levelSamples++;
        }
      }
      this._concealmentGain = 1;
      this._readFraction += this._playbackRate;
      const consumedFrames = Math.floor(this._readFraction);
      this._readFraction -= consumedFrames;
      const consumedSamples = Math.min(
        this._available,
        consumedFrames * this._format.channels,
      );
      this._read = (this._read + consumedSamples) % this._ring.length;
      this._available -= consumedSamples;
      this._playoutTimestamp = (
        this._playoutTimestamp + consumedFrames * this._format.sampleRate / sampleRate
      ) % 0x100000000;
    }
    this._framesOut++;
    if (currentTime - this._lastStats >= 1) {
      this._lastStats = currentTime;
      const rms = this._levelSamples > 0
        ? Math.sqrt(this._levelPower / this._levelSamples)
        : 0;
      const decibels = rms > 0 ? 20 * Math.log10(rms) : -100;
      const audioLevel = rms < 0.003
        ? 0
        : Math.max(0, Math.min(1, (decibels + 50) / 35));
      this._lastSilent = rms < 0.003;
      this._levelPower = 0;
      this._levelSamples = 0;
      if (
        this._targetStartFrames > this._minStartFrames &&
        this._targetStartFrames > this._adaptiveStartFrames &&
        currentTime - this._lastUnderrun >= STABLE_DECAY_SECONDS &&
        this._lastSilent
      ) {
        this._targetStartFrames--;
      }
      this.port.postMessage({
        type: "stats",
        buffered_frames: Math.floor(this._available / (this._contextFrameSamples * this._format.channels)),
        frames_in: this._framesIn,
        frames_out: this._framesOut,
        frames_drop: this._framesDrop,
        underruns: this._underruns,
        jitter_target_frames: this._targetStartFrames,
        jitter_target_ms: this._targetStartFrames * this._format.frameMs,
        max_arrival_gap_ms: Math.round(this._maxArrivalGapMs * 10) / 10,
        arrival_gaps_over_40ms: this._arrivalGapsOver40Ms,
        arrival_jitter_ms: Math.round(this._arrivalJitterMs * 10) / 10,
        max_delivery_gap_ms: Math.round(this._maxDeliveryGapMs * 10) / 10,
        clock_recovery_rate: Math.round(this._playbackRate * 1000000) / 1000000,
        clock_recovery_ppm: Math.round((this._playbackRate - 1) * 1000000),
        late_discard: this._lateDiscard,
        timeline_gaps: this._timelineGaps,
        audio_level: audioLevel,
      });
    }
    return true;
  }
}

registerProcessor("voip-stack-playback-processor", VoipPlaybackProcessor);
