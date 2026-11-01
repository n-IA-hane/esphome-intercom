#!/usr/bin/env python3
"""Owned SIP qualification network: client, NAT router and server namespaces.

The topology has no interface or route to the household network. Namespace
commands reuse maintained SIP actors; creating a topology alone proves no SIP
or physical endpoint behavior.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import AbstractContextManager
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import signal
import shutil
import subprocess
import sys
import time


CLIENT_IP = "10.203.238.2"
CLIENT_GATEWAY = "10.203.238.1"
SERVER_IP = "10.203.239.2"
NAT_IP = "10.203.239.1"


def validate_native_idle_capture(output: Path, interval: int) -> None:
    """Separate continuous native keepalive proof from an idle negative control."""
    summary = {}
    for side, client in (("rc0", CLIENT_IP), ("rs0", NAT_IP)):
        capture = output / (side + ".pcap")
        subprocess.run(["capinfos", "-c", str(capture)], capture_output=True, text=True, check=True)
        fields = ["ip.src", "udp.srcport", "tcp.srcport", "sip.Method", "sip.Call-ID", "sip.Expires", "sip.Content-Length",
                  "sip.CSeq.seq", "sip.Accept"]
        command = ["tshark", "-r", str(capture), "-Y", "sip and not icmp and not icmpv6", "-T", "fields"]
        for field in fields:
            command.extend(("-e", field))
        rows = [dict(zip(fields, line.split("\t"))) for line in subprocess.run(command,
            capture_output=True, text=True, check=True).stdout.splitlines()]
        registrations = [item for item in rows if item["ip.src"] == client and item["sip.Method"] == "REGISTER"]
        if {item["sip.Expires"] for item in registrations if item["sip.Expires"] != "0"} != {"3600"}:
            raise RuntimeError("Keepalive qualification changed the native REGISTER expiry")
        tuples = {}
        registration_sequences = {}
        for item in registrations:
            tuples.setdefault(item["sip.Call-ID"], set()).add((item["udp.srcport"], item["tcp.srcport"]))
            if item["sip.Expires"] != "0" and item["sip.CSeq.seq"].isdigit():
                protocol = "udp" if item["udp.srcport"] else "tcp"
                registration_sequences.setdefault((protocol, item["sip.Call-ID"]), set()).add(int(item["sip.CSeq.seq"]))
        probes = [item for item in rows if item["ip.src"] == client and item["sip.Method"] == "OPTIONS"]
        if any(item["sip.Content-Length"] != "0" or item["sip.Expires"]
                or (item["udp.srcport"], item["tcp.srcport"]) not in tuples.get(item["sip.Call-ID"], set()) for item in probes):
            raise RuntimeError("Native keepalive did not reuse its REGISTER signaling tuple with an empty body")
        protocols = Counter("udp" if item["udp.srcport"] else "tcp" for item in probes)
        purposes = {}
        for item in probes:
            protocol = "udp" if item["udp.srcport"] else "tcp"
            sequence = item["sip.CSeq.seq"]
            accept = item["sip.Accept"].strip().casefold()
            if not sequence.isdigit() or accept not in {"", "application/sdp"}:
                raise RuntimeError("Native OPTIONS has an unknown purpose or CSeq")
            identity = (protocol, item["sip.Call-ID"], int(sequence))
            discovery = bool(accept)
            if discovery and int(sequence) - 1 not in registration_sequences.get(identity[:2], set()):
                raise RuntimeError("Native capability discovery did not immediately follow REGISTER CSeq")
            if identity in purposes and purposes[identity] != discovery:
                raise RuntimeError("Native OPTIONS CSeq changed its purpose")
            purposes[identity] = discovery
        discovery = Counter(key[0] for key, capability in purposes.items() if capability)
        periodic = Counter(key[0] for key, capability in purposes.items() if not capability)
        if (interval == 0 and periodic) or (interval and any(periodic[mode] < 3 for mode in ("udp", "tcp"))):
            raise RuntimeError("Native OPTIONS cadence does not match the enabled/disabled idle case")
        summary[side] = {"native_options_by_transport": dict(protocols), "register_expiry_seconds": 3600,
            "capability_discovery_by_transport": dict(discovery), "periodic_keepalive_by_transport": dict(periodic),
            "same_registration_signaling_tuple": True, "native_options_have_no_sdp": True}
    results = {mode: json.loads((output / "ha-wire" / mode / "result.json").read_text()) for mode in ("udp", "tcp")}
    for mode, item in results.items():
        expected = None if interval == 0 and mode == "udp" else 200
        if (item.get("passed") is not True or item.get("initial_request_status") != 200
                or "after_idle_request_status" not in item or item["after_idle_request_status"] != expected
                or item.get("native_probe_exit_code") != 0):
            raise RuntimeError("The native post-idle request witness did not pass")
    private_json(output / "native-idle-integrity.json", {"passed": True,
        "keepalive_interval_seconds": interval, "namespace_udp_idle_timeout_seconds": 5,
        "idle_window_seconds": 8, "wire": summary, "results": results,
        "scope": "native production REGISTER/signaling with real HA and isolated NAT, not ESP media/hardware",
        "disabled_udp_retains_original_unreachable_behavior": interval == 0, "physical_esp_qualified": False})


def validate_registration_captures(output: Path, *, require_rebind: bool = False, production_engine: bool = True) -> None:
    """Observe native REGISTER Contact, source translation and response flow."""
    fields = ["ip.src", "ip.dst", "udp.srcport", "udp.dstport", "tcp.srcport", "sip.Method", "sip.Status-Code",
        "sip.Call-ID", "sip.CSeq.seq", "sip.Contact", "sip.Expires", "sip.Via.received", "sip.Via.rport"]
    requests, evidence, udp_flows = {}, {}, {}
    for side, client_ip in (("rc0", CLIENT_IP), ("rs0", NAT_IP)):
        capture = output / (side + ".pcap")
        subprocess.run(["capinfos", "-c", str(capture)], capture_output=True, text=True, check=True)
        command = ["tshark", "-r", str(capture), "-Y", 'sip.CSeq.method == "REGISTER"', "-T", "fields"]
        for field in fields:
            command.extend(("-e", field))
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        rows = [dict(zip(fields, line.split("\t"))) for line in result.stdout.splitlines()]
        sent = [item for item in rows if item["ip.src"] == client_ip and item["sip.Method"] == "REGISTER"]
        if not sent or any(CLIENT_IP not in item["sip.Contact"] for item in sent):
            raise RuntimeError("The native client did not advertise its actual private Contact")
        protocols = {"udp" if item["udp.srcport"] else "tcp" for item in sent}
        if protocols != {"udp", "tcp"}:
            raise RuntimeError("Both native UDP and TCP registration flows are required")
        requests[side] = Counter((item["sip.Call-ID"], item["sip.CSeq.seq"], item["sip.Contact"]) for item in sent)
        udp_flows[side] = {}
        for item in sent:
            if item["udp.srcport"] and item["sip.Expires"] != "0":
                flow = udp_flows[side].setdefault(item["sip.Call-ID"], {"source_ports": set(), "contacts": set()})
                flow["source_ports"].add(item["udp.srcport"])
                flow["contacts"].add(item["sip.Contact"])
        responses = [item for item in rows if item["ip.src"] == SERVER_IP and item["ip.dst"] == client_ip
            and item["sip.Status-Code"] == "200"]
        if not responses:
            raise RuntimeError("No successful registrar response crossed the NAT")
        if side == "rs0" and not any(item["udp.srcport"] and item["sip.Via.received"] == NAT_IP
                and item["sip.Via.rport"].isdigit() for item in responses):
            raise RuntimeError("The observed UDP response does not identify received/rport")
        evidence[side] = {"register_requests": len(sent), "success_responses": len(responses),
            "transport_protocols": sorted(protocols), "source_ip": client_ip,
            "private_contact_preserved": True}
    if requests["rc0"] != requests["rs0"]:
        raise RuntimeError("Native REGISTER transactions differ across the NAT boundary")
    rebindings = []
    for call_id, outer in udp_flows["rs0"].items():
        inner = udp_flows["rc0"][call_id]
        if len(outer["source_ports"]) > 1:
            if len(inner["source_ports"]) != 1 or len(inner["contacts"]) != 1:
                raise RuntimeError("A changed client socket or Contact cannot qualify a NAT rebind")
            rebindings.append({"private_source_port": next(iter(inner["source_ports"])),
                "translated_source_ports": sorted(outer["source_ports"]), "same_private_contact": True})
    if require_rebind and not rebindings:
        raise RuntimeError("No real NAT port rebind was observed on an unchanged native client socket")
    private_json(output / ("native-registration-integrity.json" if production_engine else "ha-registration-integrity.json"), {"passed": True, "actual_snat": True,
        "client_private_ip": CLIENT_IP, "translated_source_ip": NAT_IP, "wire": evidence,
        "udp_rebindings": rebindings,
        "scope": ("native production REGISTER engine with independent PBX, not ESP media or hardware"
            if production_engine else "ordinary SIPp account registered to real HA, not native ESP registration"),
        "physical_esp_qualified": False})


def validate_captures(output: Path) -> None:
    """Check the actual translated wire, clocks, PCM and payload preservation."""
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "tools"))
    import rtp_pcap_evidence as evidence

    identities = {}
    for side, client_ip in (("rc0", CLIENT_IP), ("rs0", NAT_IP)):
        capture = output / (side + ".pcap")
        subprocess.run(["capinfos", "-c", str(capture)], check=True, capture_output=True, text=True)
        streams = evidence.analyze_pcap(capture, pcm_dir=output / (side + "-decoded-pcm"))
        failures = evidence.evaluate_streams(streams, require_streams=8,
            max_audio_loss=0, max_video_loss=0, max_cadence_ratio=1.25)
        media = [item for item in streams if item.get("media_type") not in {"rtp-keepalive", "telephone-event"}
            and (item["source"].startswith(client_ip + ":") or item["destination"].startswith(client_ip + ":"))]
        if len(media) != 8:
            failures.append("Each UDP/TCP call must retain both audio and both video directions")
        for item in media:
            if item.get("media_type") == "audio":
                rate = 16000 if item["source"].startswith(client_ip + ":") else 48000
                if (item.get("encoding") != "L16" or item.get("clock_rate") != rate
                        or item.get("timestamp_step") != rate // 100 or not item.get("pcm_wav")):
                    failures.append("The actual PCM wire does not retain its directional 10ms clock")
            elif item.get("media_type") != "video" or item["payload_type"] != 26:
                failures.append("The video wire is not the negotiated RTP/JPEG stream")
        result = subprocess.run(["tshark", "-r", str(capture), "-Y", "rtp and not icmp and not icmpv6",
            "-T", "fields", "-e", "ip.src", "-e", "ip.dst", "-e", "udp.payload"],
            capture_output=True, text=True, check=True)
        packets = Counter()
        startup_private = 0
        for row in result.stdout.splitlines():
            fields = row.split("\t")
            if len(fields) != 3:
                continue
            source, target, payload = fields
            if source == client_ip and target == SERVER_IP:
                direction = "client_to_server"
            elif source == SERVER_IP and target == client_ip:
                direction = "server_to_client"
            else:
                startup_private += int(side == "rs0" and target == CLIENT_IP)
                continue
            packets[direction, hashlib.sha256(bytes.fromhex(payload.replace(":", ""))).hexdigest()] += 1
        identities[side] = packets
        private_json(output / (side + "-rtp.json"), {"streams": streams, "failures": failures,
            "initial_private_destination_packets_before_latching": startup_private})
        if failures:
            raise RuntimeError("NAT packet/PCM validation failed: " + ", ".join(failures))
    preserved = identities["rc0"] == identities["rs0"]
    private_json(output / "nat-integrity.json", {"rtp_bytes_preserved_across_translation": preserved,
        "client_private_ip": CLIENT_IP, "actual_translated_ip": NAT_IP,
        "packets_by_direction": {direction: sum(value for (key, _hash), value in identities["rc0"].items() if key == direction)
                                 for direction in ("client_to_server", "server_to_client")},
        "physical_esp_qualified": False})
    if not preserved:
        raise RuntimeError("The two NAT capture sides do not contain the same translated RTP packets")


def rebind_after_registration(lab, output: Path, process, *, conntrack: Path | None = None) -> None:
    """Change only the owned NAT's future UDP mapping after a real 200 OK."""
    deadline = time.monotonic() + 15
    display_filter = ('udp && ip.src == ' + NAT_IP + ' && sip.Status-Code == 200 && sip.CSeq.method == "OPTIONS"'
        if conntrack is not None else 'udp && sip.Status-Code == 200 && sip.CSeq.method == "REGISTER"')
    while time.monotonic() < deadline and process.poll() is None:
        result = subprocess.run(["tshark", "-r", str(output / "rs0.pcap"), "-Y",
            display_filter,
            "-T", "fields", "-e", "frame.number"], capture_output=True, text=True)
        if result.returncode == 0 and len(result.stdout.splitlines()) >= (2 if conntrack is not None else 1):
            break
        time.sleep(.25)
    else:
        raise RuntimeError("The native registrar did not establish a UDP binding before NAT rebind")
    # The existing authenticated mapping expires on the namespace's 5s idle
    # timer. Its next real refresh uses a new external source port, while the
    # native socket and private Contact remain unchanged. Forward rules do not change.
    subnet = ipaddress.ip_network(CLIENT_IP + "/30", strict=False)
    lab.execute("router", ["nft", "-f", "-"], input=f"""flush chain ip sipq_nat postrouting
add rule ip sipq_nat postrouting oifname "rs0" ip saddr {subnet} meta l4proto udp counter snat to {NAT_IP}:42000-42009
add rule ip sipq_nat postrouting oifname "rs0" ip saddr {subnet} counter snat to {NAT_IP}
""")
    if conntrack is not None:
        result = subprocess.run(["tshark", "-r", str(output / "rc0.pcap"), "-Y",
            f'udp && ip.src == {CLIENT_IP} && sip.Method == "REGISTER"', "-T", "fields", "-e", "udp.srcport"],
            capture_output=True, text=True, check=True)
        ports = set(result.stdout.splitlines())
        if len(ports) != 1 or not next(iter(ports)).isdigit():
            raise RuntimeError("Active NAT rebind did not identify one native private UDP socket")
        port = next(iter(ports))
        deleted = lab.execute("router", [conntrack, "-D", "-p", "udp", "--orig-src", CLIENT_IP,
            "--orig-dst", SERVER_IP, "--sport", port, "--dport", "15060"])
        if len(deleted.stdout.splitlines()) != 1:
            raise RuntimeError("Active NAT rebind did not evict exactly one owned UDP mapping")
    private_json(output / "rebind-stimulus.json", {"after_real_udp_register_200": True,
        "namespace_udp_idle_timeout_seconds": 5, "future_udp_external_ports": [42000, 42009],
        "forced_single_owned_udp_mapping_eviction": conntrack is not None,
        "client_socket_or_contact_changed": False, "forward_filter_changed": False})


