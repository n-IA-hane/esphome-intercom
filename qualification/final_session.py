"""Prepare grouped HIL work and measure independent media, without G0 claims."""

from __future__ import annotations

from dataclasses import dataclass
from email.parser import BytesParser
import hashlib
from ipaddress import IPv4Address
import math
import os
from pathlib import Path
import re
import struct
import subprocess
from typing import Awaitable, Callable
import wave
from xml.etree import ElementTree

from .registry import SCENARIOS


FEATURE_ORACLES = {
    "register": ["REGISTER-final-and-Contact", "ha-and-qa-bindings", "ha-default-preserved"],
    "mwi": ["SUBSCRIBE-NOTIFY-account-identity", "message-summary-counts", "native-waiting-getter"],
    "dtmf": ["negotiated-event-clock-and-mask", "RFC4733-wire", "peer-digit-consumed-once"],
    "blf": ["dialog-info-subscription-version", "native-stable-contact-getter", "matching-capture-pixels"],
    "ldap": ["successful-query-generation", "atomic-native-roster-and-manual-rows", "stable-BLF-identity"],
    "screenshot": ["fresh-native-ID-and-status", "matching-HTTP-ETag", "fully-decoded-JPEG", "stable-native-scene"],
}


def final_session_plan(head: str, candidate_id: str) -> dict[str, object]:
    """A preparation receipt cannot be supplied as a successful HIL result."""
    ids = {scenario.id for scenario in SCENARIOS}
    contracts = ["esp-to-ha-answer-hangup", "esp-to-esp-watchdog-and-bidirectional-hangup",
                 "registered-sip-to-esp-bidirectional-hangup", "p4-full-landscape-jpeg-call-lifecycle"]
    if not head or not candidate_id or not set(contracts) <= ids:
        raise ValueError("Missing candidate or maintained scenario contract")
    return {
        "schema_version": 1, "status": "prepared", "head": head, "candidate_id": candidate_id,
        "hardware_actions": [], "physical": "not_run", "acoustic": "not_run",
        "required_firmware_features": ["assist", "mww", "audio", "display"],
        "preserve": {"ha_owner": "home", "default_line": "ha", "master_volume": 0},
        "routes": ["ha-to-esp", "esp-to-ha", "esp-to-esp", "qa-to-esp", "esp-to-qa"],
        "manual_generic_ha": {
            "account_creation": "deferred-user-manual-account-not-created-by-this-harness",
            "native_account_owner": "user", "native_line": "manual_ha",
            "backend_identity": "GenericSipAccount", "endpoint_kind": "sip_account",
            "provisioner": False, "ESPHome_integration_in_test_PBX": False,
            "routing_and_codecs": "standard-SIP-AoR-and-SDP-no-MAC-or-API-identity",
            "routes": ["ha-to-generic-esp", "generic-esp-to-ha"],
            "media": ["duplex-audio", "duplex-video"],
            "terminal_cases": ["caller-BYE", "callee-BYE", "immediate-redial"],
            "firmware_group": "same-final-FULL-candidate-no-extra-build",
        },
        "registered_asterisk_trunk": {
            "routes": ["ESP1-to-HA-to-registered-Asterisk-trunk-to-Asterisk-registered-ESP2",
                       "Asterisk-registered-ESP2-to-Asterisk-to-registered-HA-trunk-to-ESP1"],
            "audio": "mandatory-duplex-independent-PCM",
            "video": "verify-actual-SDP-and-codec-per-leg",
            "H264_from_Asterisk_to_S3": "HA-transcodes-to-JPEG",
            "H264_on_S3": "forbidden", "compatible_formats": "passthrough",
            "oracles": ["both-PBX-legs-and-Call-ID-correlation", "per-leg-SDP-and-actual-RTP-codec",
                        "decoded-directional-PCM", "decoded-video-and-S3-JPEG-pixels",
                        "both-terminal-owners", "PBX-zero-channels-and-full-quiescence", "immediate-redial"],
        },
        "scenario_contracts": contracts,
        "audio_contracts": {
            "ha-d1": {"ha_tx": [48000, 10], "esp_tx": [16000, 10], "d1_required": True},
            "asterisk-standard": {"tx_rate": 16000, "rx_rate": 16000, "packet_times_ms": [20, 10, 16]},
        },
        "feature_oracles": FEATURE_ORACLES,
        "asterisk_services": {
            "targets": ["P4-FULL", "S3-LCD-FULL"], "native_account_id": "qa",
            "extensions": {"P4": "7101", "S3": "7102", "peers": ["7103", "7104"], "trunk": "6002"},
            "compile_contracts": ["registration.message_waiting=true", "BLF-enabled",
                                  "QA-contact-metadata-account_id=qa"],
            "blf": ["SUBSCRIBE-dialog", "NOTIFY-full-and-versioned-deltas", "native-stable-contact-getter",
                    "actual-UI-ringing-in_call-idle", "hangup-and-reuse"],
            "mwi": ["record-HA-mailbox-baseline", "QA-voicemail-deposit", "QA-NOTIFY-waiting-yes-and-counts",
                    "get_message_waiting(qa)", "actual-UI-QA-mailbox-identity", "QA-menu-RFC4733-DTMF",
                    "decoded-playback-and-native-sink", "QA-delete", "QA-waiting-no", "full-quiescence"],
            "aggregate_waiting_alone_is_insufficient": True,
            "preserve_home_default_and_accounts": True, "PBX_actions": [],
            "old_P4_or_other_firmware_is_not_this_candidate": True,
        },
        "dashboard": {
            "environment": "real-Lovelace-LAB", "service": "home-assistant-voip-lab.service",
            "wrapper": "scripts/with_ha_lab_candidate.sh", "lab_url": "http://127.0.0.1:18123",
            "sip_port": 15060, "esphome_entries": "must-remain-absent",
            "actions": ["tile-call", "answer", "pickup", "hangup", "remount", "reload"],
            "oracles": ["served-JS-hashes", "actual-dashboard-monitor-and-shared-engine", "both-HA-legs",
                        "CDP-decoded-duplex-PCM", "independent-peer-PCM", "owned-Call-ID-cleanup", "media-EOF"],
            "snapshot": "passive-DOM-and-store-data-only-no-capability-discovery",
            "physical": False, "acoustic": False,
        },
        "terminal_cases": ["caller-BYE", "callee-BYE", "CANCEL", "decline", "setup-failure", "immediate-redial"],
        "screens": ["idle", "ringing", "active", "contacts", "post-hangup", "redial"],
        "required_inputs": ["candidate-lock", "source-lock", "matching-FULL-firmware-receipt",
            "live-MAC-build-and-service-inventory", "Home-owner-inventory", "private-existing-account-files",
            "zero-PBX-channels", "capture-writer-PID-and-capinfos", "call-scoped-PCAP-and-SDP",
            "decoded-directional-PCM", "fresh-screenshots", "cleanup-baseline-and-redial"],
        "gate": {"entry": "scripts/run_hil_qualification.py", "validator": "qualification/evidence.py",
                 "status": "blocked_until_real_candidate_bound_measurements",
                 "unsupported_scenario_is_failure": True},
        "forbidden": ["Lite-substitution", "ESP-in-lab-integration", "Home-deploy", "demo-roster-mutation",
                      "PBX-restart", "PSTN", "host-key-bypass", "IMAP"],
    }


