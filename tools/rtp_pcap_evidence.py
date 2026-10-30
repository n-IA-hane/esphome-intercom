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
import wave


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
    "rtp.payload",
)


def _extended_sequence(sequence: int, previous: int | None, cycle: int) -> tuple[int, int]:
    if previous is None:
        return sequence, sequence
    value = (cycle & ~0xFFFF) | sequence
    if value - cycle > 0x8000:
        value -= 0x10000
    elif cycle - value > 0x8000:
        value += 0x10000
    return value, max(cycle, value)


def analyze_rows(rows: Iterable[str], *, with_records: bool = False) -> list[dict[str, Any]]:
    streams: dict[tuple[str, ...], dict[str, Any]] = {}
    flow_sequences: dict[tuple[str, ...], set[int]] = {}
    flow_highest: dict[tuple[str, ...], int] = {}
    for raw in rows:
        columns = raw.rstrip("\n").split("\t")
        if len(columns) not in (len(FIELDS), len(FIELDS) - 1) or not all(columns[:9]):
            continue
        columns[5] = columns[5].lower()
        flow = tuple(columns[1:6])
        highest = flow_highest.get(flow)
        extended, flow_highest[flow] = _extended_sequence(int(columns[7]), highest, highest or 0)
        flow_sequences.setdefault(flow, set()).add(extended)
        key = tuple(columns[1:7])
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
                "payloads": [],
            },
        )
        stream["times"].append(float(columns[0]))
        stream["sequences"].append(extended)
        stream["timestamps"].append(int(columns[8]))
        stream["payloads"].append(columns[9] if len(columns) == 10 else "")

    evidence: list[dict[str, Any]] = []
    for stream in streams.values():
        extended = stream.pop("sequences")
        unique = set(extended)
        first, last = min(unique), max(unique)
        expected = last - first + 1
        flow = tuple(stream["source"].rsplit(":", 1) + stream["destination"].rsplit(":", 1) + [stream["ssrc"]])
        received = flow_sequences[flow]
        times = stream.pop("times")
        timestamps = stream.pop("timestamps")
        payloads = stream.pop("payloads")
        timestamp_steps = [
            (current - prior) & 0xFFFFFFFF
            for prior, current in zip(timestamps, timestamps[1:], strict=False)
            if current != prior
        ]
        deltas = [
            (current - prior) * 1000
            for prior, current in zip(times, times[1:], strict=False)
        ]
        item = {
                **stream,
                "packets": len(extended),
                # Sequence numbers belong to the whole SSRC, including events.
                "lost_packets": expected - sum(first <= item <= last for item in received),
                "duplicate_packets": len(extended) - len(unique),
                "first_packet_seconds": min(times),
                "last_packet_seconds": max(times),
                "duration_seconds": round(times[-1] - times[0], 6),
                "mean_delta_ms": round(sum(deltas) / len(deltas), 6)
                if deltas
                else 0.0,
                "max_delta_ms": round(max(deltas), 6) if deltas else 0.0,
                "timestamp_step": Counter(timestamp_steps).most_common(1)[0][0]
                if timestamp_steps
                else 0,
            }
        if with_records:
            item["_records"] = list(zip(extended, timestamps, times, payloads, strict=True))
        evidence.append(item)
    return sorted(evidence, key=lambda item: (item["source"], item["ssrc"]))


def analyze_pcap(path: Path, *, pcm_dir: Path | None = None) -> list[dict[str, Any]]:
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
    streams = analyze_rows(result.stdout.splitlines(), with_records=True)
    formats = _sdp_media_formats(tshark, path)
    audio_flows: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for stream in streams:
        source_host, source_port = stream["source"].rsplit(":", 1)
        destination_host, destination_port = stream["destination"].rsplit(":", 1)
        # An SDP address describes the receiver. The source lookup preserves
        # symmetric RTP captures whose opposite description is unavailable.
        spec = formats.get((destination_host, int(destination_port), stream["payload_type"]))
        if spec is None:
            if any(key[:2] == (destination_host, int(destination_port)) for key in formats):
                stream["media_type"] = "unknown"
                continue
            spec = formats.get((source_host, int(source_port), stream["payload_type"]))
        if spec is None:
            stream["media_type"] = "unknown"
            continue
        if spec["media_type"] == "video":
            stream["media_type"] = "video"
            continue
        if spec["encoding"] == "TELEPHONE-EVENT":
            stream["media_type"] = "telephone-event"
            continue
        stream["media_type"] = "audio"
        stream["clock_rate"] = spec["clock"]
        stream["encoding"] = spec["encoding"]
        stream["_format"] = spec
        audio_flows.setdefault((stream["source"], stream["destination"], stream["ssrc"]), []).append(stream)
    for group in audio_flows.values():
        _audio_continuity(group, pcm_dir)
    for stream in streams:
        stream.pop("_records", None)
        stream.pop("_format", None)
    return streams


