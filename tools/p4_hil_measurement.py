"""Measured sidecar adapter for the maintained P4 HIL actors.

The existing actor keeps sole ownership of ESP API, serial and call cleanup.
This adapter owns only its SshPcapWriter and feeds contemporaneous observations
to HilMeasurement. Missing site identity/capture configuration fails before calls.
"""
from __future__ import annotations

import json
import os
import re
import socket

from tools.hil_measurement import HilMeasurement, packet_observations, require, sensitive_native_line
from tools.ssh_pcap_capture import SshPcapWriter


class P4HilMeasurement:
    def __init__(self, environment=None):
        self.environment = os.environ if environment is None else environment
        self.measurement = HilMeasurement.from_environment(self.environment)
        self.expected_mac = self.environment.get("HIL_DEVICE_MAC", "")
        self.expected_build = self.environment.get("HIL_DEVICE_BUILD", "")
        self.capture_host = self.environment.get("HIL_CAPTURE_SSH_HOST", "")
        require(self.expected_mac and self.expected_build and self.capture_host,
                "P4 measurement requires HIL_DEVICE_MAC, HIL_DEVICE_BUILD and HIL_CAPTURE_SSH_HOST")
        self.capture_prefix = json.loads(self.environment.get("HIL_CAPTURE_PREFIX_JSON", "[]"))
        require(isinstance(self.capture_prefix, list) and all(isinstance(arg, str) for arg in self.capture_prefix),
                "Invalid P4 capture command prefix")
        self.call, self.writer, self.native_call_id = None, None, None
        self.completed = []

    def api_observation(self, object_id, value):
        if self.call is None or object_id != "voip_state" or value not in {"in_call", "idle"}:
            return
        if value == "idle" and "connected_at" in self.call.data and "ended_at" not in self.call.data:
            self.call.ended()
        self.call.native_state("p4", value, self.native_call_id or "")
        if value == "in_call" and "connected_at" not in self.call.data:
            self.call.connected()

    def serial_observation(self, line):
        if self.call is None:
            return
        if sensitive_native_line(line):
            return
        self.call.native_log("p4", line)
        if re.search(r"incoming call|calling\.\.\.", line, re.I):
            found = re.search(r"call_id=([^\s)]+)", line)
            if found:
                self.native_call_id = found[1]

    async def directory(self, phase):
        from tools.sip_video_peer import sip

        response = await self.ws.command({"type": "voip_stack/list_devices"})
        rows = [row for row in (response.get("result") or {}).get("devices", [])
                if row.get("endpoint_type") == "sip_account"
                and str(row.get("extension")) == self.args.p4_extension]
        require(len(rows) == 1
                and rows[0].get("endpoint_id"), "P4 is absent or ambiguous in the actual HA directory")
        row = rows[0]
        states = await self.ws.command({"type": "get_states"})
        books = [state for state in states.get("result", []) if state.get("entity_id") == "sensor.voip_phonebook"]
        require(len(books) == 1, "Native account directory is unavailable")
        roster = books[0].get("attributes", {}).get("roster_json")
        roster = json.loads(roster) if isinstance(roster, str) else roster
        require(isinstance(roster, dict), "Invalid native account directory")
        contacts = [contact for contact in roster.get("contacts", [])
                    if contact.get("metadata", {}).get("endpoint_id") == row["endpoint_id"]
                    and str(contact.get("extension")) == self.args.p4_extension]
        require(len(contacts) == 1, "P4 account has no unique phonebook identity")
        metadata = contacts[0].get("metadata", {})
        require(metadata.get("registered") is True and metadata.get("device_id"),
                "P4 account has no registered native device")
        uris = {item.get("uri") for item in metadata.get("sip_contacts", []) if item.get("uri")}
        matching = []
        for uri in sorted(uris):
            try:
                parsed = sip.parse_sip_uri(uri)
            except (sip.SipError, TypeError):
                continue
            if parsed.host == self.args.p4_host:
                matching.append(uri)
        require(len(matching) == 1, "P4 has no unique registered Contact at its verified API host")
        p4_uri = matching[0]
        payload = {"endpoints": {
            "p4": {"host": self.args.p4_host, "device_id": metadata["device_id"], "destination": p4_uri},
            "peer": {"host": self.args.local_ip, "device_id": "maintained-sip-video-peer",
                     "destination": self.peer_uri}}, "signaling_hosts": [socket.gethostbyname(self.args.sip_host)]}
        self.measurement.directory(phase, payload)
        return payload

    async def verify_identity(self, esp, args):
        """Read identity before the actor changes settings or extension."""
        self.esp, self.args = esp, args
        require(args.duration >= 23 and args.video_hold_seconds >= 20,
                "Measured P4 calls require duration >=23 and video hold >=20 seconds")
        require(getattr(args, "ha_entry_id", "") and getattr(args, "ha_device_id", ""),
                "Measured P4 calls require the native HA entry and account device")
        info = await esp.client.device_info()
        def normalize(value):
            return value.replace(":", "").replace("-", "").casefold()
        require(normalize(info.mac_address) == normalize(self.expected_mac)
                and info.compilation_time == self.expected_build, "Live P4 MAC/build differs from candidate")
        firmware = self.measurement.expected_firmware
        self.measurement.identity("p4", {"mac": info.mac_address, "build": info.compilation_time},
            host=args.p4_host, profile=firmware["firmware_profile"], firmware_sha256=firmware["firmware_sha256"],
            source_lock_sha256=firmware["source_lock_sha256"])
        from tools.sip_video_peer import _remote_uri
        self.peer_uri = _remote_uri(args.p4_extension, args.sip_host, args.sip_port)

    async def open(self, esp, ws, args, *, ha):
        require(self.measurement.devices and self.esp is esp and self.args is args,
                "P4 live identity must be verified before any actor action")
        self.ws = ws
        self.ha = ha
        self.directory_before = await self.directory("pre")

    async def snapshot(self):
        public = await self.ws.softphone_state()
        diagnostics = await self.ha.diagnostics(self.args.ha_entry_id)
        runtime = diagnostics["data"]["runtime"]
        resources = runtime["resources"]
        require(len(resources.get("resource_counts", {})) >= 21,
                "Full native runtime inventory is unavailable")
        return {"state": public["state"], "active_dialogs": runtime["signaling"]["active_dialogs"],
                "runtime_resources": resources}

    async def begin(self, termination_owner):
        require(self.call is None, "Another P4 measured call is active")
        number = len(self.completed)
        pcap = self.measurement.output.with_name(self.measurement.output.stem + f"-p4-{number}.pcap")
        self.writer = SshPcapWriter(host=self.capture_host, remote_prefix=self.capture_prefix,
            bpf=f"host {self.args.p4_host} or host {self.args.local_ip}", output=pcap,
            require_zero_kernel_drops=True)
        await self.writer.start()
        self.native_call_id = None
        self.call = self.measurement.start_session(caller="peer", callee="p4",
            terminal="peer" if termination_owner == "peer" else "p4", route_kind="ha-directory")
        self.call.runtime("pre", await self.snapshot())

    async def complete(self, peer):
        require(self.call is not None and self.writer is not None, "No P4 measured call is active")
        # The actor's cleanup barrier ran already. No call is terminated here.
        if "ended_at" not in self.call.data:
            self.call.ended()
        self.call.native_state("p4", self.esp.values.get("voip_state"), self.native_call_id or "")
        self.call.runtime("post", await self.snapshot())
        await self.writer.stop()
        self.call.quiescent()
        self.completed.append((self.call, self.writer, dict(peer), self.native_call_id))
        self.call, self.writer = None, None

    async def close(self):
        if self.writer is not None and not self.writer.closed:
            await self.writer.stop()

    async def finish(self):
        await self.directory("post")
        for call, writer, peer, native_call_id in self.completed:
            packets = packet_observations(writer.output, (self.args.sip_port, 5060))["sip"]
            allowed = {self.peer_uri, self.directory_before["endpoints"]["p4"]["destination"]}
            initial = {}
            for row in packets:
                if row["sip.Method"] == "INVITE":
                    initial.setdefault(row["sip.Call-ID"], row["sip.r-uri"])
            require(peer.get("call_id") in initial and native_call_id in initial
                    and set(initial.values()) <= allowed, "P4 wire dialogs differ from the actual native/peer route")
            call.bind_dialogs(initial)
            call.finish(pcap=writer.output, writer_receipt=writer.output.with_suffix(".pcap.writer.json"),
                        sip_ports=(self.args.sip_port, 5060))
        return self.measurement.finish()
