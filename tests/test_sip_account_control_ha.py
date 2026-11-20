"""PBX controls real registered SIP legs without claiming hardware control."""

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.core import Context, ServiceCall
from homeassistant.exceptions import ServiceValidationError, Unauthorized

from custom_components.voip_stack.call_queues import QueueDefinition, QueueManager, QueuePolicy
from custom_components.voip_stack.phone_control import CallControlRequest, PhoneOperation
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.router import RouteAction, resolve_esp_origin
from custom_components.voip_stack.sip_capture import SipCapture
from .test_sip_phone_transfers_ha import assert_peer_audio_tones, registered_phones as registered_phones
from .test_trunk_calls_ha import lab as lab, trunks as trunks
from .test_sip_capabilities_ha import wait_until
from .test_managed_phones_ha import phone_lab as phone_lab, custom_integration_path as custom_integration_path

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered", [False, True])
@pytest.mark.parametrize("native_override", [False, True])
async def test_authenticated_account_opens_only_its_native_route_window(
    trunks, registered_phones, registered, native_override,
):
    from homeassistant.core import callback
    from homeassistant.helpers.trigger import TriggerConfig
    from custom_components.voip_stack.const import CONF_AUTOMATION_ROUTING_ENABLED
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
    from custom_components.voip_stack.sip_client import SipCallClient
    from custom_components.voip_stack.trigger import VoipCallTrigger
    from custom_components.voip_stack.websocket_api import CALL_EVENT
    from .test_trunk_calls_ha import sip_test_account

    lab = trunks
    lab.runtime.trunk_config[CONF_AUTOMATION_ROUTING_ENABLED] = True
    if registered:
        identity = "transfer_a"
    else:
        identity = "dial-only"
        lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id=identity, name=identity, username=identity,
            kind=EndpointKind.SIP_ACCOUNT))
        sip_test_account(lab, identity)
    registrar = lab.calls.component("registrar")
    assert bool(registrar.registered_contacts(identity)) == registered
    route_events, native_events = [], []
    @callback
    def observe(event):
        if event.data.get("route_request"):
            route_events.append(event.data["call_id"])

    unsubscribe = lab.hass.bus.async_listen(CALL_EVENT, observe)
    detach = None
    if native_override:
        async def selected(payload, description, context):
            native_events.append(payload["call"]["call_id"])

        native = VoipCallTrigger(lab.hass, TriggerConfig("voip_stack.route_requested", options={"caller": identity}))
        detach = await native.async_attach_runner(selected)
    caller = None
    try:
        if registered:
            await asyncio.to_thread(registered_phones[0].dial, "transfer_b")
        else:
            account = registrar.accounts[identity]
            caller = SipCallClient(local_ip="127.0.0.9", local_name=identity, local_uri_user=identity,
                username=identity, password=account.password, local_sip_port=0, local_rtp_port=36622,
                supported_formats=[AudioFormat(16000, frame_ms=20)])
            result = await caller.invite(target="transfer_b", remote_host="127.0.0.1",
                remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=4)
            if result == "ringing":
                result = await caller.wait_for_final(timeout=4)
            assert result == "in_call"
        session, = lab.calls.sessions.values()
        assert route_events == native_events == ([session.call_id] if native_override else [])
        assert session.metadata.get("source_endpoint_id") == identity
    finally:
        if caller is None:
            await asyncio.to_thread(registered_phones[0].hangup)
        else:
            await caller.close()
        unsubscribe()
        if detach:
            detach()
    await wait_until(lambda: not lab.calls.sessions)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("transport", ["UDP", "TCP"])