def _audio_continuity(group: list[dict[str, Any]], pcm_dir: Path | None) -> None:
    owner = group[0]
    for follower in group[1:]:
        follower["audio_timeline_follower"] = True
    owner["audio_missing_packets"] = None
    records = sorted(((sequence, timestamp, time, payload, stream["_format"])
                      for stream in group for sequence, timestamp, time, payload in stream["_records"]),
                     key=lambda item: item[0])
    clocks = {record[4]["clock"] for record in records}
    limit = ""
    if len(clocks) != 1:
        limit = "audio clock changed inside the same SSRC"
    elif any(record[4]["clock"] <= 0 or record[4]["channels"] <= 0 for record in records):
        limit = "invalid SDP audio clock or channel count"
    elif any(record[4].get("ambiguous") for record in records):
        limit = "conflicting SDP payload mappings"
    codecs = {"L16": (2, "s16be"), "PCM": (2, "s16le"), "L24": (3, "s24be"),
              "PCMA": (1, "alaw"), "PCMU": (1, "mulaw")}
    if any(record[4]["encoding"] not in codecs for record in records):
        limit = "sample count unavailable for the negotiated codec"
    if any(not record[3] for record in records):
        limit = limit or "RTP payload unavailable"
    # Keep cadence evidence even when an encoded codec prevents exact PCM proof.
    ptime = owner["_format"]["ptime"]
    if ptime:
        owner["expected_delta_ms"] = ptime
        owner["cadence_ratio"] = round(owner["mean_delta_ms"] / ptime, 6)
    if limit:
        owner["audio_continuity_limit"] = limit
        return
    seen: dict[int, tuple[int, bytes]] = {}
    ordered = []
    missing_samples = missing_packets = total_samples = 0
    previous_end = previous_samples = None
    for sequence, timestamp, time, payload_hex, spec in records:
        try:
            payload = bytes.fromhex(payload_hex.replace(":", ""))
        except ValueError:
            owner["audio_continuity_limit"] = "malformed RTP payload"
            return
        if sequence in seen:
            if seen[sequence] != (timestamp, payload):
                owner["audio_continuity_limit"] = "conflicting duplicate RTP packet"
                return
            continue
        seen[sequence] = timestamp, payload
        width = codecs[spec["encoding"]][0] * spec["channels"]
        if not width or len(payload) % width:
            owner["audio_continuity_limit"] = "unaligned PCM payload"
            return
        samples = len(payload) // width
        gap = 0 if previous_end is None else (timestamp - previous_end) & 0xFFFFFFFF
        if gap >= 0x80000000:
            owner["audio_continuity_limit"] = "overlapping or backward audio timestamps"
            return
        missing_samples += gap
        if gap:
            missing_packets += (gap + previous_samples - 1) // previous_samples
        total_samples += samples
        ordered.append((payload, spec, gap))
        previous_end, previous_samples = (timestamp + samples) & 0xFFFFFFFF, samples
    owner["audio_missing_samples"] = missing_samples
    owner["audio_missing_packets"] = missing_packets
    owner["audio_samples"] = total_samples
    owner["audio_continuity_limit"] = ""
    clock = records[0][4]["clock"]
    expected = 1000 * total_samples / len(ordered) / clock
    owner["expected_delta_ms"] = round(expected, 6)
    span = max(record[2] for record in records) - min(record[2] for record in records)
    mean = 1000 * span / (len(ordered) - 1) if len(ordered) > 1 else 0
    owner["cadence_ratio"] = round(mean / expected, 6) if expected else 0
    if pcm_dir is not None:
        _write_pcm(owner, ordered, pcm_dir, codecs)


