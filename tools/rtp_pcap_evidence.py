#!/usr/bin/env python3
"""Produce fail-closed RTP continuity evidence from a qualification PCAP."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from ipaddress import IPv4Address
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


def _extended_sequence(sequence: int, highest: int | None) -> tuple[int, int]:
    if highest is None:
        return sequence, sequence
    candidate = (highest & ~0xFFFF) + sequence
    if candidate - highest > 0x8000:
        candidate -= 0x10000
    elif highest - candidate > 0x8000:
        candidate += 0x10000
    return candidate, max(highest, candidate)


def _sequence_id(stream: dict[str, Any]) -> str:
    return f"{stream['source']}->{stream['destination']}/{stream['ssrc']}"


def sequence_statistics(stream: dict[str, Any], streams: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve one SSRC ledger, never coerce unknown per-PT loss to zero."""
    scope = stream.get("loss_scope")
    if scope is None:
        lost, duplicate = stream.get("lost_packets"), stream.get("duplicate_packets", 0)
        if type(lost) is not int or lost < 0 or type(duplicate) is not int or duplicate < 0:
            raise ValueError("Missing measured RTP sequence loss")
        return {"lost_packets": lost, "duplicate_packets": duplicate}
    if scope not in {"single_payload", "shared_sequence_space"}:
        raise ValueError("Unknown RTP loss scope")
    ledger_id = stream.get("sequence_ledger_id")
    if ledger_id != _sequence_id(stream):
        raise ValueError("RTP sequence ledger identity differs from stream")
    owners = [item["sequence_ledger"] for item in streams if "sequence_ledger" in item
              and item["sequence_ledger"].get("id") == ledger_id]
    if len(owners) != 1:
        raise ValueError("Missing or repeated RTP sequence ledger")
    ledger = owners[0]
    if any(ledger.get(key) != stream.get(key) for key in ("source", "destination", "ssrc")):
        raise ValueError("RTP sequence ledger belongs to another tuple/SSRC")
    for key in ("lost_packets", "duplicate_packets", "reordered_packets", "packets"):
        if type(ledger.get(key)) is not int or ledger[key] < 0:
            raise ValueError("Invalid RTP sequence ledger count")
    members = [item for item in streams if item.get("sequence_ledger_id") == ledger_id]
    if any(_sequence_id(item) != ledger_id or type(item.get("packets")) is not int or item["packets"] <= 0
           or type(item.get("payload_type")) is not int or not 0 <= item["payload_type"] <= 127 for item in members):
        raise ValueError("Invalid RTP sequence ledger member")
    payloads = sorted({item["payload_type"] for item in members})
    if ledger.get("payload_types") != payloads or ledger["packets"] != sum(item["packets"] for item in members):
        raise ValueError("Incomplete RTP sequence ledger payload membership")
    if scope == "shared_sequence_space":
        if len(payloads) < 2 or any(item.get("loss_scope") != scope or item.get("lost_packets") is not None for item in members):
            raise ValueError("Shared RTP loss cannot be attributed to a payload type")
    elif len(payloads) != 1 or type(stream.get("lost_packets")) is not int or stream["lost_packets"] != ledger["lost_packets"]:
        raise ValueError("Single-payload RTP loss contradicts sequence ledger")
    return ledger


