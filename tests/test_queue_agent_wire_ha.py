"""Registered independent phones dial queue keys through the real HA listener."""

import asyncio
from xml.etree import ElementTree

import pytest

from custom_components.voip_stack.call_queues import QueueManager
from custom_components.voip_stack.config import normalized_trunk_config
from custom_components.voip_stack.core import sip
from custom_components.voip_stack.core.sip_auth import build_digest_authorization
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.sip_registrar import SipAccount
from custom_components.voip_stack.sip_trunk import SipTrunkClient
from custom_components.voip_stack.store import config_entry
from custom_components.voip_stack.trunk_runtime import trunk_client_config

from .test_automation_originate_ha import lab as lab
from .test_trunk_calls_ha import trunks as trunks

pytestmark = pytest.mark.ha
NS = {"d": "urn:ietf:params:xml:ns:dialog-info"}


class Peer(asyncio.DatagramProtocol):
    def __init__(self, username):
        self.username = username
        self.messages = asyncio.Queue()
        self.notifies = asyncio.Queue()
        self.byes = asyncio.Queue()
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    @property
    def address(self):
        return self.transport.get_extra_info("sockname")

    def datagram_received(self, raw, address):
        message = sip.parse_message(raw)
        if message.method in {"BYE", "NOTIFY", "OPTIONS"}:
            headers = [(key, message.header(key)) for key in ("Via", "From", "To", "Call-ID", "CSeq")]
            self.transport.sendto(sip.build_response(200, "OK", headers), address)
            if message.method == "NOTIFY":
                self.notifies.put_nowait(message)
            elif message.method == "BYE":
                self.byes.put_nowait(message)
        else:
            self.messages.put_nowait(message)

    def request(self, method, target, call_id, cseq=1, *, from_user=None, body=b"", extra=(), tag=""):
        host, port = self.address
        headers = [("Via", f"SIP/2.0/UDP {host}:{port};branch=z9hG4bK-{call_id}-{cseq};rport"),
            ("From", f"<sip:{from_user or self.username}@127.0.0.1>;tag={call_id}"),
            ("To", f"<sip:{target}@127.0.0.1>" + (f";tag={tag}" if tag else "")),
            ("Contact", f"<sip:{self.username}@{host}:{port}>"),
            ("Call-ID", call_id), ("CSeq", f"{cseq} {method}"), *extra]
        if body:
            headers.append(("Content-Type", "application/sdp"))
        return sip.build_request(method, f"sip:{target}@127.0.0.1", headers, body)

    async def response(self, call_id, *, method, final=True):
        async with asyncio.timeout(3):
            while True:
                message = await self.messages.get()
                if (message.header("Call-ID") == call_id and message.header("CSeq").endswith(" " + method)
                        and (not final or message.status_code >= 200)):
                    return message


@pytest.fixture
async def agents(trunks):
    lab = trunks
    registrar = lab.calls.component("registrar")
    registrar.enabled = True
    names = ("queue_agent_a", "queue_agent_b")
    registrar.update_accounts([SipAccount(name, name, "queue-fixture-only") for name in names])
    listener = ("127.0.0.1", lab.runtime.transport_config["sip_port"])
    peers = []
    try:
        for index, name in enumerate(names):
            endpoint_id = "sip:" + name
            lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id=endpoint_id, name=name,
                username=name, kind=EndpointKind.SIP_ACCOUNT))
            transport, peer = await lab.hass.loop.create_datagram_endpoint(lambda: Peer(name), local_addr=(f"127.0.0.{index + 3}", 0))
            peers.append(peer)
            uri = f"sip:{listener[0]}:{listener[1]}"
            raw = peer.request("REGISTER", name, f"register-{name}", extra=(("Expires", "300"),))
            message = sip.parse_message(raw)
            raw = sip.build_request("REGISTER", uri, message.headers)
            transport.sendto(raw, listener)
            challenge = await peer.response(f"register-{name}", method="REGISTER")
            assert challenge.status_code == 401
            auth = build_digest_authorization(challenge_header=challenge.header("WWW-Authenticate"),
                username=name, password="queue-fixture-only", method="REGISTER", uri=uri)
            authenticated = peer.request("REGISTER", name, f"register-{name}", cseq=2,
                extra=(("Expires", "300"), ("Authorization", auth)))
            authenticated = sip.parse_message(authenticated)
            transport.sendto(sip.build_request("REGISTER", uri, authenticated.headers), listener)
            assert (await peer.response(f"register-{name}", method="REGISTER")).status_code == 200
            assert registrar.registration_matches_source(name, *peer.address, "UDP")
            lab.roster.append(RosterEntry(name, sip_uri=f"sip:{name}@{peer.address[0]}:{peer.address[1]}",
                metadata={"endpoint_id": endpoint_id, "endpoint_kind": "sip_account", "registered": True}))
        manager = QueueManager(lab.hass, config_entry(lab.hass))
        lab.runtime.queues = manager
        manager.reconcile([("sip:" + name, ["Support"]) for name in names])
        manager.agent_states["support"] = {"sip:" + name: "logged_out" for name in names}
        yield lab, manager, peers, listener
        await manager.close()
    finally:
        for peer in peers:
            peer.transport.close()


