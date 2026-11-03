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
from xml.etree import ElementTree


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
    for raw in rows:
        columns = raw.rstrip("\n").split("\t")
        if len(columns) != len(FIELDS) or not all(columns):
            continue
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
            },
        )
        stream["times"].append(float(columns[0]))
        stream["sequences"].append(int(columns[7]))
        stream["timestamps"].append(int(columns[8]))

    evidence: list[dict[str, Any]] = []
    for stream in streams.values():
        extended: list[int] = []
        previous: int | None = None
        cycle = 0
        for sequence in stream.pop("sequences"):
            value, cycle = _extended_sequence(sequence, previous, cycle)
            extended.append(value)
            previous = sequence
        unique = set(extended)
        expected = max(unique) - min(unique) + 1
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
                "lost_packets": expected - len(unique),
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


def _sip_decode_arguments(ports: tuple[int, ...]) -> list[str]:
    arguments = []
    for port in sorted(set(ports)):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("SIP decoding ports must be integers from 1 to 65535")
        for transport in ("udp", "tcp"):
            arguments.extend(("-d", f"{transport}.port=={port},sip"))
    return arguments


def analyze_pcap(path: Path, *, sip_ports: tuple[int, ...] = ()) -> list[dict[str, Any]]:
    tshark = shutil.which("tshark")
    if not tshark:
        raise RuntimeError("tshark is required for RTP PCAP evidence")
    command = [tshark, *_sip_decode_arguments(sip_ports), "-r", str(path), "-Y", "rtp", "-T", "fields"]
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
    clock_rates = _sdp_audio_clock_rates(tshark, path, sip_ports=sip_ports)
    for stream in streams:
        source_host, source_port = stream["source"].rsplit(":", 1)
        clock_rate = clock_rates.get(
            (source_host, int(source_port), stream["payload_type"])
        )
        if clock_rate is None:
            stream["media_type"] = "video"
            continue
        stream["media_type"] = "audio"
        expected_delta = 1000 * stream["timestamp_step"] / clock_rate
        stream["clock_rate"] = clock_rate
        stream["expected_delta_ms"] = round(expected_delta, 6)
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
    *,
    sip_ports: tuple[int, ...] = (),
) -> dict[tuple[str, int, int], int]:
    result = subprocess.run(
        [tshark, *_sip_decode_arguments(sip_ports), "-r", str(path),
         "-Y", "sdp", "-T", "pdml"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    rates: dict[tuple[str, int, int], int] = {}
    mappings: dict[tuple[str, int, int], tuple[str, str, int, int]] = {}
    media_types: dict[tuple[str, int, int], str] = {}
    static = {0: ("PCMU", 8000, 1), 8: ("PCMA", 8000, 1), 9: ("G722", 8000, 1)}

    def record(key, media, encoding, clock, channels):
        mapping = (media, encoding.upper(), clock, channels)
        if key in mappings and mappings[key] != mapping:
            raise ValueError(f"Ambiguous SDP RTP mapping for {key}")
        mappings[key] = mapping
        if media == "audio":
            rates[key] = clock

    for packet in ElementTree.fromstring(result.stdout).findall("packet"):
        source_field = packet.find(".//field[@name='ip.src']")
        source = source_field.get("show", "") if source_field is not None else ""
        if not source:
            continue
        for description in packet.findall(".//proto[@name='sdp']"):
            current = None
            for field in description.findall("field"):
                name, value = field.get("name"), field.get("show", "")
                if name == "sdp.media":
                    current = None
                    parts = value.split()
                    if len(parts) < 4 or "RTP/" not in parts[2].upper():
                        continue
                    port = int(parts[1])
                    payloads = {int(item) for item in parts[3:]}
                    if not 0 <= port <= 65535 or any(not 0 <= pt <= 127 for pt in payloads):
                        raise ValueError("Invalid SDP RTP port or payload type")
                    if port == 0:
                        continue
                    media = parts[0].lower()
                    current = (media, port, payloads)
                    for pt in payloads:
                        key = (source, port, pt)
                        if key in media_types and media_types[key] != media:
                            raise ValueError(f"Ambiguous SDP media for RTP mapping {key}")
                        media_types[key] = media
                        if media == "audio" and pt in static:
                            record(key, media, *static[pt])
                elif name == "sdp.media_attr" and current and value.startswith("rtpmap:"):
                    match = re.fullmatch(r"rtpmap:(\d+) ([^/\s]+)/(\d+)(?:/(\d+))?", value)
                    if match is None:
                        raise ValueError("Malformed SDP rtpmap attribute")
                    pt, encoding, clock, channels = match.groups()
                    pt, clock, channels = int(pt), int(clock), int(channels or 1)
                    if clock <= 0 or channels <= 0:
                        raise ValueError("Invalid SDP RTP clock or channels")
                    media, port, payloads = current
                    if pt in payloads:
                        record((source, port, pt), media, encoding, clock, channels)
    return rates


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
    parser.add_argument("--sip-port", type=int, action="append", default=[],
                        help="decode SIP on this TCP/UDP port (repeat for additional lab peers)")
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
    try:
        streams = analyze_pcap(args.pcap, sip_ports=tuple(args.sip_port))
    except ValueError as error:
        parser.error(str(error))
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
        "sip_decode_ports": sorted(set(args.sip_port)),
        "streams": streams,
        "failures": failures,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