@dataclass(frozen=True)
class AudioWireFormat:
    rate: int
    ptime_ms: int
    payload_type: int
    channels: int = 1


def _rtp_payload(packet: bytes) -> tuple[int, int, int, int, bytes]:
    if len(packet) < 12 or packet[0] >> 6 != 2:
        raise ValueError("Malformed RTP header")
    offset = 12 + (packet[0] & 15) * 4
    if offset > len(packet):
        raise ValueError("Truncated RTP CSRCs")
    if packet[0] & 16:
        if offset + 4 > len(packet):
            raise ValueError("Truncated RTP extension")
        offset += 4 + struct.unpack_from("!H", packet, offset + 2)[0] * 4
    end = len(packet)
    if packet[0] & 32:
        padding = packet[-1]
        if not padding or padding > end - offset:
            raise ValueError("Malformed RTP padding")
        end -= padding
    if offset >= end:
        raise ValueError("Empty or truncated RTP payload")
    sequence, timestamp, ssrc = struct.unpack_from("!HII", packet, 2)
    return packet[1] & 127, sequence, timestamp, ssrc, packet[offset:end]


def decode_l16_rtp(packets: list[bytes], fmt: AudioWireFormat, output: Path,
                   *, minimum_seconds: float = 20) -> dict[str, object]:
    """Independent RFC3551 big-endian PCM decoder, never acoustic proof.

    Format comes from this direction's real SDP, not a generic PT cache. Both
    directions must be decoded separately, including D1 asymmetric clocks.
    """
    if (fmt.rate not in {8000, 16000, 24000, 32000, 48000} or fmt.ptime_ms not in {10, 16, 20, 32}
            or fmt.channels not in {1, 2} or type(fmt.payload_type) is not int or not 0 <= fmt.payload_type <= 127
            or not math.isfinite(minimum_seconds) or minimum_seconds <= 0):
        raise ValueError("Unsupported measured L16 format")
    samples_per_packet = fmt.rate * fmt.ptime_ms // 1000
    expected_bytes = samples_per_packet * fmt.channels * 2
    decoded = bytearray()
    energy, sample_count = 0, 0
    previous, stream_ssrc = None, None
    if len(packets) < 2:
        raise ValueError("Missing measured audio window")
    for packet in packets:
        pt, seq, timestamp, ssrc, payload = _rtp_payload(packet)
        if pt != fmt.payload_type or len(payload) != expected_bytes:
            raise ValueError("RTP payload contradicts the negotiated PCM clock or packet time")
        if stream_ssrc is not None and ssrc != stream_ssrc:
            raise ValueError("Multiple SSRCs in one decoded direction")
        stream_ssrc = ssrc
        if previous is not None and ((seq - previous[0]) & 65535 != 1
                or (timestamp - previous[1]) & 0xFFFFFFFF != samples_per_packet):
            raise ValueError("RTP loss, duplicate, reordering or sampling-clock gap")
        previous = seq, timestamp
        samples = struct.unpack(f">{len(payload) // 2}h", payload)
        decoded.extend(struct.pack(f"<{len(samples)}h", *samples))
        energy += sum(sample * sample for sample in samples)
        sample_count += len(samples)
    duration = sample_count / fmt.channels / fmt.rate
    if duration < minimum_seconds or energy == 0:
        raise ValueError("Decoded PCM is silent or shorter than the observation window")
    output.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        with wave.open(stream, "wb") as wav:
            wav.setnchannels(fmt.channels)
            wav.setsampwidth(2)
            wav.setframerate(fmt.rate)
            wav.writeframes(decoded)
    return {"proof_layer": "software", "physical": False, "acoustic": False,
            "encoding": "L16", "rate": fmt.rate, "ptime_ms": fmt.ptime_ms,
            "channels": fmt.channels, "packets": len(packets), "ssrc": stream_ssrc,
            "samples_per_channel": sample_count // fmt.channels, "duration_seconds": duration,
            "rms": math.sqrt(energy / sample_count), "pcm_sha256": hashlib.sha256(decoded).hexdigest(),
            "wav_sha256": hashlib.sha256(output.read_bytes()).hexdigest()}