@pytest.mark.parametrize("offered", [False, True])
async def test_initial_invite_challenges_before_session_or_media(trunks, transport, offered):
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.phone_directory import PhoneDirectory
    from .test_inbound_did_identity import wire_invite
    from .test_mwi_unregister_ha import MwiPeer, TcpMwiPeer

    lab = trunks
    address = ("127.0.0.1", lab.runtime.transport_config["sip_port"])
    # The fixture's two registered UDP trunks share 127.0.0.1.
    source_host = "127.0.0.24" if transport == "UDP" else "127.0.0.1"
    contact = RosterEntry("001111", address=source_host)
    lab.roster.append(contact)
    lab.runtime.directory = PhoneDirectory(lab.roster)
    if transport == "UDP":
        connection, peer = await asyncio.get_running_loop().create_datagram_endpoint(
            MwiPeer, local_addr=(source_host, 0))
    else:
        connection = peer = await TcpMwiPeer().connect(address)
    try:
        request = sip.parse_message(wire_invite(body=offered))
        via = f"SIP/2.0/{transport} {source_host}:{peer.port};branch=z9hG4bK-admission;rport"
        headers = [(k, via if k == "Via" else v) for k, v in request.headers if k != "Content-Length"]
        invite = sip.build_request("INVITE", request.uri, headers, request.body)
        first = await peer.exchange(invite, address)
        repeated = await peer.exchange(invite, address)
        assert first.status_code == repeated.status_code == 401
        assert first.header("WWW-Authenticate") == repeated.header("WWW-Authenticate")
        assert first.header("WWW-Authenticate").startswith("Digest ")
        assert sip.extract_tag(first.header("To")) == sip.extract_tag(repeated.header("To")) != ""
        for method in ("ACK", "CANCEL"):
            control_headers = [(k, "1 " + method if k == "CSeq" else v) for k, v in headers
                               if k != "Content-Type"]
            if method == "ACK":
                control_headers = [(k, first.header("To") if k == "To" else v) for k, v in control_headers]
            peer.transport.sendto(sip.build_request(method, request.uri, control_headers), address)
        cancelled, _ = await peer.receive()
        assert cancelled.status_code == 481 and cancelled.header("CSeq") == "1 CANCEL"
        assert not lab.calls.sessions and not lab.calls.endpoint_claims_snapshot()
        assert not lab.calls.relays_snapshot() and not lab.runtime.rtp_port_pool.get("used")
    finally:
        connection.close()
        if transport == "TCP":
            await connection.wait_closed()


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_local_account_can_authenticate_on_an_ambiguous_provider_address(trunks):
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.sip_client import SipCallClient
    from .test_inbound_did_identity import wire_invite
    from .test_mwi_unregister_ha import MwiPeer
    from .test_trunk_calls_ha import sip_test_account

    lab = trunks
    address = ("127.0.0.1", lab.runtime.transport_config["sip_port"])
    connection, peer = await asyncio.get_running_loop().create_datagram_endpoint(
        MwiPeer, local_addr=("127.0.0.1", 0))
    try:
        ambiguous = await peer.exchange(wire_invite(body=False), address)
        assert ambiguous.status_code == 403 and not lab.calls.sessions
    finally:
        connection.close()
    account = sip_test_account(lab, "sip:bedroom1")
    lab.runtime.endpoints.update("sip:bedroom0", availability="available")
    origin = SipCallClient(local_ip=address[0], local_name=account.display_name, local_uri_user=account.username,
        local_sip_port=0, local_rtp_port=36022, supported_formats=[AudioFormat(16000, frame_ms=20)],
        username=account.username, password=account.password)
    try:
        state = await origin.invite(target="bedroom0", remote_host=address[0], remote_sip_port=address[1], timeout=3)
        if state == "ringing":
            state = await origin.wait_for_final(timeout=3)
        assert state == "in_call"
        session = lab.calls.get_session(origin.dialog_ids.call_id)
        assert session is not None and session.metadata["source_endpoint_id"] == "sip:bedroom1"
        assert not session.metadata.get("inbound_trunk_id")
        assert origin.bye()
        await wait_until(lambda: not lab.calls.sessions)
    finally:
        await origin.close()


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_provider_contact_line_keeps_its_route_with_foreign_authorization(trunks):
    from dataclasses import replace
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.runtime_data import sip_trunks
    from .test_inbound_did_identity import wire_invite

    lab = trunks
    work = sip_trunks(lab.hass)["work"]
    request = sip.parse_message(wire_invite(body=False))
    request = replace(request, uri=request.uri + ";line=" + work.contact_line,
        headers=(*request.headers, ("Authorization", 'Digest realm="provider.example", username="gateway"')))
    listener = lab.calls.component("udp_listener").udp_server.endpoint
    initial = listener._parse_initial_invite(request, ("127.0.0.1", 5060), authorize=True)
    assert initial.trunk_id == "work" and initial.received_via_trunk
    assert not initial.identified_source_endpoint_id


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
async def test_authenticated_invite_retransmission_and_cancel_share_one_ringing_call(registered_phones, trunks):
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.core.sip_auth import build_digest_authorization
    from .test_inbound_did_identity import wire_invite
    from .test_mwi_unregister_ha import MwiPeer

    lab = trunks
    address = ("127.0.0.1", lab.runtime.transport_config["sip_port"])
    connection, peer = await asyncio.get_running_loop().create_datagram_endpoint(
        MwiPeer, local_addr=("127.0.0.24", 0))
    account = lab.calls.component("registrar").accounts["transfer_a"]
    uri = f"sip:transfer_b@127.0.0.1:{address[1]}"
    body = sip.parse_message(wire_invite()).body

    def request(method, sequence, authorization="", to_tag=""):
        headers = [
            ("Via", f"SIP/2.0/UDP 127.0.0.24:{peer.port};branch=z9hG4bK-auth{sequence};rport"),
            ("From", "<sip:transfer_a@127.0.0.1>;tag=caller"),
            ("To", f"<{uri}>" + (f";tag={to_tag}" if to_tag else "")),
            ("Call-ID", "authenticated-ringing"), ("CSeq", f"{sequence} {method}"),
            ("Contact", f"<sip:transfer_a@127.0.0.24:{peer.port}>"),
        ]
        if authorization:
            headers.append(("Authorization", authorization))
        if method == "INVITE":
            headers.append(("Content-Type", "application/sdp"))
        return sip.build_request(method, uri, headers, body if method == "INVITE" else b"")

    try:
        challenge = await peer.exchange(request("INVITE", 1), address)
        assert challenge.status_code == 401 and not lab.calls.sessions
        peer.transport.sendto(request("ACK", 1, to_tag=sip.extract_tag(challenge.header("To"))), address)

        def digest(password):
            return build_digest_authorization(challenge_header=challenge.header("WWW-Authenticate"),
                username=account.username, password=password, method="INVITE", uri=uri)

        denied = await peer.exchange(request("INVITE", 2, digest("incorrect")), address)
        assert denied.status_code == 401 and not lab.calls.sessions
        assert not lab.calls.endpoint_claims_snapshot() and not lab.runtime.rtp_port_pool.get("used")
        peer.transport.sendto(request("ACK", 2, to_tag=sip.extract_tag(denied.header("To"))), address)
        authorized = request("INVITE", 3, digest(account.password))
        trying = await peer.exchange(authorized, address)
        assert trying.status_code == 100
        await asyncio.to_thread(registered_phones[1].wait_for, "Incoming call", 5)
        ringing, _ = await peer.receive()
        assert ringing.status_code == 180
        duplicate = await peer.exchange(authorized, address)
        assert duplicate.status_code == 180 and duplicate.header("To") == ringing.header("To")
        assert list(lab.calls.sessions) == ["authenticated-ringing"]
        peer.transport.sendto(request("CANCEL", 3), address)
        finals = [response for response, _ in (await peer.receive(), await peer.receive())]
        assert {(r.status_code, r.header("CSeq")) for r in finals} == {(200, "3 CANCEL"), (487, "3 INVITE")}
        peer.transport.sendto(request("ACK", 3, to_tag=sip.extract_tag(ringing.header("To"))), address)
        await wait_until(lambda: not lab.calls.sessions)
        assert not lab.calls.endpoint_claims_snapshot() and not lab.calls.relays_snapshot()
        assert not lab.runtime.rtp_port_pool.get("used")
    finally:
        connection.close()


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize(("label", "wire_user"), [
    ("Reception@Hotel", "Reception%40Hotel"),
    ("Caffè 20%@Casa", "Caff%C3%A8%2020%25%40Casa"),
    ("*42", "*42"),
])
async def test_registered_call_to_literal_name_keeps_the_registered_sip_identity(
    registered_phones, trunks, tmp_path, label, wire_user,
):
    from custom_components.voip_stack.endpoint_routing import roster_from_peers

    caller, handset, other = registered_phones
    lab = trunks
    registrar = lab.calls.component("registrar")
    previous = registrar.accounts["transfer_b"]
    lab.runtime.endpoints.update("transfer_b", name=label)
    lab.roster[:] = roster_from_peers(lab.hass, [], registrar.registered_roster_entries())
    lab.runtime.directory.replace(lab.roster)
    port = lab.runtime.transport_config["sip_port"]
    capture = SipCapture()
    capture.start()
    try:
        await asyncio.to_thread(caller.command, f"/dial sip:{wire_user}@127.0.0.1:{port}")
        await asyncio.to_thread(handset.wait_for, "Call established", 5)
        await asyncio.to_thread(caller.wait_for, "Call established", 5)
        assert f"INVITE sip:{wire_user}@127.0.0.1:{port} SIP/2.0\r\n".encode() in capture.buffer
        assert "Incoming call" not in other.read()
        for endpoint in ("transfer_a", "transfer_b"):
            await wait_until(lambda: any(wav.stat().st_size > 8192
                                         for wav in (tmp_path / endpoint).glob("*-dec.wav")))
    finally:
        try:
            await asyncio.to_thread(caller.hangup)
            await wait_until(lambda: not lab.calls.sessions)
        finally:
            capture.stop()
            (tmp_path / "literal-name.pcap").write_bytes(capture.buffer)
            capture.clear()
    assert registrar.accounts["transfer_b"] == previous
    assert not lab.calls.endpoint_claims_snapshot()
    assert not lab.calls.relays_snapshot()
    assert not lab.runtime.rtp_port_pool.get("used")
    # The PBX can be idle before the independent peers finalize their WAV headers.
    for peer in (caller, handset):
        await asyncio.to_thread(peer.close)
    assert_peer_audio_tones(tmp_path, (("transfer_b", 450), ("transfer_a", 550)))


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize(("kind", "name", "extension", "wire_user"), [
    ("ring", "RG Casa", "", "RG%20Casa"),
    ("ring", "RG Casa", "861", "861"),
    ("conference", "CG Casa", "", "CG%20Casa"),
    ("conference", "CG Casa", "862", "862"),
])
async def test_esp_group_destination_reaches_the_member_over_sip(
    registered_phones, trunks, tmp_path, kind, name, extension, wire_user,
):
    """Dial the address supplied for the group from an independent registered phone."""
    lab = trunks
    caller, ring_member, conference_member = registered_phones
    member, phone, tone = (("transfer_b", ring_member, 550) if kind == "ring"
                           else ("transfer_c", conference_member, 650))
    group = RosterEntry(name, ha_bridge=True, extension=extension,
                        metadata={"group_type": kind, "members": [member], "ring_members": [member]})
    lab.roster.append(group)
    lab.runtime.directory.replace(lab.roster)
    port = lab.runtime.transport_config["sip_port"]
    route = resolve_esp_origin(name, lab.roster, f"sip:HA@127.0.0.1:{port}")
    assert route.action == RouteAction.BRIDGE
    assert route.entry is group
    assert route.target == (extension or name)
    capture = SipCapture()
    capture.start()
    try:
        await asyncio.to_thread(caller.command, "/dial " + route.sip_uri)
        await asyncio.to_thread(caller.wait_for, "Call established", 5)
        await asyncio.to_thread(phone.wait_for, "Call established", 5)
        assert f"INVITE sip:{wire_user}@127.0.0.1:{port} SIP/2.0\r\n".encode() in capture.buffer
        for endpoint in ("transfer_a", member):
            await wait_until(lambda: any(wav.stat().st_size > 8192
                                         for wav in (tmp_path / endpoint).glob("*-dec.wav")))
    finally:
        try:
            await asyncio.to_thread(caller.hangup)
            await asyncio.to_thread(phone.hangup)
            await wait_until(lambda: not lab.calls.sessions)
        finally:
            capture.stop()
            (tmp_path / "group-sip.pcap").write_bytes(capture.buffer)
            capture.clear()
    assert not lab.calls.endpoint_claims_snapshot()
    assert not lab.calls.relays_snapshot()
    assert not lab.runtime.rtp_port_pool.get("used")
    for peer in (caller, phone):
        await asyncio.to_thread(peer.close)
    assert_peer_audio_tones(tmp_path, (("transfer_a", tone), (member, 450)))


