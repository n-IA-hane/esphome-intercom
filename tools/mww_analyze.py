#!/usr/bin/env python3
"""Offline PCM inspection and stateful ESPHome-style microWakeWord replay.

No device, Home Assistant, server or firmware changes are made. Replay uses the
small pymicro-wakeword TFLite C runtime, not TensorFlow. Its integer input and
decision policy follow ESPHome; target parity still requires a device trace.
"""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import sys
from typing import Iterable
import wave

import numpy as np

RATE = 16000
FEATURE_SAMPLES = 160
FEATURE_BINS = 40
STRIDE = 3


def fnv1a32(data: bytes, value=2166136261) -> int:
    for byte in data:
        value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
    return value


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def quantize_raw(raw) -> np.ndarray:
    """ESPHome's rounded integer frontend conversion, including saturation."""
    values = np.asarray(raw)
    if np.any(values < 0) or np.any(values > 65535) or np.any(values != np.floor(values)):
        raise ValueError("Raw frontend features must be unsigned 16-bit integers")
    return np.clip(((values.astype(np.int64) * 256 + 333) // 666) - 128, -128, 127).astype(np.int8)


def restore_raw(features) -> np.ndarray:
    """Undo pymicro-features 2.0.2's exact raw * (5 / 128) output scale.

    The extension exposes float32_scale=0.0390625, not the integer frontend
    output. Recover and verify that integer before applying ESPHome's formula.
    """
    values = np.asarray(features, dtype=np.float64) * (128 / 5)
    rounded = np.rint(values)
    if np.any(np.abs(values - rounded) > 1e-5):
        raise ValueError("Frontend scale differs from the qualified pymicro-features contract")
    return rounded.astype(np.int64)


@dataclass
class Pcm:
    samples: np.ndarray
    rate: int
    bits: int
    source_sha256: str

    @property
    def channels(self):
        return self.samples.shape[1]


def read_wav(path: Path) -> Pcm:
    with wave.open(str(path), "rb") as stream:
        bits = stream.getsampwidth() * 8
        channels, rate = stream.getnchannels(), stream.getframerate()
        if bits not in (16, 32) or stream.getcomptype() != "NONE":
            raise ValueError("Only uncompressed S16/S32 WAV is supported; convert explicitly")
        frames = stream.getnframes()
        raw = stream.readframes(frames)
    samples = np.frombuffer(raw, dtype="<i2" if bits == 16 else "<i4")
    if samples.size != frames * channels:
        raise ValueError("Truncated WAV payload")
    return Pcm(samples.reshape((-1, channels)), rate, bits, sha256(path))


def zero_runs(samples, minimum_frames: int) -> list[dict]:
    """Exact-zero intervals are observations, not proof of transport loss."""
    zeros = np.asarray(samples) == 0
    edges = np.diff(np.concatenate(([False], zeros, [False])).astype(np.int8))
    return [dict(start_frame=int(a), frames=int(b - a))
            for a, b in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1))
            if b - a >= minimum_frames]


def dbfs(value: float):
    return 20 * math.log10(value) if value > 0 else None


def pcm_statistics(pcm: Pcm, zero_ms: float = 20) -> dict:
    if zero_ms <= 0:
        raise ValueError("Zero-run minimum must be positive")
    full_scale = float(1 << (pcm.bits - 1))
    result = {"rate": pcm.rate, "bits": pcm.bits, "channels": pcm.channels,
              "frames": len(pcm.samples), "duration_seconds": len(pcm.samples) / pcm.rate,
              "sha256": pcm.source_sha256, "per_channel": [],
              "zero_runs_are_not_transport_gaps": True}
    for index in range(pcm.channels):
        values = pcm.samples[:, index].astype(np.float64)
        rms = float(np.sqrt(np.mean(values * values))) if len(values) else 0.0
        peak = float(np.max(np.abs(values))) if len(values) else 0.0
        clipped = int(np.count_nonzero((values == -full_scale) | (values == full_scale - 1)))
        result["per_channel"].append({
            "channel": index, "rms": rms, "rms_dbfs": dbfs(rms / full_scale),
            "peak": peak, "peak_dbfs": dbfs(peak / full_scale),
            "dc": float(np.mean(values)) if len(values) else 0.0,
            "clipped_samples": clipped, "clipped_fraction": clipped / len(values) if len(values) else 0.0,
            "zero_runs": zero_runs(values, max(1, math.ceil(zero_ms * pcm.rate / 1000))),
        })
    return result


