"""Measured packet arrivals must not produce holes in actual worklet output."""
import json
from pathlib import Path
import subprocess

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.js_runtime


@pytest.mark.parametrize('source_rate,context_rate,channels', [
    (16000, 48000, 1), (24000, 48000, 1), (32000, 48000, 1),
    (48000, 44100, 1), (16000, 48000, 2),
])
def test_measured_jitter_preserves_tone_and_continuity(tmp_path, source_rate, context_rate, channels):
    trace = json.loads((ROOT / 'tests/fixtures/audio/arrival-jitter.json').read_text())
    trace['format'] = f'{source_rate}:s16le:{channels}:10'
    fixture = tmp_path / 'arrivals.json'
    fixture.write_text(json.dumps(trace))
    report = tmp_path / 'output.json'
    subprocess.run([
        'node', str(ROOT / 'tools/ha_voip_lab/playout_replay.mjs'),
        str(ROOT / 'custom_components/voip_stack/frontend/voip-stack-playback-processor.js'),
        str(fixture), str(report), str(context_rate),
    ], check=True, capture_output=True, text=True)
    result = json.loads(report.read_text())
    assert result['packets'] == len(trace['times'])
    assert result['underruns'] == 0
    assert result['framesDropped'] == 0
    samples = np.fromfile(str(report) + '.f32', dtype='<f4').reshape(-1, channels)
    signal = samples[:, 0]
    zero = np.max(np.abs(samples), axis=1) == 0
    edges = np.flatnonzero(np.r_[False, zero, False][1:] != np.r_[False, zero, False][:-1])
    assert max(edges[1::2] - edges[::2], default=0) / context_rate < 0.0005
    # Independent signal checks: preserve pitch and level, not just counters.
    spectrum = abs(np.fft.rfft(signal * np.hanning(len(signal))))
    frequencies = np.fft.rfftfreq(len(signal), 1 / context_rate)
    assert abs(frequencies[np.argmax(spectrum)] - 440) < 0.15
    expected_rms = 10000 / 32768 / np.sqrt(2)
    assert abs(20 * np.log10(np.sqrt(np.mean(signal**2)) / expected_rms)) < 0.2
    if channels == 2:
        assert abs(np.sqrt(np.mean(samples[:, 1]**2)) / np.sqrt(np.mean(signal**2)) - 0.5) < 0.01
        assert abs(np.corrcoef(samples.T)[0, 1]) < 0.02


def test_real_network_outage_is_still_reported(tmp_path):
    trace = {'format': '16000:s16le:1:10',
             'times': [n / 100 + (0.6 if n >= 300 else 0) for n in range(700)]}
    fixture = tmp_path / 'outage.json'
    fixture.write_text(json.dumps(trace))
    report = tmp_path / 'output.json'
    subprocess.run([
        'node', str(ROOT / 'tools/ha_voip_lab/playout_replay.mjs'),
        str(ROOT / 'custom_components/voip_stack/frontend/voip-stack-playback-processor.js'),
        str(fixture), str(report),
    ], check=True, capture_output=True, text=True)
    result = json.loads(report.read_text())
    assert result['underruns'] >= 1
    assert result['maxZeroRunMs'] > 100
