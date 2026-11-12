"""Authenticated outbound routing and ordinary UDP dialog routing stay distinct."""

import asyncio
from dataclasses import replace
import struct
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.core import Context
import pytest

from custom_components.voip_stack.core import sdp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.core.sip_auth import build_digest_authorization
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_registrar import SipAccount, SipRegistrar
from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol
from custom_components.voip_stack import sip_runtime
from tests.test_automation_originate_ha import lab as lab, dial
from tests.test_trunk_calls_ha import trunks as trunks


pytestmark = [pytest.mark.ha, pytest.mark.parametrize("trunks", [True], indirect=True)]
PASSWORD = "fixture-only-password"


class WirePhone(asyncio.DatagramProtocol):
    """A wire peer can advertise another Contact while keeping its flow alive."""

    def __init__(self):
        self.messages = asyncio.Queue()
        self.requests = []
        self.contact = None

    def connection_made(self, transport):
        self.transport = transport
        self.address = transport.get_extra_info("sockname")[:2]

    def respond(self, request, address, *, body=b"", headers=()):
        values = [(name, request.header(name)) for name in ("Via", "From", "Call-ID", "CSeq")]
        to = request.header("To")
        values.append(("To", to if ";tag=" in to else to + ";tag=wire-phone"))
        if body:
            values.append(("Content-Type", "application/sdp"))
        values.extend(headers)
        response = sip.build_response(200, "OK", values, body)
        self.transport.sendto(response, address)
        return response

    def datagram_received(self, raw, address):
        message = sip.parse_message(raw)
        self.messages.put_nowait((message, address))
        if not message.is_request:
            return
        self.requests.append(message)
        if message.method == "INVITE":
            fmt = next(item for item in sdp.offered_pcm_formats(message.body)
                       if item.encoding == "L16" and item.sample_rate == 16000)
            answer = sdp.build_answer_directional("127.0.0.1", "127.0.0.1", 35000,
                fmt, fmt, remote_sdp=message.body).encode()
            self.last_invite = message
            self.last_invite_size = len(raw)
            self.last_invite_response = self.respond(message, address, body=answer,
                headers=(("Contact", f"<{sip.SipUri('wire', *self.contact.address)}>"),))
        elif message.method == "OPTIONS":
            body = ("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=Wire capabilities\r\n"
                "c=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 0 RTP/AVP 96\r\n"
                "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\na=sendrecv\r\n").encode()
            self.respond(message, address, body=body)
        elif message.method in {"BYE", "INFO"}:
            self.respond(message, address)

    async def receive(self, method=None, *, status=None):
        async with asyncio.timeout(2):
            while True:
                message, address = await self.messages.get()
                if (method is None or message.method == method) and (
                    status is None or message.status_code == status
                ):
                    return message, address


@pytest.fixture
async def wire_lab(trunks):
    from custom_components.voip_stack.runtime_data import sip_endpoint_manager, sip_registrar

    lab = trunks
    registrar = sip_registrar(lab.hass)
    registrar.enabled = True
    registrar.update_accounts([*registrar.accounts.values(), SipAccount("outbound-phone", "Wire phone", PASSWORD)])
    manager = sip_endpoint_manager(lab.hass)
    listener_port = manager.udp_server.transport.get_extra_info("sockname")[1]
    lab.runtime.transport_config["sip_port"] = listener_port
    phones = []
    for _ in range(2):
        _transport, phone = await lab.hass.loop.create_datagram_endpoint(WirePhone,
            local_addr=("127.0.0.1", 0))
        phones.append(phone)
    phones[0].contact = phones[1]
    phones[1].contact = phones[1]
    try:
        yield SimpleNamespace(lab=lab, registrar=registrar, manager=manager,
            listener=("127.0.0.1", listener_port), a=phones[0], b=phones[1],
            idle_ports=set(lab.runtime.rtp_port_pool.get("used", ())))
    finally:
        for session in tuple(lab.calls.sessions.values()):
            await lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
        for phone in phones:
            phone.transport.close()


