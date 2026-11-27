"""Resource ownership across directional video renegotiation."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.js_runtime
ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "custom_components/voip_stack/frontend"
LOADER = ROOT / "tests/support/frontend_module_loader.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
@pytest.mark.parametrize("kept", ["send", "receive"])
@pytest.mark.parametrize("update", ["toggle", "active-codec", "same-generation-codec", "generation-only"])
def test_video_update_preserves_only_unchanged_active_direction(kept: str, update: str) -> None:
    script = r"""
import vm from 'node:vm';
import assert from 'node:assert/strict';

const cameras = [], encoders = [], decoders = [];
class Codec {
  static async isConfigSupported(config) { return { supported: true, config }; }
  constructor(callbacks) {
    this.callbacks = callbacks; this.state = 'unconfigured';
    this.encodeQueueSize = 0; this.decodeQueueSize = 0;
  }
  configure(config) { this.config = config; this.state = 'configured'; }
  close() { this.state = 'closed'; }
}
const context = vm.createContext({
  EventTarget, performance, console, URL, setTimeout, clearTimeout,
  CustomEvent: class extends Event {
    constructor(type, init) { super(type); this.detail = init?.detail; }
  },
  WebSocket: { OPEN: 1 },
  localStorage: { getItem() { return null; }, setItem() {} },
  navigator: { mediaDevices: { async getUserMedia() {
    const track = {
      stopped: false,
      stop() { this.stopped = true; },
      getSettings() { return { width: 320, height: 240, frameRate: 15 }; },
    };
    const stream = { getVideoTracks() { return [track]; }, getTracks() { return [track]; } };
    cameras.push(stream);
    return stream;
  } } },
  MediaStreamTrackProcessor: class {
    constructor() {
      let finish;
      const reader = {
        read() { return new Promise(resolve => { finish = resolve; }); },
        async cancel() { finish?.({ done: true }); },
      };
      this.readable = { getReader() { return reader; } };
    }
  },
  VideoEncoder: class extends Codec { constructor(init) { super(init); encoders.push(this); } },
  VideoDecoder: class extends Codec { constructor(init) { super(init); decoders.push(this); } },
});
const { createFrontendLoader } = await import(LOADER);
const frontend = createFrontendLoader(FRONTEND, context,
  'https://ha.example/voip-stack-video.js?v=direction-update');
const { VoipStackVideo } = (await frontend.load('voip-stack-video.js')).namespace;
const settle = () => new Promise(resolve => setImmediate(resolve));

// Captured HOME card -> Fusion 450 -> OFFICE, 2026-11-27, source 4adf848f:
// OFFICE's first update is media_generation 0 -> 1, sendrecv -> sendonly.
// Only the disabled RECEIVE fmtp disappears; SDP format repeats recvonly.
const fmtp = 'profile-level-id=42801f;packetization-mode=1;level-asymmetry-allowed=1';
const format = direction => 'pt=103:H264/90000;profile-level-id=42801f;packetization-mode=1;rtp-profile=RTP/AVP;direction=' + direction;
const contract = {
  codec: 'avc1.42801F', encoding: 'H264', clock_rate: 90000, payload_type: 103,
  fmtp, profile_level_id: '42801f', packetization_mode: 1, max_framerate: null,
  format: format('sendrecv'),
};
const before = {
  state: 'in_call', call_id: 'bc00a198-6521-1240-d09c-bc2411cb7584',
  send: { ...contract }, receive: { ...contract },
  source_format: format('sendrecv'), sip_send_format: format('sendrecv'),
  sip_receive_format: format('sendrecv'), direction: 'sendrecv',
  can_send: true, can_receive: true, remote_connection_held: false,
  camera_send_enabled: true, transcoding_enabled: true, debug: true, media_generation: 0,
};
const disabled = KEPT === 'send' ? 'receive' : 'send';
const after = {
  ...before, type: 'media_update', direction: KEPT === 'send' ? 'sendonly' : 'recvonly',
  send: { ...contract, format: format('recvonly') },
  receive: { ...contract, format: format('recvonly') },
  source_format: format('recvonly'), sip_send_format: format('recvonly'),
  sip_receive_format: format('recvonly'), media_generation: 1,
  ['can_' + disabled]: false,
};
after[disabled].fmtp = '';
if (UPDATE === 'same-generation-codec' || UPDATE === 'generation-only') {
  after.can_send = after.can_receive = true;
  after.direction = 'sendrecv';
  after.send = { ...before.send }; after.receive = { ...before.receive };
  if (UPDATE === 'same-generation-codec') after.media_generation = 0;
}
if (UPDATE === 'active-codec' || UPDATE === 'same-generation-codec') {
  after[KEPT].codec = 'avc1.42801E';
  after[KEPT].profile_level_id = '42801e';
  after[KEPT].fmtp = 'profile-level-id=42801e;packetization-mode=1';
}
const video = new VoipStackVideo();
const ws = { readyState: 1, bufferedAmount: 0, send() {}, close() {} };
video._ws = ws; video._callId = before.call_id;
video._generation = 2; video._cameraEnabled = true; video._negotiated = before;
await video._setupCodecs(before, 2);
await settle();
assert.equal(video._canSend, true); assert.equal(video._canReceive, true);
const stream = video._sender._cameraStream;
const encoder = video._sender._encoder;
const decoder = video._receiver._decoder;
const reader = video._sender._cameraReader;
const task = video._sender._encodeTask;
const senderGeneration = video._sender._senderGeneration;
video._receiver._lastRenderedTimestamp = 90000;
await video._applyMediaUpdate(after, ws, before.call_id);
await settle();
assert.equal(video._negotiated.media_generation, after.media_generation);
assert.equal(video._ws, ws);

