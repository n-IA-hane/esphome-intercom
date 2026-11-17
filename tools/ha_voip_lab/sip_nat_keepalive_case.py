"""Real HA registrar and an independent SIP/STUN peer across isolated SNAT."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import struct
import subprocess
import sys
import time
from unittest.mock import AsyncMock
import zlib

import pytest
import pytest_socket

from custom_components.voip_stack.core import sip
from custom_components.voip_stack.core.sip_auth import build_digest_authorization
from tools.ha_voip_lab.sip_nat_lab import CLIENT_IP, NAT_IP, SERVER_IP, private_json
from tests.test_automation_originate_ha import lab as lab


pytestmark = pytest.mark.ha
COOKIE = 0x2112A442


def state_json(path, value):
    """Publish mutable cross-namespace state atomically; receipts stay immutable."""
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream)
    temporary.replace(path)


class NatPeer(asyncio.DatagramProtocol):
    """Wire responses and STUN parsing independent of the HA responder."""

    def __init__(self, username, password, output):
        self.username, self.password, self.output = username, password, output
        self.responses = asyncio.Queue()
        self.state = {"register_requests": 0, "options_received": 0, "stun_sent": 0,
            "stun_received": 0, "mapped": [], "errors": [], "registered": False}
        self.cseq = 0

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, raw, address):
        if raw[:1] in (b"\0", b"\1"):
            kind, length, cookie = struct.unpack_from("!HHI", raw)
            assert kind == 0x0101 and cookie == COOKIE and len(raw) == 20 + length
            attrs = {}
            offset = 20
            while offset < len(raw):
                attr, size = struct.unpack_from("!HH", raw, offset)
                attrs[attr] = raw[offset + 4:offset + 4 + size]
                if attr == 0x8028:
                    assert int.from_bytes(attrs[attr], "big") == zlib.crc32(raw[:offset]) ^ 0x5354554E
                offset += 4 + ((size + 3) & ~3)
            mapped = attrs[0x0020]
            assert mapped[:2] == b"\0\1"
            port = int.from_bytes(mapped[2:4], "big") ^ 0x2112
            host = ".".join(str(a ^ b) for a, b in zip(mapped[4:], COOKIE.to_bytes(4, "big")))
            self.state["mapped"].append([host, port])
            self.state["stun_received"] += 1
        else:
            message = sip.parse_message(raw)
            if message.is_response:
                self.responses.put_nowait(message)
            elif message.method == "OPTIONS":
                self.state["options_received"] += 1
                headers = [(name, message.header(name)) for name in ("Via", "From", "Call-ID", "CSeq")]
                headers.append(("To", message.header("To") + ";tag=nat-peer"))
                self.transport.sendto(sip.build_response(200, "OK", headers), address)
        self.save()

    def save(self):
        state_json(self.output / "peer.json", self.state)

    async def register(self):
        uri = f"sip:{SERVER_IP}:15060"
        contact = (f'<sip:{self.username}@{CLIENT_IP}:20600>;reg-id=1;'
            '+sip.instance="<urn:uuid:11111111-2222-3333-4444-555555555555>"')
        authorization = ""
        for _ in range(2):
            self.cseq += 1
            headers = [("Via", f"SIP/2.0/UDP {CLIENT_IP}:20600;branch=z9hG4bK-nat-{self.cseq};rport"),
                ("From", f"<sip:{self.username}@{SERVER_IP}>;tag=nat"), ("To", f"<sip:{self.username}@{SERVER_IP}>"),
                ("Call-ID", "nat-registration"), ("CSeq", f"{self.cseq} REGISTER"), ("Contact", contact),
                ("Expires", "120"), ("Supported", "outbound")]
            if authorization:
                headers.append(("Authorization", authorization))
            self.transport.sendto(sip.build_request("REGISTER", uri, headers), (SERVER_IP, 15060))
            self.state["register_requests"] += 1
            reply = await asyncio.wait_for(self.responses.get(), 3)
            if reply.status_code == 401:
                authorization = build_digest_authorization(challenge_header=reply.header("WWW-Authenticate"),
                    username=self.username, password=self.password, method="REGISTER", uri=uri)
            else:
                assert reply.status_code == 200 and reply.header("Require") == "outbound"
                assert reply.header("Flow-Timer") == "25"
                self.state.update(registered=True, cseq=self.cseq, flow_timer=25)
                self.save()
                return
        raise RuntimeError("Peer REGISTER did not complete")

    def stun(self):
        raw = struct.pack("!HHI", 1, 8, COOKIE) + secrets.token_bytes(12)
        raw += struct.pack("!HHI", 0x8028, 4, zlib.crc32(raw) ^ 0x5354554E)
        self.transport.sendto(raw, (SERVER_IP, 15060))
        self.state["stun_sent"] += 1
        self.save()


async def run_peer(output, mode, username):
    loop = asyncio.get_running_loop()
    transport, peer = await loop.create_datagram_endpoint(
        lambda: NatPeer(username, os.environ["SIP_NAT_FIXTURE_PASSWORD"], output), local_addr=(CLIENT_IP, 20600))
    try:
        await peer.register()
        sequence, next_keepalive = 0, time.monotonic()
        while True:
            command = json.loads((output / "command.json").read_text()) if (output / "command.json").exists() else {}
            if command.get("action") == "stop":
                return
            if command.get("sequence", 0) > sequence:
                sequence = command["sequence"]
                if command.get("action") == "register":
                    await peer.register()
            if mode == "enabled" and time.monotonic() >= next_keepalive:
                peer.stun()
                next_keepalive = time.monotonic() + 2
            await asyncio.sleep(.05)
    finally:
        peer.save()
        transport.close()


@pytest.mark.parametrize("mode", ["expired", "enabled", "rebind"])
async def test_real_kernel_nat_expiry_keepalive_and_authenticated_rebind(lab, monkeypatch, mode):
    from custom_components.voip_stack import endpoint_runtime, peer_snapshot
    from custom_components.voip_stack.const import CONF_REGISTRAR_ENABLED
    from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
    from custom_components.voip_stack.runtime_data import sip_endpoint_manager
    from custom_components.voip_stack.sip_registrar import SipAccount

    output = Path(os.environ["SIP_NAT_OUTPUT"]) / mode
    output.mkdir(parents=True, mode=0o700)
    pytest_socket.socket_allow_hosts(["127.0.0.1", CLIENT_IP, NAT_IP, SERVER_IP])
    lab.runtime.sip = lab.calls = SipEndpointRuntime(allow_dark_sessions=True)
    lab.runtime.transport_config.update(sip_port=15060, rtp_port=18000, advertise_host=SERVER_IP)
    lab.runtime.transport_config[CONF_REGISTRAR_ENABLED] = True
    monkeypatch.setattr(peer_snapshot, "async_advertise_host", AsyncMock(return_value=SERVER_IP))
    monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", AsyncMock(return_value=[]))
    assert await endpoint_runtime.async_start_sip_endpoint(lab.hass)
    registrar, manager = lab.calls.component("registrar"), sip_endpoint_manager(lab.hass)
    username, password = "nat-software-" + mode, secrets.token_hex(24)
    registrar.update_accounts([SipAccount(username, username, password)])
    namespace = os.environ["SIP_NAT_CLIENT_NS"]
    with (output / "peer.log").open("wb") as log:
        process = subprocess.Popen(["ip", "netns", "exec", namespace, os.environ["SIP_NAT_HA_PYTHON"],
            "-m", "tools.ha_voip_lab.sip_nat_keepalive_case", "peer", str(output), mode, username],
            stdout=log, stderr=subprocess.STDOUT, env={**os.environ, "SIP_NAT_FIXTURE_PASSWORD": password})

    async def wait_for(predicate, timeout=5):
        async with asyncio.timeout(timeout):
            while not predicate():
                assert process.poll() is None, (output / "peer.log").read_text()
                await asyncio.sleep(.05)

    def peer_state():
        return json.loads((output / "peer.json").read_text()) if (output / "peer.json").exists() else {}

    result = {"passed": False, "mode": mode, "nat_udp_timeout": 5, "registration_expires": 120,
        "flow_timer": 25, "client_keepalive_seconds": 2 if mode == "enabled" else 0}
    try:
        await wait_for(lambda: peer_state().get("registered") and not lab.runtime.capability_probes)
        initial, = registrar.registered_contacts(username)
        assert initial.source_host == NAT_IP and CLIENT_IP in initial.advertised_contact_uri
        selected = registrar.select_outbound_flow(username, sip.parse_sip_uri(initial.contact_uri))
        expiry, generation = initial.expires_at, initial.capability_generation
        initial_probe = await manager.async_options(initial.contact_uri,
            (initial.source_host, initial.source_port), "UDP", timeout=1)
        assert initial_probe is not None and initial_probe.status_code == 200
        before = peer_state()
        result["initial_binding"] = initial.snapshot()
        await asyncio.sleep(8)
        probe = await manager.async_options(initial.contact_uri,
            (initial.source_host, initial.source_port), "UDP", timeout=1)
        assert (probe is not None and probe.status_code == 200) == (mode == "enabled")
        assert registrar.registered_contacts(username)[0].expires_at == expiry
        assert registrar.registered_contacts(username)[0].cseq == 2
        state = peer_state()
        assert state["register_requests"] == before["register_requests"] == 2
        if mode == "enabled":
            assert state["stun_received"] >= 3 and state["options_received"] > before["options_received"]
            assert {tuple(item) for item in state["mapped"]} == {(NAT_IP, initial.source_port)}
        else:
            assert state["stun_sent"] == 0 and state["options_received"] == before["options_received"]
        if mode == "rebind":
            router = os.environ["SIP_NAT_ROUTER_NS"]
            rules = ("flush chain ip sipq_nat postrouting\n"
                f"add rule ip sipq_nat postrouting oifname rs0 ip saddr {CLIENT_IP}/32 meta l4proto udp snat to {NAT_IP}:42000-42009\n")
            subprocess.run(["ip", "netns", "exec", router, "nft", "-f", "-"], input=rules,
                text=True, check=True, capture_output=True)
            state_json(output / "command.json", {"sequence": 1, "action": "register"})
            await wait_for(lambda: registrar.registered_contacts(username)[0].cseq == 4)
            current, = registrar.registered_contacts(username)
            assert current.source_port != initial.source_port and 42000 <= current.source_port <= 42009
            assert current.advertised_contact_uri == initial.advertised_contact_uri
            assert current.capability_generation != generation and registrar.resolve_outbound_flow(selected) is None
            reply = await manager.async_options(current.contact_uri, (current.source_host, current.source_port), "UDP", timeout=1)
            assert reply is not None and reply.status_code == 200
            result["rebound_binding"] = current.snapshot()
        result.update(passed=True, peer=peer_state(), after_idle_status=probe.status_code if probe else None,
            register_expiry_unchanged=True, media_owners=0)
        assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")
    finally:
        state_json(output / "command.json", {"action": "stop"})
        try:
            await asyncio.to_thread(process.wait, timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            await asyncio.to_thread(process.wait, timeout=3)
        result["peer_exit_code"] = process.returncode
        private_json(output / "result.json", result)
        await endpoint_runtime.async_stop_sip_endpoint(lab.hass)


if __name__ == "__main__":
    assert len(sys.argv) == 5 and sys.argv[1] == "peer"
    asyncio.run(run_peer(Path(sys.argv[2]), sys.argv[3], sys.argv[4]))