async def register_phone(wire, *, username="outbound-phone", outbound=True, phone=None, discover=True, cseq=1, contact_uri=""):
    phone = phone or wire.a
    uri = f"sip:127.0.0.1:{wire.listener[1]}"
    contact = f"<{contact_uri or sip.SipUri(username, *phone.address)}>"
    if outbound:
        contact += ';+sip.instance="<urn:uuid:11111111-2222-3333-4444-555555555555>";reg-id=1'

    def request(cseq, authorization=""):
        headers = [("Via", f"SIP/2.0/UDP {phone.address[0]}:{phone.address[1]};branch=z9hG4bK-register-{username}-{cseq};rport"),
            ("From", f"<sip:{username}@127.0.0.1>;tag=registration"),
            ("To", f"<sip:{username}@127.0.0.1>"), ("Call-ID", f"register-{username}"),
            ("CSeq", f"{cseq} REGISTER"), ("Contact", contact), ("Expires", "120")]
        if outbound:
            headers.append(("Supported", "outbound"))
        if authorization:
            headers.append(("Authorization", authorization))
        phone.transport.sendto(sip.build_request("REGISTER", uri, headers), wire.listener)

    request(cseq)
    challenge, _ = await phone.receive(status=401)
    authorization = build_digest_authorization(challenge_header=challenge.header("WWW-Authenticate"),
        username=username, password=PASSWORD, method="REGISTER", uri=uri)
    request(cseq + 1, authorization)
    accepted, _ = await phone.receive(status=200)
    binding, = wire.registrar.registered_contacts(username)
    if discover:
        async with asyncio.timeout(2):
            while binding.capabilities is None:
                await asyncio.sleep(.01)
    assert binding.outbound is outbound
    if outbound:
        assert accepted.header("Require") == "outbound"
    # Publish the registrar's actual selected Contact, not a synthetic flag.
    wire.lab.roster.append(next(entry for entry in wire.registrar.roster_entries() if entry.id == username))
    return binding




@pytest.mark.parametrize("outbound", [False, True], ids=["ordinary-contact-moves", "authenticated-outbound-flow"])
async def test_public_call_service_keeps_selected_outbound_flow_but_ordinary_contact_moves(wire_lab, outbound):
    wire = wire_lab
    await register_phone(wire, outbound=outbound)
    context = Context()
    token = await dial(wire.lab, context, destination="outbound-phone", answer_timeout=2)
    destination = wire.a if outbound else wire.b
    ack, address = await destination.receive("ACK")
    assert ack.uri == str(sip.SipUri("wire", *wire.b.address))
    assert address == wire.listener
    assert not [request for request in (wire.b if outbound else wire.a).requests if request.method == "ACK"]
    client = wire.lab.calls.sip_client_for(token["call_id"])
    assert await client.send_dtmf_info("5", timeout=1)
    info, info_address = await destination.receive("INFO")
    assert info.uri == ack.uri and info_address == wire.listener
    usage_id = "usage-" + client.dialog_ids.call_id
    close_usage = client.bind_signaling_usage(usage_id)
    request = sip.build_request("OPTIONS", ack.uri, [
        (name, usage_id if name == "Call-ID" else "1 OPTIONS" if name == "CSeq" else ack.header(name))
        for name in ("Via", "From", "To", "Call-ID", "CSeq")])
    assert client._send_dialog_request(request, *destination.address)
    await destination.receive("OPTIONS")
    listener = wire.manager.udp_server.endpoint
    assert (destination.address, usage_id) in listener._dialog_queues
    close_usage()
    assert not any(key[1] == usage_id for key in listener._dialog_queues)
    await wire.lab.hass.services.async_call("voip_stack", "hangup", {
        "call_id": token["call_id"], "expected_generation": token["generation"],
    }, blocking=True, context=context)
    bye, _ = await destination.receive("BYE")
    assert bye.uri == ack.uri
    listener = wire.manager.udp_server.endpoint
    assert not listener._dialog_queues and not listener._reused_dialog_keys
    assert not wire.lab.calls.sessions
    assert wire.registrar.registered_contacts("outbound-phone")
    assert wire.manager.udp_server.transport is not None


