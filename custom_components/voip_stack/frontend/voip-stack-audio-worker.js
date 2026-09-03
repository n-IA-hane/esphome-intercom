const AUDIO_FRAME_MAGIC = 0x56;
const AUDIO_FRAME_VERSION = 1;
const AUDIO_FRAME_TYPE = 1;
const AUDIO_FRAME_HEADER_BYTES = 16;
const STATS_INTERVAL_MS = 1000;

let socket = null;
let playbackPort = null;
let capturePort = null;
let captureEnabled = false;
let maxBufferedBytes = 0;
let sent = 0;
let received = 0;
let txDropped = 0;
let rxDropped = 0;
let txFrame = null;
let statsTimer = null;
let lastCaptureAt = 0;
let maxCaptureGapMs = 0;
let captureGapsOver40Ms = 0;

function publishStats() {
  globalThis.postMessage({
    type: "stats",
    sent,
    received,
    tx_dropped: txDropped,
    rx_dropped: rxDropped,
    buffered_amount: Number(socket?.bufferedAmount || 0),
    max_capture_gap_ms: Math.round(maxCaptureGapMs * 10) / 10,
    capture_gaps_over_40ms: captureGapsOver40Ms,
  });
}

function startStats() {
  if (statsTimer) return;
  statsTimer = setInterval(publishStats, STATS_INTERVAL_MS);
}

function stopStats() {
  if (statsTimer) clearInterval(statsTimer);
  statsTimer = null;
}

function sendAudio(message) {
  const buffer = message?.buffer;
  if (!captureEnabled || socket?.readyState !== WebSocket.OPEN || !buffer?.byteLength) return;
  if (maxBufferedBytes > 0 && socket.bufferedAmount >= maxBufferedBytes) {
    txDropped++;
    return;
  }
  if (!txFrame || txFrame.byteLength !== buffer.byteLength + AUDIO_FRAME_HEADER_BYTES) {
    txFrame = new Uint8Array(buffer.byteLength + AUDIO_FRAME_HEADER_BYTES);
  }
  const header = new DataView(txFrame.buffer, 0, AUDIO_FRAME_HEADER_BYTES);
  header.setUint8(0, AUDIO_FRAME_MAGIC);
  header.setUint8(1, AUDIO_FRAME_VERSION);
  header.setUint8(2, AUDIO_FRAME_TYPE);
  header.setUint8(3, 0);
  header.setUint32(4, Number(message.generation || 0) >>> 0);
  header.setUint16(8, Number(message.sequence || 0) & 0xffff);
  header.setUint32(10, Number(message.timestamp || 0) >>> 0);
  header.setUint16(14, buffer.byteLength);
  txFrame.set(new Uint8Array(buffer), AUDIO_FRAME_HEADER_BYTES);
  socket.send(txFrame);
  sent++;
}

function bindCapturePort(port) {
  try { capturePort?.close(); } catch (_) {}
  capturePort = port;
  if (!capturePort) return;
  capturePort.onmessage = (event) => {
    if (event.data?.type !== "audio") return;
    const now = Number(globalThis.performance?.now?.()) || 0;
    if (now > 0 && lastCaptureAt > 0) {
      const gap = now - lastCaptureAt;
      maxCaptureGapMs = Math.max(maxCaptureGapMs, gap);
      if (gap >= 40) captureGapsOver40Ms++;
    }
    lastCaptureAt = now;
    sendAudio(event.data);
  };
  capturePort.start?.();
}

function bindPlaybackPort(port) {
  try { playbackPort?.close(); } catch (_) {}
  playbackPort = port;
  playbackPort?.start?.();
}

function closeSocket() {
  const active = socket;
  socket = null;
  try { active?.close(); } catch (_) {}
}

globalThis.onmessage = (event) => {
  const message = event.data || {};
  if (message.type === "connect") {
    closeSocket();
    const active = new WebSocket(String(message.url || ""));
    socket = active;
    active.binaryType = "arraybuffer";
    active.onopen = () => {
      if (socket !== active) return;
      startStats();
      globalThis.postMessage({ type: "open" });
    };
    active.onerror = () => {
      if (socket === active) globalThis.postMessage({ type: "error" });
    };
    active.onclose = (closeEvent) => {
      if (socket !== active) return;
      socket = null;
      stopStats();
      globalThis.postMessage({
        type: "close",
        code: Number(closeEvent.code || 0),
        reason: String(closeEvent.reason || ""),
      });
    };
    active.onmessage = (socketEvent) => {
      if (socket !== active) return;
      if (typeof socketEvent.data === "string") {
        globalThis.postMessage({ type: "message", data: socketEvent.data });
        return;
      }
      const buffer = socketEvent.data;
      if (!(buffer instanceof ArrayBuffer) || buffer.byteLength < AUDIO_FRAME_HEADER_BYTES) {
        rxDropped++;
        return;
      }
      const header = new DataView(buffer, 0, AUDIO_FRAME_HEADER_BYTES);
      const payloadBytes = header.getUint16(14);
      if (
        header.getUint8(0) !== AUDIO_FRAME_MAGIC ||
        header.getUint8(1) !== AUDIO_FRAME_VERSION ||
        header.getUint8(2) !== AUDIO_FRAME_TYPE ||
        payloadBytes !== buffer.byteLength - AUDIO_FRAME_HEADER_BYTES
      ) {
        rxDropped++;
        return;
      }
      received++;
      if (playbackPort) {
        playbackPort.postMessage({
          type: "audio",
          buffer,
          byteOffset: AUDIO_FRAME_HEADER_BYTES,
          generation: header.getUint32(4),
          sequence: header.getUint16(8),
          timestamp: header.getUint32(10),
          arrivalMs: globalThis.performance?.now?.(),
        }, [buffer]);
      } else {
        rxDropped++;
      }
    };
    return;
  }
  if (message.type === "send") {
    if (socket?.readyState === WebSocket.OPEN) socket.send(message.data);
    return;
  }
  if (message.type === "bind_capture") {
    bindCapturePort(message.port);
    return;
  }
  if (message.type === "bind_playback") {
    bindPlaybackPort(message.port);
    return;
  }
  if (message.type === "configure_capture") {
    captureEnabled = Boolean(message.enabled);
    maxBufferedBytes = Math.max(0, Number(message.max_buffered_bytes || 0));
    return;
  }
  if (message.type === "close") {
    closeSocket();
    stopStats();
    try { capturePort?.close(); } catch (_) {}
    try { playbackPort?.close(); } catch (_) {}
    capturePort = null;
    playbackPort = null;
    globalThis.close();
  }
};
