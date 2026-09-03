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
const minimumStartFrames = Math.ceil(80 / {frame_ms});
assert.equal(timed._targetStartFrames, minimumStartFrames);
assert.equal(timed._maxArrivalGapMs, {frame_ms});
assert.equal(timed._arrivalGapsOver40Ms, 0);

const androidOutput = new Processor({{
  processorOptions: {{
    format: {{sampleRate: {input_rate}, frameMs: {frame_ms}, channels: 1, pcmFormat: "s16le"}},
  }},
}});
assert.equal(androidOutput._minStartFrames, minimumStartFrames);
context.currentTime = 2;
androidOutput._push(new ArrayBuffer(frameSamples * 2), 0, 2000);
context.currentTime = 2.216;
androidOutput._push(new ArrayBuffer(frameSamples * 2), 0, 2216);
assert.ok(androidOutput._targetStartFrames > minimumStartFrames);
const targetAfterFirstBurst = androidOutput._targetStartFrames;
context.currentTime = 2.332;
androidOutput._push(new ArrayBuffer(frameSamples * 2), 0, 2332);
assert.ok(androidOutput._targetStartFrames >= targetAfterFirstBurst);
assert.ok(androidOutput._targetStartFrames <= androidOutput._maxStartFrames);
assert.ok(androidOutput._maxDeliveryGapMs > 200);
'''
    completed = subprocess.run(
        ["node", "--experimental-vm-modules", "--input-type=module", "-"],
        input=script,
        text=True,
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_playback_clock_recovery_bounds_buffer_without_drops() -> None:
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
  currentTime: 0,
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

function run(sourceRateMultiplier) {{
  const processor = new Processor({{
    processorOptions: {{
      format: {{sampleRate: 16000, frameMs: 16, channels: 1, pcmFormat: "s16le"}},
    }},
  }});
  const frameSamples = 256;
  const frame = new ArrayBuffer(frameSamples * 2);
  const view = new DataView(frame);
  for (let index = 0; index < frameSamples; index++) {{
    view.setInt16(index * 2, Math.round(12000 * Math.sin(2 * Math.PI * index / 80)), true);
  }}
  let sourceFrames = 0;
  let maxBuffered = 0;
  const quanta = Math.round(60 * 48000 / 128);
  for (let quantum = 0; quantum < quanta; quantum++) {{
    sourceFrames += 128 / 48000 * 1000 / 16 * sourceRateMultiplier;
    while (sourceFrames >= 1) {{
      processor._push(frame.slice(0), 0, quantum * 128 / 48);
      sourceFrames--;
    }}
    context.currentTime = quantum * 128 / 48000;
    processor.process([], [[new Float32Array(128)]]);
    maxBuffered = Math.max(maxBuffered, processor._available / processor._contextFrameSamples);
  }}
  return {{
    processor,
    buffered: processor._available / processor._contextFrameSamples,
    maxBuffered,
  }};
}}

const matched = run(1);
assert.equal(matched.processor._framesDrop, 0);
assert.equal(matched.processor._underruns, 0);
assert.ok(Math.abs(matched.processor._playbackRate - 1) < 0.001);
assert.ok(matched.buffered <= matched.processor._targetStartFrames + 2, JSON.stringify({{buffered: matched.buffered, target: matched.processor._targetStartFrames, rate: matched.processor._playbackRate}}));

const faster = run(1.001);
assert.equal(faster.processor._framesDrop, 0);
assert.equal(faster.processor._underruns, 0);
assert.ok(faster.processor._playbackRate <= 1.001, String(faster.processor._playbackRate));
assert.ok(faster.buffered <= faster.processor._targetStartFrames + 3, JSON.stringify({{buffered: faster.buffered, target: faster.processor._targetStartFrames, rate: faster.processor._playbackRate}}));
assert.ok(faster.maxBuffered < faster.processor._dropFrames, JSON.stringify({{maxBuffered: faster.maxBuffered, drop: faster.processor._dropFrames}}));

const slower = run(0.999);
assert.equal(slower.processor._framesDrop, 0);
assert.equal(slower.processor._underruns, 0);
assert.ok(slower.processor._playbackRate < 1, slower.processor._playbackRate);
assert.ok(slower.processor._playbackRate >= 0.999, String(slower.processor._playbackRate));
assert.ok(slower.buffered >= slower.processor._targetStartFrames - 2, JSON.stringify({{buffered: slower.buffered, target: slower.processor._targetStartFrames, rate: slower.processor._playbackRate}}));
'''
    completed = subprocess.run(
        ["node", "--experimental-vm-modules", "--input-type=module", "-"],
        input=script,
        text=True,
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_playback_recovers_a_transient_queue_without_dropping_audio() -> None:
    script = rf'''
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";

let Processor;
class MockAudioWorkletProcessor {{
  constructor() {{
    this.port = {{postMessage() {{}}, onmessage: null}};
  }}
}}
const context = vm.createContext({{
  AudioWorkletProcessor: MockAudioWorkletProcessor,
  sampleRate: 48000,
  currentTime: 0,
  registerProcessor(_name, value) {{ Processor = value; }},
  ArrayBuffer, DataView, Float32Array, Math, Number, Object, Error,
}});
vm.runInContext(fs.readFileSync({json.dumps(str(PLAYBACK_PROCESSOR))}, "utf8"), context);
const processor = new Processor({{
  processorOptions: {{format: {{sampleRate: 16000, frameMs: 16, channels: 1, pcmFormat: "s16le"}}}},
}});
const frameSamples = 256;
const frame = new ArrayBuffer(frameSamples * 2);
const view = new DataView(frame);
for (let index = 0; index < frameSamples; index++) {{
  view.setInt16(index * 2, Math.round(12000 * Math.sin(2 * Math.PI * index / 80)), true);
}}
let timestamp = 0;
for (let index = 0; index < 10; index++, timestamp += frameSamples) {{
  processor._push(frame.slice(0), 0, 0, 0, index, timestamp);
}}
let sourceFrames = 0;
for (let quantum = 0; quantum < Math.round(30 * 48000 / 128); quantum++) {{
  sourceFrames += 128 / 48000 * 1000 / 16;
  while (sourceFrames >= 1) {{
    processor._push(frame.slice(0), 0, context.currentTime * 1000, 0, 0, timestamp);
    timestamp += frameSamples;
    sourceFrames--;
  }}
  context.currentTime = quantum * 128 / 48000;
  processor.process([], [[new Float32Array(128)]]);
}}
const buffered = processor._available / processor._contextFrameSamples;
assert.equal(processor._framesDrop, 0);
assert.equal(processor._underruns, 0);
assert.ok(buffered <= processor._targetStartFrames + 2, JSON.stringify({{buffered, rate: processor._playbackRate}}));
'''
    completed = subprocess.run(
        ["node", "--experimental-vm-modules", "--input-type=module", "-"],
        input=script,
        text=True,
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_playback_rebuffers_after_a_real_underflow() -> None:
    script = rf'''
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";

let Processor;
class MockAudioWorkletProcessor {{
  constructor() {{
    this.port = {{postMessage() {{}}, onmessage: null}};
  }}
}}
const context = vm.createContext({{
  AudioWorkletProcessor: MockAudioWorkletProcessor,
  sampleRate: 48000,
  currentTime: 0,
  registerProcessor(_name, value) {{ Processor = value; }},
  ArrayBuffer, DataView, Float32Array, Math, Number, Object, Error,
}});
vm.runInContext(fs.readFileSync({json.dumps(str(PLAYBACK_PROCESSOR))}, "utf8"), context);
const processor = new Processor({{
  processorOptions: {{format: {{sampleRate: 16000, frameMs: 16, channels: 1, pcmFormat: "s16le"}}}},
}});
const frameSamples = 256;
const frame = new ArrayBuffer(frameSamples * 2);
let timestamp = 0;
for (let index = 0; index < processor._targetStartFrames; index++, timestamp += frameSamples) {{
  processor._push(frame.slice(0), 0, index * 16, 0, index, timestamp);
}}
for (let quantum = 0; quantum < 80; quantum++) {{
  context.currentTime = quantum * 128 / 48000;
  processor.process([], [[new Float32Array(128)]]);
}}
assert.equal(processor._underruns, 1);
assert.equal(processor._started, false);
const discardedBeforeRecovery = processor._lateDiscard;
for (let index = 0; index < processor._targetStartFrames; index++, timestamp += frameSamples) {{
  processor._push(frame.slice(0), 0, 250 + index * 16, 0, index + 8, timestamp);
}}
assert.equal(processor._lateDiscard, discardedBeforeRecovery);
assert.equal(processor._started, true);
const underrunsBeforeRecovery = processor._underruns;
for (let quantum = 80; quantum < 84; quantum++) {{
  context.currentTime = quantum * 128 / 48000;
  processor.process([], [[new Float32Array(128)]]);
}}
assert.equal(processor._underruns, underrunsBeforeRecovery);
'''
    completed = subprocess.run(
        ["node", "--experimental-vm-modules", "--input-type=module", "-"],
        input=script,
        text=True,
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_playback_timeline_preserves_loss_and_rejects_late_audio() -> None:
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
  currentTime: 0,
  registerProcessor(_name, value) {{ Processor = value; }},
  ArrayBuffer, DataView, Float32Array, Math, Number, Object, Error,
}});
vm.runInContext(fs.readFileSync({json.dumps(str(PLAYBACK_PROCESSOR))}, "utf8"), context);
const processor = new Processor({{
  processorOptions: {{
    format: {{sampleRate: 16000, frameMs: 20, channels: 1, pcmFormat: "s16le"}},
  }},
}});
const frame = new ArrayBuffer(640);
processor._push(frame.slice(0), 0, 0, 0, 0, 0);
processor._push(frame.slice(0), 0, 20, 0, 1, 320);
processor._push(frame.slice(0), 0, 60, 0, 3, 960);
assert.equal(processor._timelineGaps, 1);
assert.equal(processor._available / processor._contextFrameSamples, 4);
processor._push(frame.slice(0), 0, 80, 0, 2, 640);
assert.equal(processor._lateDiscard, 1);
assert.equal(processor._available / processor._contextFrameSamples, 4);
'''
    completed = subprocess.run(
        ["node", "--experimental-vm-modules", "--input-type=module", "-"],
        input=script,
        text=True,
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


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
