#!/usr/bin/env python3
"""Produce fail-closed RTP continuity evidence from a qualification PCAP."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Iterable


FIELDS = (
    "frame.time_relative",
    "ip.src",
    "udp.srcport",
    "ip.dst",
    "udp.dstport",
    "rtp.ssrc",
    "rtp.p_type",
    "rtp.seq",
    "rtp.timestamp",
)


def _extended_sequence(sequence: int, previous: int | None, cycle: int) -> tuple[int, int]:
    if previous is not None:
        if previous > 0xF000 and sequence < 0x1000:
            cycle += 0x10000
        elif previous < 0x1000 and sequence > 0xF000:
            return cycle - 0x10000 + sequence, cycle
    return cycle + sequence, cycle


def analyze_rows(rows: Iterable[str]) -> list[dict[str, Any]]:
    streams: dict[tuple[str, ...], dict[str, Any]] = {}
    flow_sequences: dict[tuple[str, ...], set[int]] = {}
    flow_state: dict[tuple[str, ...], tuple[int | None, int]] = {}
    for raw in rows:
        columns = raw.rstrip("\n").split("\t")
        if len(columns) != len(FIELDS) or not all(columns):
            continue
        key = tuple(columns[1:7])
        flow_key = tuple(columns[1:6])
        sequence = int(columns[7])
        previous, cycle = flow_state.get(flow_key, (None, 0))
        extended, cycle = _extended_sequence(sequence, previous, cycle)
        flow_state[flow_key] = (sequence, cycle)
        flow_sequences.setdefault(flow_key, set()).add(extended)
        stream = streams.setdefault(
            key,
            {
                "source": f"{columns[1]}:{columns[2]}",
                "destination": f"{columns[3]}:{columns[4]}",
                "ssrc": columns[5].lower(),
                "payload_type": int(columns[6]),
                "times": [],
                "sequences": [],
                "timestamps": [],
            },
        )
        stream["times"].append(float(columns[0]))
        stream["sequences"].append(extended)
        stream["timestamps"].append(int(columns[8]))

    evidence: list[dict[str, Any]] = []
    for key, stream in streams.items():
        extended = stream.pop("sequences")
        unique = set(extended)
        first, last = min(unique), max(unique)
        flow_unique = flow_sequences[key[:5]]
        received_in_range = sum(first <= item <= last for item in flow_unique)
        expected = last - first + 1
        times = stream.pop("times")
        timestamps = stream.pop("timestamps")
        timestamp_steps = [
            (current - prior) & 0xFFFFFFFF
            for prior, current in zip(timestamps, timestamps[1:], strict=False)
            if current != prior
        ]
        deltas = [
            (current - prior) * 1000
            for prior, current in zip(times, times[1:], strict=False)
        ]
        evidence.append(
            {
                **stream,
                "packets": len(extended),
                # Payload types on one SSRC share a sequence-number space.
                # A telephone-event packet between two audio packets is not
                # a lost audio packet merely because it has another PT.
                "lost_packets": expected - received_in_range,
                "duplicate_packets": len(extended) - len(unique),
                "duration_seconds": round(times[-1] - times[0], 6),
                "mean_delta_ms": round(sum(deltas) / len(deltas), 6)
                if deltas
                else 0.0,
                "max_delta_ms": round(max(deltas), 6) if deltas else 0.0,
                "timestamp_step": Counter(timestamp_steps).most_common(1)[0][0]
                if timestamp_steps
                else 0,
            }
        )
    return sorted(evidence, key=lambda item: (item["source"], item["ssrc"]))


def analyze_pcap(path: Path) -> list[dict[str, Any]]:
    tshark = shutil.which("tshark")
    if not tshark:
        raise RuntimeError("tshark is required for RTP PCAP evidence")
    command = [tshark, "-r", str(path), "-Y", "rtp", "-T", "fields"]
    for field in FIELDS:
        command.extend(("-e", field))
    result = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    streams = analyze_rows(result.stdout.splitlines())
    audio_formats = _sdp_audio_formats(tshark, path)
    for stream in streams:
        source_host, source_port = stream["source"].rsplit(":", 1)
        audio_format = audio_formats.get(
            (source_host, int(source_port), stream["payload_type"])
        )
        if audio_format is None:
            stream["media_type"] = "video"
            continue
        clock_rate, encoding = audio_format
        if encoding == "telephone-event":
            stream["media_type"] = "event"
            stream["clock_rate"] = clock_rate
            continue
        stream["media_type"] = "audio"
        expected_delta = 1000 * stream["timestamp_step"] / clock_rate
        stream["clock_rate"] = clock_rate
        stream["expected_delta_ms"] = round(expected_delta, 6)
        if expected_delta > 0:
            stream["cadence_ratio"] = round(
                stream["mean_delta_ms"] / expected_delta, 6
            )
    return streams


def evaluate_streams(
    streams: list[dict[str, Any]],
    *,
    require_streams: int,
    max_audio_loss: int,
    max_video_loss: int | None,
    max_cadence_ratio: float,
) -> list[str]:
    """Evaluate strict audio and optional video continuity contracts."""

    failures = []
    if len(streams) < require_streams:
        failures.append(f"expected at least {require_streams} RTP streams")
    for stream in streams:
        media_type = stream.get("media_type", "video")
        loss_limit = (
            max_audio_loss
            if media_type == "audio"
            else max_video_loss
            if media_type == "video"
            else None
        )
        if loss_limit is not None and stream["lost_packets"] > loss_limit:
            failures.append(
                f"{stream['source']}->{stream['destination']} lost "
                f"{stream['lost_packets']} {media_type} packets"
            )
        cadence_ratio = stream.get("cadence_ratio")
        if cadence_ratio is not None and cadence_ratio > max_cadence_ratio:
            failures.append(
                f"{stream['source']}->{stream['destination']} cadence "
                f"{cadence_ratio:.3f}x negotiated clock"
            )
    return failures


def _sdp_audio_formats(
    tshark: str,
    path: Path,
) -> dict[tuple[str, int, int], tuple[int, str]]:
    result = subprocess.run(
        [
            tshark,
            "-r",
            str(path),
            "-Y",
            "sdp",
            "-T",
            "fields",
            "-e",
            "ip.src",
            "-e",
            "sdp.media",
            "-e",
            "sdp.media_attr",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    formats: dict[tuple[str, int, int], tuple[int, str]] = {}
    for row in result.stdout.splitlines():
        columns = row.split("\t")
        if len(columns) != 3:
            continue
        source, media, attributes = columns
        audio = re.search(r"(?:^|,)audio (\d+) [^,\s]+((?: \d+)+)", media)
        if not source or audio is None:
            continue
        port = int(audio.group(1))
        payloads = {int(item) for item in audio.group(2).split()}
        for payload, encoding, clock in re.findall(
            r"rtpmap:(\d+) ([^/,]+)/([0-9]+)(?:/\d+)?",
            attributes,
            flags=re.IGNORECASE,
        ):
            payload_type = int(payload)
            if payload_type in payloads:
                formats[(source, port, payload_type)] = (
                    int(clock),
                    encoding.lower(),
                )
        for payload in (0, 8, 9):
            if payload in payloads:
                formats.setdefault((source, port, payload), (8000, "static"))
    return formats


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pcap", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-streams", type=int, default=1)
    parser.add_argument(
        "--max-loss",
        type=int,
        default=0,
        help="maximum loss for negotiated audio streams",
    )
    parser.add_argument(
        "--max-video-loss",
        type=int,
        default=None,
        help="optional video loss limit; omit across direction-changing re-INVITEs",
    )
    parser.add_argument("--max-cadence-ratio", type=float, default=1.25)
    args = parser.parse_args()
    streams = analyze_pcap(args.pcap)
    failures = evaluate_streams(
        streams,
        require_streams=args.require_streams,
        max_audio_loss=args.max_loss,
        max_video_loss=args.max_video_loss,
        max_cadence_ratio=args.max_cadence_ratio,
    )
    payload = {
        "status": "failed" if failures else "passed",
        "pcap_sha256": _sha256(args.pcap),
        "streams": streams,
        "failures": failures,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