def decode_pcap_l16(pcap: Path, selection: dict, fmt: AudioWireFormat, output: Path,
                    *, sip_ports=(5060,), minimum_seconds=20):
    """Reuse maintained SDP/transport analysis, then decode exact wire payloads.

    The caller binds this capture to the run/Call-ID through evidence.py. This
    helper alone only reports software decoding of the selected PCAP bytes.
    """
    from tools.rtp_pcap_evidence import analyze_pcap
    checked = subprocess.run(["capinfos", str(pcap)], capture_output=True, check=True)
    streams = analyze_pcap(pcap, sip_ports=tuple(sip_ports))
    selected = [stream for stream in streams if all(stream.get(key) == selection.get(key)
                for key in ("source", "destination", "ssrc", "payload_type"))]
    if (len(selected) != 1 or selected[0].get("media_type") != "audio"
            or selected[0].get("clock_rate") != fmt.rate or selected[0].get("payload_type") != fmt.payload_type
            or selected[0].get("lost_packets") != 0 or selected[0].get("duplicate_packets") != 0):
        raise ValueError("PCAP has no unique lossless SDP-bound audio direction")
    source, sport = selection["source"].rsplit(":", 1)
    destination, dport = selection["destination"].rsplit(":", 1)
    source, destination = str(IPv4Address(source)), str(IPv4Address(destination))
    sport, dport, ssrc = int(sport), int(dport), int(selection["ssrc"], 0)
    if not 0 < sport < 65536 or not 0 < dport < 65536 or not 0 <= ssrc <= 0xFFFFFFFF:
        raise ValueError("Invalid selected RTP tuple")
    display_filter = (f"rtp && ip.src == {source} && ip.dst == {destination} && udp.srcport == {sport} "
                      f"&& udp.dstport == {dport} && rtp.ssrc == {ssrc} && rtp.p_type == {fmt.payload_type}")
    decoded = subprocess.run(["tshark", "-r", str(pcap), "-d", f"udp.port=={sport},rtp",
        "-Y", display_filter, "-T", "fields", "-e", "udp.payload"], capture_output=True, text=True, check=True)
    packets = [bytes.fromhex(line.replace(":", "")) for line in decoded.stdout.splitlines() if line]
    result = decode_l16_rtp(packets, fmt, output, minimum_seconds=minimum_seconds)
    return {**result, "pcap_sha256": hashlib.sha256(pcap.read_bytes()).hexdigest(),
            "capinfos_stdout_sha256": hashlib.sha256(checked.stdout).hexdigest(), "selection": dict(selection)}


