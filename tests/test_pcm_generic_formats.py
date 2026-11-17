"""PCM capability bounds and continuous rational conversion across packets."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import numpy as np
import pytest

from .voip_phase1_support import audio_format, audio_pcm
from .test_frontend_audio_worklet_runtime import _worklet_script


@pytest.mark.parametrize(
    ("rate", "duration", "samples"),
    [
        (48000, 2.5, 120),
        (24000, 15, 360),
        (22050, 20, 441),
        (8000, 32, 256),
        (192000, 1000, 192000),
        (1, 1000, 1),
    ],
)
def test_pcm_formats_follow_numeric_capabilities(rate, duration, samples):
    fmt = audio_format.AudioFormat(rate, "s24le_in_s32", 2, duration)

    assert fmt.nominal_frame_samples == samples
    assert type(fmt.nominal_frame_samples) is int
    assert fmt.nominal_frame_bytes == samples * 8
    assert type(fmt.nominal_frame_bytes) is int
    assert audio_format.parse_audio_format_token(fmt.wire_token()) == fmt


def test_pcm_integral_duration_keeps_existing_token():
    assert (
        audio_format.AudioFormat(16000, "s16le", 1, 20.0).wire_token()
        == "16000:s16le:1:20"
    )
    assert (
        audio_format.AudioFormat(48000, "s16le", 1, 2.5).wire_token()
        == "48000:s16le:1:2.5"
    )


@pytest.mark.parametrize("rate", [0, -1, 192001, 16000.0, True])
def test_pcm_sample_rate_stays_inside_integer_backend_budget(rate):
    with pytest.raises(ValueError):
        audio_format.AudioFormat(rate, "s16le", 1, 20)


@pytest.mark.parametrize(
    "duration", [0, -1, 1001, 10**999, float("nan"), float("inf"), True]
)
def test_pcm_duration_stays_inside_finite_backend_budget(duration):
    with pytest.raises(ValueError):
        audio_format.AudioFormat(48000, "s16le", 1, duration)


def test_pcm_quantum_requires_whole_source_samples():
    with pytest.raises(ValueError, match="whole PCM frames"):
        audio_format.AudioFormat(44100, "s16le", 1, 2.5)


@pytest.mark.parametrize(
    ("source_rate", "duration", "blocks"),
    [(8000, 32, 25), (24000, 15, 40)],
)
def test_fractional_destination_samples_keep_phase_and_count(
    source_rate, duration, blocks
):
    source = audio_format.AudioFormat(source_rate, "s16le", 1, duration)
    destination = audio_format.AudioFormat(44100, "s16le", 1, 20)
    converter = audio_pcm.PcmFrameConverter(source, destination)
    whole_source = audio_format.AudioFormat(source_rate, "s16le", 1, duration * blocks)
    reference = audio_pcm.PcmFrameConverter(whole_source, destination)
    positions = np.arange(whole_source.nominal_frame_samples)
    pcm = (
        (12000 * np.sin(2 * np.pi * 997 * positions / source_rate))
        .astype("<i2")
        .tobytes()
    )

    actual = []
    for offset in range(0, len(pcm), source.nominal_frame_bytes):
        actual.extend(
            converter.convert(pcm[offset : offset + source.nominal_frame_bytes])
        )

    assert b"".join(actual) == b"".join(reference.convert(pcm))
    assert sum(
        len(frame) // 2 for frame in actual
    ) == source_rate * duration * blocks * 44100 // (source_rate * 1000)
    assert all(len(frame) == destination.nominal_frame_bytes for frame in actual)
    assert not converter.has_pending_pcm


def test_fractional_downsampling_preserves_tone_and_rejects_alias():
    source = audio_format.AudioFormat(48000, "s16le", 1, 2.5)
    destination = audio_format.AudioFormat(11025, "s16le", 1, 40)
    converter = audio_pcm.PcmFrameConverter(source, destination)
    positions = np.arange(48000)
    pcm = (
        (
            10000
            * (
                np.sin(2 * np.pi * 1000 * positions / source.sample_rate)
                + np.sin(2 * np.pi * 12000 * positions / source.sample_rate)
            )
        )
        .astype("<i2")
        .tobytes()
    )
    converted = []
    for offset in range(0, len(pcm), source.nominal_frame_bytes):
        converted.extend(
            converter.convert(pcm[offset : offset + source.nominal_frame_bytes])
        )
    samples = np.frombuffer(b"".join(converted), dtype="<i2").astype(np.float64)
    assert len(samples) == 11025
    samples = samples[2205:]
    phase = np.arange(len(samples)) / destination.sample_rate

    def amplitude(frequency):
        return (
            abs(np.sum(samples * np.exp(-2j * np.pi * frequency * phase)))
            * 2
            / len(samples)
        )

    assert amplitude(1000) > 9000
    assert amplitude(975) < amplitude(1000) * 0.08
    assert not converter.has_pending_pcm


def test_fractional_phase_handles_blocks_with_no_output_samples():
    source = audio_format.AudioFormat(1000, "s16le", 1, 1)
    destination = audio_format.AudioFormat(1, "s16le", 1, 1000)
    converter = audio_pcm.PcmFrameConverter(source, destination)
    whole = audio_pcm.PcmFrameConverter(
        audio_format.AudioFormat(1000, "s16le", 1, 1000), destination
    )
    pcm = np.arange(1000, dtype="<i2").tobytes()
    output = []
    for offset in range(0, len(pcm), 2):
        output.extend(converter.convert(pcm[offset : offset + 2]))

    assert output == whole.convert(pcm)
    assert len(output) == 1
    assert not converter.has_pending_pcm


@pytest.mark.js_runtime
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_worklets_accept_generic_and_fractional_pcm_quanta():
    frontend = (
        Path(__file__).resolve().parents[1] / "custom_components/voip_stack/frontend"
    )
    paths = [
        str(frontend / name)
        for name in (
            "voip-stack-processor.js",
            "voip-stack-playback-processor.js",
        )
    ]
    script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert/strict");
for (const path of PATHS) {
  let Processor;
  const context = vm.createContext({
    AudioWorkletProcessor: class {
      constructor() { this.port = { postMessage() {}, onmessage: null }; }
    },
    sampleRate: 48000, currentTime: 0,
    registerProcessor(_name, value) { Processor = value; },
    ArrayBuffer, DataView, Float32Array, Math, Number, Object, Error,
  });
  vm.runInContext(fs.readFileSync(path, "utf8"), context);
  function create(sampleRate, frameMs) {
    return new Processor({processorOptions: {
      format: {sampleRate, frameMs, pcmFormat: "s16le", channels: 1},
    }});
  }
  for (const [rate, duration, samples] of [[50000, 1.1, 55], [48000, 2.5, 120], [24000, 15, 360], [8000, 30, 240], [22050, 20, 441]]) {
    const processor = create(rate, duration);
    assert.equal(processor._format.frameSamples, samples);
  }
  for (const [rate, duration] of [[50000, 1.10000001], [50000, 1.11], [192001, 20], [0, 20], [16000.5, 20], [48000, 1001], [48000, 0], [48000, Infinity], [44100, 2.5]]) {
    assert.throws(() => create(rate, duration));
  }
}
""".replace("PATHS", json.dumps(paths))
    subprocess.run(
        ["node", "-"], input=_worklet_script(script), text=True, check=True, capture_output=True
    )
