// Stateless codec support shared by the video resource owners.
export const VIDEO_ACCESS_UNIT = 1;
export const VIDEO_HEADER_BYTES = 6;
export const CODEC_SETUP_TIMEOUT_MS = 3000;
const MEDIA_CLEANUP_TIMEOUT_MS = 500;

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

export function codecWorkerUrl() {
  const url = new URL("./voip-stack-video-worker.js", import.meta.url);
  try {
    url.search = new URL(import.meta.url).search;
  } catch (_) {}
  return url;
}

export async function supportedConfig(codecClass, base, realtimeHardware = false) {
  // Keep camera encoding off the Android main CPU when MediaCodec can
  // satisfy the exact negotiated profile. The software fallback uses
  // quality mode: WebCodecs realtime mode may discard encoder inputs, and
  // a frame_num gap in a dependent H.264 GOP is not recoverable by the P4
  // decoder until the next IDR. Our bounded encode queue still drops raw
  // frames before admission, which preserves a valid encoded dependency
  // chain without allowing latency to grow.
  for (const candidate of encoderCandidates(base, realtimeHardware)) {
    try {
      const support = await codecClass.isConfigSupported(candidate);
      if (support?.supported) return support.config || candidate;
    } catch (_) {}
  }
  return base;
}

// Hardware first, then software, then the browser's choice.
export function encoderCandidates(base, realtimeHardware = false) {
  return realtimeHardware
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
}


