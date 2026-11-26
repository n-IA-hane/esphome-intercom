"""Synthetic protocol fixtures exercise the real gate, never physical claims."""
from __future__ import annotations

import hashlib
import json
import shutil
import socket
import struct
import subprocess
from types import SimpleNamespace

import pytest

from qualification.evidence import EvidenceError, validate_hil_measurement
from tools import hil_measurement as hil


pytestmark = pytest.mark.skipif(not shutil.which("tshark") or not shutil.which("capinfos"),
                                reason="Actual PCAP reader tools are required")
BASE = 1_700_000_000
SCENARIO = "registered-sip-to-esp-bidirectional-hangup"
FIRMWARE = {"firmware_sha256": "a" * 64, "firmware_profile": "actual-test-s3-full",
            "source_lock_sha256": "b" * 64}


def _capture(path, *, offset, call_id, caller, callee, terminal, video=False, pbx=None, reinvite=False):
    frames = []
    def packet(at, source, destination, sport, dport, payload):
        udp = struct.pack("!HHHH", sport, dport, len(payload) + 8, 0) + payload
        ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, len(udp) + 20, 0, 0, 64, 17, 0,
                         socket.inet_aton(source), socket.inet_aton(destination))
        frames.append((BASE + offset + at, bytes(12) + b"\x08\x00" + ip + udp))
    def sdp(host, port, include_video=False):
        text = f"v=0\r\no=- 1 1 IN IP4 {host}\r\ns=test\r\nc=IN IP4 {host}\r\nt=0 0\r\nm=audio {port} RTP/AVP 96\r\na=rtpmap:96 L16/16000\r\na=ptime:10\r\n"
        if include_video:
            pt, codec = (102, "H264") if reinvite else (26, "JPEG")
            text += f"m=video {port+100} RTP/AVP {pt}\r\na=rtpmap:{pt} {codec}/90000\r\na=sendrecv\r\n"
        return text
    uri = f"sip:callee@{callee}"
    def sip(start, cseq, body="", leg_id=None):
        return (f"{start}\r\nVia: SIP/2.0/UDP {caller}:5060;branch=z9hG4bK-test\r\n"
                f"From: <sip:caller@{caller}>;tag=one\r\nTo: <sip:callee@{callee}>;tag=two\r\n"
                f"Call-ID: {leg_id or call_id}\r\nCSeq: {cseq}\r\nContent-Type: application/sdp\r\nContent-Length: {len(body)}\r\n\r\n{body}").encode()
    legs = [(caller, callee, 40000, 40002, call_id, uri)] if pbx is None else [
        (caller, pbx, 40000, 40002, call_id, f"sip:callee@{pbx}"),
        (pbx, callee, 40004, 40006, call_id + "-esp", uri)]
    for source, destination, sport, dport, leg_id, leg_uri in legs:
        packet(.1, source, destination, 5060, 5060, sip(f"INVITE {leg_uri} SIP/2.0", "1 INVITE", sdp(source, sport, video and not reinvite), leg_id))
        packet(.2, destination, source, 5060, 5060, sip("SIP/2.0 200 OK", "1 INVITE", sdp(destination, dport, video and not reinvite), leg_id))
        packet(.3, source, destination, 5060, 5060, sip(f"ACK {leg_uri} SIP/2.0", "1 ACK", leg_id=leg_id))
        if reinvite:
            packet(1.5, source, destination, 5060, 5060, sip(f"INVITE {leg_uri} SIP/2.0", "2 INVITE", sdp(source, sport, True), leg_id))
            packet(1.6, destination, source, 5060, 5060, sip("SIP/2.0 200 OK", "2 INVITE", sdp(destination, dport, True), leg_id))
            packet(1.7, source, destination, 5060, 5060, sip(f"ACK {leg_uri} SIP/2.0", "2 ACK", leg_id=leg_id))
        terminal_is_caller = terminal == caller
        end_source, end_destination = (source, destination) if terminal_is_caller else (destination, source)
        packet(21.2, end_source, end_destination, 5060, 5060, sip(f"BYE {leg_uri} SIP/2.0", "2 BYE", leg_id=leg_id))
        packet(21.3, end_destination, end_source, 5060, 5060, sip("SIP/2.0 200 OK", "2 BYE", leg_id=leg_id))
    directions = [(src, dst, sp, dp) for src, dst, sp, dp, _, _ in legs]
    directions += [(dst, src, dp, sp) for src, dst, sp, dp in directions]
    for direction, (source, destination, sport, dport) in enumerate(directions):
        for sequence in range(2001):
            raw = struct.pack("!BBHII", 0x80, 96, sequence, sequence * 160, 100 + direction) + bytes(320)
            packet(1 + sequence / 100, source, destination, sport, dport, raw)
        if video:
            from io import BytesIO
            from PIL import Image
            from tools.sip_video_peer import rtp, video_rtp
            buffer = BytesIO()
            Image.new("RGB", (64, 64), "red").save(buffer, format="JPEG", subsampling=2)
            encoded = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=size=64x64:rate=10", "-frames:v", "1",
                "-c:v", "libx264", "-x264-params", "keyint=1:repeat-headers=1", "-f", "h264", "pipe:1"],
                check=True, capture_output=True).stdout if reinvite else buffer.getvalue()
            next_sequence = 0
            for frame in range(4):
                packetize = video_rtp.packetize_annex_b if reinvite else video_rtp.packetize_jpeg
                for raw in packetize(encoded, payload_type=102 if reinvite else 26, sequence=next_sequence,
                        timestamp=frame * 9000, ssrc=200 + direction):
                    packet(2 + frame / 10, source, destination, sport + 100, dport + 100, rtp.build_packet(raw))
                    next_sequence = (raw.sequence + 1) & 65535
    raw = bytearray(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
    for epoch, frame in sorted(frames, key=lambda row: row[0]):
        seconds, micros = int(epoch), round((epoch % 1) * 1_000_000)
        raw += struct.pack("<IIII", seconds, micros, len(frame), len(frame)) + frame
    path.write_bytes(raw)
    writer = {"status": "capture_closed_capinfos_accepted", "capture_file_valid": True,
              "pcap_sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
              "capture_quality": "zero_kernel_drops_reported", "remote_pid_reported": 123,
              "argv": ["tcpdump", "-w", "-"], "ssh_exit_code": 0,
              "tcpdump_counts": {"dropped_by_kernel": 0},
              "verified_remote_identity": {"pid": 123, "starttime": "12345", "cmdline": ["tcpdump", "-w", "-"], "stdout": "pipe:[123]"}}
    writer_path = path.with_suffix(".pcap.writer.json")
    writer_path.write_text(json.dumps(writer))
    return {leg_id: leg_uri for _, _, _, _, leg_id, leg_uri in legs}, writer_path


def _produce(tmp_path, monkeypatch, scenario=SCENARIO):
    clock = SimpleNamespace(now=99.5)
    monkeypatch.setattr(hil, "time", SimpleNamespace(monotonic=lambda: clock.now, time=lambda: BASE + clock.now - 100))
    env = {"HIL_EVIDENCE_ROOT": str(tmp_path), "HIL_MEASUREMENT_RECEIPT": str(tmp_path / "measurement.json"),
           "HIL_PLAN_ID": "plan", "HIL_HEAD": "head", "HIL_CANDIDATE_ID": "candidate", "HIL_SCENARIO_ID": scenario,
           "HIL_RUN_ID": "run", "HIL_ACTOR_STARTED_MONOTONIC": "99", "HIL_ACTOR_STARTED_UNIX": str(BASE - 1),
           **{"HIL_" + key.upper(): value for key, value in FIRMWARE.items()}}
    m = hil.HilMeasurement.from_environment(env)
    esp_esp = scenario.startswith("esp-to-esp-")
    video = scenario.startswith("p4-")
    reinvite = scenario == "p4-audio-to-bidirectional-video-reinvite"
    peers = {"peer": {"host": "192.0.2.1", "device_id": "real-peer", "destination": "sip:peer@192.0.2.1"},
             "esp": {"host": "192.0.2.2", "device_id": "real-esp", "destination": "sip:callee@192.0.2.2"}}
    peers["peer"]["registration"] = {"mode": "registered", "aor": "sip:peer@example.test", "contact": "sip:peer@192.0.2.1", "expires_unix": BASE + 1000}
    profiles = {**FIRMWARE, "firmware_profile": "waveshare-p4-h264" if reinvite else "waveshare-p4-full-landscape"} if video else FIRMWARE
    m.expected_firmware = profiles
    for name in (peers if esp_esp else ["esp"]):
        m.identity(name, {"mac": "aa:bb:cc:dd:ee:" + ("01" if name == "peer" else "02"), "build": "current build"},
            host=peers[name]["host"], profile=profiles["firmware_profile"], firmware_sha256=profiles["firmware_sha256"],
            source_lock_sha256=profiles["source_lock_sha256"])
    signaling = ["192.0.2.3"] if scenario == SCENARIO else []
    m.directory("pre", {"endpoints": peers, "signaling_hosts": signaling})
    cases = [("peer", "esp", "peer"), ("peer", "esp", "esp")]
    if esp_esp:
        cases += [("esp", "peer", "esp"), ("esp", "peer", "peer")]
    idle = {"state": "idle", "active_dialogs": 0, "call_scoped_quiescent": True, "resource_counts": {"rtp_ports": 0, "runtime_tasks": 1}}
    for number, (caller, callee, terminal) in enumerate(cases):
        clock.now = start = 100 + 23 * number
        call = m.start_session(caller=caller, callee=callee, terminal=terminal,
                               route_kind="ha-directory" if esp_esp or video else "registered")
        call.runtime("pre", idle)
        call_id = f"call-{number}"
        pcap = tmp_path / f"call-{number}.pcap"
        dialogs, writer = _capture(pcap, offset=23 * number, call_id=call_id, caller=peers[caller]["host"],
                               callee=peers[callee]["host"], terminal=peers[terminal]["host"], video=video,
                               pbx=signaling[0] if signaling else None, reinvite=reinvite)
        call.bind_dialogs(dialogs)
        clock.now = start + .5
        for device in ({caller, callee} if esp_esp else {"esp"}):
            call.native_log(device, f"Incoming call (call_id={call_id})")
            call.native_state(device, "in_call", call_id)
        call.connected()
        clock.now = start + 22
        call.ended()
        for device in ({caller, callee} if esp_esp else {"esp"}):
            call.native_state(device, "idle", call_id)
        if video:
            for line in ("JPEG RX stats: admitted=4 rendered=4 presented=4 refresh_done=4", "Video session totals: tx=4 rx=4 completed_au=4 send_fail=0"):
                call.native_log("esp", line)
        call.runtime("post", idle)
        clock.now = start + 22.5
        call.quiescent()
        call.finish(pcap=pcap, writer_receipt=writer)
    clock.now += .1
    m.directory("post", {"endpoints": peers, "signaling_hosts": signaling})
    clock.now += .1
    receipt = m.finish()
    result = {"scenario": scenario, "run_id": "run", "started_monotonic": 99, "finished_monotonic": clock.now + 1,
              "started_unix": BASE - 1, "finished_unix": BASE + clock.now - 99, "firmware_binding": profiles,
              "snapshots": {"pre": idle, "post": idle}, "measurement_receipt": hil.reference(m.output, tmp_path)}
    return receipt, result


@pytest.mark.parametrize("scenario", [SCENARIO, "esp-to-esp-watchdog-and-bidirectional-hangup", "p4-full-landscape-jpeg-call-lifecycle", "p4-audio-to-bidirectional-video-reinvite"])
def test_measured_producer_and_gate_rederive_actual_pcap(tmp_path, monkeypatch, scenario):
    receipt, result = _produce(tmp_path, monkeypatch, scenario)
    validate_hil_measurement(result, plan={"plan_id": "plan", "head": "head"}, candidate_id="candidate", root=tmp_path, require_redial=True)
    assert receipt["proof_limits"] == {"acoustic": False, "physical_panel": False}


def test_benign_dma_i2s_watchdog_status_is_not_a_native_failure():
    assert hil.native_failures("DMA budget internal=65536 I2S running watchdog enabled timeout=15000 panic handler configured") == []


@pytest.mark.parametrize("line", ["TX DMA completion/refill contract failed", "I2S error detected in audio task",
    "Task watchdog got triggered", "Guru Meditation Error: Core 0 panic'ed", "abort() was called at PC 0x1",
    "rst:0xc (SW_CPU_RESET)"])
def test_native_failure_classifier_retains_actual_fault_witnesses(line):
    assert hil.native_failures(line)


@pytest.mark.parametrize("fault", ["stale_capture", "old_capture_bytes", "wrong_terminal", "wrong_uri", "reused_call", "bad_lease", "missing_identity", "bad_cleanup", "missing_native", "edited_report", "capture_drops", "old_observations"])
def test_new_scenario_gate_rejects_missing_stale_misroute_and_tampered_inputs(tmp_path, monkeypatch, fault):
    receipt, result = _produce(tmp_path, monkeypatch)
    session = receipt["sessions"][0]
    if fault == "stale_capture":
        result["started_unix"] += 3
    elif fault == "old_capture_bytes":
        pcap = tmp_path / session["capture"]["path"]
        _, writer = _capture(pcap, offset=-1000, call_id=session["sip_call_ids"][0],
            caller="192.0.2.1", callee="192.0.2.2", terminal="192.0.2.1", pbx="192.0.2.3")
        report_path = tmp_path / session["rtp_report"]["path"]
        report = json.loads(report_path.read_bytes())
        report.update(hil.transport_report(pcap, session["sip_ports"]))
        report_path.write_text(json.dumps(report))
        session.update(capture=hil.reference(pcap, tmp_path), writer_receipt=hil.reference(writer, tmp_path),
                       rtp_report=hil.reference(report_path, tmp_path))
    elif fault == "wrong_terminal":
        session["terminal"] = "esp"
    elif fault == "wrong_uri":
        session["request_uris"][session["sip_call_ids"][0]] = "sip:unrelated@192.0.2.2"
    elif fault == "reused_call":
        receipt["sessions"][1]["sip_call_ids"] = session["sip_call_ids"]
    elif fault in {"bad_lease", "missing_identity", "bad_cleanup", "missing_native", "old_observations"}:
        path = tmp_path / receipt["observations"]["path"]
        events = json.loads(path.read_bytes())
        if fault == "bad_lease":
            for event in events["events"]:
                if event["kind"] == "directory":
                    event["payload"]["endpoints"]["peer"]["registration"]["expires_unix"] = BASE - 1
        elif fault == "missing_identity":
            events["events"] = [event for event in events["events"] if event["kind"] != "identity"]
        elif fault == "bad_cleanup":
            next(event for event in events["events"] if event["kind"] == "runtime" and event["phase"] == "post")["snapshot"]["resource_counts"]["rtp_ports"] = 1
        elif fault == "missing_native":
            events["events"] = [event for event in events["events"] if event["kind"] != "native-state"]
        else:
            events["run_id"] = "old-run"
        path.write_text(json.dumps(events))
        receipt["observations"] = hil.reference(path, tmp_path)
    elif fault in {"edited_report", "capture_drops"}:
        key = "rtp_report" if fault == "edited_report" else "writer_receipt"
        path = tmp_path / session[key]["path"]
        data = json.loads(path.read_bytes())
        if fault == "edited_report":
            data["streams"][0]["packets"] += 100
        else:
            data["tcpdump_counts"]["dropped_by_kernel"] = 1
        path.write_text(json.dumps(data))
        session[key] = hil.reference(path, tmp_path)
    path = tmp_path / result["measurement_receipt"]["path"]
    path.write_text(json.dumps(receipt))
    result["measurement_receipt"] = hil.reference(path, tmp_path)
    with pytest.raises(EvidenceError):
        validate_hil_measurement(result, plan={"plan_id": "plan", "head": "head"}, candidate_id="candidate", root=tmp_path, require_redial=True)
