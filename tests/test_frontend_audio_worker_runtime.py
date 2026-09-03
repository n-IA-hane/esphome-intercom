#!/usr/bin/env python3
"""Runtime checks for the browser audio WebSocket worker."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.js_runtime

WORKER = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "voip_stack"
    / "frontend"
    / "voip-stack-audio-worker.js"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_worker_routes_versioned_media_without_the_ui_thread() -> None:
    script = rf'''
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";

const workerMessages = [];
let activeSocket;
let nowMs = 1234;
class MockWebSocket {{
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  constructor(url) {{
    this.url = url;
    this.readyState = MockWebSocket.CONNECTING;
    this.bufferedAmount = 0;
    this.sent = [];
    activeSocket = this;
  }}
  send(value) {{ this.sent.push(value instanceof Uint8Array ? value.slice() : value); }}
  close() {{ this.readyState = MockWebSocket.CLOSED; }}
}}
const playback = {{
  messages: [],
  postMessage(message) {{ this.messages.push(message); }},
  start() {{}}, close() {{}},
}};
const capture = {{ start() {{}}, close() {{}}, onmessage: null }};
const context = vm.createContext({{
  WebSocket: MockWebSocket,
  ArrayBuffer, DataView, Uint8Array,
  Number, String, Boolean, Math,
  performance: {{now: () => nowMs}},
  setInterval: () => 1,
  clearInterval() {{}},
  globalThis: null,
}});
context.globalThis = context;
context.postMessage = (message) => workerMessages.push(message);
context.close = () => {{}};
vm.runInContext(fs.readFileSync({json.dumps(str(WORKER))}, "utf8"), context);

function framed(payload, generation, sequence, timestamp) {{
  const result = new Uint8Array(16 + payload.length);
  const header = new DataView(result.buffer);
  header.setUint8(0, 0x56);
  header.setUint8(1, 1);
  header.setUint8(2, 1);
  header.setUint32(4, generation);
  header.setUint16(8, sequence);
  header.setUint32(10, timestamp);
  header.setUint16(14, payload.length);
  result.set(payload, 16);
  return result.buffer;
}}

context.onmessage({{data: {{type: "connect", url: "ws://lab/audio?audio_protocol=1"}}}});
activeSocket.readyState = MockWebSocket.OPEN;
activeSocket.onopen();
activeSocket.onmessage({{data: new Uint8Array([1, 7, 7, 7]).buffer}});
context.onmessage({{data: {{type: "bind_playback", port: playback}}}});
context.onmessage({{data: {{type: "bind_capture", port: capture}}}});
context.onmessage({{data: {{type: "configure_capture", enabled: true, max_buffered_bytes: 1024}}}});

const inbound = framed(new Uint8Array([2, 3, 4]), 7, 9, 320);
activeSocket.onmessage({{data: inbound}});
assert.equal(playback.messages.length, 1);
assert.equal(playback.messages[0].buffer, inbound);
assert.equal(playback.messages[0].byteOffset, 16);
assert.equal(playback.messages[0].generation, 7);
assert.equal(playback.messages[0].sequence, 9);
assert.equal(playback.messages[0].timestamp, 320);
assert.equal(playback.messages[0].arrivalMs, 1234);

capture.onmessage({{data: {{
  type: "audio",
  buffer: new Uint8Array([9, 8, 7]).buffer,
  generation: 4,
  sequence: 5,
  timestamp: 960,
}}}});
const outbound = activeSocket.sent[0];
const outboundHeader = new DataView(outbound.buffer, outbound.byteOffset, outbound.byteLength);
assert.equal(outboundHeader.getUint8(0), 0x56);
assert.equal(outboundHeader.getUint8(1), 1);
assert.equal(outboundHeader.getUint32(4), 4);
assert.equal(outboundHeader.getUint16(8), 5);
assert.equal(outboundHeader.getUint32(10), 960);
assert.deepEqual(Array.from(outbound.slice(16)), [9, 8, 7]);

activeSocket.bufferedAmount = 1024;
capture.onmessage({{data: {{
  type: "audio",
  buffer: new Uint8Array([6, 5, 4]).buffer,
  generation: 4,
  sequence: 6,
  timestamp: 1280,
}}}});
assert.equal(activeSocket.sent.length, 1);
context.publishStats();
const stats = workerMessages.filter((message) => message.type === "stats").at(-1);
assert.equal(stats.tx_dropped, 1);
assert.equal(stats.rx_dropped, 1);
'''
    completed = subprocess.run(
        ["node", "--experimental-vm-modules", "--input-type=module", "-"],
        input=script,
        text=True,
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