async def test_ordinary_registered_phone_uses_listener_for_provisional_and_cancel(wire_lab, monkeypatch):
    from custom_components.voip_stack import softphone_originate
    from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
    from custom_components.voip_stack.roster import dump_roster_json

    wire = wire_lab
    await register_phone(wire, outbound=False)
    wire.lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="browser", name="Browser",
        device_id="device-browser", kind=EndpointKind.BROWSER, availability="available"))
    wire.lab.hass.states.async_set("sensor.voip_phonebook", "4", {"roster_json": dump_roster_json(wire.lab.roster)})
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    original = wire.a.datagram_received
    pending = None

    def respond(request, status, reason, *, sender=None, tag="fixed-registrar-peer", wrong_branch=False):
        headers = [(name, request.header(name)) for name in ("Via", "From", "Call-ID", "CSeq")]
        if wrong_branch:
            headers[0] = ("Via", headers[0][1].replace(sip.parse_via(headers[0][1]).branch, "z9hG4bK-foreign"))
        headers.append(("To", request.header("To") + ";tag=" + tag))
        # Independent peer routes all responses to its configured registrar.
        (sender or wire.a).transport.sendto(sip.build_response(status, reason, headers), wire.listener)

    def receive(raw, address):
        nonlocal pending
        request = sip.parse_message(raw)
        if request.method not in {"INVITE", "CANCEL", "ACK"}:
            return original(raw, address)
        wire.a.messages.put_nowait((request, address))
        wire.a.requests.append(request)
        if request.method == "INVITE":
            pending = request
            respond(request, 180, "Ringing", tag="wrong-branch", wrong_branch=True)
            respond(request, 180, "Ringing", sender=wire.b, tag="foreign-source")
            respond(request, 100, "Trying")
            respond(request, 180, "Ringing")
        elif request.method == "CANCEL":
            respond(request, 200, "OK")
            respond(pending, 487, "Request Terminated")

    monkeypatch.setattr(wire.a, "datagram_received", receive)
    context = Context()
    calling = asyncio.create_task(wire.lab.hass.services.async_call("voip_stack", "call", {
        "device_id": "device-browser", "destination": "outbound-phone",
    }, blocking=True, context=context))
    try:
        invite, address = await wire.a.receive("INVITE")
        await asyncio.wait_for(asyncio.shield(calling), 2)
        client = wire.lab.calls.sip_client_for(invite.header("Call-ID"))
        assert client._received_provisional and client.last_sip_status_code == 180
        assert set(client.early_dialogs) == {"fixed-registrar-peer"}
        assert address == wire.listener
        assert sip.parse_via(invite.header("Via")).port == wire.listener[1]
        assert not wire.registrar.registered_contacts("outbound-phone")[0].outbound
        await wire.lab.hass.services.async_call("voip_stack", "hangup", {
            "device_id": "device-browser", "call_id": invite.header("Call-ID"),
        }, blocking=True, context=context)
        cancel, _ = await wire.a.receive("CANCEL")
        await wire.a.receive("ACK")
        assert cancel.header("Via") == invite.header("Via")
        assert not wire.lab.calls.sessions
        assert not wire.manager.udp_server.endpoint._dialog_queues
        assert set(wire.lab.runtime.rtp_port_pool.get("used", ())) == wire.idle_ports
        assert wire.manager.udp_server.transport is not None
    finally:
        for session in tuple(wire.lab.calls.sessions.values()):
            await wire.lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
        if not calling.done():
            calling.cancel()
        await asyncio.gather(calling, return_exceptions=True)


@pytest.mark.parametrize("ingress", ["browser", "inbound-bridge"])
async def test_selected_outbound_binding_reaches_browser_and_inbound_factories(wire_lab, monkeypatch, ingress):
    from custom_components.voip_stack import softphone_originate
    from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
    from custom_components.voip_stack.roster import dump_roster_json

    wire = wire_lab
    await register_phone(wire)
    context = Context()
    caller = None
    try:
        if ingress == "browser":
            browser = PhoneEndpoint(endpoint_id="browser", name="Browser", device_id="device-browser",
                kind=EndpointKind.BROWSER, availability="available")
            wire.lab.runtime.endpoints.register(browser)
            wire.lab.hass.states.async_set("sensor.voip_phonebook", "4", {"roster_json": dump_roster_json(wire.lab.roster)})
            monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
            monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
            await wire.lab.hass.services.async_call("voip_stack", "call", {
                "device_id": browser.device_id, "destination": "outbound-phone",
            }, blocking=True, context=context)
        else:
            caller = SipCallClient(local_ip="127.0.0.2", local_name="Direct incoming caller",
                local_sip_port=5060, local_rtp_port=40010,
                supported_formats=[AudioFormat(16000, frame_ms=20)])
            caller.transport, caller.protocol = await wire.lab.hass.loop.create_datagram_endpoint(
                lambda: SipDatagramQueueProtocol(caller.queue), local_addr=("127.0.0.2", 0))
            caller.local_sip_port = caller.transport.get_extra_info("sockname")[1]
            result = await caller.invite(target="outbound-phone", remote_host=wire.listener[0],
                remote_sip_port=wire.listener[1], timeout=3)
            if result == "ringing":
                result = await caller.wait_for_final(timeout=3)
            assert result == "in_call"
        request, _ = await wire.a.receive("INVITE")
        ack, _ = await wire.a.receive("ACK")
        assert ack.uri == str(sip.SipUri("wire", *wire.b.address))
        assert wire.b.requests == []
        call_id = request.header("Call-ID")
        if caller is not None:
            await caller.terminate(timeout=2)
        else:
            await wire.lab.hass.services.async_call("voip_stack", "hangup", {
                "device_id": "device-browser", "call_id": call_id,
            }, blocking=True, context=context)
        await wire.a.receive("BYE")
        async with asyncio.timeout(2):
            while wire.lab.calls.sessions:
                await asyncio.sleep(.01)
        assert not wire.manager.udp_server.endpoint._dialog_queues
        assert set(wire.lab.runtime.rtp_port_pool.get("used", ())) == wire.idle_ports
    finally:
        if caller is not None:
            await caller.close()