def analyze_rows(rows: Iterable[str]) -> list[dict[str, Any]]:
    streams: dict[tuple[str, ...], dict[str, Any]] = {}
    sequences: dict[tuple[str, ...], dict[str, Any]] = {}
    for raw in rows:
        columns = raw.rstrip("\n").split("\t")
        if len(columns) != len(FIELDS) or not all(columns):
            continue
        key = tuple(columns[1:7])
        sequence_key = tuple(columns[1:6])
        ledger = sequences.setdefault(sequence_key, {"highest": None, "seen": set(), "packets": 0,
                                                     "duplicates": 0, "reordered": 0, "payloads": set()})
        extended, highest = _extended_sequence(int(columns[7]), ledger["highest"])
        if extended in ledger["seen"]:
            ledger["duplicates"] += 1
        elif ledger["highest"] is not None and extended < ledger["highest"]:
            ledger["reordered"] += 1
        ledger["highest"] = highest
        ledger["seen"].add(extended)
        ledger["packets"] += 1
        ledger["payloads"].add(int(columns[6]))
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
        stream.setdefault("sequence_key", sequence_key)

    evidence: list[dict[str, Any]] = []
    emitted = set()
    for stream in sorted(streams.values(), key=lambda item: (item["source"], item["destination"], item["ssrc"], item["payload_type"])):
        ledger = sequences[stream.pop("sequence_key")]
        numbers = stream.pop("sequences")
        expected = max(ledger["seen"]) - min(ledger["seen"]) + 1
        lost = expected - len(ledger["seen"])
        shared = len(ledger["payloads"]) > 1
        ledger_id = _sequence_id(stream)
        stream["loss_scope"] = "shared_sequence_space" if shared else "single_payload"
        stream["sequence_ledger_id"] = ledger_id
        if ledger_id not in emitted:
            stream["sequence_ledger"] = {"id": ledger_id, **{key: stream[key] for key in ("source", "destination", "ssrc")},
                                          "payload_types": sorted(ledger["payloads"]), "packets": ledger["packets"],
                                          "lost_packets": lost, "duplicate_packets": ledger["duplicates"],
                                          "reordered_packets": ledger["reordered"]}
            emitted.add(ledger_id)
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
                "packets": len(numbers),
                "lost_packets": None if shared else lost,
                "duplicate_packets": len(numbers) - len(set(numbers)),
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
    return evidence


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
    mappings = _sdp_rtp_mappings(tshark, path, sip_ports=sip_ports)
    for stream in streams:
        receiver_host, receiver_port = stream["destination"].rsplit(":", 1)
        mapping = mappings.get(
            (receiver_host, int(receiver_port), stream["payload_type"])
        )
        if mapping is None or mapping[0] not in {"audio", "video"}:
            stream["media_type"] = "unknown"
            continue
        _, encoding, clock_rate, _ = mapping
        stream["clock_rate"] = clock_rate
        stream["sdp_binding"] = "receiver_connection_endpoint"
        if mapping[0] == "video":
            stream["media_type"] = "video"
            continue
        if encoding == "TELEPHONE-EVENT":
            # RFC4733: packets for one event retain its start timestamp.
            # Their cadence is not a voice sample clock. Digit/End semantics
            # require their own event oracle, not an audio-cadence verdict.
            stream["media_type"] = "telephone-event"
            continue
        stream["media_type"] = "audio"
        if stream["timestamp_step"] <= 0:
            raise ValueError("Audio RTP timestamp does not advance")
        expected_delta = 1000 * stream["timestamp_step"] / clock_rate
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
    media_streams = [stream for stream in streams if stream.get("media_type") in {"audio", "video"}]
    if len(media_streams) < require_streams:
        failures.append(f"expected at least {require_streams} RTP streams")
    evaluated = set()
    for stream in streams:
        media_type = stream.get("media_type", "unknown")
        if media_type == "unknown":
            failures.append(f"{stream['source']}->{stream['destination']} unknown SDP media mapping for PT {stream['payload_type']}")
        ledger_id = stream.get("sequence_ledger_id")
        identity = ledger_id if ledger_id is not None else id(stream)
        if identity in evaluated:
            continue
        evaluated.add(identity)
        try:
            statistics = sequence_statistics(stream, streams)
        except (ValueError, KeyError, TypeError) as error:
            failures.append(str(error))
            continue
        members = [item for item in streams if ledger_id is not None and item.get("sequence_ledger_id") == ledger_id] or [stream]
        types = {item.get("media_type", "unknown") for item in members}
        loss_limit = max_audio_loss if types - {"video"} else max_video_loss
        if loss_limit is not None and statistics["lost_packets"] > loss_limit:
            suffix = "RTP packets in shared sequence space (payload type unknown)" if len(members) > 1 else media_type+" packets"
            failures.append(
                f"{stream['source']}->{stream['destination']} lost "
                f"{statistics['lost_packets']} {suffix}"
            )
    for stream in media_streams:
        cadence_ratio = stream.get("cadence_ratio")
        if cadence_ratio is not None and cadence_ratio > max_cadence_ratio:
            failures.append(
                f"{stream['source']}->{stream['destination']} cadence "
                f"{cadence_ratio:.3f}x negotiated clock"
            )
    return failures


