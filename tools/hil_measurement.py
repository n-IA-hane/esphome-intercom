"""Contemporaneous HIL receipts for an existing, independently owned actor.

No connection, call, upload or capture starts on import. Actors supply their
existing API observations and SshPcapWriter output. CLOCK_MONOTONIC is sampled
here, never reconstructed from an older report. Offline validation re-reads
SIP/RTP and decoded video from the hash-bound capture.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.rtp_pcap_evidence import analyze_pcap, evaluate_streams, _sip_decode_arguments  # noqa: E402


SCENARIOS = {
    "esp-to-ha-answer-hangup",
    "registered-sip-to-esp-bidirectional-hangup",
    "esp-to-esp-watchdog-and-bidirectional-hangup",
    "p4-audio-to-bidirectional-video-reinvite",
    "p4-full-landscape-jpeg-call-lifecycle",
}
BINDING = ("plan_id", "head", "candidate_id", "scenario", "run_id")


class MeasurementError(ValueError):
    """A bounded producer/validator invariant, safe to report to the actor."""


NATIVE_FAILURE_PATTERNS = (
    "TX DMA completion/refill contract failed", "I2S error detected in audio task",
    "watchdog got triggered", "Guru Meditation", "panic'ed", "abort() was called",
    "Interrupt wdt timeout", "Brownout detector was triggered", "Rebooting...",
    "Video worker stop timeout", "retaining sockets", "Media timeout after",
)
SIP_FIELDS = ("frame.time_epoch", "ip.src", "ip.dst", "sip.Call-ID", "sip.Method",
              "sip.Status-Code", "sip.CSeq.method", "sip.CSeq.seq", "sip.r-uri",
              "sdp.media", "sdp.media_attr", "sdp.session_attr")


def require(condition, message):
    if not condition:
        raise MeasurementError(message)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def native_failures(text):
    """Classify actual fault messages; DMA/I2S/watchdog status is normal data."""
    failures = [pattern for pattern in NATIVE_FAILURE_PATTERNS if pattern.casefold() in text.casefold()]
    if re.search(r"(?mi)^\s*rst:0x[0-9a-f]+\s+\(", text):
        failures.append("native reset banner during the call")
    return failures


def sensitive_native_line(line):
    return bool(re.search(r"authorization\s*:|(?:password|passwd|secret|nonce)\s*[:=]", line, re.I))


def reference(path: Path, root: Path):
    path, root = path.resolve(), root.resolve()
    require(root in path.parents, "HIL input must be inside the evidence root")
    raw = path.read_bytes()
    require(bool(raw), "HIL input is empty")
    return {"path": str(path.relative_to(root)), "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest()}


def bound(reference, root):
    require(isinstance(reference, dict), "Missing bound HIL input")
    relative = Path(str(reference.get("path") or ""))
    path = (root / relative).resolve()
    require(not relative.is_absolute() and ".." not in relative.parts and root in path.parents,
            "HIL input escapes evidence root")
    require(globals()["reference"](path, root) == reference, "HIL input hash or size mismatch")
    return path


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(payload, stream, sort_keys=True, indent=2)
        stream.write("\n")


def packet_observations(pcap, sip_ports):
    """Read actual SIP identities/SDP and capture epoch, including TCP SIP."""
    subprocess.run(["capinfos", "-c", str(pcap)], check=True, capture_output=True, timeout=30)
    command = ["tshark", *_sip_decode_arguments(tuple(sip_ports)), "-r", str(pcap),
               "-Y", "sip && !(icmp || icmpv6)", "-T", "fields", "-E", "occurrence=a", "-E", "aggregator=|"]
    for field in SIP_FIELDS:
        command += ["-e", field]
    rows = subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
    packets = []
    for line in rows.stdout.splitlines():
        columns = line.split("\t")
        require(len(columns) == len(SIP_FIELDS), "Ambiguous captured SIP fields")
        packet = dict(zip(SIP_FIELDS, columns, strict=True))
        packet["frame.time_epoch"] = float(packet["frame.time_epoch"])
        packets.append(packet)
    epochs = subprocess.run(["tshark", "-r", str(pcap), "-T", "fields", "-e", "frame.time_epoch"],
                            check=True, capture_output=True, text=True, timeout=60)
    times = [float(line) for line in epochs.stdout.splitlines() if line]
    require(times and all(map(finite, times)), "Missing actual capture timestamps")
    return {"sip": packets, "first_epoch": min(times), "last_epoch": max(times)}


def transport_report(pcap, sip_ports):
    streams = analyze_pcap(pcap, sip_ports=tuple(sip_ports))
    failures = evaluate_streams(streams, require_streams=2, max_audio_loss=0,
                               max_video_loss=0, max_cadence_ratio=1.25)
    wire = packet_observations(pcap, sip_ports)
    command = ["tshark", *_sip_decode_arguments(tuple(sip_ports)), "-r", str(pcap),
               "-Y", "rtp && !(icmp || icmpv6)", "-T", "fields"]
    for field in ("frame.time_epoch", "ip.src", "udp.srcport", "ip.dst", "udp.dstport", "rtp.ssrc", "rtp.p_type"):
        command += ["-e", field]
    rows = subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
    timeline = {}
    for line in rows.stdout.splitlines():
        epoch, source, sport, destination, dport, ssrc, pt = line.split("\t")
        key = f"{source}:{sport}->{destination}:{dport}/{ssrc}/{pt}"
        item = timeline.setdefault(key, {"source": f"{source}:{sport}", "destination": f"{destination}:{dport}",
            "ssrc": ssrc, "payload_type": int(pt), "first_epoch": float(epoch), "last_epoch": float(epoch)})
        item["first_epoch"] = min(item["first_epoch"], float(epoch))
        item["last_epoch"] = max(item["last_epoch"], float(epoch))
    wire["rtp_timeline"] = list(timeline.values())
    return {"streams": streams, "failures": failures, "wire": wire}


def _idle(snapshot):
    resources = snapshot.get("runtime_resources", snapshot)
    resources = (snapshot.get("media_debug") or {}).get("runtime_resources", resources)
    counts = resources.get("resource_counts")
    return (snapshot.get("state") == "idle" and snapshot.get("active_dialogs") == 0
            and resources.get("call_scoped_quiescent") is True
            and isinstance(counts, dict) and bool(counts)
            and all(type(value) is int and value >= 0 and (key == "runtime_tasks" or value == 0)
                    for key, value in counts.items()))


def _counts(snapshot):
    return ((snapshot.get("media_debug") or {}).get("runtime_resources")
            or snapshot.get("runtime_resources") or snapshot)["resource_counts"]


class HilMeasurement:
    """An actor adapter: every observation is stamped when it is supplied."""

    def __init__(self, environment, *, allow_existing=False):
        self.root = Path(environment["HIL_EVIDENCE_ROOT"]).resolve()
        self.output = Path(environment["HIL_MEASUREMENT_RECEIPT"]).resolve()
        self.binding = {key: environment["HIL_" + ({"scenario": "SCENARIO_ID"}.get(key, key.upper()))]
                        for key in BINDING}
        require(all(self.binding.values()) and self.binding["scenario"] in SCENARIOS,
                "Missing or unsupported HIL actor identity")
        self.start = float(environment["HIL_ACTOR_STARTED_MONOTONIC"])
        self.epoch = float(environment["HIL_ACTOR_STARTED_UNIX"])
        require(self.root in self.output.parents and (allow_existing or not self.output.exists()), "Receipt destination is not fresh")
        self.events, self.sessions, self.devices, self.attachments = [], [], {}, []
        self._observation_lock = threading.Lock()
        self.expected_firmware = {key: environment.get("HIL_" + key.upper(), "") for key in
                                  ("firmware_sha256", "firmware_profile", "source_lock_sha256")}

    @classmethod
    def from_environment(cls, environment=None, *, allow_existing=False):
        return cls(os.environ if environment is None else environment, allow_existing=allow_existing)

    def observe(self, kind, **data):
        require(not {"kind", "at", "unix"}.intersection(data), "Actor observations cannot be backdated")
        # Freeze nested native dictionaries now; a later directory refresh or
        # actor mutation must never rewrite a previously observed baseline.
        data = json.loads(json.dumps(data, allow_nan=False))
        with self._observation_lock:
            event = {"kind": kind, "at": time.monotonic(), "unix": time.time(), **data}
            require(event["at"] >= self.start, "Observation precedes actor start")
            self.events.append(event)
        return event

    def identity(self, device, native_identity, *, host, profile, firmware_sha256, source_lock_sha256):
        require(device not in self.devices and native_identity.get("mac") and native_identity.get("build"),
                "Missing live native MAC/build identity")
        require(re.fullmatch(r"[0-9a-f]{64}", firmware_sha256) and source_lock_sha256,
                "Missing candidate firmware/source identity")
        value = {"native": native_identity, "host": host, "profile": profile,
                 "firmware_sha256": firmware_sha256, "source_lock_sha256": source_lock_sha256}
        self.devices[device] = value
        self.observe("identity", device=device, **value)

    def directory(self, phase, payload):
        """payload: endpoints[name]={host,device_id,destination[,registration]}.

        Registered endpoints additionally retain registration={mode:'registered',
        aor,contact,expires_unix}. signaling_hosts contains the actual PBX hosts.
        Preserve native directory responses separately with attach() if needed.
        """
        require(phase in {"pre", "post"}, "Invalid directory observation phase")
        self.observe("directory", phase=phase, payload=payload)

    def attach(self, path):
        """Hash an additional raw native/runtime input, without assigning claims."""
        item = reference(Path(path), self.root)
        if item not in self.attachments:
            self.attachments.append(item)
        return item

    def start_session(self, *, caller, callee, terminal, route_kind, request_uris=None, sip_call_ids=()):
        session = HilCall(self, caller=caller, callee=callee, terminal=terminal, route_kind=route_kind,
                          request_uris=request_uris or {}, sip_call_ids=list(sip_call_ids))
        self.sessions.append(session)
        return session

    def finish(self):
        require(self.sessions and all(session.complete for session in self.sessions), "Unfinished HIL session")
        event_path = self.output.with_name(self.output.stem + "-observations.json")
        write_json(event_path, {**self.binding, "events": self.events})
        receipt = {"schema_version": 2, **self.binding, "expected_firmware": self.expected_firmware,
                   "devices": self.devices, "observations": reference(event_path, self.root),
                   "attachments": self.attachments,
                   "proof_limits": {"acoustic": False, "physical_panel": False},
                   "sessions": [session.data for session in self.sessions]}
        # The final wrapper supplies its own independent ending clock and idle
        # snapshots. Validate producer-owned material before writing the receipt.
        validate_receipt(receipt, self.root, actor_start=self.start, actor_end=time.monotonic(),
                         actor_epoch=self.epoch, actor_end_epoch=time.time())
        write_json(self.output, receipt)
        return receipt


class HilCall:
    def __init__(self, owner, **data):
        self.owner, self.complete = owner, False
        self.data = {**data, "session_id": str(len(owner.sessions))}
        self.data["started_at"] = self.observe("call-start")["at"]

    def observe(self, kind, **data):
        return self.owner.observe(kind, session_id=self.data["session_id"], **data)

    def bind_dialogs(self, request_uris):
        """Map exact observed Call-ID to its intended initial Request-URI."""
        self.data["request_uris"] = dict(request_uris)
        self.data["sip_call_ids"] = list(request_uris)

    def native_state(self, device, state, call_id):
        self.observe("native-state", device=device, state=state, call_id=call_id)

    def native_log(self, device, line):
        require(not sensitive_native_line(line),
                "Sensitive native line must be omitted before HIL collection")
        self.observe("native-log", device=device, line=line)

    def runtime(self, phase, snapshot):
        require(phase in {"pre", "post"}, "Invalid runtime observation phase")
        self.observe("runtime", phase=phase, snapshot=snapshot)

    def connected(self):
        require("connected_at" not in self.data, "Call connected observation is already recorded")
        self.data["connected_at"] = self.observe("call-connected")["at"]

    def ended(self):
        require("connected_at" in self.data and "ended_at" not in self.data, "Call end has no unique connected generation")
        self.data["ended_at"] = self.observe("call-ended")["at"]

    def quiescent(self):
        require("ended_at" in self.data and "quiescent_at" not in self.data, "Cleanup has no unique terminal generation")
        self.data["quiescent_at"] = self.observe("call-quiescent")["at"]

    def finish(self, *, pcap, writer_receipt, sip_ports=(5060,)):
        require(not self.complete, "HIL call cannot be finalized twice")
        require("quiescent_at" in self.data, "HIL call has no observed cleanup barrier")
        pcap, writer_receipt = Path(pcap), Path(writer_receipt)
        report = transport_report(pcap, sip_ports)
        report_path = self.owner.output.with_name(self.owner.output.stem + "-" + self.data["session_id"] + "-wire.json")
        write_json(report_path, {**self.owner.binding, **report})
        self.data.update(capture=reference(pcap, self.owner.root),
                         writer_receipt=reference(writer_receipt, self.owner.root),
                         rtp_report=reference(report_path, self.owner.root), sip_ports=list(sip_ports))
        video = [stream for stream in report["streams"] if stream.get("media_type") == "video"]
        self.data["decoded_video"] = []
        for index, stream in enumerate(video):
            output = self.owner.root / (self.data["capture"]["path"] + f".video-{index}")
            decoded = decode_video(pcap, stream, output)
            self.data["decoded_video"].append({"artifact": reference(output, self.owner.root), "decoded": decoded})
        self.complete = True
        return self.data


def _events(events, session, kind):
    return [event for event in events if event.get("session_id") == session["session_id"] and event.get("kind") == kind]


def _validate_sip(session, report, endpoints, signaling_hosts):
    packets = report["wire"]["sip"]
    call_ids = session["sip_call_ids"]
    require(isinstance(call_ids, list) and call_ids and len(set(call_ids)) == len(call_ids)
            and set(session["request_uris"]) == set(call_ids), "Missing exact SIP dialog route identities")
    require(not {row["sip.Call-ID"] for row in packets if row["sip.CSeq.method"] in {"INVITE", "ACK", "BYE"}} - set(call_ids),
            "Capture includes another call's SIP dialog")
    allowed_hosts = {value["host"] for value in endpoints.values()} | set(signaling_hosts)
    for call_id in call_ids:
        dialog = [row for row in packets if row["sip.Call-ID"] == call_id]
        require(dialog and all(row["ip.src"] in allowed_hosts and row["ip.dst"] in allowed_hosts for row in dialog),
                "Captured SIP dialog uses an unplanned route")
        invites = [row for row in dialog if row["sip.Method"] == "INVITE"]
        require(invites and invites[0]["sip.r-uri"] == session["request_uris"][call_id],
                "Captured INVITE Request-URI differs from the intended route")
        initial_cseq = invites[0]["sip.CSeq.seq"]
        require(any(row["sip.Status-Code"] == "200" and row["sip.CSeq.method"] == "INVITE"
                    and row["sip.CSeq.seq"] == initial_cseq for row in dialog)
                and any(row["sip.Method"] == "ACK" and row["sip.CSeq.seq"] == initial_cseq for row in dialog),
                "Missing captured established SIP transaction")
        byes = [row for row in dialog if row["sip.Method"] == "BYE"]
        require(byes and len({(row["ip.src"], row["sip.CSeq.seq"]) for row in byes}) == 1,
                "Missing or repeated terminal SIP owner")
        require(any(row["sip.Status-Code"] == "200" and row["sip.CSeq.method"] == "BYE"
                    and row["sip.CSeq.seq"] == byes[0]["sip.CSeq.seq"] and row["ip.dst"] == byes[0]["ip.src"] for row in dialog),
                "Missing final response to captured BYE")
    terminal_host = endpoints[session["terminal"]]["host"]
    require(any(row["sip.Method"] == "BYE" and row["ip.src"] == terminal_host
                and row["sip.Call-ID"] in call_ids for row in packets), "Captured BYE has the wrong terminal owner")
    return packets


def decode_video(pcap, selection, output):
    """Depacketize the selected actual RTP bytes, then decode them offline."""
    from tools.sip_video_peer import rtp, video_rtp

    source, sport = selection["source"].rsplit(":", 1)
    destination, dport = selection["destination"].rsplit(":", 1)
    # Validate every interpolated display-filter operand.
    from ipaddress import IPv4Address
    source, destination = str(IPv4Address(source)), str(IPv4Address(destination))
    sport, dport, ssrc, pt = int(sport), int(dport), int(selection["ssrc"], 0), int(selection["payload_type"])
    require(0 < sport < 65536 and 0 < dport < 65536 and 0 <= ssrc < 2**32 and 0 <= pt <= 127,
            "Invalid video tuple")
    rows = subprocess.run(["tshark", "-r", str(pcap), "-d", f"udp.port=={sport},rtp", "-Y",
        f"rtp && !icmp && !icmpv6 && ip.src == {source} && ip.dst == {destination} && udp.srcport == {sport} && udp.dstport == {dport} && rtp.ssrc == {ssrc} && rtp.p_type == {pt}",
        "-T", "fields", "-e", "udp.payload"], check=True, capture_output=True, text=True, timeout=60)
    # JPEG is statically assigned PT26; H264 must be the negotiated dynamic PT.
    codec = "mjpeg" if pt == 26 else "h264"
    depacketizer = video_rtp.JpegDepacketizer() if codec == "mjpeg" else video_rtp.H264Depacketizer()
    units = []
    for line in rows.stdout.splitlines():
        unit = depacketizer.push(rtp.parse_packet(bytes.fromhex(line.replace(":", ""))))
        if unit is not None:
            units.append(unit.data)
    require(len(units) >= 3 and depacketizer.dropped_access_units == 0, "Captured video has incomplete access units")
    raw = b"".join(units)
    if output.exists():
        require(output.read_bytes() == raw, "Decoded video differs from captured RTP")
    else:
        with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            stream.write(raw)
    decoded = subprocess.run(["ffprobe", "-v", "error", "-f", codec, "-count_frames", "-show_streams",
                              "-of", "json", str(output)], check=True, capture_output=True, text=True, timeout=60)
    streams = json.loads(decoded.stdout)["streams"]
    require(len(streams) == 1 and streams[0].get("codec_name") == codec
            and int(streams[0].get("nb_read_frames", 0)) >= 3
            and min(int(streams[0].get(key, 0)) for key in ("width", "height")) > 0,
            "Captured video cannot be decoded")
    return {"codec": codec, "frames": int(streams[0]["nb_read_frames"]),
            "width": streams[0]["width"], "height": streams[0]["height"],
            "sha256": hashlib.sha256(raw).hexdigest()}


def validate_receipt(receipt, root, *, actor_start, actor_end, actor_epoch, actor_end_epoch):
    """Validate schema2 observations and rederive every wire/media assertion."""
    root = root.resolve()
    require(receipt.get("schema_version") == 2 and receipt.get("scenario") in SCENARIOS, "Unsupported measured HIL receipt")
    for attachment in receipt.get("attachments", []):
        bound(attachment, root)
    require(all(finite(value) for value in (actor_start, actor_end, actor_epoch, actor_end_epoch))
            and actor_start < actor_end and actor_epoch < actor_end_epoch, "Missing actor clock bounds")
    observations = json.loads(bound(receipt["observations"], root).read_bytes())
    require(all(observations.get(key) == receipt.get(key) and receipt.get(key) for key in BINDING),
            "Observation identity mismatch")
    events = observations["events"]
    require(events and all(isinstance(event, dict) and finite(event.get("at")) and finite(event.get("unix"))
            and actor_start <= event["at"] <= actor_end and actor_epoch <= event["unix"] <= actor_end_epoch
            and abs((event["unix"] - actor_epoch) - (event["at"] - actor_start)) <= 1 for event in events),
            "Native observation is stale or outside the actor clock interval")
    require([event["at"] for event in events] == sorted(event["at"] for event in events), "Native observation order differs")
    directories = {phase: [event for event in events if event["kind"] == "directory" and event["phase"] == phase]
                   for phase in ("pre", "post")}
    require(all(len(rows) == 1 for rows in directories.values()), "Missing native directory before/after")
    before, after = (directories[phase][0]["payload"] for phase in ("pre", "post"))
    endpoints = before["endpoints"]
    def stable_endpoints(payload):
        stable = json.loads(json.dumps(payload["endpoints"]))
        for name, endpoint in stable.items():
            registration = endpoint.get("registration")
            if registration is not None:
                expiry = registration.pop("expires_unix")
                observed = directories["pre" if payload is before else "post"][0]["unix"]
                require(finite(expiry) and expiry > observed, "Native directory registration lease is expired")
        return stable
    require(endpoints and stable_endpoints(before) == stable_endpoints(after)
            and before["signaling_hosts"] == after["signaling_hosts"],
            "Native directory or route changed during HIL")
    devices = receipt["devices"]
    require(len({device["host"] for device in devices.values()}) == len(devices)
            and len({re.sub(r"[:-]", "", device["native"]["mac"]).casefold() for device in devices.values()}) == len(devices),
            "Physical peer identities are reused")
    identities = [event for event in events if event["kind"] == "identity"]
    require(devices and len(identities) == len(devices), "Missing live firmware identities")
    for name, device in devices.items():
        rows = [event for event in identities if event["device"] == name]
        require(len(rows) == 1 and all(rows[0].get(key) == value for key, value in device.items())
                and device["native"].get("mac") and device["native"].get("build")
                and re.fullmatch(r"[0-9a-f]{64}", device["firmware_sha256"])
                and device["host"] == endpoints[name]["host"], "Native firmware identity or endpoint differs")
    expected = receipt["expected_firmware"]
    require(all(expected.values()) and any(device["firmware_sha256"] == expected["firmware_sha256"]
            and device["profile"] == expected["firmware_profile"]
            and device["source_lock_sha256"] == expected["source_lock_sha256"] for device in devices.values()),
            "Live firmware is not bound to the installed candidate")
    require(all(device["source_lock_sha256"] == expected["source_lock_sha256"] for device in devices.values()),
            "A physical peer uses another candidate source lock")
    sessions = receipt["sessions"]
    scenario = receipt["scenario"]
    minimum = 4 if scenario.startswith("esp-to-esp-") else 2
    require(isinstance(sessions, list) and len(sessions) >= minimum, "Missing terminal-owner or redial sessions")
    captures, seen_calls, combinations = set(), set(), set()
    previous = None
    for session in sessions:
        times = [session[key] for key in ("started_at", "connected_at", "ended_at", "quiescent_at")]
        require(all(map(finite, times)) and actor_start <= times[0] <= times[1] < times[2] <= times[3] <= actor_end,
                "Invalid measured session timeline")
        for kind, timestamp in zip(("call-start", "call-connected", "call-ended", "call-quiescent"), times, strict=True):
            observed = _events(events, session, kind)
            require(len(observed) == 1 and observed[0]["at"] == timestamp,
                    "Session timeline differs from contemporaneous actor observations")
        require(directories["pre"][0]["at"] <= times[0] and directories["post"][0]["at"] >= times[3],
                "Directory snapshots do not bracket the calls")
        if previous is not None:
            require(times[0] >= previous, "Measured calls overlap cleanup")
            if not scenario.startswith("esp-to-esp-"):
                require(times[0] - previous <= 1, "Redial did not start immediately after cleanup")
        previous = times[3]
        caller, callee, terminal = (session[key] for key in ("caller", "callee", "terminal"))
        require(caller != callee and {caller, callee} <= endpoints.keys() and terminal in {caller, callee},
                "Invalid actual call route or terminal owner")
        combinations.add((caller, callee, terminal))
        native_devices = {caller, callee} & devices.keys()
        require(native_devices and (len(native_devices) == 2 if scenario.startswith("esp-to-esp-") else True),
                "Required physical peers are absent")
        for device in native_devices:
            states = [event for event in _events(events, session, "native-state") if event["device"] == device]
            native_logs = [event for event in _events(events, session, "native-log") if event["device"] == device]
            owned_ids = {match[1] for event in native_logs
                         if re.search(r"incoming call|calling\.\.\.", event["line"], re.I)
                         for match in [re.search(r"call_id=([^\s)]+)", event["line"])] if match}
            require(native_logs and bool(owned_ids & set(session["sip_call_ids"]))
                    and not native_failures("\n".join(event["line"] for event in native_logs)),
                "Native call identity or runtime failure evidence is missing or failed")
            def owns(event):
                return event["call_id"] in session["sip_call_ids"] or (not event["call_id"] and bool(owned_ids & set(session["sip_call_ids"])))
            require(any(event["state"] == "in_call" and owns(event)
                        and times[0] <= event["at"] <= times[2] for event in states)
                    and any(event["state"] == "idle" and owns(event)
                            and times[2] <= event["at"] <= times[3] for event in states), "Missing call-bound native state transitions")
        runtime = {phase: [event for event in _events(events, session, "runtime") if event["phase"] == phase]
                   for phase in ("pre", "post")}
        require(all(len(rows) == 1 and _idle(rows[0]["snapshot"]) for rows in runtime.values())
                and _counts(runtime["pre"][0]["snapshot"]) == _counts(runtime["post"][0]["snapshot"])
                and runtime["pre"][0]["at"] <= times[1] and times[2] <= runtime["post"][0]["at"] <= times[3],
                "Call resources did not return to the observed baseline")
        capture = bound(session["capture"], root)
        require(session["capture"]["sha256"] not in captures, "A call capture was reused")
        captures.add(session["capture"]["sha256"])
        require(not seen_calls.intersection(session["sip_call_ids"]), "A SIP Call-ID was reused")
        seen_calls.update(session["sip_call_ids"])
        writer = json.loads(bound(session["writer_receipt"], root).read_bytes())
        writer_identity = writer.get("verified_remote_identity") or {}
        require(writer.get("status") == "capture_closed_capinfos_accepted" and writer.get("capture_file_valid") is True
                and writer.get("pcap_sha256") == session["capture"]["sha256"]
                and writer.get("bytes") == session["capture"]["bytes"]
                and writer.get("capture_quality") == "zero_kernel_drops_reported"
                and type(writer_identity.get("pid")) is int and writer_identity["pid"] > 0
                and writer_identity["pid"] == writer.get("remote_pid_reported")
                and writer_identity.get("starttime") and writer_identity.get("cmdline") == writer.get("argv")
                and str(writer_identity.get("stdout", "")).startswith("pipe:")
                and writer.get("tcpdump_counts", {}).get("dropped_by_kernel") == 0
                and writer.get("ssh_exit_code") in {0, 130, -2},
                "Actual capture writer identity, acceptance or quality is missing")
        report = json.loads(bound(session["rtp_report"], root).read_bytes())
        require(all(report.get(key) == receipt.get(key) for key in BINDING), "Wire report identity mismatch")
        actual = transport_report(capture, session["sip_ports"])
        require(all(report.get(key) == value for key, value in actual.items()) and not actual["failures"],
                "Wire report differs from actual capture or has transport failures")
        wire = actual["wire"]
        require(actor_epoch <= wire["first_epoch"] <= wire["last_epoch"] <= actor_end_epoch,
                "Actual capture is stale or outside this actor run")
        session_epoch = actor_epoch + times[0] - actor_start
        end_epoch = actor_epoch + times[2] - actor_start
        allowed_hosts = {value["host"] for value in endpoints.values()} | set(before["signaling_hosts"])
        for stream in actual["streams"]:
            if stream.get("media_type") not in {"audio", "video"}:
                continue
            require(stream["source"].rsplit(":", 1)[0] in allowed_hosts
                    and stream["destination"].rsplit(":", 1)[0] in allowed_hosts,
                    "Captured RTP uses an unplanned endpoint")
            timeline = [row for row in wire["rtp_timeline"] if all(row.get(key) == stream.get(key)
                        for key in ("source", "destination", "ssrc", "payload_type"))]
            require(len(timeline) == 1 and session_epoch - 1 <= timeline[0]["first_epoch"]
                    <= timeline[0]["last_epoch"] <= end_epoch + 1
                    and stream["duration_seconds"] <= times[2] - times[1] + 1,
                    "RTP observation exceeds the actual native call interval")
        packets = _validate_sip(session, actual, endpoints, before["signaling_hosts"])
        audio = [stream for stream in actual["streams"] if stream.get("media_type") == "audio"]
        native_hosts = {endpoints[name]["host"] for name in native_devices}
        for host in native_hosts:
            for direction in ("source", "destination"):
                require(any(stream[direction].rsplit(":", 1)[0] == host
                            and stream["duration_seconds"] >= (20 if session is sessions[0] or scenario.startswith("esp-to-esp-") else 0.1)
                            for stream in audio), "Missing sustained directional audio on the actual ESP route")
        if scenario.startswith("registered-"):
            require(session["route_kind"] == "registered", "Registered scenario used a substitute route")
            signaling = set(before["signaling_hosts"])
            require(len(session["sip_call_ids"]) >= 2
                    and any(row["sip.Method"] == "INVITE" and row["ip.src"] == endpoints[caller]["host"]
                            and row["ip.dst"] in signaling for row in packets)
                    and any(row["sip.Method"] == "INVITE" and row["ip.src"] in signaling
                            and row["ip.dst"] == endpoints[callee]["host"] for row in packets),
                    "Registered SIP call did not traverse the actual PBX and both dialogs")
            registration = endpoints[caller]["registration"]
            require(registration["mode"] == "registered" and registration["aor"] and registration["contact"],
                    "Actual registered binding has no valid live lease")
        if scenario.startswith("p4-"):
            video = [stream for stream in actual["streams"] if stream.get("media_type") == "video"]
            require(len(session["decoded_video"]) == len(video), "Missing capture-bound decoded video inputs")
            require(any(stream["source"].rsplit(":", 1)[0] in native_hosts for stream in video)
                    and any(stream["destination"].rsplit(":", 1)[0] in native_hosts for stream in video),
                    "Missing bidirectional P4 video on the actual route")
            for index, stream in enumerate(video):
                output = root / (session["capture"]["path"] + f".video-{index}")
                recorded = session["decoded_video"][index]
                require(bound(recorded["artifact"], root) == output, "Decoded video input belongs to another capture")
                decoded = decode_video(capture, stream, output)
                require(decoded == recorded["decoded"], "Decoded video report differs from captured RTP")
                if scenario == "p4-full-landscape-jpeg-call-lifecycle":
                    require(decoded["codec"] == "mjpeg" and any(device["profile"] == "waveshare-p4-full-landscape" for device in devices.values()),
                            "FULL landscape scenario requires the actual JPEG firmware and stream")
            logs = "\n".join(event["line"] for event in _events(events, session, "native-log")
                             if times[0] <= event["at"] <= times[3])
            from scripts.run_p4_h264_reinvite_hil import parse_serial_metrics
            metrics = parse_serial_metrics(logs)
            require(not metrics["fatal_errors"] and all(metrics["rx"].get(key, 0) >= 3
                    for key in ("rendered", "presented", "refresh_done"))
                    and metrics["session"].get("send_fail") == 0, "P4 render/runtime evidence failed")
            if scenario == "p4-audio-to-bidirectional-video-reinvite":
                invites = [row for row in packets if row["sip.Method"] == "INVITE" and row["sip.Call-ID"] in session["sip_call_ids"]]
                require(invites and not re.search(r"video\s+[1-9]", invites[0]["sdp.media"]), "Initial call was not audio-only")
                reinvites = [row for row in invites if row["sip.CSeq.seq"] != invites[0]["sip.CSeq.seq"]
                             and re.search(r"video\s+[1-9]", row["sdp.media"])]
                require(reinvites and any(row["sip.Status-Code"] == "200" and row["sip.CSeq.method"] == "INVITE"
                        and row["sip.CSeq.seq"] == reinvites[0]["sip.CSeq.seq"] and row["sip.Call-ID"] == reinvites[0]["sip.Call-ID"]
                        for row in packets), "Missing captured successful video re-INVITE")
                for direction in ("source", "destination"):
                    require(any(stream[direction].rsplit(":", 1)[0] in native_hosts
                        and any(all(row.get(key) == stream.get(key) for key in ("source", "destination", "ssrc", "payload_type"))
                            and row["first_epoch"] < reinvites[0]["frame.time_epoch"] for row in wire["rtp_timeline"])
                        for stream in audio), "No actual duplex audio preceded the video re-INVITE")
                require(all(item["decoded"]["codec"] == "h264" for item in session["decoded_video"]),
                        "P4 re-INVITE scenario requires actual decoded H264 in both directions")
    if scenario.startswith("esp-to-esp-"):
        peers = {session["caller"] for session in sessions} | {session["callee"] for session in sessions}
        require(len(peers) == 2 and len(combinations) == 4, "ESP-to-ESP requires both directions and terminal owners")
    else:
        require({session["terminal"] for session in sessions} >= {sessions[0]["caller"], sessions[0]["callee"]},
                "Both terminal owners were not observed")


def main():
    """A sidecar finalizer for files written by a *current* measured actor.

    It cannot upgrade legacy P4 result.json: the actor must use HilMeasurement
    to collect native clocks, identities, directory, terminal and writer inputs.
    --receipt verifies those producer inputs against the current HIL environment.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    actor = HilMeasurement.from_environment(allow_existing=True)
    receipt = json.loads(args.receipt.read_bytes())
    require(all(receipt.get(key) == value for key, value in actor.binding.items()), "Sidecar actor identity mismatch")
    validate_receipt(receipt, actor.root, actor_start=actor.start, actor_end=time.monotonic(),
                     actor_epoch=actor.epoch, actor_end_epoch=time.time())
    require(args.receipt.resolve() == actor.output, "Sidecar does not rename or import an earlier receipt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