def _idle(snapshot: object) -> bool:
    if not isinstance(snapshot, dict):
        return False
    counts = snapshot.get("resource_counts")
    return (snapshot.get("state") == "idle" and snapshot.get("active_dialogs") == 0
            and snapshot.get("call_scoped_quiescent") is True and isinstance(counts, dict) and bool(counts)
            and all(type(value) is int and value >= 0 and (key == "runtime_tasks" or value == 0)
                    for key, value in counts.items()))


def sip_record(raw: bytes) -> tuple[str, dict[str, str], bytes]:
    """Independent capture parser retaining no Authorization or credentials."""
    head, separator, body = raw.partition(b"\r\n\r\n")
    if not separator or len(raw) > 65536 or b"\r\n" not in head:
        raise ValueError("Incomplete bounded SIP capture")
    line, header_bytes = head.split(b"\r\n", 1)
    fields = BytesParser().parsebytes(header_bytes)
    names = ["Call-ID", "CSeq", "Content-Length", "Content-Type", "Event", "Subscription-State", "To", "Contact", "Expires", "Via"]
    for name in names:
        if name not in {"Via", "Contact"} and len(fields.get_all(name, [])) > 1:
            raise ValueError("Ambiguous SIP identity or content headers")
    headers = {name.casefold(): fields.get(name, "") for name in names}
    if not headers["call-id"] or not headers["cseq"] or int(headers["content-length"] or "0") != len(body):
        raise ValueError("SIP transaction identity or body length differs")
    return line.decode("ascii"), headers, body


def validate_register_pair(request: bytes, response: bytes, *, expected_aor: str, expected_contact: str,
                           expected_request_contact: str | None = None):
    request_line, sent, _ = sip_record(request)
    response_line, received, _ = sip_record(response)
    def uri(value):
        return value.partition("<")[2].partition(">")[0] if "<" in value else value.partition(";")[0].strip()
    def branch(value):
        return re.search(r"(?:^|;)branch=([^;\s]+)", value)
    sent_branch, received_branch = branch(sent["via"]), branch(received["via"])
    requested_contact = expected_contact if expected_request_contact is None else expected_request_contact
    if (not request_line.startswith("REGISTER ") or response_line.split()[:2] != ["SIP/2.0", "200"]
            or sent["call-id"] != received["call-id"] or sent["cseq"] != received["cseq"]
            or not sent["cseq"].endswith(" REGISTER") or uri(sent["to"]) != expected_aor
            or uri(received["to"]) != expected_aor or not sent_branch or not received_branch
            or sent_branch[1] != received_branch[1] or uri(sent["contact"]) != requested_contact
            or uri(received["contact"]) != expected_contact):
        raise ValueError("REGISTER final does not belong to the expected binding and transaction")
    expiry = re.search(r"(?:^|;)\s*expires=(\d+)", received["contact"], re.IGNORECASE)
    expires = int(expiry[1] if expiry else received["expires"] or "0")
    if expires <= 0:
        raise ValueError("REGISTER final has no live Contact lease")
    return {"call_id": sent["call-id"], "cseq": sent["cseq"], "expires": expires, "binding_verified": True}