def _write_pcm(owner, ordered, directory: Path, codecs) -> None:
    """Decode sorted codec payloads with FFmpeg; retain timestamp holes as zeros."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required with --pcm-dir")
    identities = {(spec["encoding"], spec["clock"], spec["channels"]) for _, spec, _ in ordered}
    if len(identities) != 1:
        owner["pcm_export_limit"] = "codec or channel change requires separate WAV segments"
        return
    encoding, rate, channels = identities.pop()
    result = subprocess.run([ffmpeg, "-v", "error", "-f", codecs[encoding][1], "-ar", str(rate),
                             "-ac", str(channels), "-i", "pipe:0", "-f", "s16le", "-c:a", "pcm_s16le", "pipe:1"],
                            input=b"".join(payload for payload, _, _ in ordered),
                            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    decoded = memoryview(result.stdout)
    name = re.sub(r"[^a-zA-Z0-9_.-]", "_", owner["source"] + "-to-" + owner["destination"] + "-" + owner["ssrc"])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / (name + ".wav")
    offset = 0
    with wave.open(str(path), "wb") as output:
        output.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        for payload, spec, gap in ordered:
            samples = len(payload) // (codecs[encoding][0] * channels)
            remaining = gap * channels * 2
            while remaining:
                size = min(remaining, 65536)
                output.writeframesraw(bytes(size))
                remaining -= size
            size = samples * channels * 2
            output.writeframesraw(decoded[offset:offset + size])
            offset += size
    if offset != len(decoded):
        raise RuntimeError("FFmpeg decoded sample count differs from RTP PCM")
    owner["pcm_wav"] = str(path)
    path.chmod(0o600)
    owner["pcm_wav_sha256"] = _sha256(path)


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
    media_flows = {(s["source"], s["destination"], s.get("ssrc")) for s in streams
                   if s.get("media_type") not in {"telephone-event", "unknown"}}
    if len(media_flows) < require_streams:
        failures.append(f"expected at least {require_streams} RTP streams")
    for stream in streams:
        media_type = stream.get("media_type", "video")
        if media_type == "unknown":
            failures.append(f"{stream['source']}->{stream['destination']} RTP payload absent from receiver SDP")
            continue
        if media_type == "telephone-event" or stream.get("audio_timeline_follower"):
            continue
        if media_type == "audio" and "audio_missing_packets" in stream:
            if stream["audio_missing_packets"] is None:
                failures.append(f"{stream['source']}->{stream['destination']} audio continuity unqualified: "
                                + stream["audio_continuity_limit"])
            elif stream["audio_missing_packets"] > max_audio_loss:
                failures.append(f"{stream['source']}->{stream['destination']} missing "
                                f"{stream['audio_missing_samples']} audio samples")
            loss_limit = None
        else:
            loss_limit = max_audio_loss if media_type == "audio" else max_video_loss
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


def _sdp_audio_clock_rates(
    tshark: str,
    path: Path,
) -> dict[tuple[str, int, int], int]:
    return {key: value["clock"] for key, value in _sdp_audio_formats(tshark, path).items()}


def _sdp_audio_formats(tshark: str, path: Path) -> dict[tuple[str, int, int], dict[str, Any]]:
    return {key: value for key, value in _sdp_media_formats(tshark, path).items()
            if value["media_type"] == "audio"}


def _sdp_media_formats(tshark: str, path: Path) -> dict[tuple[str, int, int], dict[str, Any]]:
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
            "-e",
            "sdp.connection_info.address",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    formats: dict[tuple[str, int, int], dict[str, Any]] = {}
    for row in result.stdout.splitlines():
        columns = row.split("\t")
        if len(columns) not in (3, 4):
            continue
        source, media, attributes = columns[:3]
        if len(columns) == 4 and columns[3]:
            source = columns[3].split(",")[0].split("/")[0]
        if not source:
            continue
        ptime = re.search(r"(?:^|,)ptime:(\d+)", attributes)
        declared = re.findall(
            r"rtpmap:(\d+) ([^/,]+)/(\d+)(?:/(\d+))?",
            attributes,
            flags=re.IGNORECASE,
        )
        for description in re.finditer(r"(?:^|,)(audio|video) (\d+) [^,\s]+((?: \d+)+)", media):
            kind, port = description[1], int(description[2])
            payloads = {int(item) for item in description[3].split()}
            mappings = [item for item in declared if int(item[0]) in payloads]
            for payload in payloads:
                if not any(int(item[0]) == payload for item in mappings):
                    if kind == "audio" and payload in (0, 8, 9):
                        mappings.append((str(payload), {0: "PCMU", 8: "PCMA", 9: "G722"}[payload], "8000", "1"))
                    elif kind == "video":
                        mappings.append((str(payload), "VIDEO", "90000", "1"))
            for payload, encoding, clock, channels in mappings:
                payload_type = int(payload)
                key = source, port, payload_type
                value = {"encoding": encoding.upper(), "clock": int(clock),
                         "channels": int(channels or 1), "ptime": int(ptime[1]) if ptime else 0,
                         "media_type": kind}
                old = formats.get(key)
                if old is not None and (old.get("ambiguous") or
                                        any(old[k] != value[k] for k in ("encoding", "clock", "channels"))):
                    value["ambiguous"] = True
                formats[key] = value
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
    parser.add_argument("--pcm-dir", type=Path, help="optional decoded WAV output, preserving timestamp gaps")
    args = parser.parse_args()
    streams = analyze_pcap(args.pcap, pcm_dir=args.pcm_dir)
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