@pytest.fixture
async def ringing_group(trunks, registered_phones, request):
    lab = trunks
    kind = request.param
    manager = None
    if kind == "queue":
        entry = lab.hass.config_entries.async_entries("voip_stack")[0]
        manager = QueueManager(lab.hass, entry)
        manager.definitions["controlled-queue"] = QueueDefinition("controlled-queue", "Controlled", ("transfer_b", "transfer_c"))
        manager.policies["controlled-queue"] = QueuePolicy(ring_timeout=5)
        manager.agent_states["controlled-queue"] = {"transfer_b": "available", "transfer_c": "available"}
        lab.runtime.queues = manager
        lab.roster.extend(manager.roster_entries())
    else:
        lab.roster.append(RosterEntry("Controlled", ha_bridge=True,
            metadata={"group_type": "ring", "members": ["transfer_b", "transfer_c"]}))
    caller, first, second = registered_phones
    await asyncio.to_thread(caller.command, "/dial Controlled")
    await wait_until(lambda: any(session.endpoint_claims.get("transfer_b") and session.endpoint_claims.get("transfer_c")
        for session in lab.calls.sessions.values()))
    session = next(iter(lab.calls.sessions.values()))
    await wait_until(lambda: all(any(leg.endpoint_id == identity and leg.state == "remote_ringing"
        for leg in session.legs.values()) for identity in ("transfer_b", "transfer_c")))
    yield SimpleNamespace(lab=lab, caller=caller, first=first, second=second, session=session, manager=manager)
    await asyncio.to_thread(caller.hangup)
    await wait_until(lambda: not lab.calls.sessions)
    if manager is not None:
        await manager.close()


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": ("transfer_b", "transfer_c")}], indirect=True)
@pytest.mark.parametrize("ringing_group", ["group", "queue"], indirect=True)
@pytest.mark.parametrize("operation", ["hangup", "decline"])
async def test_sip_caller_control_ends_every_owned_offer(ringing_group, operation):
    env = ringing_group
    session = env.session
    await env.lab.hass.services.async_call("voip_stack", operation, {
        "device_id": "transfer_a", "call_id": session.call_id,
    }, blocking=True)
    await asyncio.wait_for(session.terminated.wait(), 3)
    assert not env.lab.calls.sessions and not session.tasks
    assert session.terminal_reason == ("local_hangup" if operation == "hangup" else "declined")
    assert all(not endpoint.active_call_id for endpoint in env.lab.runtime.endpoints.endpoints)
    assert not env.lab.runtime.rtp_port_pool.get("used")
    if env.manager is not None:
        assert not env.manager.waiting and not env.manager.reservations
    # A repeated command cannot affect another call or revive this generation.
    await env.lab.hass.services.async_call("voip_stack", operation, {
        "device_id": "transfer_a", "call_id": session.call_id,
    }, blocking=True)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": ("transfer_b", "transfer_c")}], indirect=True)
