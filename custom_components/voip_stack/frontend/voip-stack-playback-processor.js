const PCM_FORMATS = Object.freeze(["s16le", "s24le", "s24le_in_s32", "s32le"]);
const FRAME_MS = Object.freeze([10, 16, 20, 32]);
const BUFFER_CAPACITY_SECONDS = 1.28;
const MIN_START_LATENCY_MS = 80;
const MAX_START_LATENCY_MS = 320;
const JITTER_SAFETY_MULTIPLIER = 4;
const STABLE_DECAY_SECONDS = 12;
const PLC_DECAY_PER_SAMPLE = 0.9997;

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
    this._maxStartFrames = Math.max(this._minStartFrames, Math.ceil(MAX_START_LATENCY_MS / this._format.frameMs));
    this._dropFrames = this._maxStartFrames + 1;
    this._ring = new Float32Array(this._contextFrameSamples * this._format.channels * this._capacityFrames);
    this._read = 0;
    this._write = 0;
    this._available = 0;
    this._started = false;
    this._framesIn = 0;
    this._framesOut = 0;
    this._framesDrop = 0;
    this._underruns = 0;
    this._lastStats = 0;
    this._targetStartFrames = this._minStartFrames;
    this._lastUnderrun = 0;
    this._lastOutput = new Float32Array(this._format.channels);
    this._concealmentGain = 0;
    this._lastArrivalTime = 0;
    this._arrivalJitterSeconds = 0;
    this._previousInput = new Float32Array(this._format.channels);
    this._hasPreviousInput = false;

    this.port.onmessage = (event) => {
      const data = event.data;
      if (data?.type === "audio" && data.buffer) this._push(data.buffer, data.byteOffset || 0);
    };
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

  _push(buffer, byteOffset = 0) {
    const frameBytes = this._format.frameSamples * this._format.channels * this._format.bytesPerSample;
    if (byteOffset < 0 || buffer.byteLength - byteOffset !== frameBytes) return;
    const frameSamples = this._contextFrameSamples * this._format.channels;
    this._updateArrivalJitter();
    if (this._available >= frameSamples * this._dropFrames) {
      const queuedFrames = Math.floor(this._available / frameSamples);
      const framesToDrop = Math.max(1, queuedFrames - this._maxStartFrames);
      const samplesToDrop = framesToDrop * frameSamples;
      this._read = (this._read + samplesToDrop) % this._ring.length;
      this._available -= samplesToDrop;
      this._framesDrop += framesToDrop;
    }
    const view = new DataView(buffer, byteOffset, frameBytes);
    if (!this._hasPreviousInput) {
      for (let ch = 0; ch < this._format.channels; ch++) {
        this._previousInput[ch] = this._decode(view, ch);
      }
      this._hasPreviousInput = true;
    }
    for (let i = 0; i < this._contextFrameSamples; i++) {
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

  _updateArrivalJitter() {
    const now = currentTime;
    if (!Number.isFinite(now) || now <= 0) return;
    if (this._lastArrivalTime > 0) {
      const expected = this._format.frameMs / 1000;
      const deviation = Math.abs((now - this._lastArrivalTime) - expected);
      this._arrivalJitterSeconds += (deviation - this._arrivalJitterSeconds) / 16;
      const adaptiveFrames = Math.ceil(
        (MIN_START_LATENCY_MS + this._arrivalJitterSeconds * 1000 * JITTER_SAFETY_MULTIPLIER) /
          this._format.frameMs,
      );
      this._targetStartFrames = Math.max(
        this._targetStartFrames,
        Math.min(this._maxStartFrames, Math.max(this._minStartFrames, adaptiveFrames)),
      );
    }
    this._lastArrivalTime = now;
  }

  process(_inputs, outputs) {
    const channels = outputs?.[0] || [];
    if (!channels.length) return true;

    let underrunThisQuantum = false;
    for (let i = 0; i < channels[0].length; i++) {
      if (!this._started) {
        for (const out of channels) out[i] = 0;
        continue;
      }
      if (this._available < this._format.channels) {
        if (!underrunThisQuantum) {
          underrunThisQuantum = true;
          this._underruns++;
          this._lastUnderrun = currentTime;
          this._targetStartFrames = Math.min(this._maxStartFrames, this._targetStartFrames + 2);
        }
        for (let ch = 0; ch < channels.length; ch++) {
          channels[ch][i] = this._lastOutput[Math.min(ch, this._format.channels - 1)] * this._concealmentGain;
        }
        this._concealmentGain *= PLC_DECAY_PER_SAMPLE;
        continue;
      }
      for (let ch = 0; ch < channels.length; ch++) {
        const sample = this._ring[(this._read + Math.min(ch, this._format.channels - 1)) % this._ring.length];
        channels[ch][i] = sample;
        this._lastOutput[Math.min(ch, this._format.channels - 1)] = sample;
      }
      this._concealmentGain = 1;
      this._read = (this._read + this._format.channels) % this._ring.length;
      this._available -= this._format.channels;
    }

    if (underrunThisQuantum) this._started = false;
    this._framesOut++;
    if (currentTime - this._lastStats >= 1) {
      this._lastStats = currentTime;
      if (
        this._targetStartFrames > this._minStartFrames &&
        currentTime - this._lastUnderrun >= STABLE_DECAY_SECONDS
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
        arrival_jitter_ms: Math.round(this._arrivalJitterSeconds * 10000) / 10,
      });
    }
    return true;
  }
}

registerProcessor("voip-stack-playback-processor", VoipPlaybackProcessor);