@pytest.mark.parametrize("invalid", ["binding-removed", "registrar-replaced", "wrong-contact", "wrong-account", "account-revoked",
    "binding-expired", "binding-aba", "binding-rebound"])
@pytest.mark.parametrize("outbound", [False, True])
async def test_stale_or_mismatched_outbound_selection_cannot_borrow_listener(wire_lab, invalid, outbound):
    wire = wire_lab
    binding = await register_phone(wire, outbound=outbound)
    uri = sip.parse_sip_uri(binding.contact_uri)
    selected = wire.registrar.select_outbound_flow(binding.username, uri, allow_ordinary=not outbound)
    assert selected is not None
    if invalid == "binding-removed":
        wire.registrar.remove_registration(binding.username)
    elif invalid == "binding-expired":
        binding.expires_at = time.time() - 1
    elif invalid == "binding-aba":
        wire.registrar.remove_registration(binding.username)
        await register_phone(wire, cseq=3, outbound=outbound)
    elif invalid == "binding-rebound":
        # Ordinary Contacts are independent bindings; rebind the same URI.
        await register_phone(wire, phone=wire.b, cseq=3, outbound=outbound,
            contact_uri=binding.contact_uri if not outbound else "")
    elif invalid == "account-revoked":
        wire.registrar.update_accounts([replace(account, enabled=False)
            for account in wire.registrar.accounts.values()])
    elif invalid == "registrar-replaced":
        assert wire.lab.calls.release_component("registrar", wire.registrar)
        replacement = SipRegistrar(enabled=True, accounts=[], local_ip="127.0.0.1", local_sip_port=5060)
        wire.lab.calls.adopt_component("registrar", replacement)
    elif invalid == "wrong-contact":
        uri = sip.SipUri(uri.user, *wire.b.address)
    else:
        uri = sip.SipUri("another-account", uri.host, uri.port)
    client = SipCallClient(local_ip="127.0.0.1", local_name="Rejected selection",
        local_sip_port=wire.listener[1], local_rtp_port=40000,
        supported_formats=[AudioFormat(16000, frame_ms=20)])
    try:
        with pytest.raises(ConnectionError, match="binding"):
            sip_runtime.enable_reused_signaling_flow(wire.lab.hass, client, uri,
                target=binding.username, default_sip_port=wire.listener[1], selected_flow=selected)
        assert not wire.manager.udp_server.endpoint._dialog_queues
        assert not [request for request in wire.a.requests if request.method != "OPTIONS"]
        assert not [request for request in wire.b.requests if request.method != "OPTIONS"]
    finally:
        await client.close()