if (UPDATE === 'toggle') {
  assert.equal(video._generation, 2, 'direction update must preserve current codec callbacks');
  if (KEPT === 'send') {
    assert.equal(video._sender._cameraStream, stream, 'disabled RX fmtp must not reacquire camera');
    assert.equal(video._sender._encoder, encoder);
    assert.equal(video._sender._cameraReader, reader);
    assert.equal(video._sender._encodeTask, task);
    assert.equal(video._sender._senderGeneration, senderGeneration);
    assert.equal(stream.getTracks()[0].stopped, false);
    assert.equal(cameras.length, 1);
    assert.equal(decoder.state, 'closed');
    assert.equal(video._receiver._decoder, null);
    // The original encoder callback still belongs to this live generation.
    encoder.callbacks.output({ type: 'key', byteLength: 1, timestamp: 0,
      copyTo(target) { target[0] = 1; } });
    assert.equal(video.stats.sent, 1);
  } else {
    assert.equal(video._receiver._decoder, decoder, 'disabled TX contract must not rebuild decoder');
    assert.equal(decoder.state, 'configured');
    assert.equal(decoders.length, 1);
    assert.equal(video._receiver._lastRenderedTimestamp, 90000);
    assert.equal(encoder.state, 'closed');
    assert.equal(stream.getTracks()[0].stopped, true);
    // The preserved decoder's error callback still belongs to this generation.
    decoder.callbacks.error();
    assert.equal(video.stats.decode_errors, 1);
  }
  assert.equal(video['_can' + (disabled === 'send' ? 'Send' : 'Receive')], false);

  // Enabling a direction uses its NEW contract, while the other stays alive.
  const resumed = { ...before, media_generation: 2,
    [disabled]: { ...before[disabled], codec: 'avc1.42801E', profile_level_id: '42801e' } };
  await video._applyMediaUpdate(resumed, ws, before.call_id);
  await settle();
  assert.equal(video._generation, 2);
  assert.equal(video._negotiated.media_generation, 2);
  assert.equal(video._canSend, true); assert.equal(video._canReceive, true);
  if (KEPT === 'send') {
    assert.equal(video._sender._cameraStream, stream);
    assert.equal(video._sender._encoder, encoder);
    assert.equal(cameras.length, 1);
    assert.notEqual(video._receiver._decoder, decoder);
    assert.equal(video._receiver._decoder.config.codec, 'avc1.42801E');
  } else {
    assert.equal(video._receiver._decoder, decoder);
    assert.equal(decoders.length, 1);
    assert.notEqual(video._sender._encoder, encoder);
    assert.equal(video._sender._encoder.config.codec, 'avc1.42801E');
  }
} else {
  assert.equal(video._generation, 3, 'active codec changes and a new media epoch must rebuild');
  assert.equal(encoder.state, 'closed');
  assert.equal(decoder.state, 'closed');
  assert.equal(stream.getTracks()[0].stopped, true);
  assert.equal(video._receiver._lastRenderedTimestamp, null);
  const rebuilt = KEPT === 'send' ? video._sender._encoder : video._receiver._decoder;
  assert.equal(rebuilt.config.codec, after[KEPT].codec);
}
await video.close();
assert.ok(cameras.every(item => item.getTracks()[0].stopped));
assert.ok(encoders.every(item => item.state === 'closed'));
assert.ok(decoders.every(item => item.state === 'closed'));
"""
    for token, value in {
        "LOADER": str(LOADER), "FRONTEND": str(FRONTEND), "KEPT": kept, "UPDATE": update,
    }.items():
        script = script.replace(token, json.dumps(value))
    result = subprocess.run(
        ["node", "--no-warnings", "--experimental-vm-modules", "--input-type=module", "-e", script],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr or result.stdout
