"""Offline PCM/rechunking and integer decision contracts, without devices."""

import hashlib
import json
import os
from pathlib import Path
import wave

import numpy as np
import pytest

from tools.mww_analyze import (Decider, Pcm, StreamingReplay, capture_gaps, compare_pcm, compare_traces, fnv1a32,
                               main, pcm_statistics, quantize_raw, read_wav, replay_wav,
                               restore_raw)


def write_wav(path, samples, rate=16000):
    values = np.asarray(samples, dtype="<i2")
    channels = values.shape[1] if values.ndim > 1 else 1
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(values.tobytes())


def test_integer_quantization_rounding_saturation_and_raw_recovery():
    raw = np.array([0, 1, 332, 333, 334, 665, 666, 65535], dtype=np.uint16)
    assert quantize_raw(raw).tolist() == [-128, -128, 0, 0, 0, 127, 127, 127]
    scaled = raw.astype(float) * 0.0390625
    np.testing.assert_array_equal(restore_raw(scaled), raw)
    # A changed frontend scale must not quietly pass as raw features.
    with pytest.raises(ValueError, match="scale"):
        restore_raw([0.01])
    with pytest.raises(ValueError, match="unsigned"):
        quantize_raw([-1])


def test_fnv_hash_is_chunk_invariant_and_unsigned():
    assert fnv1a32(b"hello") == 0x4F9F2CAB
    assert fnv1a32(b"llo", fnv1a32(b"he")) == fnv1a32(b"hello")
    assert 0 <= fnv1a32(bytes(range(256))) <= 0xFFFFFFFF


def test_cutoff_is_strict_sum_not_rounded_average():
    decider = Decider(warmup=0)
    for _ in range(5):
        assert not decider.update(229)["detected"]
    # Integer average stays 229, but the real upstream sum is above 229*5.
    decision = decider.update(230)
    assert decision["average_u8"] == 229
    assert decision["detected"]
    assert decider.probabilities == [0] * 5


def test_cooldown_counts_feature_slices_and_requires_below_cutoff():
    decider = Decider(warmup=100)
    for index in range(99):
        result = decider.update(0 if index % 3 == 0 else None)
        assert not result["eligible"]
    assert decider.ignore == -1
    decider.update(None)
    assert decider.ignore == 0
    for _ in range(5):
        result = decider.update(255)
    assert result["detected"]
    for _ in range(150):
        decider.update(255)
    assert decider.ignore == -100
    for _ in range(100):
        decider.update(0)
    assert decider.ignore == 0


def test_pending_score_can_become_eligible_between_inferences():
    decider = Decider(cutoff=229, window=5, warmup=2)
    decider.update(0)
    assert decider.ignore == -1
    result = decider.update(None)
    assert result["eligible"]