def validate_notify(raw: bytes, *, call_id: str, event: str, entity_uri: str, previous_version=None):
    line, headers, body = sip_record(raw)
    if (not line.startswith("NOTIFY ") or headers["call-id"] != call_id
            or not headers["cseq"].endswith(" NOTIFY") or headers["event"].partition(";")[0].strip() != event
            or headers["subscription-state"].partition(";")[0] != "active"):
        raise ValueError("NOTIFY is outside the expected active subscription")
    if event == "message-summary":
        if headers["content-type"].partition(";")[0] != "application/simple-message-summary":
            raise ValueError("MWI body type differs")
        fields = BytesParser().parsebytes(body)
        waiting, account = fields.get("Messages-Waiting", "").casefold(), fields.get("Message-Account", "")
        counts = re.fullmatch(r"(\d+)/(\d+)(?:\s+\(\d+/\d+\))?", fields.get("Voice-Message", ""))
        if waiting not in {"yes", "no"} or account != entity_uri or counts is None:
            raise ValueError("MWI counts or account identity differ")
        return {"waiting": waiting == "yes", "new": int(counts[1]), "old": int(counts[2])}
    if event != "dialog" or headers["content-type"].partition(";")[0] != "application/dialog-info+xml":
        raise ValueError("Unsupported measured subscription body")
    if b"<!" in body:
        raise ValueError("External XML declarations are not allowed in captured dialog-info")
    root = ElementTree.fromstring(body)
    version, state = int(root.attrib["version"]), root.attrib["state"]
    if (root.tag != "{urn:ietf:params:xml:ns:dialog-info}dialog-info" or root.attrib.get("entity") != entity_uri
            or version < 0 or state not in {"full", "partial"}
            or (previous_version is None and state != "full")
            or (previous_version is not None and version != previous_version + 1)):
        raise ValueError("BLF entity, full snapshot or version continuity differs")
    states = [node.text for node in root.findall("{*}dialog/{*}state")]
    if any(value not in {"trying", "proceeding", "early", "confirmed", "terminated"} for value in states):
        raise ValueError("Unknown dialog state")
    return {"version": version, "state": state, "dialogs": states}


def decode_dtmf_events(packets: list[bytes], *, payload_type: int) -> str:
    """RFC4733 end repeats remain one measured digit, never an extra event."""
    events, seen_packets = {}, {}
    for packet in packets:
        pt, sequence, timestamp, ssrc, payload = _rtp_payload(packet)
        identity = ssrc, sequence
        if identity in seen_packets:
            if seen_packets[identity] != packet:
                raise ValueError("Conflicting RTP retransmission")
            continue
        seen_packets[identity] = packet
        if pt != payload_type or len(payload) != 4 or payload[0] > 15 or payload[1] & 64:
            raise ValueError("Invalid negotiated telephone-event packet")
        key = ssrc, timestamp, payload[0]
        duration = int.from_bytes(payload[2:], "big")
        previous, ended = events.get(key, (0, False))
        end = bool(payload[1] & 128)
        if duration <= 0 or duration < previous or (ended and (not end or duration != previous)):
            raise ValueError("Telephone-event duration or terminal repetition differs")
        events[key] = duration, end
    if not events or any(not ended for _, ended in events.values()):
        raise ValueError("Incomplete measured telephone event")
    return "".join("0123456789*#ABCD"[key[2]] for key in events)


async def run_owned_case(action: Callable[[], Awaitable[object]], cleanup: Callable[[], Awaitable[object]],
                         observe: Callable[[], Awaitable[dict]]) -> dict[str, object]:
    """Aggregate existing owner's cleanup; caller supplies only its owned call.

    No call, account, SIP or media lifecycle is created here. Exceptions remain
    failures even when cleanup succeeds, and cleanup errors preserve the cause.
    """
    result = {"status": "failed", "physical": False, "acoustic": False}
    before = await observe()
    if not _idle(before):
        result["error"] = "Precondition: existing resources are not idle"
        return result
    try:
        await action()
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        try:
            await cleanup()
        except Exception as error:
            result["cleanup_error"] = f"{type(error).__name__}: {error}"
    try:
        after = await observe()
        if not _idle(after) or before["resource_counts"] != after["resource_counts"]:
            result["cleanup_error"] = result.get("cleanup_error", "Call resources differ from the measured baseline")
        result["snapshots"] = {"pre": before, "post": after}
    except Exception as error:
        result["quiescence_error"] = f"{type(error).__name__}: {error}"
        result["cleanup_error"] = result.get("cleanup_error", result["quiescence_error"])
    if "error" not in result and "cleanup_error" not in result:
        result["status"] = "observed"
    return result
