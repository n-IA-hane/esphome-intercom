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
def test_worker_routes_media_without_the_ui_thread() -> None:
    script = rf'''
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";

const workerMessages = [];
let activeSocket;
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
  send(value) {{
    this.sent.push(value instanceof Uint8Array ? value.slice() : value);
  }}
  close() {{ this.readyState = MockWebSocket.CLOSED; }}
}}
const playback = {{
  messages: [],
  postMessage(message) {{ this.messages.push(message); }},
  start() {{}},
  close() {{}},
}};
const capture = {{ start() {{}}, close() {{}}, onmessage: null }};
const context = vm.createContext({{
  WebSocket: MockWebSocket,
  Uint8Array,
  Number,
  String,
  Boolean,
  Math,
  globalThis: null,
}});
context.globalThis = context;
context.postMessage = (message) => workerMessages.push(message);
context.close = () => {{}};
vm.runInContext(fs.readFileSync({json.dumps(str(WORKER))}, "utf8"), context);

context.onmessage({{data: {{type: "connect", url: "ws://lab/audio"}}}});
activeSocket.readyState = MockWebSocket.OPEN;
activeSocket.onopen();
const staleInbound = new Uint8Array([1, 7, 7, 7]).buffer;
activeSocket.onmessage({{data: staleInbound}});
assert.equal(
  workerMessages.some((message) => message.type === "message" && message.data === staleInbound),
  false,
);
context.onmessage({{data: {{type: "bind_playback", port: playback}}}});
context.onmessage({{data: {{type: "bind_capture", port: capture}}}});
context.onmessage({{data: {{type: "configure_capture", enabled: true, max_buffered_bytes: 4096}}}});

const inbound = new Uint8Array([1, 2, 3, 4]).buffer;
activeSocket.onmessage({{data: inbound}});
assert.equal(playback.messages.length, 1);
assert.equal(playback.messages[0].buffer, inbound);
assert.equal(playback.messages[0].byteOffset, 1);
assert.equal(workerMessages.some((message) => message.type === "message" && message.data === inbound), false);

capture.onmessage({{data: {{type: "audio", buffer: new Uint8Array([9, 8, 7]).buffer}}}});
assert.deepEqual(Array.from(activeSocket.sent[0]), [1, 9, 8, 7]);
context.onmessage({{data: {{type: "send", data: "control"}}}});
assert.equal(activeSocket.sent[1], "control");
'''
    subprocess.run(
        ["node", "--experimental-vm-modules", "--input-type=module", "-"],
        input=script,
        text=True,
        check=True,
        capture_output=True,
    )