@pytest.mark.parametrize("owner", ["registrar", "udp_listener"])
async def test_replaced_owner_cannot_send_through_an_existing_outbound_lease(wire_lab, owner):
    wire = wire_lab
    binding = await register_phone(wire)
    uri = sip.parse_sip_uri(binding.contact_uri)
    selected = wire.registrar.select_outbound_flow(binding.username, uri)
    client = SipCallClient(local_ip="127.0.0.1", local_name="Replaced owner",
        local_sip_port=wire.listener[1], local_rtp_port=40000,
        supported_formats=[AudioFormat(16000, frame_ms=20)])
    try:
        assert sip_runtime.enable_reused_signaling_flow(wire.lab.hass, client, uri,
            target=binding.username, default_sip_port=wire.listener[1], selected_flow=selected)
        previous = wire.registrar if owner == "registrar" else wire.manager
        assert wire.lab.calls.release_component(owner, previous)
        wire.lab.calls.adopt_component(owner, object())
        raw = sip.build_request("OPTIONS", str(uri), [("Call-ID", client.dialog_ids.call_id)])
        with pytest.raises(ConnectionError, match="not writable"):
            await client._send_raw(raw, *wire.a.address)
        assert not [request for request in wire.a.requests if request.method != "OPTIONS"]
        assert not wire.b.requests
        assert (wire.b.address, client.dialog_ids.call_id) not in wire.manager.udp_server.endpoint._dialog_queues
    finally:
        await client.close()
        replacement = wire.lab.calls.component(owner)
        wire.lab.calls.release_component(owner, replacement)
        wire.lab.calls.adopt_component(owner, previous,
            closer=wire.manager.stop if owner == "udp_listener" else None)
    assert not wire.manager.udp_server.endpoint._dialog_queues


async def test_selected_outbound_flow_never_falls_back_after_its_udp_socket_disappears(wire_lab):
    wire = wire_lab
    binding = await register_phone(wire)
    uri = sip.parse_sip_uri(binding.contact_uri)
    selected = wire.registrar.select_outbound_flow(binding.username, uri)
    client = SipCallClient(local_ip="127.0.0.1", local_name="Missing UDP socket",
        local_sip_port=wire.listener[1], local_rtp_port=40000,
        supported_formats=[AudioFormat(16000, frame_ms=20)])
    listener = wire.manager.udp_server.endpoint
    transport = listener.transport
    listener.transport = None
    try:
        with pytest.raises(ConnectionError, match="socket is unavailable"):
            sip_runtime.enable_reused_signaling_flow(wire.lab.hass, client, uri,
                target=binding.username, default_sip_port=wire.listener[1], selected_flow=selected)
        assert client.transport is None
        assert not listener._dialog_queues
    finally:
        listener.transport = transport
        await client.close()


async def test_two_authenticated_accounts_on_one_udp_flow_keep_independent_leases(wire_lab):
    wire = wire_lab
    first = await register_phone(wire)
    wire.registrar.update_accounts([*wire.registrar.accounts.values(),
        SipAccount("second-phone", "Second phone", PASSWORD)])
    second = await register_phone(wire, username="second-phone")
    assert (first.source_host, first.source_port) == (second.source_host, second.source_port)
    clients = []
    try:
        for binding in (first, second):
            uri = sip.parse_sip_uri(binding.contact_uri)
            selected = wire.registrar.select_outbound_flow(binding.username, uri)
            assert selected.username == binding.username
            assert wire.registrar.select_outbound_flow(
                "second-phone" if binding is first else "outbound-phone", uri) is None
            client = SipCallClient(local_ip="127.0.0.1", local_name=binding.username,
                local_sip_port=wire.listener[1], local_rtp_port=40000,
                supported_formats=[AudioFormat(16000, frame_ms=20)])
            clients.append(client)
            assert sip_runtime.enable_reused_signaling_flow(wire.lab.hass, client, uri,
                target=binding.username, default_sip_port=wire.listener[1], selected_flow=selected)
        listener = wire.manager.udp_server.endpoint
        before = dict(listener._dialog_queues)
        with pytest.raises(RuntimeError, match="already has a reader"):
            clients[0].bind_signaling_usage(clients[1].dialog_ids.call_id)
        assert listener._dialog_queues == before
        await clients[0].close()
        assert (wire.a.address, clients[1].dialog_ids.call_id) in listener._dialog_queues
        assert wire.registrar.registered_contacts("outbound-phone")
        assert wire.registrar.registered_contacts("second-phone")
    finally:
        await asyncio.gather(*(client.close() for client in clients))
    assert not wire.manager.udp_server.endpoint._dialog_queues


