#!/usr/bin/env python3
"""Runtime checks for browser microphone resampling."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


PROCESSOR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "voip_stack"
    / "frontend"
    / "voip-stack-processor.js"
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