class Decider:
    """ESPHome StreamingModel/WakeWordModel state, counted in feature slices.

    A new uint8 score arrives every stride features. Cooldown increments on
    *every feature* while the latest score is below cutoff. Detection clears
    probabilities, not the interpreter, frontend or stride phase.
    """

    def __init__(self, cutoff=229, window=5, warmup=100):
        if not 0 <= cutoff <= 255 or window < 1 or warmup < 0:
            raise ValueError("Invalid cutoff, window or warmup")
        self.cutoff, self.window, self.warmup = cutoff, window, warmup
        self.probabilities = [0] * window
        self.index = 0
        self.ignore = -warmup
        self.pending = False

    def update(self, score: int | None) -> dict:
        if score is not None:
            if not 0 <= score <= 255:
                raise ValueError("Score must be uint8")
            self.index = (self.index + 1) % self.window
            self.probabilities[self.index] = int(score)
            self.pending = True
        if self.probabilities[self.index] < self.cutoff:
            self.ignore = min(self.ignore + 1, 0)
        total = sum(self.probabilities)
        eligible = self.pending and self.ignore == 0
        detected = eligible and total > self.cutoff * self.window
        result = dict(score_u8=score, sum=total, average_u8=total // self.window,
                      max_u8=max(self.probabilities), eligible=eligible,
                      ignore_slices=self.ignore, detected=bool(detected))
        if eligible:
            self.pending = False
        if detected:
            self.probabilities = [0] * self.window
            self.ignore = -self.warmup
        return result


class TensorRunner:
    """Checked access to the packaged public TFLite C inference interface."""

    def __init__(self, config: Path | None = None):
        try:
            from pymicro_wakeword import MicroWakeWord, Model
        except ImportError as error:
            raise ValueError("Replay requires pymicro-wakeword==2.5.0 and pymicro-features==2.0.2") from error
        self.model = MicroWakeWord.from_config(config) if config else MicroWakeWord.from_builtin(Model.ALEXA)
        model = self.model
        if model.stride != STRIDE or model.input_dtype != np.int8 or model.output_dtype != np.uint8:
            model.close()
            raise ValueError("Expected a stride-3 int8-input/uint8-output model")
        if model.lib.TfLiteTensorByteSize(model.input_tensor) != STRIDE * FEATURE_BINS or \
                model.lib.TfLiteTensorByteSize(model.output_tensor) != 1:
            model.close()
            raise ValueError("Unexpected model tensor geometry")
        self.metadata = {"file": str(model.tflite_model), "sha256": sha256(Path(model.tflite_model)),
                         "stride": model.stride, "input_dtype": "int8", "output_dtype": "uint8",
                         "input_scale": model.input_scale, "input_zero_point": model.input_zero_point,
                         "output_scale": model.output_scale, "output_zero_point": model.output_zero_point}

    def invoke(self, features: np.ndarray) -> int:
        model = self.model
        features = np.ascontiguousarray(features, dtype=np.int8)
        if features.shape != (STRIDE, FEATURE_BINS):
            raise ValueError("Expected exactly three 40-bin feature slices")
        status = model.lib.TfLiteTensorCopyFromBuffer(
            model.input_tensor, features.ctypes.data_as(ctypes.c_void_p), features.nbytes)
        if status != 0 or model.lib.TfLiteInterpreterInvoke(model.interpreter) != 0:
            raise ValueError("TFLite input copy or invoke failed")
        output = np.empty(1, dtype=np.uint8)
        if model.lib.TfLiteTensorCopyToBuffer(
                model.output_tensor, output.ctypes.data_as(ctypes.c_void_p), 1) != 0:
            raise ValueError("TFLite output copy failed")
        return int(output[0])

    def close(self):
        self.model.close()