async def test_outbound_flow_keeps_fork_and_retransmission_targets_without_redirecting_transport(wire_lab, monkeypatch):
    wire = wire_lab
    await register_phone(wire)
    context = Context()
    token = await dial(wire.lab, context, destination="outbound-phone", answer_timeout=2)
    await wire.a.receive("ACK")
    client = wire.lab.calls.sip_client_for(token["call_id"])
    winner = client.dialog
    resolve = AsyncMock(side_effect=AssertionError("Selected outbound flow must not resolve the Contact"))
    monkeypatch.setattr(client.target_resolver, "resolve", resolve)
    request = wire.a.last_invite
    headers = [(name, request.header(name)) for name in ("Via", "From", "Call-ID", "CSeq")]
    headers.extend([("To", request.header("To") + ";tag=losing-fork"),
        ("Contact", "<sip:fork@unresolved.invalid:5070>"), ("Content-Type", "application/sdp")])
    fork = sip.build_response(200, "OK", headers, sip.parse_message(wire.a.last_invite_response).body)
    wire.a.transport.sendto(fork, wire.listener)
    ack, _ = await wire.a.receive("ACK")
    bye, _ = await wire.a.receive("BYE")
    assert ack.uri == bye.uri == "sip:fork@unresolved.invalid:5070"
    await asyncio.wait_for(asyncio.gather(*tuple(client._exceptional_bye_tasks)), 2)
    wire.a.transport.sendto(fork, wire.listener)
    await wire.a.receive("ACK")
    wire.a.transport.sendto(wire.a.last_invite_response, wire.listener)
    ack, _ = await wire.a.receive("ACK")
    assert ack.uri == str(sip.SipUri("wire", *wire.b.address))
    assert client.dialog is winner
    assert not wire.b.requests
    resolve.assert_not_called()
    await wire.lab.hass.services.async_call("voip_stack", "hangup", {
        "call_id": token["call_id"], "expected_generation": token["generation"],
    }, blocking=True, context=context)
    await wire.a.receive("BYE")
    assert not wire.manager.udp_server.endpoint._dialog_queues
    assert not wire.lab.calls.sessions








# Independently encode/decode STUN here, without using the production builder.
# RFC 5769 section 2.1 verifies the CRC before it is used on our wire requests.
_STUN_COOKIE = bytes.fromhex("2112a442")
_STUN_TRANSACTION = bytes.fromhex("0102030405060708090a0b0c")


def _stun_crc(raw):
    crc = 0xFFFFFFFF
    for byte in raw:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 if crc & 1 else 0)
    return (crc ^ 0xFFFFFFFF) ^ 0x5354554E


def _stun_attribute(kind, value):
    return struct.pack("!HH", kind, len(value)) + value + b"\0" * (-len(value) % 4)


def _stun_request(*, attributes=b"", fingerprint=True, kind=1):
    raw = struct.pack("!HH", kind, len(attributes) + (8 if fingerprint else 0)) + _STUN_COOKIE + _STUN_TRANSACTION + attributes
    return raw + _stun_attribute(0x8028, struct.pack("!I", _stun_crc(raw))) if fingerprint else raw


def _stun_response(raw):
    kind, size = struct.unpack("!HH", raw[:4])
    assert size + 20 == len(raw) and raw[4:8] == _STUN_COOKIE
    assert raw[8:20] == _STUN_TRANSACTION
    attrs, offset = {}, 20
    while offset < len(raw):
        attr, length = struct.unpack("!HH", raw[offset:offset+4])
        attrs[attr] = raw[offset+4:offset+4+length]
        if attr == 0x8028:
            assert length == 4 and offset + 8 == len(raw)
            assert struct.unpack("!I", attrs[attr])[0] == _stun_crc(raw[:offset])
        offset += 4 + ((length + 3) & ~3)
    assert offset == len(raw) and 0x8028 in attrs
    return kind, attrs