def _sdp_rtp_mappings(
    tshark: str,
    path: Path,
    *,
    sip_ports: tuple[int, ...] = (),
) -> dict[tuple[str, int, int], tuple[str, str, int, int]]:
    result = subprocess.run(
        [tshark, *_sip_decode_arguments(sip_ports), "-r", str(path),
         "-Y", "sdp", "-T", "pdml"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    mappings: dict[tuple[str, int, int], tuple[str, str, int, int]] = {}
    media_types: dict[tuple[str, int, int], str] = {}
    static = {"audio": {0: ("PCMU", 8000, 1), 8: ("PCMA", 8000, 1), 9: ("G722", 8000, 1)},
              "video": {26: ("JPEG", 90000, 1)}}

    def record(key, media, encoding, clock, channels):
        mapping = (media, encoding.upper(), clock, channels)
        if key in mappings and mappings[key] != mapping:
            raise ValueError(f"Ambiguous SDP RTP mapping for {key}")
        mappings[key] = mapping

    for packet in ElementTree.fromstring(result.stdout).findall("packet"):
        for description in packet.findall(".//proto[@name='sdp']"):
            session_address, current, sections, seen_media = None, None, [], False
            for field in description.findall("field"):
                name, value = field.get("name"), field.get("show", "")
                if name == "sdp.connection_info":
                    if seen_media and current is None:
                        continue  # A non-RTP media connection never becomes session-level.
                    parts = value.split()
                    if len(parts) != 3 or parts[:2] != ["IN", "IP4"]:
                        raise ValueError("Unsupported SDP connection address")
                    address = str(IPv4Address(parts[2]))
                    if not seen_media:
                        session_address = address
                    else:
                        current["address"] = address
                elif name == "sdp.media":
                    seen_media = True
                    current = None
                    parts = value.split()
                    if len(parts) < 4 or "RTP/" not in parts[2].upper():
                        continue
                    port = int(parts[1])
                    payloads = {int(item) for item in parts[3:]}
                    if not 0 <= port <= 65535 or any(not 0 <= pt <= 127 for pt in payloads):
                        raise ValueError("Invalid SDP RTP port or payload type")
                    media = parts[0].lower()
                    current = {"media": media, "port": port, "payloads": payloads,
                               "address": session_address, "formats": []}
                    sections.append(current)
                elif name == "sdp.media_attr" and current and value.startswith("rtpmap:"):
                    match = re.fullmatch(r"rtpmap:(\d+) ([^/\s]+)/(\d+)(?:/(\d+))?", value)
                    if match is None:
                        raise ValueError("Malformed SDP rtpmap attribute")
                    pt, encoding, clock, channels = match.groups()
                    pt, clock, channels = int(pt), int(clock), int(channels or 1)
                    if clock <= 0 or channels <= 0:
                        raise ValueError("Invalid SDP RTP clock or channels")
                    if pt in current["payloads"]:
                        current["formats"].append((pt, encoding, clock, channels))
            for section in sections:
                address, port, media = section["address"], section["port"], section["media"]
                if not address or address == "0.0.0.0" or port == 0:
                    continue
                for pt in section["payloads"]:
                    key = (address, port, pt)
                    if key in media_types and media_types[key] != media:
                        raise ValueError(f"Ambiguous SDP media for RTP mapping {key}")
                    media_types[key] = media
                    if pt in static.get(media, {}):
                        record(key, media, *static[media][pt])
                for pt, encoding, clock, channels in section["formats"]:
                    record((address, port, pt), media, encoding, clock, channels)
    return mappings


def _sdp_audio_clock_rates(
    tshark: str,
    path: Path,
    *,
    sip_ports: tuple[int, ...] = (),
) -> dict[tuple[str, int, int], int]:
    """Compatibility projection of the one authoritative SDP mapping parser."""
    return {key: mapping[2]
            for key, mapping in _sdp_rtp_mappings(tshark, path, sip_ports=sip_ports).items()
            if mapping[0] == "audio"}


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