def private_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


class SipNatLab(AbstractContextManager):
    """Own only freshly created namespaces and explicitly launched processes."""

    def __init__(self, output: Path, *, identity: str | None = None, mtu: int = 1500):
        identity = identity or secrets.token_hex(4)
        if re.fullmatch(r"[a-z0-9]{1,12}", identity) is None:
            raise ValueError("NAT qualification identity must be 1-12 lowercase letters or digits")
        if mtu not in {1280, 1500}:
            raise ValueError("Qualification MTU must be 1280 or 1500")
        self.mtu = mtu
        self.output = output.resolve()
        self.names = {role: f"sipq-{identity}-{role}" for role in ("client", "router", "server")}
        self.created: list[str] = []
        self.processes: list[tuple[str, str, subprocess.Popen]] = []
        self.commands: list[list[str]] = []

    def run(self, command, *, input: str | None = None, timeout: float = 15):
        command = [str(item) for item in command]
        self.commands.append(command)
        return subprocess.run(command, input=input, capture_output=True, text=True, check=True, timeout=timeout)

    def prefix(self, role: str) -> list[str]:
        return ["ip", "netns", "exec", self.names[role]]

    def execute(self, role: str, command, **kwargs):
        return self.run([*self.prefix(role), *command], **kwargs)

    def host_network(self) -> dict[str, str]:
        def stable(value, parent=""):
            if isinstance(value, dict):
                return {key: stable(item, key) for key, item in value.items()
                    if key not in {"stats", "stats64", "cacheinfo"}
                    and not (parent == "counter" and key in {"packets", "bytes"})}
            if isinstance(value, list):
                return [stable(item, parent) for item in value]
            return value
        return {name: hashlib.sha256(json.dumps(stable(json.loads(self.run(command).stdout)), sort_keys=True).encode()).hexdigest() for name, command in (
            ("interfaces", ["ip", "-j", "link", "show"]),
            ("routes", ["ip", "-j", "route", "show", "table", "all"]),
            ("firewall", ["nft", "-j", "list", "ruleset"]),
            ("udp_timeout", ["sysctl", "-n", "net.netfilter.nf_conntrack_udp_timeout"]),
            ("udp_stream_timeout", ["sysctl", "-n", "net.netfilter.nf_conntrack_udp_timeout_stream"]),
        )}

    def __enter__(self):
        if os.geteuid() != 0:
            raise PermissionError("Run the isolated NAT qualification with sudo -n")
        self.output.mkdir(parents=True, mode=0o700)
        existing = {line.split()[0] for line in self.run(["ip", "netns", "list"]).stdout.splitlines() if line.strip()}
        if existing & set(self.names.values()):
            raise RuntimeError("A requested namespace already exists; refusing to adopt it")
        self.before = self.host_network()
        try:
            for name in self.names.values():
                self.run(["ip", "netns", "add", name])
                self.created.append(name)
            # Create both veth pairs directly in the owned namespaces. No host
            # interface, default route or firewall rule is ever installed.
            self.execute("client", ["ip", "link", "add", "c0", "type", "veth", "peer", "name", "rc0", "netns", self.names["router"]])
            self.execute("server", ["ip", "link", "add", "s0", "type", "veth", "peer", "name", "rs0", "netns", self.names["router"]])
            for role, interface, address in (("client", "c0", CLIENT_IP), ("router", "rc0", CLIENT_GATEWAY),
                                               ("router", "rs0", NAT_IP), ("server", "s0", SERVER_IP)):
                self.execute(role, ["ip", "address", "add", address + "/30", "dev", interface])
                self.execute(role, ["ip", "link", "set", interface, "mtu", str(self.mtu)])
                self.execute(role, ["ip", "link", "set", interface, "up"])
            for role in self.names:
                self.execute(role, ["ip", "link", "set", "lo", "up"])
            self.execute("client", ["ip", "route", "add", "default", "via", CLIENT_GATEWAY])
            self.execute("server", ["ip", "route", "add", "default", "via", NAT_IP])
            self.execute("router", ["sysctl", "-q", "-w", "net.ipv4.ip_forward=1"])
            self.execute("router", ["nft", "-f", "-"], input=f"""table inet sipq_filter {{
 chain forward {{ type filter hook forward priority filter; policy drop;
  ct state established,related counter accept
  iifname "rc0" oifname "rs0" ip daddr {SERVER_IP} counter accept
 }}
}}
table ip sipq_nat {{
 chain postrouting {{ type nat hook postrouting priority srcnat; policy accept;
  oifname "rs0" ip saddr {ipaddress.ip_network(CLIENT_IP + '/30', strict=False)} counter snat to {NAT_IP}
 }}
}}
""")
            self.verify_isolation()
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def verify_isolation(self):
        for role, expected in (("client", {"lo", "c0"}), ("router", {"lo", "rc0", "rs0"}), ("server", {"lo", "s0"})):
            interfaces = json.loads(self.execute(role, ["ip", "-j", "link", "show"]).stdout)
            if {item["ifname"].split("@")[0] for item in interfaces} != expected:
                raise RuntimeError("Unexpected interface in the owned NAT qualification topology")
        if self.host_network() != self.before:
            raise RuntimeError("Host network changed during isolated topology creation")

    def start_actor(self, role: str, command, *, label: str = "actor", env=None):
        """Launch an existing actor in one owned namespace and process group."""
        if re.fullmatch(r"[a-z0-9_-]+", label) is None:
            raise ValueError("Invalid actor label")
        fd = os.open(self.output / (label + ".log"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as log:
            process = subprocess.Popen([*self.prefix(role), *map(str, command)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=env)
            self.processes.append((role, label, process))
            return process

    def run_actor(self, role: str, command, *, timeout: float = 240, label: str = "actor", env=None):
        """Bound one existing actor process tree to this qualification window."""
        process = self.start_actor(role, command, label=label, env=env)
        try:
            status = process.wait(timeout=timeout)
            if status:
                raise RuntimeError(f"SIP qualification actor {label} failed with exit {status}")
        finally:
            self._stop_actor(role, process)

    def _group_members(self, role, group):
        members = []
        # A server-side test may launch a maintained actor in the client
        # namespace. It retains our process group across that namespace jump.
        for name in self.created:
            for value in self.run(["ip", "netns", "pids", name]).stdout.split():
                try:
                    if os.getpgid(int(value)) == group:
                        members.append(int(value))
                except ProcessLookupError:
                    pass
        return members

    def _stop_actor(self, role, process):
        group = process.pid
        if process.poll() is None or self._group_members(role, group):
            try:
                os.killpg(group, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(group, signal.SIGKILL)
            process.wait(timeout=5)
        deadline = time.monotonic() + 5
        while self._group_members(role, group) and time.monotonic() < deadline:
            time.sleep(.05)
        if self._group_members(role, group):
            os.killpg(group, signal.SIGKILL)

    def __exit__(self, exception_type=None, exception=None, traceback=None):
        failures = []
        for role, _label, process in reversed(self.processes):
            try:
                self._stop_actor(role, process)
            except (OSError, subprocess.SubprocessError) as error:
                failures.append({"stage": "actor_stop", "namespace": self.names[role], "pgid": process.pid, "type": type(error).__name__})
        for name in reversed(self.created.copy()):
            try:
                if self.run(["ip", "netns", "pids", name]).stdout.strip():
                    raise RuntimeError("Owned namespace has untracked processes; preserving it")
                self.run(["ip", "netns", "delete", name])
                self.created.remove(name)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                failures.append({"stage": "namespace_delete", "namespace": name, "type": type(error).__name__})
        after = None
        try:
            after = self.host_network() if hasattr(self, "before") else None
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            failures.append({"stage": "host_snapshot_after", "type": type(error).__name__})
        if hasattr(self, "before") and after != self.before:
            failures.append({"stage": "host_config_compare", "type": "HostConfigurationChanged"})
        private_json(self.output / "topology.json", {
            "namespaces": self.names, "client_private_ip": CLIENT_IP, "translated_source_ip": NAT_IP,
            "server_ip": SERVER_IP, "household_links": False, "commands": self.commands,
            "mtu": self.mtu,
            "host_network_unchanged": after == getattr(self, "before", None) if after is not None else None,
            "remaining_owned_namespaces": self.created, "cleanup_errors": failures,
            "actors": [{"namespace": self.names[role], "label": label, "pid": process.pid,
                        "pgid": process.pid, "return_code": process.returncode, "command": process.args}
                       for role, label, process in self.processes],
        })
        if failures:
            if exception is not None:
                exception.add_note("NAT qualification cleanup: " + ", ".join(item["stage"] for item in failures))
            else:
                raise RuntimeError("NAT qualification cleanup failed: " + ", ".join(item["stage"] for item in failures))
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--connectivity-only", action="store_true", help="Prove namespace isolation and real SNAT, not SIP")
    parser.add_argument("--ha-wire", action="store_true", help="Run the real HA fixture, ordinary SIPp account and JPEG peer across NAT")
    parser.add_argument("--native-ha-idle", action="store_true", help="Check native REGISTER3600 reachability beyond the isolated NAT idle timer")
    parser.add_argument("--native-keepalive-interval", type=int, choices=(0, 2), default=2,
        help="Native idle qualification: zero disables the feature, two seconds precedes the 5s NAT timeout")
    parser.add_argument("--pbx-registration", choices=("asterisk", "freeswitch"),
        help="Reuse the maintained native REGISTER qualifier against an isolated independent PBX")
    parser.add_argument("--probe", type=Path, help="Maintained native registration probe, built on persistent disk")
    parser.add_argument("--freeswitch", type=Path, help="Explicit native FreeSWITCH executable, independent of sudo's home")
    parser.add_argument("--conntrack", type=Path, help="Maintained conntrack executable for active-flow eviction only in the owned router namespace")
    parser.add_argument("--refresh", action="store_true", help="Observe the native engine's real 60s registration renewal")
    parser.add_argument("--rebind", action="store_true", help="Expire and replace the private NAT's UDP mapping before a real refresh")
    parser.add_argument("--mtu", type=int, choices=(1280, 1500), default=1500)
    parser.add_argument("--options-body", choices=("native", "empty"), default="native",
        help="Native captured capability body, or a normal 200 response without a body")
    args = parser.parse_args()
    if sum((args.connectivity_only, args.ha_wire, args.native_ha_idle, bool(args.pbx_registration))) != 1:
        parser.error("Select an implemented qualification phase")
    if (args.pbx_registration or args.native_ha_idle) and (args.probe is None or not args.probe.is_file()):
        parser.error("Native REGISTER qualification needs an existing --probe binary")
    if args.pbx_registration == "freeswitch" and (args.freeswitch is None or not args.freeswitch.is_file()):
        parser.error("FreeSWITCH qualification needs an explicit existing --freeswitch executable")
    if args.native_ha_idle and args.rebind and (args.conntrack is None or not args.conntrack.is_file() or args.native_keepalive_interval == 0):
        parser.error("Active native idle/rebind needs --conntrack and an enabled keepalive")
    if args.rebind and not (args.ha_wire or args.native_ha_idle or (args.pbx_registration and args.refresh)):
        parser.error("A real UDP NAT rebind requires HA wire or native PBX registration with --refresh")
    required = ["ip", "nft", "ping"]
    if args.ha_wire or args.native_ha_idle or args.pbx_registration:
        required.extend(("tcpdump", "tshark", "capinfos"))
    if args.ha_wire:
        required.extend(("sipp", "ffmpeg", "ffprobe"))
    if missing := [name for name in required if shutil.which(name) is None]:
        parser.error("Required maintained tools are missing: " + ", ".join(missing))
    with SipNatLab(args.output, mtu=args.mtu) as lab:
        lab.execute("client", ["ping", "-c", "1", "-W", "2", SERVER_IP])
        rules = json.loads(lab.execute("router", ["nft", "-j", "list", "table", "ip", "sipq_nat"]).stdout)
        translated = any(expression.get("counter", {}).get("packets", 0) > 0
            for item in rules["nftables"] if "rule" in item for expression in item["rule"]["expr"])
        if not translated:
            raise RuntimeError("The NAT rule did not translate the connectivity witness")
        private_json(args.output / "connectivity.json", {"passed": True, "actual_snat": True,
            "sip_qualified": False, "physical_esp_qualified": False, "nat_rules": rules})
        if args.ha_wire or args.native_ha_idle or args.pbx_registration:
            root = Path(__file__).resolve().parents[2]
            for interface in ("rc0", "rs0"):
                capture = args.output / (interface + ".pcap")
                fd = os.open(capture, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
                process = lab.start_actor("router", ["tcpdump", "--immediate-mode", "-n", "-i", interface,
                    "-U", "-s", "0", "-w", str(capture)], label="capture-" + interface)
                deadline = time.monotonic() + 5
                while b"listening on" not in (args.output / ("capture-" + interface + ".log")).read_bytes():
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("Owned NAT packet writer did not become ready")
                    time.sleep(.05)
            if args.rebind or args.native_ha_idle:
                lab.execute("router", ["sysctl", "-q", "-w", "net.netfilter.nf_conntrack_udp_timeout=5",
                    "net.netfilter.nf_conntrack_udp_timeout_stream=5"])
            if args.ha_wire or args.native_ha_idle:
                env = {**os.environ, "SIP_NAT_CLIENT_NS": lab.names["client"], "SIP_NAT_OUTPUT": str(args.output / "ha-wire"),
                    "SIP_NAT_OPTIONS_BODY": args.options_body, "SIP_NAT_REBIND": str(int(args.rebind))}
                if args.native_ha_idle:
                    env["SIP_NAT_NATIVE_PROBE"] = str(args.probe.resolve())
                    env["SIP_NAT_NATIVE_KEEPALIVE_INTERVAL"] = str(args.native_keepalive_interval)
                module = "sip_nat_keepalive_case.py" if args.native_ha_idle else "sip_nat_wire_case.py"
                command = [root.parent / "ha-voip-lab/.venv/bin/python", "-m", "pytest", "-q",
                    root / "tools/ha_voip_lab" / module, "--tb=short", "--show-capture=no"]
                label, timeout = "ha-wire", 100
            else:
                env = None
                tool = "qualify_sip_registration" + ("_freeswitch" if args.pbx_registration == "freeswitch" else "") + ".py"
                command = [sys.executable, root.parent / "esphome-voip-stack/tools" / tool,
                    "--probe", args.probe.resolve(), "--bind-ip", SERVER_IP,
                    "--probe-netns", lab.names["client"], "--output", args.output / args.pbx_registration]
                if args.refresh:
                    command.append("--refresh")
                if args.pbx_registration == "freeswitch":
                    command.extend(("--freeswitch", args.freeswitch.resolve()))
                label, timeout = "native-registration", 240
            process = lab.start_actor("server", command, label=label, env=env)
            try:
                if args.rebind:
                    rebind_after_registration(lab, args.output, process, conntrack=args.conntrack if args.native_ha_idle else None)
                if process.wait(timeout=timeout):
                    raise RuntimeError("SIP qualification actor failed: " + label)
            finally:
                lab._stop_actor("server", process)
            private_json(args.output / "nat-rules-after.json", json.loads(lab.execute("router",
                ["nft", "-j", "list", "table", "ip", "sipq_nat"]).stdout))
    if args.ha_wire:
        validate_captures(args.output)
        validate_registration_captures(args.output, require_rebind=args.rebind, production_engine=False)
    elif args.native_ha_idle:
        validate_native_idle_capture(args.output, args.native_keepalive_interval)
    elif args.pbx_registration:
        validate_registration_captures(args.output, require_rebind=args.rebind)
    print(json.dumps({"connectivity_only": args.connectivity_only, "passed": True,
        "ha_wire_actor_passed": args.ha_wire, "native_ha_idle_passed": args.native_ha_idle, "physical_esp_qualified": False}))


if __name__ == "__main__":
    main()