async def test_authenticated_udp_outbound_stun_uses_same_socket_without_mutating_sip(wire_lab, monkeypatch):
    wire = wire_lab
    replies = asyncio.Queue()
    register_responses = []
    original = wire.a.datagram_received

    def receive(raw, address):
        if raw[:1] in (b"\0", b"\1"):
            replies.put_nowait((raw, address))
        else:
            message = sip.parse_message(raw)
            if message.status_code == 200 and message.header("CSeq").endswith("REGISTER"):
                register_responses.append(message)
            original(raw, address)

    monkeypatch.setattr(wire.a, "datagram_received", receive)
    binding = await register_phone(wire)
    assert register_responses[-1].header("Require") == "outbound"
    assert register_responses[-1].header("Flow-Timer") == "25"
    before = wire.registrar.registered_contacts("outbound-phone")
    binding_before = replace(binding)
    endpoint = wire.manager.udp_server.endpoint
    dropped_before = endpoint.dropped_datagrams
    pending_before = set(endpoint._request_tasks)
    # Same real peer socket and listener: no replacement STUN listener exists.
    for attrs, fingerprint in ((b"", True), (b"", False),
                              (_stun_attribute(0x8022, b"baresip/re 4.6") + _stun_attribute(0x9999, b"x"), True)):
        wire.a.transport.sendto(_stun_request(attributes=attrs, fingerprint=fingerprint), wire.listener)
        raw, source = await asyncio.wait_for(replies.get(), 1)
        kind, values = _stun_response(raw)
        assert kind == 0x0101 and source == wire.listener
        mapped = values[0x0020]
        assert mapped[:2] == b"\0\1" and len(mapped) == 8
        port = int.from_bytes(mapped[2:4], "big") ^ 0x2112
        host = ".".join(str(a ^ b) for a, b in zip(mapped[4:], _STUN_COOKIE))
        assert (host, port) == wire.a.address
    assert wire.registrar.registered_contacts("outbound-phone") == before
    assert binding == binding_before
    assert wire.registrar.registered_contacts("outbound-phone")[0] is binding
    assert endpoint.dropped_datagrams == dropped_before
    assert set(endpoint._request_tasks) == pending_before
    assert not endpoint._dialog_queues and not wire.lab.calls.sessions
    assert set(wire.lab.runtime.rtp_port_pool.get("used", ())) == wire.idle_ports
    # Ordinary SIP remains parseable/routable after binary keep-alives.
    uri = f"sip:127.0.0.1:{wire.listener[1]}"
    headers = [("Via", f"SIP/2.0/UDP {wire.a.address[0]}:{wire.a.address[1]};branch=z9hG4bK-stun-options;rport"),
               ("From", "<sip:outbound-phone@127.0.0.1>;tag=control"),
               ("To", "<sip:outbound-phone@127.0.0.1>"),
               ("Call-ID", "after-stun"), ("CSeq", "1 OPTIONS")]
    wire.a.transport.sendto(sip.build_request("OPTIONS", uri, headers), wire.listener)
    answer, source = await wire.a.receive(status=200)
    assert answer.header("CSeq") == "1 OPTIONS" and source == wire.listener
    assert wire.registrar.registered_contacts("outbound-phone") == before
    assert binding == binding_before


async def test_udp_stun_unknown_required_attribute_gets_420_on_observed_flow(wire_lab, monkeypatch):
    wire = wire_lab
    replies = asyncio.Queue()
    original = wire.a.datagram_received

    def receive(raw, address):
        if raw[:1] in (b"\0", b"\1"):
            replies.put_nowait((raw, address))
        else:
            original(raw, address)

    monkeypatch.setattr(wire.a, "datagram_received", receive)
    # RFC 5626 section 8 does not require REGISTER or STUN credentials first.
    attrs = _stun_attribute(0x7777, b"a") + _stun_attribute(0x7776, b"") + _stun_attribute(0x7777, b"")
    wire.a.transport.sendto(_stun_request(attributes=attrs), wire.listener)
    raw, source = await asyncio.wait_for(replies.get(), 1)
    kind, values = _stun_response(raw)
    assert kind == 0x0111 and source == wire.listener
    assert values[0x0009] == b"\0\0\x04\x14Unknown Attribute"
    assert values[0x000A] == bytes.fromhex("77767777")
    assert 0x0020 not in values and not wire.registrar.registered_contacts("outbound-phone")


@pytest.mark.parametrize("invalid", ["fingerprint", "truncated", "short-header", "trailing", "cookie", "unaligned",
                                     "attribute-overrun", "fingerprint-size", "fingerprint-not-last",
                                     "indication", "success", "error", "other-method", "mi-empty", "mi-short", "mi-long"])
