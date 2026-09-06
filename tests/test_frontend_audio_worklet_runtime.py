#!/usr/bin/env python3
"""Runtime checks for browser microphone resampling."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.js_runtime


PROCESSOR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "voip_stack"
    / "frontend"
    / "voip-stack-processor.js"
)

PLAYBACK_PROCESSOR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "voip_stack"
    / "frontend"
    / "voip-stack-playback-processor.js"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_playback_distinguishes_packet_loss_from_remote_silence() -> None:
    script = rf'''
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";

let Processor;
let now = 1;
class MockAudioWorkletProcessor {{
  constructor() {{
    this.messages = [];
    this.port = {{
      postMessage: (message) => this.messages.push(message),
      onmessage: null,
    }};
  }}
}}
const context = vm.createContext({{
  AudioWorkletProcessor: MockAudioWorkletProcessor,
  sampleRate: 48000,
  registerProcessor(_name, value) {{ Processor = value; }},
  ArrayBuffer,
  DataView,
  Float32Array,
  Math,
  Number,
  Object,
  Error,
}});
Object.defineProperty(context, "currentTime", {{ get: () => now }});
vm.runInContext(fs.readFileSync({json.dumps(str(PLAYBACK_PROCESSOR))}, "utf8"), context);

function audioFrame() {{
  return new ArrayBuffer(320);
}}
function drain(processor) {{
  const output = [[new Float32Array(128)]];
  while (processor._started) {{
    processor.process([], output);
    now += 128 / 48000;
  }}
  assert.equal(processor._starvationPending, true);
}}
function fillToStart(processor) {{
  while (!processor._started) {{
    processor.port.onmessage({{data: {{type: "audio", buffer: audioFrame()}}}});
    now += 0.01;
  }}
}}

const loss = new Processor({{processorOptions: {{format: {{sampleRate: 16000, frameMs: 10, channels: 1, pcmFormat: "s16le"}}}}}});
fillToStart(loss);
drain(loss);
const lossTarget = loss._targetStartFrames;
loss.port.onmessage({{data: {{type: "audio", buffer: audioFrame()}}}});
assert.equal(loss._underruns, 1);
assert.equal(loss._targetStartFrames, lossTarget + 2);

const silence = new Processor({{processorOptions: {{format: {{sampleRate: 16000, frameMs: 10, channels: 1, pcmFormat: "s16le"}}}}}});
fillToStart(silence);
drain(silence);
const silenceTarget = silence._targetStartFrames;
silence.port.onmessage({{data: {{type: "remote_silence_resume"}}}});
silence.port.onmessage({{data: {{type: "audio", buffer: audioFrame()}}}});
assert.equal(silence._underruns, 0);
assert.equal(silence._targetStartFrames, silenceTarget);
assert.equal(silence._hasPreviousInput, true);
'''
    subprocess.run(
        ["node", "--experimental-vm-modules", "--input-type=module", "-"],
        input=script,
        text=True,
        check=True,
        capture_output=True,
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_microphone_anti_alias_filter_is_effective_and_optional() -> None:
    script = rf'''
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";

let Processor;
class MockAudioWorkletProcessor {{
  constructor() {{
    this.messages = [];
    this.port = {{
      postMessage: (message) => this.messages.push({{
        ...message,
        buffer: message.buffer.slice(0),
      }}),
    }};
  }}
}}
const context = vm.createContext({{
  AudioWorkletProcessor: MockAudioWorkletProcessor,
  sampleRate: 48000,
  registerProcessor(_name, value) {{ Processor = value; }},
  ArrayBuffer,
  DataView,
  Float32Array,
  Math,
  Number,
  Object,
  Error,
}});
vm.runInContext(fs.readFileSync({json.dumps(str(PROCESSOR))}, "utf8"), context);

function run(antiAlias) {{
  const processorOptions = {{
    format: {{sampleRate: 16000, frameMs: 20, channels: 1, pcmFormat: "s16le"}},
  }};
  if (antiAlias !== undefined) processorOptions.antiAlias = antiAlias;
  const processor = new Processor({{processorOptions}});
  let phase = 0;
  for (let offset = 0; offset < 48000; offset += 128) {{
    const input = new Float32Array(Math.min(128, 48000 - offset));
    for (let i = 0; i < input.length; i++, phase++) {{
      input[i] = 0.4 * Math.sin(2 * Math.PI * 1000 * phase / 48000)
        + 0.4 * Math.sin(2 * Math.PI * 12000 * phase / 48000);
    }}
    processor.process([[input]]);
  }}
  const samples = processor.messages.flatMap((message) => {{
    const view = new DataView(message.buffer);
    const result = [];
    for (let offset = 0; offset < view.byteLength; offset += 2) {{
      result.push(view.getInt16(offset, true) / 32768);
    }}
    return result;
  }});
  function amplitudeAt(frequency) {{
    const start = 4000;
    let sine = 0;
    let cosine = 0;
    for (let i = start; i < samples.length; i++) {{
      const angle = 2 * Math.PI * frequency * (i - start) / 16000;
      sine += samples[i] * Math.sin(angle);
      cosine += samples[i] * Math.cos(angle);
    }}
    return 2 * Math.hypot(sine, cosine) / (samples.length - start);
  }}
  return {{
    frames: processor.messages.length,
    samples: samples.length,
    wanted: amplitudeAt(1000),
    alias: amplitudeAt(4000),
  }};
}}

const implicit = run(undefined);
const enabled = run(true);
const disabled = run(false);
assert.deepEqual(implicit, enabled);
assert.equal(enabled.frames, 50);
assert.equal(enabled.samples, 16000);
assert.ok(Math.abs(enabled.wanted - 0.4) < 0.01);
assert.ok(Math.abs(disabled.wanted - 0.4) < 0.01);
assert.ok(disabled.alias > 0.39);
assert.ok(enabled.alias < disabled.alias * 0.08);
'''
    subprocess.run(
        ["node", "--experimental-vm-modules", "--input-type=module", "-"],
        input=script,
        text=True,
        check=True,
        capture_output=True,
    )