@pytest.mark.parametrize("ringing_group", ["group", "queue"], indirect=True)
@pytest.mark.parametrize("operation", ["hangup", "decline"])
async def test_sip_recipient_control_retires_only_its_fork_offer(ringing_group, operation):
    env = ringing_group
    session = env.session
    await env.lab.hass.services.async_call("voip_stack", operation, {
        "device_id": "transfer_b", "call_id": session.call_id,
    }, blocking=True)
    assert session.live and not session.terminated.is_set()
    assert env.lab.runtime.endpoints.get("transfer_a").active_call_id == session.call_id
    assert not env.lab.runtime.endpoints.get("transfer_b").active_call_id
    assert env.lab.runtime.endpoints.get("transfer_c").active_call_id == session.call_id
    await asyncio.to_thread(env.second.command, "/accept")
    await wait_until(lambda: session.metadata.get("dest_endpoint_id") == "transfer_c")
    relay = env.lab.calls.resource_for(session.call_id, "relay")
    await wait_until(lambda: relay.left_tx_packets > 5 and relay.right_tx_packets > 5)
    assert env.caller.read().count("Call established") == 1
    assert "Call established" not in env.first.read()
    await env.lab.hass.services.async_call("voip_stack", "hangup", {
        "device_id": "transfer_c", "call_id": session.call_id,
    }, blocking=True)
    await asyncio.wait_for(session.terminated.wait(), 3)
    assert not env.lab.calls.sessions and not session.tasks and not env.lab.runtime.rtp_port_pool.get("used")


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_sip_control_rejects_wrong_endpoint_generation_acl_and_unsupported_hardware(trunks, registered_phones, hass_read_only_user):
    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.helpers import device_registry as dr
    from custom_components.voip_stack.physical_phone import normalize_physical_phone, sync_physical_phone_devices
    lab = trunks
    caller = registered_phones[0]
    await asyncio.to_thread(caller.command, "/dial transfer_b")
    await wait_until(lambda: any(session.metadata.get("dest_endpoint_id") == "transfer_b" for session in lab.calls.sessions.values()))
    session = next(iter(lab.calls.sessions.values()))
    for data in ({"device_id": "transfer_c", "call_id": session.call_id},
                 {"device_id": "transfer_b", "call_id": session.call_id, "expected_generation": session.generation + 1}):
        with pytest.raises(ServiceValidationError):
            await lab.hass.services.async_call("voip_stack", "hangup", data, blocking=True)
        assert session.live
    with pytest.raises(Unauthorized):
        await lab.hass.services.async_call("voip_stack", "hangup", {
            "device_id": "transfer_b", "call_id": session.call_id,
        }, blocking=True, context=Context(user_id=hass_read_only_user.id))
    # Exercise the adapter guard too, independently of the outer hangup service.
    with pytest.raises(ServiceValidationError, match="generation"):
        await lab.runtime.phones.control(ServiceCall(lab.hass, "voip_stack", "decline", {
            "device_id": "transfer_b", "call_id": session.call_id, "expected_generation": session.generation + 1,
        }), PhoneOperation.DECLINE, CallControlRequest(call_id=session.call_id))
    with pytest.raises(ServiceValidationError) as denied:
        await lab.hass.services.async_call("voip_stack", "answer", {
            "device_id": "transfer_b", "call_id": session.call_id,
        }, blocking=True)
    # The common account supports HA answering, but this native SIP dialog
    # is already answered and has no pending HA offer to accept.
    assert denied.value.translation_key == "call_not_ringing"
    assert session.live
    # Generic physical inventory resolves a registered line, not a vendor HTTP
    # endpoint. Its second configured line must never be guessed for this call.
    entry = lab.hass.config_entries.async_entries("voip_stack")[0]
    physical = normalize_physical_phone({"name": "Generic Fanvil", "mac": "0200000000ac", "profile_id": "fanvil/x3u-pro",
        "lines": [{"slot": "1", "endpoint_id": "transfer_b"}, {"slot": "2", "endpoint_id": "transfer_c"}]})
    lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(subentry_type="physical_phone",
        title=physical["name"], unique_id=physical["phone_id"], data=physical))
    sync_physical_phone_devices(lab.hass, entry)
    device = next(device for device in dr.async_entries_for_config_entry(dr.async_get(lab.hass), entry.entry_id)
        if ("voip_stack", "physical_phone:" + physical["phone_id"]) in device.identifiers)
    logical_devices = {identity: lab.runtime.endpoints.get(identity).device_id for identity in ("transfer_b", "transfer_c")}
    with pytest.raises(ServiceValidationError) as hardware_denied:
        await lab.hass.services.async_call("voip_stack", "answer", {
            "device_id": device.id, "call_id": session.call_id,
        }, blocking=True)
    assert hardware_denied.value.translation_key == "phone_operation_not_supported"
    assert session.live
    await lab.hass.services.async_call("voip_stack", "hangup", {
        "device_id": device.id, "call_id": session.call_id,
    }, blocking=True)
    await asyncio.wait_for(session.terminated.wait(), 3)
    await lab.hass.services.async_call("voip_stack", "hangup", {
        "device_id": device.id, "call_id": session.call_id, "expected_generation": session.generation,
    }, blocking=True)
    assert {identity: lab.runtime.endpoints.get(identity).device_id for identity in logical_devices} == logical_devices
    with pytest.raises(ServiceValidationError, match="generation"):
        await lab.hass.services.async_call("voip_stack", "hangup", {
            "device_id": device.id, "call_id": session.call_id, "expected_generation": session.generation + 1,
        }, blocking=True)
    with pytest.raises(ServiceValidationError):
        await lab.hass.services.async_call("voip_stack", "hangup", {
            "device_id": "transfer_c", "call_id": session.call_id, "expected_generation": session.generation,
        }, blocking=True)


