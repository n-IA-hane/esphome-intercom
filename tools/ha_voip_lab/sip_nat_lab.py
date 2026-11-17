"""Owned kernel NAT fixture, without household links or external PBXs."""

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
import shutil
import signal
import struct
import subprocess
import time
import sys


CLIENT_IP = "10.203.238.2"


CLIENT_GATEWAY = "10.203.238.1"


SERVER_IP = "10.203.239.2"


NAT_IP = "10.203.239.1"


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


def validate_snat_captures(output):
    """Compare original UDP payloads on both independently captured NAT sides."""
    sides, outer_stun = {}, {}
    registration_requests = 0
    flow_timer_responses = 0
    for interface, source in (("rc0", CLIENT_IP), ("rs0", NAT_IP)):
        rows = subprocess.run(["tshark", "-r", str(output / (interface + ".pcap")),
            "-Y", "udp and not icmp", "-T", "fields", "-e", "ip.src", "-e", "ip.dst",
            "-e", "udp.srcport", "-e", "udp.dstport", "-e", "udp.payload"],
            capture_output=True, text=True, check=True).stdout.splitlines()
        forwarded, returned = Counter(), Counter()
        for row in rows:
            fields = row.split("\t")
            if len(fields) != 5:
                continue
            src, dst, sport, dport, payload = fields
            data = bytes.fromhex(payload.replace(":", ""))
            digest = hashlib.sha256(data).hexdigest()
            if src == source and dst == SERVER_IP:
                forwarded[digest] += 1
                if data.startswith(b"REGISTER "):
                    assert b"\r\nExpires: 120\r\n" in data and CLIENT_IP.encode() in data
                    registration_requests += int(interface == "rs0")
                if interface == "rs0" and data[:2] == b"\0\1":
                    outer_stun[data[8:20]] = (src, int(sport))
            elif src == SERVER_IP and dst == source:
                returned[digest] += 1
                if data.startswith(b"SIP/2.0 200") and b" REGISTER\r\n" in data:
                    assert b"\r\nFlow-Timer: 25\r\n" in data
                    flow_timer_responses += int(interface == "rs0")
                if interface == "rs0" and data[:2] == b"\1\1":
                    attrs, offset = {}, 20
                    while offset < len(data):
                        kind, length = struct.unpack_from("!HH", data, offset)
                        attrs[kind] = data[offset + 4:offset + 4 + length]
                        offset += 4 + ((length + 3) & ~3)
                    mapped = attrs[0x0020]
                    host = ".".join(str(a ^ b) for a, b in zip(mapped[4:], bytes.fromhex("2112a442")))
                    port = int.from_bytes(mapped[2:4], "big") ^ 0x2112
                    assert (host, port) == outer_stun[data[8:20]]
        sides[interface] = forwarded, returned
    assert sides["rc0"][0] == sides["rs0"][0]
    assert registration_requests == 8 and flow_timer_responses == 4 and len(outer_stun) >= 3
    not_forwarded = sum((sides["rs0"][1] - sides["rc0"][1]).values())
    assert not_forwarded >= 2
    return {"passed": True, "client_udp_payloads_preserved_across_snat": sum(sides["rc0"][0].values()),
        "register_requests": registration_requests, "flow_timer_25_responses": flow_timer_responses,
        "stun_transactions_with_verified_xor_source": len(outer_stun),
        "server_packets_not_forwarded_after_expiry": not_forwarded}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--ha-python", type=Path, required=True)
    parser.add_argument("--build-parent", type=Path, required=True)
    parser.add_argument("--exercise-failure-cleanup", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    ha_python, normal_python = args.ha_python.absolute(), args.python.absolute()
    args.output = args.output.resolve()
    for executable in (ha_python, normal_python):
        if not executable.is_file() or not os.access(executable, os.X_OK):
            parser.error(f"Python executable is unavailable: {executable}")
    required = ("ip", "nft", "sysctl", "findmnt", "tcpdump", "capinfos", "tshark")
    if missing := [name for name in required if shutil.which(name) is None]:
        parser.error("Missing qualification prerequisites: " + ", ".join(missing))
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("The output directory must be new or empty")
    build_parent = args.build_parent.resolve()
    if build_parent == Path("/tmp") or Path("/tmp") in build_parent.parents:
        parser.error("Build storage must be persistent, outside /tmp")
    build_parent.mkdir(parents=True, exist_ok=True)
    filesystem = subprocess.run(["findmnt", "-T", str(build_parent), "-n", "-o", "FSTYPE"],
        text=True, capture_output=True, check=True).stdout.strip()
    if filesystem in {"tmpfs", "ramfs"}:
        parser.error("Build storage must use a persistent filesystem")
    basetemp = build_parent / args.output.name
    if basetemp.exists():
        parser.error("Refusing to replace an existing qualification build directory")
    subprocess.run([ha_python, "-c", "import pytest, homeassistant"], check=True, capture_output=True)
    try:
        with SipNatLab(args.output) as lab:
            if args.exercise_failure_cleanup:
                lab.start_actor("client", [normal_python, "-c", "import time; time.sleep(30)"], label="cleanup-witness")
                raise RuntimeError("Intentional NAT cleanup witness")
            lab.execute("router", ["sysctl", "-q", "-w", "net.netfilter.nf_conntrack_udp_timeout=5",
                "net.netfilter.nf_conntrack_udp_timeout_stream=5"])
            for interface in ("rc0", "rs0"):
                capture = args.output / (interface + ".pcap")
                descriptor = os.open(capture, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)
                process = lab.start_actor("router", ["tcpdump", "--immediate-mode", "-n", "-i", interface,
                    "-U", "-s", "0", "-w", capture], label="capture-" + interface)
                deadline = time.monotonic() + 5
                while b"listening on" not in (args.output / ("capture-" + interface + ".log")).read_bytes():
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("NAT packet writer did not become ready")
                    time.sleep(.05)
            env = {**os.environ, "PYTHONPATH": str(root), "SIP_NAT_CLIENT_NS": lab.names["client"],
                "SIP_NAT_ROUTER_NS": lab.names["router"], "SIP_NAT_OUTPUT": str(args.output / "wire"),
                "SIP_NAT_HA_PYTHON": str(ha_python)}
            lab.run_actor("server", [ha_python, "-m", "pytest", "-q",
                root / "tools/ha_voip_lab/sip_nat_keepalive_case.py", "--tb=short",
                "--basetemp=" + str(basetemp),
                "--junitxml=" + str(args.output / "kernel-cases.xml")], timeout=100, label="ha-kernel-nat", env=env)
    except RuntimeError as error:
        if not args.exercise_failure_cleanup or str(error) != "Intentional NAT cleanup witness":
            raise
    topology = json.loads((args.output / "topology.json").read_text())
    if not topology["host_network_unchanged"] or topology["remaining_owned_namespaces"] or topology["cleanup_errors"]:
        raise RuntimeError("NAT topology did not return to its initial state")
    captures = {}
    if not args.exercise_failure_cleanup:
        for interface in ("rc0", "rs0"):
            capture = args.output / (interface + ".pcap")
            result = subprocess.run(["capinfos", "-c", str(capture)], capture_output=True, text=True, check=True)
            log = (args.output / ("capture-" + interface + ".log")).read_text()
            if "0 packets dropped by kernel" not in log:
                raise RuntimeError("Kernel NAT capture reports packet loss or no writer statistics")
            captures[interface] = {"capinfos": result.stdout, "sha256": hashlib.sha256(capture.read_bytes()).hexdigest()}
        private_json(args.output / "wire-integrity.json", validate_snat_captures(args.output))
    private_json(args.output / "result.json", {"passed": True, "host_network_unchanged": True,
        "owned_namespaces_removed": True, "failure_cleanup_witness": args.exercise_failure_cleanup,
        "captures": captures, "scope": "isolated kernel NAT and HA software only", "physical_esp_qualified": False})
    print(json.dumps({"passed": True, "output": str(args.output), "physical_esp_qualified": False}))


if __name__ == "__main__":
    main()

