const WS_AUDIO = 1;

let socket = null;
let playbackPort = null;
let capturePort = null;
let captureEnabled = false;
let maxBufferedBytes = 0;
let sent = 0;
let received = 0;
let txDropped = 0;
let txFrame = null;

function publishStats() {
  globalThis.postMessage({
    type: "stats",
    sent,
    received,
    tx_dropped: txDropped,
    buffered_amount: Number(socket?.bufferedAmount || 0),
  });
}

function sendAudio(buffer) {
  if (!captureEnabled || socket?.readyState !== WebSocket.OPEN || !buffer?.byteLength) return;
  if (maxBufferedBytes > 0 && socket.bufferedAmount >= maxBufferedBytes) {
    txDropped++;
    if ((txDropped & 31) === 1) publishStats();
    return;
  }
  if (!txFrame || txFrame.byteLength !== buffer.byteLength + 1) {
    txFrame = new Uint8Array(buffer.byteLength + 1);
    txFrame[0] = WS_AUDIO;
  }
  txFrame.set(new Uint8Array(buffer), 1);
  socket.send(txFrame);
  sent++;
  if ((sent & 31) === 0) publishStats();
}

function bindCapturePort(port) {
  try { capturePort?.close(); } catch (_) {}
  capturePort = port;
  if (!capturePort) return;
  capturePort.onmessage = (event) => {
    if (event.data?.type === "audio") sendAudio(event.data.buffer);
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
      if (socket === active) globalThis.postMessage({ type: "open" });
    };
    active.onerror = () => {
      if (socket === active) globalThis.postMessage({ type: "error" });
    };
    active.onclose = (closeEvent) => {
      if (socket !== active) return;
      socket = null;
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
      received++;
      if (playbackPort) {
        playbackPort.postMessage(
          { type: "audio", buffer, byteOffset: 1 },
          [buffer],
        );
      } else {
        globalThis.postMessage({ type: "message", data: buffer }, [buffer]);
      }
      if ((received & 31) === 0) publishStats();
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
    try { capturePort?.close(); } catch (_) {}
    try { playbackPort?.close(); } catch (_) {}
    capturePort = null;
    playbackPort = null;
    globalThis.close();
  }
};