class StreamingReplay:
    """State survives arbitrary PCM chunk boundaries and detections."""

    def __init__(self, runner, frontend=None, *, cutoff=229, window=5, warmup=100):
        if frontend is None:
            from pymicro_features import MicroFrontend
            frontend = MicroFrontend()
        self.frontend, self.runner = frontend, runner
        self.decider = Decider(cutoff, window, warmup)
        self.buffer = bytearray()
        self.pending_features = []
        self.frames_consumed = 0
        self.trace = []
        self.detections = []
        self.feature_hash = 2166136261
        self.score_hash = 2166136261

    def feed(self, pcm_bytes: bytes):
        self.buffer.extend(pcm_bytes)
        while len(self.buffer) >= FEATURE_SAMPLES * 2:
            output = self.frontend.process_samples(bytes(self.buffer[:FEATURE_SAMPLES * 2]))
            consumed = output.samples_read
            if not 0 < consumed <= FEATURE_SAMPLES:
                raise ValueError("Frontend failed to make bounded progress")
            del self.buffer[:consumed * 2]
            self.frames_consumed += consumed
            if not output.features:
                continue
            raw = restore_raw(output.features)
            if raw.shape != (FEATURE_BINS,):
                raise ValueError("Expected 40 frontend bins")
            features = quantize_raw(raw)
            self.feature_hash = fnv1a32(features.tobytes(), self.feature_hash)
            self.pending_features.append(features)
            score = None
            if len(self.pending_features) == STRIDE:
                score = self.runner.invoke(np.stack(self.pending_features))
                self.score_hash = fnv1a32(bytes([score]), self.score_hash)
                self.pending_features.clear()
            decision = self.decider.update(score)
            event = {"slice": len(self.trace), "pcm_end_frame": self.frames_consumed,
                     "features": features.tolist(), **decision}
            self.trace.append(event)
            if decision["detected"]:
                self.detections.append({key: value for key, value in event.items() if key != "features"})


def replay_wav(path: Path, *, config=None, channel=0, chunk_frames=256,
               pre_roll_ms=2000, post_roll_ms=1000, cutoff=229, window=5, warmup=100) -> tuple[dict, dict]:
    pcm = read_wav(path)
    if pcm.rate != RATE or pcm.bits != 16 or not 0 <= channel < pcm.channels:
        raise ValueError("Replay needs S16 at 16000 Hz and an existing explicit channel; no implicit resampling")
    if chunk_frames < 1 or pre_roll_ms < 0 or post_roll_ms < 0:
        raise ValueError("Invalid chunk size or padding")
    runner = TensorRunner(config)
    try:
        replay = StreamingReplay(runner, cutoff=cutoff, window=window, warmup=warmup)
        pre_frames = int(pre_roll_ms * RATE / 1000)
        post_frames = int(post_roll_ms * RATE / 1000)
        for data in (bytes(pre_frames * 2), pcm.samples[:, channel].astype("<i2").tobytes(), bytes(post_frames * 2)):
            for offset in range(0, len(data), chunk_frames * 2):
                replay.feed(data[offset:offset + chunk_frames * 2])
        for detection in replay.detections:
            detection["source_end_frame"] = detection["pcm_end_frame"] - pre_frames
            detection["source_end_seconds"] = detection["source_end_frame"] / RATE
        scores = [row["score_u8"] for row in replay.trace if row["score_u8"] is not None]
        result = {"file": str(path), "pcm": pcm_statistics(pcm), "model": runner.metadata,
                  "policy": dict(cutoff_u8=cutoff, sliding_window=window, warmup_cooldown_slices=warmup,
                                 stride=STRIDE, reset_interpreter_after_detection=False,
                                 feature_quantization="clip(((raw*256+333)//666)-128,-128,127)",
                                 channel=channel, pre_roll_frames=pre_frames, post_roll_frames=post_frames),
                  "feature_slices": len(replay.trace), "inferences": len(scores),
                  "features": len(replay.trace), "feature_fnv1a32": replay.feature_hash,
                  "score_fnv1a32": replay.score_hash, "invoke": {"calls": len(scores)},
                  "peak_score_u8": max(scores, default=0),
                  "peak_window_average_u8": max((r["average_u8"] for r in replay.trace), default=0),
                  "detections": replay.detections, "remaining_pcm_frames": len(replay.buffer) // 2,
                  "remaining_feature_slices": len(replay.pending_features),
                  "qualification": "host_streaming_only_device_parity_unverified"}
        trace = {"version": 1, "pcm_sha256": pcm.source_sha256, "rate": RATE,
                 "pre_roll_frames": pre_frames, "model": runner.metadata, "records": replay.trace}
        return result, trace
    finally:
        runner.close()