def test_pcm_stats_preserve_channels_extrema_and_label_zero_runs(tmp_path):
    samples = np.array([[0, 32767]] * 400 + [[-32768, 100]] * 50, dtype=np.int16)
    path = tmp_path / "two.wav"
    write_wav(path, samples)
    pcm = read_wav(path)
    np.testing.assert_array_equal(pcm.samples, samples)
    stats = pcm_statistics(pcm)
    assert stats["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert stats["per_channel"][0]["zero_runs"] == [{"start_frame": 0, "frames": 400}]
    assert stats["per_channel"][0]["clipped_samples"] == 50
    assert stats["per_channel"][1]["clipped_samples"] == 400
    assert stats["zero_runs_are_not_transport_gaps"]


def test_compare_pcm_detects_known_delay_without_inventing_device_latency():
    rng = np.random.default_rng(17)
    source = rng.integers(-10000, 10000, size=(2000, 1), dtype=np.int16)
    delayed = np.concatenate((np.zeros((37, 1), np.int16), source))
    report = compare_pcm(Pcm(source, 16000, 16, "a"), Pcm(delayed, 16000, 16, "b"))
    assert report["lag_frames"] == 37
    assert report["correlation"] == pytest.approx(1, abs=1e-6)
    assert report["alignment_is_not_pipeline_latency_proof"]


def test_trace_compare_refuses_misalignment_and_counts_missing_values():
    trace = {"rate": 16000, "pre_roll_frames": 0,
             "records": [{"slice": 0, "pcm_end_frame": 480, "features": [1], "score_u8": None}]}
    report = compare_traces(trace, trace)
    assert report["features"] == {"compared": 1, "equal": 1}
    changed = json.loads(json.dumps(trace))
    changed["records"][0]["pcm_end_frame"] += 160
    assert "comparison_skipped" in compare_traces(trace, changed)
    changed["pre_roll_frames"] = 16000
    with pytest.raises(ValueError, match="align"):
        compare_traces(trace, changed)


def test_capture_gaps_distinguish_queue_loss_from_valid_zero_audio():
    records = [{"point": 0, "seq": seq, "position": pos, "frames": 160,
                "rate": 16000, "generation": 7, "dropped": dropped}
               for seq, pos, dropped in ((0, 0, 0), (2, 320, 1), (3, 480, 1))]
    report = capture_gaps({"complete": True, "records": records})
    assert report["points"]["0"]["gap_frames"] == 160
    assert report["points"]["0"]["missing_records"] == 1
    assert report["points"]["0"]["reported_dropped_records"] == 1
    assert report["capture_transport_loss_is_not_device_audio_loss"]
    records[2]["position"] = 100
    with pytest.raises(ValueError, match="Out-of-order"):
        capture_gaps({"records": records})


def test_analysis_cli_operates_without_model_and_reports_all_files(tmp_path):
    for name in ("a", "b"):
        write_wav(tmp_path / f"{name}.wav", np.arange(400))
    target = tmp_path / "report.json"
    assert main(["analyze", str(tmp_path), "--output", str(target)]) == 0
    report = json.loads(target.read_text())
    assert len(report["files"]) == 2
    assert all(row["frames"] == 400 for row in report["files"])


def test_replay_rejects_implicit_resampling(tmp_path):
    path = tmp_path / "wrong-rate.wav"
    write_wav(path, np.arange(100), rate=48000)
    with pytest.raises(ValueError, match="16000"):
        replay_wav(path)


@pytest.mark.diagnostic_reference
def test_stream_rechunking_keeps_feature_sequence_and_model_state():
    pytest.importorskip("pymicro_features")

    class Runner:
        def __init__(self):
            self.inputs = []

        def invoke(self, features):
            self.inputs.append(features.copy())
            return 255

    source = (np.sin(np.arange(32000) * 0.19) * 8000).astype("<i2").tobytes()
    results = []
    for chunk in (1, 159, 256, 32000):
        runner = Runner()
        replay = StreamingReplay(runner, warmup=0)
        for offset in range(0, len(source), chunk * 2):
            replay.feed(source[offset:offset + chunk * 2])
        results.append((replay.trace, runner.inputs))
        assert len(replay.detections) > 1
        # There is no reset method on the backend: detections cannot reload it.
        assert len(runner.inputs) == len(replay.trace) // 3
    for trace, inputs in results[1:]:
        assert trace == results[0][0]
        np.testing.assert_array_equal(inputs, results[0][1])


@pytest.mark.diagnostic_reference
def test_actual_model_silence_does_not_activate(tmp_path):
    pytest.importorskip("pymicro_wakeword")
    path = tmp_path / "silence.wav"
    write_wav(path, np.zeros(48000, dtype=np.int16))
    report, _ = replay_wav(path)
    assert report["detections"] == []
    assert report["peak_score_u8"] < 229


CORPUS = Path(os.environ["MWW_TEST_CORPUS"]) if os.environ.get("MWW_TEST_CORPUS") else None


@pytest.mark.skipif(CORPUS is None, reason="Set MWW_TEST_CORPUS to an identified official fixture corpus")
@pytest.mark.diagnostic_reference
@pytest.mark.parametrize("number", [1, 2, 3])
def test_official_alexa_corpus_stateful_replay_is_rechunking_invariant(number):
    pytest.importorskip("pymicro_wakeword")
    manifest = json.loads((CORPUS / "manifest.json").read_text())
    path = CORPUS / f"alexa-{number}.wav"
    fixture = next(row for row in manifest["fixtures"] if row["file"] == path.name)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == fixture["sha256"]
    first, trace_a = replay_wav(path, chunk_frames=159)
    second, trace_b = replay_wav(path, chunk_frames=1024)
    assert trace_a == trace_b
    assert first["detections"] == second["detections"]
    assert len(first["detections"]) == 1
    assert first["peak_score_u8"] > 229
    assert first["qualification"] == "host_streaming_only_device_parity_unverified"
