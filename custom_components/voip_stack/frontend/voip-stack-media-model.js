/** Pure negotiated-media rules for the browser softphone. */

const PCM_FORMATS = Object.freeze(["s16le", "s24le", "s24le_in_s32", "s32le"]);
const MAX_SAMPLE_RATE = 192000;
const MAX_FRAME_MS = 1000;
const MAX_PCM_FRAME_BYTES = MAX_SAMPLE_RATE * 2 * 4;

export function parsePcmFormat(token, label = "audio format") {
  const parts = String(token || "").split(":");
  if (parts.length !== 4) throw new Error(`${label} missing negotiated PCM token`);
  const sampleRate = Number(parts[0]);
  const pcmFormat = parts[1];
  const channels = Number(parts[2]);
  const frameMs = Number(parts[3]);
  if (!Number.isFinite(sampleRate) || !Number.isFinite(channels) || !Number.isFinite(frameMs)) {
    throw new Error(`${label} has invalid numeric fields`);
  }
  if (!PCM_FORMATS.includes(pcmFormat)) {
    throw new Error(`${label} has unsupported PCM format ${pcmFormat}`);
  }
  if (!Number.isInteger(sampleRate) || sampleRate < 1 || sampleRate > MAX_SAMPLE_RATE) {
    throw new Error(`${label} has unsupported sample_rate ${sampleRate}`);
  }
  if (![1, 2].includes(channels)) {
    throw new Error(`${label} has unsupported channel count ${channels}`);
  }
  if (frameMs <= 0 || frameMs > MAX_FRAME_MS) {
    throw new Error(`${label} has unsupported frame_ms ${frameMs}`);
  }
  const frameSamples = sampleRate * frameMs / 1000;
  if (!Number.isSafeInteger(frameSamples) || frameSamples < 1) {
    throw new Error(`${label} does not form whole PCM frames`);
  }
  const bytesPerSample = pcmFormat === "s16le" ? 2 : pcmFormat === "s24le" ? 3 : 4;
  if (frameSamples * channels * bytesPerSample > MAX_PCM_FRAME_BYTES) {
    throw new Error(`${label} exceeds local PCM frame byte budget`);
  }
  return { sampleRate, pcmFormat, channels, frameMs };
}

export function resolveSessionFormats(negotiated = null) {
  const txFormat = negotiated?.selected_tx_format || negotiated?.tx_format;
  const rxFormat = negotiated?.selected_rx_format || negotiated?.rx_format;
  if (!txFormat || !rxFormat) {
    throw new Error("SIP session missing selected_tx_format/selected_rx_format");
  }
  return {
    tx: parsePcmFormat(txFormat, "selected_tx_format"),
    rx: parsePcmFormat(rxFormat, "selected_rx_format"),
  };
}

export function normaliseAudioMode(value) {
  const mode = String(value || "").trim().toLowerCase();
  return ["full_duplex", "mic_only", "speaker_only"].includes(mode)
    ? mode
    : "full_duplex";
}

export function normaliseAudioDirection(value) {
  const direction = String(value || "sendrecv").trim().toLowerCase();
  return ["sendrecv", "sendonly", "recvonly", "inactive"].includes(direction)
    ? direction
    : "sendrecv";
}

export function desiredAudioPaths(audioMode, audioDirection) {
  const mode = normaliseAudioMode(audioMode);
  const direction = normaliseAudioDirection(audioDirection);
  const modeCanCapture = mode === "full_duplex" || mode === "speaker_only";
  const modeCanPlayback = mode === "full_duplex" || mode === "mic_only";
  return {
    capture: modeCanCapture && ["sendrecv", "sendonly"].includes(direction),
    playback: modeCanPlayback && ["sendrecv", "recvonly"].includes(direction),
  };
}

export function sameAudioFormat(left, right) {
  return Boolean(left && right) &&
    left.sampleRate === right.sampleRate &&
    left.pcmFormat === right.pcmFormat &&
    left.channels === right.channels &&
    left.frameMs === right.frameMs;
}