async def _dial_key(lab, peer, listener, target, call_id, *, from_user=None, retransmit=False, expected=200):
    body = (f"v=0\r\no=- 1 1 IN IP4 {peer.address[0]}\r\ns=Queue key\r\nc=IN IP4 {peer.address[0]}\r\n"
            "t=0 0\r\nm=audio 35000 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\na=ptime:20\r\na=sendrecv\r\n").encode()
    raw = peer.request("INVITE", target, call_id, from_user=from_user, body=body)
    peer.transport.sendto(raw, listener)
    if retransmit:
        peer.transport.sendto(raw, listener)
    response = await peer.response(call_id, method="INVITE")
    assert response.status_code == expected
    request = sip.parse_message(raw)
    headers = [(key, request.header(key)) for key in ("Via", "From", "Call-ID")]
    if expected == 200:
        headers = [(key, value.replace(";rport", "-ack;rport") if key == "Via" else value)
                   for key, value in headers]
    headers += [("To", response.header("To")), ("CSeq", "1 ACK")]
    ack_uri = sip.name_addr_uri(response.header("Contact")) if expected == 200 else request.uri
    peer.transport.sendto(sip.build_request("ACK", ack_uri, headers), listener)
    if expected == 200:
        async with asyncio.timeout(3):
            while (await peer.byes.get()).header("Call-ID") != call_id:
                pass
    async with asyncio.timeout(3):
        while lab.calls.sessions:
            await asyncio.sleep(.001)
    assert not lab.runtime.rtp_port_pool.get("used")


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_login_pause_keys_retransmit_once_and_notify_only_the_registered_operator(agents):
    lab, manager, peers, listener = agents
    a, b = peers
    for control in ("login", "pause"):
        identity = f"watch-{control}"
        a.transport.sendto(a.request("SUBSCRIBE", f"queue-{control}+support", identity,
            extra=(("Event", "dialog"), ("Expires", "300"), ("Accept", "application/dialog-info+xml"))), listener)
        assert (await a.response(identity, method="SUBSCRIBE")).status_code == 200
        await asyncio.wait_for(a.notifies.get(), 3)
    await _dial_key(lab, a, listener, "queue-login+support", "login-a", retransmit=True)
    assert manager.agent_states["support"] == {"sip:queue_agent_a": "available", "sip:queue_agent_b": "logged_out"}
    login = ElementTree.fromstring((await asyncio.wait_for(a.notifies.get(), 3)).body)
    assert "queue-login" in login.attrib["entity"]
    assert not login.findall("d:dialog", NS)
    await _dial_key(lab, a, listener, "queue-pause+support", "pause-a", retransmit=True)
    assert manager.agent_states["support"] == {"sip:queue_agent_a": "paused", "sip:queue_agent_b": "logged_out"}
    pause = ElementTree.fromstring((await asyncio.wait_for(a.notifies.get(), 3)).body)
    assert "queue-pause" in pause.attrib["entity"]
    assert pause.find("d:dialog/d:state", NS).text == "confirmed"
    await _dial_key(lab, b, listener, "queue-login+support", "login-b", retransmit=True)
    assert manager.agent_states["support"] == {"sip:queue_agent_a": "paused", "sip:queue_agent_b": "available"}
    assert a.notifies.empty()
    assert all(not endpoint.active_call_id for endpoint in lab.runtime.endpoints.endpoints)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_queue_control_rejects_spoofed_registered_identity_wrong_source_and_trunk(agents):
    lab, manager, peers, listener = agents
    a, b = peers
    original = dict(manager.agent_states["support"])
    await _dial_key(lab, a, listener, "queue-login+support", "cross-agent", from_user=b.username, expected=403)
    transport, intruder = await lab.hass.loop.create_datagram_endpoint(lambda: Peer(a.username), local_addr=("127.0.0.5", 0))
    try:
        await _dial_key(lab, intruder, listener, "queue-pause+support", "unregistered-spoof", expected=403)
        cfg = normalized_trunk_config({"trunk_enabled": True, "trunk_register": False,
            "trunk_server": "127.0.0.5", "trunk_allowed_ips": ["127.0.0.5/32"]})
        trunk = SipTrunkClient(config=trunk_client_config(cfg), local_ip="127.0.0.1", local_sip_port=listener[1], trunk_id="spoof-test")
        lab.runtime.trunk_configs["spoof-test"] = cfg
        lab.calls.adopt_component("trunk:spoof-test", trunk, closer=trunk.stop)
        await _dial_key(lab, intruder, listener, "queue-login+support", "trusted-trunk-spoof", expected=403)
        assert manager.agent_states["support"] == original
        assert not any(phone.invites for phone in lab.phones)
    finally:
        transport.close()