def compare_pcm(reference: Pcm, candidate: Pcm, channel=0, max_lag_ms=250) -> dict:
    if reference.rate != candidate.rate or not 0 <= channel < min(reference.channels, candidate.channels):
        raise ValueError("PCM comparison requires equal rates and an existing channel")
    if max_lag_ms < 0:
        raise ValueError("Maximum lag cannot be negative")
    a = reference.samples[:, channel].astype(np.float64) / (1 << (reference.bits - 1))
    b = candidate.samples[:, channel].astype(np.float64) / (1 << (candidate.bits - 1))
    if not len(a) or not len(b):
        raise ValueError("Cannot correlate empty PCM")
    a -= np.mean(a)
    b -= np.mean(b)
    size = 1 << (len(a) + len(b) - 2).bit_length()
    corr = np.fft.irfft(np.conj(np.fft.rfft(a, size)) * np.fft.rfft(b, size), size)
    limit = min(int(reference.rate * max_lag_ms / 1000), len(a) - 1, len(b) - 1)
    lags = np.arange(-limit, limit + 1)
    lag = int(lags[np.argmax(corr[lags % size])])
    left, right = (a[:len(b) - lag], b[lag:]) if lag >= 0 else (a[-lag:], b[:len(a) + lag])
    count = min(len(left), len(right))
    left, right = left[:count], right[:count]
    norm = float(np.linalg.norm(left) * np.linalg.norm(right))
    return {"lag_frames": lag, "lag_seconds": lag / reference.rate,
            "lag_convention": "positive_candidate_delayed", "overlap_frames": count,
            "correlation": float(np.dot(left, right) / norm) if norm else None,
            "rms_error_fs": float(np.sqrt(np.mean((left - right) ** 2))) if count else None,
            "alignment_is_not_pipeline_latency_proof": True}


def compare_traces(reference: dict, candidate: dict) -> dict:
    """Compare supplied feature/score streams without inventing missing rows."""
    for key in ("rate", "pre_roll_frames"):
        if reference.get(key) != candidate.get(key):
            raise ValueError(f"Trace {key} differs; align the acquisition contracts first")
    if reference.get("model", {}).get("sha256") != candidate.get("model", {}).get("sha256"):
        raise ValueError("Trace model hashes differ")
    a, b = reference["records"], candidate["records"]
    pairs = list(zip(a, b))
    aligned = all(x.get("slice") == y.get("slice") and x.get("pcm_end_frame") == y.get("pcm_end_frame")
                  for x, y in pairs)
    result = {"reference_rows": len(a), "candidate_rows": len(b), "compared_rows": len(pairs),
              "positions_match": aligned, "complete": len(a) == len(b),
              "device_parity_claimed": False}
    if not aligned:
        result["comparison_skipped"] = "Feature/PCM positions differ"
        return result
    for field in ("features", "score_u8", "detected"):
        available = [(x[field], y[field]) for x, y in pairs if field in x and field in y]
        result[field] = {"compared": len(available), "equal": sum(x == y for x, y in available)}
    return result


def capture_gaps(manifest: dict) -> dict:
    """Recount position gaps independently of zero-filled WAV samples.

    esp_audio_capture positions advance even for records dropped by its bounded
    queue. Its dropped counter counts records, not PCM frames. Missing trailing
    records cannot be inferred from the last received record.
    """
    points = {}
    generation = None
    for row in manifest.get("records", []):
        keys = ("point", "seq", "position", "frames", "rate", "generation", "dropped")
        if any(type(row.get(key)) is not int or row[key] < 0 for key in keys) or row["rate"] == 0:
            raise ValueError("Invalid capture record")
        if generation is None:
            generation = row["generation"]
        if row["generation"] != generation:
            raise ValueError("Capture generation changed")
        stream = points.setdefault(str(row["point"]), {
            "end_frame": 0, "last_sequence": -1, "gap_frames": 0,
            "missing_records": 0, "reported_dropped_records": 0, "rate": row["rate"]})
        if row["position"] < stream["end_frame"] or row["seq"] <= stream["last_sequence"]:
            raise ValueError("Out-of-order capture position or sequence")
        if row["rate"] != stream["rate"]:
            raise ValueError("Capture sample rate changed")
        stream["gap_frames"] += row["position"] - stream["end_frame"]
        stream["missing_records"] += row["seq"] - stream["last_sequence"] - 1
        stream["end_frame"] = row["position"] + row["frames"]
        stream["last_sequence"] = row["seq"]
        stream["reported_dropped_records"] = max(stream["reported_dropped_records"], row["dropped"])
    return {"complete": manifest.get("complete") is True, "generation": generation,
            "points": points, "trailing_loss_unknown": True,
            "capture_transport_loss_is_not_device_audio_loss": True}


