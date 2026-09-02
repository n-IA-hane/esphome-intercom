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
PLAYBACK_PROCESSOR = PROCESSOR.with_name("voip-stack-playback-processor.js")


@pytest.mark.parametrize(
    ("input_rate", "frame_ms"),
    ((8000, 20), (16000, 16), (48000, 20)),
)
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_playback_resampling_is_continuous_across_rtp_frames(
    input_rate: int,
    frame_ms: int,
) -> None:
    script = rf'''
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";

let Processor;
class MockAudioWorkletProcessor {{
  constructor() {{
    this.messages = [];
    this.port = {{ postMessage: (message) => this.messages.push(message), onmessage: null }};
  }}
}}
const context = vm.createContext({{
  AudioWorkletProcessor: MockAudioWorkletProcessor,
  sampleRate: 48000,
  currentTime: 1,
  registerProcessor(_name, value) {{ Processor = value; }},
  ArrayBuffer,
  DataView,
  Float32Array,
  Math,
  Number,
  Object,
  Error,
}});
vm.runInContext(fs.readFileSync({json.dumps(str(PLAYBACK_PROCESSOR))}, "utf8"), context);
const processor = new Processor({{
  processorOptions: {{
    format: {{sampleRate: {input_rate}, frameMs: {frame_ms}, channels: 1, pcmFormat: "s16le"}},
  }},
}});
const frameSamples = {input_rate} * {frame_ms} / 1000;
const outputSamples = 48000 * {frame_ms} / 1000;
let source = 0;
for (let frame = 0; frame < 5; frame++) {{
  const buffer = new ArrayBuffer(frameSamples * 2);
  const view = new DataView(buffer);
  for (let index = 0; index < frameSamples; index++, source++) {{
    view.setInt16(index * 2, -8000 + source * 20, true);
  }}
  processor._push(buffer);
}}
const output = Array.from(processor._ring.slice(0, 5 * outputSamples));
const differences = output.slice(1).map((value, index) =>
  Math.abs(value - output[index]) * 32768
);
const boundaryIndexes = [1, 2, 3, 4].map((value) => value * outputSamples);
const boundaries = boundaryIndexes.map((index) => differences[index - 1]);
const excluded = new Set(boundaryIndexes.map((index) => index - 1));
const ordinary = differences.filter((_value, index) => !excluded.has(index));
ordinary.sort((left, right) => left - right);
const median = ordinary[Math.floor(ordinary.length / 2)];
assert.ok(Math.max(...boundaries) <= median * 2, {{boundaries, median}});
processor.process([], [[new Float32Array(128)]]);
processor.process([], [[new Float32Array(128)]]);
assert.equal(processor.messages.filter((message) => message.type === "playback_ready").length, 1);

const timed = new Processor({{
  processorOptions: {{
    format: {{sampleRate: {input_rate}, frameMs: {frame_ms}, channels: 1, pcmFormat: "s16le"}},
  }},
}});
for (let frame = 0; frame < 20; frame++) {{
  timed._push(new ArrayBuffer(frameSamples * 2), 0, 1000 + frame * {frame_ms});
}}
assert.equal(timed._arrivalJitterMs, 0);
assert.equal(timed._targetStartFrames, timed._minStartFrames);
assert.equal(timed._maxArrivalGapMs, {frame_ms});
assert.equal(timed._arrivalGapsOver40Ms, 0);

const androidOutput = new Processor({{
  processorOptions: {{
    format: {{sampleRate: {input_rate}, frameMs: {frame_ms}, channels: 1, pcmFormat: "s16le"}},
    renderLeadMs: 90.8333333333,
  }},
}});
assert.equal(
  androidOutput._minStartFrames,
  Math.ceil((80 + 90.8333333333) / {frame_ms}),
);
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