async def test_udp_stun_invalid_or_non_request_packets_receive_no_response(wire_lab, monkeypatch, invalid):
    wire = wire_lab
    replies = asyncio.Queue()
    original = wire.a.datagram_received

    def receive(raw, address):
        if raw[:1] in (b"\0", b"\1"):
            replies.put_nowait((raw, address))
        else:
            original(raw, address)

    monkeypatch.setattr(wire.a, "datagram_received", receive)
    valid = _stun_request()
    packets = {
        "fingerprint": valid[:-1] + bytes((valid[-1] ^ 1,)),
        "truncated": valid[:-1], "trailing": valid + b"\0",
        "short-header": valid[:19],
        "cookie": valid[:4] + b"\0" * 4 + valid[8:],
        "unaligned": valid[:2] + b"\0\x07" + valid[4:-1],
        "attribute-overrun": _stun_request(attributes=bytes.fromhex("80220010"), fingerprint=False),
        "fingerprint-size": _stun_request(attributes=_stun_attribute(0x8028, b""), fingerprint=False),
        "fingerprint-not-last": _stun_request(attributes=_stun_attribute(0x8028, b"\0" * 4) + _stun_attribute(0x8022, b"x")),
        "indication": _stun_request(kind=0x0011), "success": _stun_request(kind=0x0101),
        "error": _stun_request(kind=0x0111), "other-method": _stun_request(kind=0x0002),
        **{f"mi-{name}": _stun_request(attributes=_stun_attribute(0x0008, b"x" * size) + _stun_attribute(0x7777, b""))
           for name, size in (("empty", 0), ("short", 19), ("long", 21))},
    }
    endpoint = wire.manager.udp_server.endpoint
    before = endpoint.dropped_datagrams
    wire.a.transport.sendto(packets[invalid], wire.listener)
    async with asyncio.timeout(1):
        while endpoint.dropped_datagrams == before:
            await asyncio.sleep(0)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(replies.get(), .03)
    assert endpoint.dropped_datagrams == before + 1
    assert not endpoint._request_tasks and not endpoint._dialog_queues
    assert not wire.registrar.registered_contacts("outbound-phone")


def test_stun_crc_known_vector_ipv6_and_payload_address_cannot_override_source(trunks):
    from custom_components.voip_stack.core.sip_stun import binding_keepalive_response

    vector = bytes.fromhex("000100582112a442b7e7a701bc34d686fa87dfae802200105354554e207465737420636c69656e74002400046e0001ff80290008932ff9b151263b36000600096576746a3a68367659202020000800149aeaa70cbfd8cb56781ef2b5b2d3f249c1b571a280280004e57a3bcf")
    assert _stun_crc(vector[:-8]) == int.from_bytes(vector[-4:], "big")
    # A caller-supplied address attribute is unexpected and cannot redirect us.
    spoof = _stun_attribute(0x0001, bytes.fromhex("0001270fc0000201"))
    raw = binding_keepalive_response(_stun_request(attributes=spoof), ("2001:db8::9", 5069, 0, 0))
    kind, values = _stun_response(raw)
    assert kind == 0x0101 and values[0x0020][:2] == b"\0\2"
    assert int.from_bytes(values[0x0020][2:4], "big") ^ 0x2112 == 5069
    mask = _STUN_COOKIE + _STUN_TRANSACTION
    host = bytes(a ^ b for a, b in zip(values[0x0020][4:], mask))
    assert host.hex() == "20010db8000000000000000000000009"
    # RFC 5389 section 15.4: ignore everything after MI except FINGERPRINT.
    attrs = _stun_attribute(0x0008, b"x" * 20) + _stun_attribute(0x7777, b"ignored")
    assert _stun_response(binding_keepalive_response(_stun_request(attributes=attrs), ("127.0.0.1", 9)))[0] == 0x0101


async def test_retired_dialog_callback_cannot_close_or_send_on_a_replacement_lease(wire_lab):
    wire = wire_lab
    binding = await register_phone(wire)
    uri = sip.parse_sip_uri(binding.contact_uri)
    selected = wire.registrar.select_outbound_flow(binding.username, uri)
    client = SipCallClient(local_ip="127.0.0.1", local_name="Retired lease",
        local_sip_port=wire.listener[1], local_rtp_port=40000,
        supported_formats=[AudioFormat(16000, frame_ms=20)])
    listener = wire.manager.udp_server.endpoint
    try:
        assert sip_runtime.enable_reused_signaling_flow(wire.lab.hass, client, uri,
            target=binding.username, default_sip_port=wire.listener[1], selected_flow=selected)
        retired_close = client._reused_flow_close
        retired_close()
        replacement = listener.open_reused_dialog(wire.a.address, client.dialog_ids.call_id, flow_active=lambda: True)
        assert replacement is not None
        _send, queue = replacement
        retired_close()
        assert listener._dialog_queues[(wire.a.address, client.dialog_ids.call_id)] is queue
        with pytest.raises(ConnectionError, match="not writable"):
            await client._send_raw(sip.build_request("OPTIONS", str(uri), [
                ("Call-ID", client.dialog_ids.call_id)]), *wire.a.address)
        listener.close_reused_dialog(wire.a.address, client.dialog_ids.call_id, queue=queue)
    finally:
        await client.close()
    assert not listener._dialog_queues and not listener._reused_dialog_keys