def wav_paths(paths: Iterable[Path]) -> list[Path]:
    files = sorted({item for path in paths for item in
                    (path.rglob("*.wav") if path.is_dir() else [path])})
    if not files:
        raise ValueError("No WAV files found")
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    stats = sub.add_parser("analyze", help="PCM statistics, preserving every source sample")
    stats.add_argument("paths", type=Path, nargs="+")
    stats.add_argument("--capture-manifest", type=Path,
                       help="Optional esp_audio_capture manifest, independent of waveform zero runs")
    replay = sub.add_parser("replay", help="Stateful streaming replay, with an explicit ESPHome policy")
    replay.add_argument("paths", type=Path, nargs="+")
    replay.add_argument("--model-config", type=Path)
    replay.add_argument("--channel", type=int, default=0)
    replay.add_argument("--chunk-frames", type=int, default=256)
    replay.add_argument("--pre-roll-ms", type=int, default=2000)
    replay.add_argument("--post-roll-ms", type=int, default=1000)
    replay.add_argument("--cutoff", type=int, default=229)
    replay.add_argument("--window", type=int, default=5)
    replay.add_argument("--warmup", type=int, default=100)
    replay.add_argument("--trace-dir", type=Path)
    compare = sub.add_parser("compare", help="Optional waveform alignment or supplied trace comparison")
    compare.add_argument("reference", type=Path)
    compare.add_argument("candidate", type=Path)
    compare.add_argument("--channel", type=int, default=0)
    compare.add_argument("--max-lag-ms", type=float, default=250)
    compare.add_argument("--traces", action="store_true")
    for command in (stats, replay, compare):
        command.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = {"version": 1, "tool": "mww_analyze", "command": args.command}
        if args.command == "analyze":
            result["files"] = [{"file": str(path), **pcm_statistics(read_wav(path))}
                               for path in wav_paths(args.paths)]
            if args.capture_manifest:
                result["capture"] = capture_gaps(json.loads(args.capture_manifest.read_text()))
        elif args.command == "replay":
            result["dependencies"] = {name: importlib.metadata.version(name)
                                      for name in ("pymicro-wakeword", "pymicro-features", "numpy")}
            result["files"] = []
            if args.trace_dir:
                args.trace_dir.mkdir(parents=True, exist_ok=True)
            for path in wav_paths(args.paths):
                report, trace = replay_wav(path, config=args.model_config, channel=args.channel,
                                          chunk_frames=args.chunk_frames, pre_roll_ms=args.pre_roll_ms,
                                          post_roll_ms=args.post_roll_ms, cutoff=args.cutoff,
                                          window=args.window, warmup=args.warmup)
                if args.trace_dir:
                    trace_path = args.trace_dir / f"{path.stem}-{report['pcm']['sha256'][:12]}.json"
                    trace_path.write_text(json.dumps(trace, separators=(",", ":")) + "\n")
                    report["trace"] = str(trace_path)
                result["files"].append(report)
        elif args.traces:
            result["comparison"] = compare_traces(json.loads(args.reference.read_text()),
                                                  json.loads(args.candidate.read_text()))
        else:
            result["comparison"] = compare_pcm(read_wav(args.reference), read_wav(args.candidate),
                                               args.channel, args.max_lag_ms)
        body = json.dumps(result, indent=2, allow_nan=False) + "\n"
        if args.output:
            args.output.write_text(body)
        else:
            print(body, end="")
        return 0
    except (ValueError, OSError, wave.Error, ImportError, importlib.metadata.PackageNotFoundError) as error:
        parser.exit(2, f"mww_analyze: {error}\n")


if __name__ == "__main__":
    sys.exit(main())
