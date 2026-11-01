"""Authenticated outbound routing and ordinary UDP dialog routing stay distinct."""

import asyncio
from dataclasses import replace
import errno
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.core import Context
import pytest

from custom_components.voip_stack.core import sdp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.core.sip_auth import build_digest_authorization
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_endpoint import SipEndpointManager
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


async def register_phone(wire, *, username="outbound-phone", outbound=True, phone=None, discover=True):
    phone = phone or wire.a
    uri = f"sip:127.0.0.1:{wire.listener[1]}"
    contact = f"<{sip.SipUri(username, *phone.address)}>"
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

    request(1)
    challenge, _ = await phone.receive(status=401)
    authorization = build_digest_authorization(challenge_header=challenge.header("WWW-Authenticate"),
        username=username, password=PASSWORD, method="REGISTER", uri=uri)
    request(2, authorization)
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


@pytest.fixture
async def original_no_options_lab(wire_lab):
    """Keep the original public-service witness without any OPTIONS probe hook."""
    lab = wire_lab.lab
    previous = lab.calls, lab.runtime.sip, lab.runtime.transport_config["sip_port"]
    registrar = SipRegistrar(enabled=True,
        accounts=[SipAccount("outbound-phone", "Wire phone", PASSWORD)],
        local_ip="127.0.0.1", local_sip_port=5060)
    manager = SipEndpointManager(host="127.0.0.1", port=0, local_ip="127.0.0.1",
        local_rtp_port=40000, supported_formats=[AudioFormat(16000, frame_ms=20)],
        on_invite=None, on_register=registrar.handle_register)
    calls = SipEndpointRuntime(allow_dark_sessions=True)
    calls.attach_component("registrar", registrar)
    calls.attach_component("udp_listener", manager)
    calls.activate()
    try:
        assert await manager.start()
        port = manager.udp_server.transport.get_extra_info("sockname")[1]
        lab.calls = lab.runtime.sip = calls
        lab.runtime.transport_config["sip_port"] = port
        yield SimpleNamespace(**{**vars(wire_lab), "registrar": registrar, "manager": manager,
            "listener": ("127.0.0.1", port)})
    finally:
        for session in tuple(calls.sessions.values()):
            await calls.request_termination(session.call_id, TerminationIntent("test_done"))
        await manager.stop()
        lab.calls, lab.runtime.sip, lab.runtime.transport_config["sip_port"] = previous


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


@pytest.mark.parametrize("invalid", ["binding-removed", "registrar-replaced", "wrong-contact", "wrong-account", "account-revoked"])
async def test_stale_or_mismatched_outbound_selection_cannot_borrow_listener(wire_lab, invalid):
    wire = wire_lab
    binding = await register_phone(wire)
    uri = sip.parse_sip_uri(binding.contact_uri)
    selected = wire.registrar.select_outbound_flow(binding.username, uri)
    assert selected is not None
    if invalid == "binding-removed":
        wire.registrar.remove_registration(binding.username)
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
        assert not wire.b.requests
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