async def test_physical_device_line_resolution_never_guesses_or_changes_logical_device_ids(phone_lab):
    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.helpers import device_registry as dr
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.physical_phone import normalize_physical_phone, sync_physical_phone_devices
    lab = phone_lab
    for identity in ("line-a", "line-b"):
        lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id=identity, name=identity, username=identity,
            device_id="logical-" + identity, kind=EndpointKind.SIP_ACCOUNT))
    data = normalize_physical_phone({"name": "Generic Fanvil", "mac": "0200000000ab", "profile_id": "fanvil/x3u-pro",
        "lines": [{"slot": "1", "endpoint_id": "line-a"}, {"slot": "2", "endpoint_id": "line-b"}]})
    item = ConfigSubentry(subentry_type="physical_phone", title=data["name"], unique_id=data["phone_id"], data=data)
    lab.hass.config_entries.async_add_subentry(lab.entry, item)
    sync_physical_phone_devices(lab.hass, lab.entry)
    device = next(device for device in dr.async_entries_for_config_entry(dr.async_get(lab.hass), lab.entry.entry_id)
        if ("voip_stack", "physical_phone:" + data["phone_id"]) in device.identifiers)
    with pytest.raises(ServiceValidationError):
        await lab.runtime.phones.resolve_source(ServiceCall(lab.hass, "voip_stack", "hangup", {"device_id": device.id}), PhoneOperation.HANGUP)
    call = ServiceCall(lab.hass, "voip_stack", "hangup", {"device_id": device.id, "line_id": "2"})
    resolved = await lab.runtime.phones.resolve_source(call, PhoneOperation.HANGUP)
    assert resolved.endpoint_id == "line-b"
    lab.runtime.endpoints.claim_call("line-a", "active-a")
    resolved = await lab.runtime.phones.resolve_source(ServiceCall(lab.hass, "voip_stack", "hangup", {
        "device_id": device.id, "call_id": "active-a",
    }), PhoneOperation.HANGUP)
    assert resolved.endpoint_id == "line-a"
    assert lab.runtime.endpoints.get("line-a").device_id == "logical-line-a"
    assert lab.runtime.endpoints.get("line-b").device_id == "logical-line-b"
    lab.runtime.endpoints.release_call("line-a", "active-a")