@pytest.mark.parametrize("outbound", [False, True], ids=["ordinary-direct-contact", "authenticated-outbound"])
async def test_original_no_options_offer_keeps_existing_tcp_refusal_fallback(original_no_options_lab, monkeypatch, outbound):
    """The original >1300-byte INVITE must connect without adding capabilities."""
    wire = original_no_options_lab
    binding = await register_phone(wire, outbound=outbound, discover=False)
    assert binding.capabilities is None and not wire.a.requests
    attempts = []
    open_connection = asyncio.open_connection

    async def observe(host, port, *args, **kwargs):
        attempts.append((host, port))
        return await open_connection(host, port, *args, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", observe)
    # Reserve this TCP port without listening: the OS returns a real refusal,
    # and another concurrent fixture cannot accidentally accept the attempt.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as refused:
        refused.bind(wire.a.address)
        context = Context()
        token = await dial(wire.lab, context, destination="outbound-phone", answer_timeout=2)
    destination = wire.a if outbound else wire.b
    ack, address = await destination.receive("ACK")
    assert wire.a.last_invite_size > 1300
    assert ack.uri == str(sip.SipUri("wire", *wire.b.address))
    assert address == wire.listener
    assert attempts and set(attempts) == {wire.a.address}
    assert wire.registrar.registered_contacts("outbound-phone")[0].capabilities is None
    assert not [request for request in (wire.b if outbound else wire.a).requests if request.method == "ACK"]
    await wire.lab.hass.services.async_call("voip_stack", "hangup", {
        "call_id": token["call_id"], "expected_generation": token["generation"],
    }, blocking=True, context=context)
    bye, _ = await destination.receive("BYE")
    assert bye.uri == ack.uri
    assert not wire.manager.udp_server.endpoint._dialog_queues
    assert not wire.lab.calls.sessions
    assert set(wire.lab.runtime.rtp_port_pool.get("used", ())) == wire.idle_ports


async def test_original_no_options_connect_timeout_never_retries_invite_over_udp(original_no_options_lab, monkeypatch):
    from homeassistant.exceptions import ServiceValidationError

    wire = original_no_options_lab
    await register_phone(wire, discover=False)
    attempts = []

    async def timeout(host, port, *args, **kwargs):
        attempts.append((host, port))
        raise TimeoutError(errno.ETIMEDOUT, "Controlled TCP connect deadline")

    monkeypatch.setattr(asyncio, "open_connection", timeout)
    with pytest.raises(ServiceValidationError):
        await dial(wire.lab, Context(), destination="outbound-phone", answer_timeout=1)
    assert attempts and set(attempts) == {wire.a.address}
    assert not wire.a.requests and not wire.b.requests
    assert not wire.manager.udp_server.endpoint._dialog_queues
    assert not wire.lab.calls.sessions
    assert set(wire.lab.runtime.rtp_port_pool.get("used", ())) == wire.idle_ports


@pytest.mark.parametrize("answers", [True, False], ids=["tcp-a-success", "tcp-a-response-timeout"])
async def test_original_no_options_tcp_exchange_never_connects_to_contact_b(original_no_options_lab, answers):
    """Observe the existing automatic TCP upgrade; it is not the UDP flow."""
    wire = original_no_options_lab
    await register_phone(wire, discover=False)
    messages = {"a": asyncio.Queue(), "b": asyncio.Queue()}
    connections = []
    writers, tasks = set(), set()

    async def peer(name, reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        writers.add(writer)
        connections.append((name, writer.get_extra_info("sockname")[:2]))
        try:
            while True:
                try:
                    header = await reader.readuntil(b"\r\n\r\n")
                except asyncio.IncompleteReadError:
                    return
                length = next(int(line.partition(b":")[2]) for line in header.split(b"\r\n")
                    if line.lower().startswith(b"content-length:"))
                raw = header + await reader.readexactly(length)
                request = sip.parse_message(raw)
                messages[name].put_nowait((request, raw))
                if request.method == "ACK":
                    continue
                if request.method == "INVITE" and not answers:
                    continue
                headers = [(key, request.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
                to = request.header("To")
                headers.append(("To", to if ";tag=" in to else to + ";tag=tcp-phone"))
                body = b""
                if request.method == "INVITE":
                    fmt = next(item for item in sdp.offered_pcm_formats(request.body)
                        if item.encoding == "L16" and item.sample_rate == 16000)
                    body = sdp.build_answer_directional("127.0.0.1", "127.0.0.1", 35000,
                        fmt, fmt, remote_sdp=request.body).encode()
                    headers.extend([("Content-Type", "application/sdp"),
                        ("Contact", f"<{sip.SipUri('wire', *wire.b.address)}>" )])
                writer.write(sip.build_response(200, "OK", headers, body))
                await writer.drain()
        finally:
            writers.discard(writer)
            tasks.discard(task)
            writer.close()
            await writer.wait_closed()

    servers = []
    try:
        for name, phone in (("a", wire.a), ("b", wire.b)):
            servers.append(await asyncio.start_server(
                lambda reader, writer, name=name: peer(name, reader, writer), *phone.address))
        context = Context()
        if not answers:
            from homeassistant.exceptions import ServiceValidationError

            with pytest.raises(ServiceValidationError, match="did not answer"):
                await dial(wire.lab, context, destination="outbound-phone", answer_timeout=1)
            invite, raw = await asyncio.wait_for(messages["a"].get(), 2)
            assert invite.method == "INVITE" and len(raw) > 1300
            assert connections == [("a", wire.a.address)]
            assert not wire.a.requests and not wire.b.requests and messages["b"].empty()
            assert not wire.manager.udp_server.endpoint._dialog_queues
            assert not wire.lab.calls.sessions
            assert set(wire.lab.runtime.rtp_port_pool.get("used", ())) == wire.idle_ports
            return
        token = await dial(wire.lab, context, destination="outbound-phone", answer_timeout=2)
        async with asyncio.timeout(2):
            invite, raw = await messages["a"].get()
            ack, _ = await messages["a"].get()
        assert invite.method == "INVITE" and len(raw) > 1300
        assert sip.parse_via(invite.header("Via")).transport == "TCP"
        assert ack.method == "ACK" and ack.uri == str(sip.SipUri("wire", *wire.b.address))
        assert connections == [("a", wire.a.address)]
        assert messages["b"].empty() and not wire.a.requests and not wire.b.requests
        # A TCP success is a new connection. The prior UDP lease is released,
        # while the authenticated UDP registration remains independently live.
        assert not wire.manager.udp_server.endpoint._dialog_queues
        binding, = wire.registrar.registered_contacts("outbound-phone")
        assert binding.transport == "UDP" and binding.capabilities is None
        await wire.lab.hass.services.async_call("voip_stack", "hangup", {
            "call_id": token["call_id"], "expected_generation": token["generation"],
        }, blocking=True, context=context)
        bye, _ = await asyncio.wait_for(messages["a"].get(), 2)
        assert bye.method == "BYE" and bye.uri == ack.uri
        assert connections == [("a", wire.a.address)] and messages["b"].empty()
        assert not wire.lab.calls.sessions
        assert set(wire.lab.runtime.rtp_port_pool.get("used", ())) == wire.idle_ports
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()
        for writer in tuple(writers):
            writer.close()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tuple(tasks)), 3)
