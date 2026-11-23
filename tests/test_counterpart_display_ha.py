"""The dialed route and the displayed connected identity have separate contracts."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from custom_components.voip_stack.core import sip
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
from custom_components.voip_stack.roster import RosterEntry, dump_roster_json

from .test_automation_originate_ha import lab as lab
from .test_trunk_calls_ha import trunks as trunks
from .test_sip_phone_transfers_ha import registered_phones as registered_phones

pytestmark = pytest.mark.ha


@pytest.mark.parametrize(("name", "remote_name", "expected"), [
    ("Waveshare P4 Touch", "", "Waveshare P4 Touch"),
    ("Waveshare P4 Touch", "Remote reception", "Remote reception"),
    ("", "", "204"),
    ("", "54da57478a40e69420df7586730f6706", "54da57478a40e69420df7586730f6706"),
])
async def test_outgoing_number_displays_identity_without_rewriting_request_uri(
    lab, monkeypatch, name, remote_name, expected,
):
    from custom_components.voip_stack import softphone_originate
    from custom_components.voip_stack.browser_phone_projection import _ha_softphone_state
    from custom_components.voip_stack.dialog_info import snapshot
    from custom_components.voip_stack.logbook import _describe_call, account_call_details

    source = PhoneEndpoint("identity-browser", "Casa", "browser", device_id="identity-browser-device")
    lab.runtime.endpoints.register(source)
    lab.runtime.endpoints.update("bedroom0", name=name or "204", extension="204", availability="available")
    previous = next(row for row in lab.roster
                    if row.metadata.get("endpoint_id") == "bedroom0")
    entry = replace(previous, name=name, extension="204")
    lab.roster[:] = [entry if row is previous else row for row in lab.roster]
    lab.runtime.directory.replace([*lab.roster, RosterEntry("Remote reception", name="Unrelated directory contact")])
    lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    phone = lab.phones[0]
    phone.auto_answer = False
    allow_ringing = False
    answers = []

    def identity_response(request, address, status, body=b""):
        if status == 180 and not allow_ringing:
            return
        to = request.header("To")
        if remote_name and status == 200 and request.method == "INVITE":
            to = f'"{remote_name}" <{sip.name_addr_uri(to)}>'
        headers = [(key, request.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
        headers += [("To", to if ";tag=" in to else to + ";tag=bedroom"), ("Supported", "from-change")]
        if body:
            port = phone.transport.get_extra_info("sockname")[1]
            headers += [("Content-Type", "application/sdp"), ("Contact", f"<sip:bedroom@127.0.0.1:{port}>")]
        reason = {180: "Ringing", 200: "OK", 486: "Busy Here", 487: "Request Terminated"}[status]
        if status == 200 and request.method == "INVITE":
            answers.append(to)
        phone.transport.sendto(sip.build_response(status, reason, headers, body), address)

    monkeypatch.setattr(phone, "respond", identity_response)
    originate = asyncio.create_task(lab.hass.services.async_call("voip_stack", "call", {
        "device_id": source.device_id, "destination": "204",
    }, blocking=True))
    try:
        await asyncio.wait_for(phone.invited.wait(), 2)
        calling = _ha_softphone_state(lab.hass, source.endpoint_id)
        assert calling["state"] == "calling"
        assert calling["peer_name"] == (name or "204")
        assert calling["dialed_target"] == "204"
        assert not answers
        allow_ringing = True
        phone.respond(phone.last_request, phone.remote, 180)
        await asyncio.wait_for(originate, 2)
        assert not answers
        phone.answer()
    finally:
        if not originate.done():
            originate.cancel()
            await asyncio.gather(originate, return_exceptions=True)
    async with asyncio.timeout(2):
        while _ha_softphone_state(lab.hass, source.endpoint_id)["state"] != "in_call":
            await asyncio.sleep(.01)
    current = _ha_softphone_state(lab.hass, source.endpoint_id)
    session = lab.calls.get_session(current["call_id"])
    assert phone.invites[-1].uri == entry.sip_uri
    assert sip.name_addr_uri(answers[-1]) == sip.name_addr_uri(phone.invites[-1].header("To"))
    assert current["peer_name"] == expected
    assert current["dialed_target"] == "204"
    row = snapshot(lab.runtime, source.endpoint_id)[0]
    assert row.peer_name == expected
    assert account_call_details({**session.metadata, "callee": session.callee}, source.endpoint_id,
                                {"peer_name": row.peer_name}) == ("outgoing", expected)
    assert expected in _describe_call({**current, "type": "ended"})["message"]
    for sequence, (display, user, updated) in enumerate([
        ("Remote operator", "205", "Remote operator"),
        ("", "206", "206"),
        ("", "204", "204"),
        ("Anonymous", "anonymous", "Anonymous"),
    ], start=2):
        request = phone.invites[-1]
        headers = [
            ("Via", f"SIP/2.0/UDP 127.0.0.1;branch=z9hG4bK-identity-{sequence}"),
            ("From", f'"{display}" <sip:{user}@127.0.0.1>;tag=bedroom'),
            ("To", request.header("From")), ("Call-ID", session.call_id),
            ("CSeq", f"{sequence} UPDATE"), ("Supported", "from-change"),
        ]
        if user == "anonymous":
            headers.append(("Privacy", "id"))
        phone.transport.sendto(sip.build_request("UPDATE", sip.name_addr_uri(request.header("Contact")), headers), phone.remote)
        async with asyncio.timeout(2):
            while _ha_softphone_state(lab.hass, source.endpoint_id)["peer_name"] != updated:
                await asyncio.sleep(.01)
        assert lab.calls.get_session(session.call_id).metadata["connected_party"] == updated
        updated_row = snapshot(lab.runtime, source.endpoint_id)[0]
        assert updated_row.peer_name == updated
        updated_state = _ha_softphone_state(lab.hass, source.endpoint_id)
        assert updated_state["dialed_target"] == "204"
        assert updated_state["call_id"] == session.call_id
        assert account_call_details({**session.metadata, "callee": session.callee}, source.endpoint_id,
                                    {"peer_name": updated_row.peer_name}) == ("outgoing", updated)
        assert updated in _describe_call({**updated_state, "type": "ended"})["message"]
    await lab.hass.services.async_call("voip_stack", "hangup", {
        "device_id": source.device_id, "call_id": session.call_id,
    }, blocking=True)
    assert _ha_softphone_state(lab.hass, source.endpoint_id)["peer_name"] == "Anonymous"


async def test_party_change_without_a_name_does_not_restore_previous_name(lab):
    from custom_components.voip_stack.browser_phone_projection import _ha_softphone_state
    from custom_components.voip_stack.browser_phone_runtime import _set_ha_softphone_call_state

    phone = PhoneEndpoint("identity-browser", "Casa", "browser")
    lab.runtime.endpoints.register(phone)
    scope = {"endpoint_id": phone.endpoint_id, "call_id": "party-change", "direction": "outgoing"}
    _set_ha_softphone_call_state(lab.hass, "in_call", callee="204", peer_name="Waveshare P4 Touch", **scope)
    _set_ha_softphone_call_state(lab.hass, "in_call", connected_party="205", remote_uri="sip:205@example.test", **scope)
    assert _ha_softphone_state(lab.hass, phone.endpoint_id)["peer_name"] == "205"
    _set_ha_softphone_call_state(lab.hass, "idle", **scope)
    assert _ha_softphone_state(lab.hass, phone.endpoint_id)["peer_name"] == "205"


def test_explicit_sip_display_name_precedes_a_directory_alias():
    from custom_components.voip_stack.counterpart_display import counterpart_display
    from custom_components.voip_stack.phone_directory import PhoneDirectory

    directory = PhoneDirectory([RosterEntry("Remote reception", name="Unrelated directory contact")])
    assert counterpart_display("Remote reception", display_name="Remote reception", directory=directory) == "Remote reception"


def test_sip_user_display_uses_the_existing_uri_parser():
    from custom_components.voip_stack.counterpart_display import counterpart_display

    assert counterpart_display("sips:remote%20desk@example.test;transport=tls") == "remote desk"


def test_e164_numeric_echo_uses_the_directory_without_replacing_explicit_or_updated_names():
    from custom_components.voip_stack.counterpart_display import counterpart_display
    from custom_components.voip_stack.phone_directory import PhoneDirectory

    directory = PhoneDirectory([RosterEntry("mobile", name="Cliente", number="+393331234567"),
                                RosterEntry("numeric-name", name="Different contact", number="123")])
    assert counterpart_display("+393331234567", fallback="+393331234567",
        display_name="+393331234567", directory=directory) == "Cliente"
    assert counterpart_display("123", fallback="service", display_name="123", directory=directory) == "123"
    assert counterpart_display("+393331234567", fallback="+393331234567",
        display_name="+393331234567", directory=directory, updated=True) == "+393331234567"


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("number,display,privacy,expected", [
    ("+393331234567", "+393331234567", "", "Cliente"),
    ("service", "123", "", "123"),
    ("+393331234567", "Anonymous", "id", "Anonymous"),
])
async def test_incoming_trunk_display_preserves_real_names_and_resolves_e164_echo(trunks, number, display, privacy, expected):
    from custom_components.voip_stack.counterpart_display import counterpart_display
    from custom_components.voip_stack.runtime_data import sip_trunks
    from .test_inbound_did_identity import wire_invite

    lab = trunks
    lab.runtime.directory.replace([RosterEntry("mobile", name="Cliente", number="+393331234567"),
                                   RosterEntry("numeric-name", name="Different contact", number="123")])
    work = sip_trunks(lab.hass)["work"]
    request = sip.parse_message(wire_invite(body=False))
    from_value = f'"{display}" <sip:{number}@provider.example>;tag=external'
    headers = tuple((key, from_value if key == "From" else value) for key, value in request.headers)
    if privacy:
        headers += (("Privacy", privacy),)
    request = replace(request, uri=request.uri + ";line=" + work.contact_line, headers=headers)
    listener = lab.calls.component("udp_listener").udp_server.endpoint
    invite = listener._parse_initial_invite(request, ("127.0.0.1", 5060), authorize=True)
    assert invite.received_via_trunk and invite.caller_number == number and invite.caller_name == display
    assert counterpart_display(invite.caller, fallback=invite.caller_number,
        display_name=invite.caller_name, directory=lab.runtime.directory) == expected


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("remote_name", ["n-IA-hane", "Remote reception"])
async def test_incoming_caller_name_survives_public_forward_to_another_phone(
    trunks, registered_phones, remote_name, tmp_path,
):
    await _assert_incoming_caller_identity_through_forward(
        trunks, registered_phones, remote_name, tmp_path,
    )


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("remote_name", ["n-IA-hane", "Remote reception"])
async def test_forwarded_phone_receives_original_sip_caller_name_and_number(
    trunks, remote_name, tmp_path,
):
    await _assert_incoming_caller_identity_through_forward(
        trunks, (), remote_name, tmp_path,
        receiving_phone=trunks.phones[0], caller_number="+3902123456",
    )


async def _assert_incoming_caller_identity_through_forward(
    lab, registered_phones, remote_name, tmp_path, *,
    receiving_phone=None, caller_number="external",
):
    from custom_components.voip_stack.browser_phone_projection import _ha_softphone_state
    from custom_components.voip_stack.config import normalized_trunk_config
    from custom_components.voip_stack.phone_endpoint import EndpointKind
    from custom_components.voip_stack.sip_trunk import SipTrunkClient
    from custom_components.voip_stack.trunk_runtime import trunk_client_config

    browser = PhoneEndpoint("casa", "Casa", EndpointKind.BROWSER, username="Casa",
        device_id="browser-casa", availability="available")
    lab.runtime.endpoints.register(browser)
    lab.roster.append(RosterEntry("Casa", metadata={"local_ha": True, "endpoint_id": "casa"}))
    target_username = "bedroom0" if receiving_phone is not None else "transfer_b"
    lab.runtime.endpoints.update(target_username, name="Waveshare P4 Touch")
    for index, entry in enumerate(lab.roster):
        if entry.metadata.get("endpoint_id") == target_username:
            lab.roster[index] = replace(entry, name="Waveshare P4 Touch")
    # Explicit SIP display must win even when another contact uses that alias.
    lab.roster.append(RosterEntry(remote_name, name="Unrelated directory alias"))
    lab.runtime.directory.replace(lab.roster)
    lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
    listener = ("127.0.0.1", lab.runtime.transport_config["sip_port"])
    config = normalized_trunk_config({"trunk_enabled": True, "trunk_register": False,
        "trunk_server": "127.0.0.2", "trunk_allowed_ips": ["127.0.0.2/32"],
        "trunk_inbound_mode": "direct", "trunk_inbound_default_target": "Casa"})
    lab.runtime.trunk_configs["identity"] = config
    work = SipTrunkClient(config=trunk_client_config(config), local_ip="127.0.0.1",
        local_sip_port=listener[1], trunk_id="identity")
    lab.calls.adopt_component("trunk:identity", work, closer=work.stop)
    uri = f"sip:Casa@127.0.0.1:{listener[1]};line={work.contact_line}"
    call_id = "incoming-forward-" + remote_name.replace(" ", "-")
    messages = []
    final = asyncio.Queue()

    class Caller(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, raw, address):
            response = sip.parse_message(raw)
            messages.append(response)
            if response.status_code and response.status_code >= 200:
                final.put_nowait(response)
                if response.header("CSeq") == "1 INVITE" and response.status_code == 200:
                    self.transport.sendto(sip.build_request("ACK", uri,
                        [(key, response.header(key)) for key in ("Via", "From", "To", "Call-ID")]
                        + [("CSeq", "1 ACK")]), address)

    transport, caller = await lab.hass.loop.create_datagram_endpoint(Caller, local_addr=("127.0.0.2", 0))
    source_port = transport.get_extra_info("sockname")[1]
    headers = [("Via", f"SIP/2.0/UDP 127.0.0.2:{source_port};branch=z9hG4bK-incoming-forward;rport"),
        ("From", f'"{remote_name}" <sip:{caller_number}@provider.example>;tag=external'),
        ("To", "<sip:Casa@127.0.0.1>"), ("Call-ID", call_id), ("CSeq", "1 INVITE"),
        ("Contact", f"<sip:{caller_number}@127.0.0.2:{source_port}>"), ("Content-Type", "application/sdp")]
    offer = ("v=0\r\no=- 1 1 IN IP4 127.0.0.2\r\ns=Incoming caller\r\n"
        "c=IN IP4 127.0.0.2\r\nt=0 0\r\nm=audio 35000 RTP/AVP 110\r\n"
        "a=rtpmap:110 L16/16000/1\r\na=ptime:20\r\na=sendrecv\r\n")
    try:
        transport.sendto(sip.build_request("INVITE", uri, headers, offer.encode()), listener)
        async with asyncio.timeout(3):
            while _ha_softphone_state(lab.hass, "casa")["state"] != "ringing":
                await asyncio.sleep(.01)
        ringing = _ha_softphone_state(lab.hass, "casa")
        assert ringing["caller"] == ringing["peer_name"] == remote_name
        await lab.hass.services.async_call("voip_stack", "forward", {
            "device_id": browser.device_id, "call_id": call_id,
            "destination": target_username if receiving_phone is not None else "Waveshare P4 Touch",
        }, blocking=True)
        response = await asyncio.wait_for(final.get(), 3)
        assert response.status_code == 200
        async with asyncio.timeout(3):
            while lab.calls.get_session(call_id).state != "in_call":
                await asyncio.sleep(.01)
        connected = _ha_softphone_state(lab.hass, "casa")
        assert connected["caller"] == connected["peer_name"] == remote_name
        received_from = ""
        if receiving_phone is not None:
            received = receiving_phone.invites[-1]
            received_from = received.header("From")
            assert sip.name_addr_display_name(received_from) == remote_name
            assert sip.parse_sip_uri(sip.name_addr_uri(received_from)).user == caller_number
        else:
            assert "Call established" in registered_phones[1].read()
        dialog = [(key, response.header(key)) for key in ("Via", "From", "To", "Call-ID")]
        transport.sendto(sip.build_request("BYE", uri, [*dialog, ("CSeq", "2 BYE")]), listener)
        bye = await asyncio.wait_for(final.get(), 3)
        assert bye.status_code == 200 and bye.header("CSeq") == "2 BYE"
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(.01)
        terminal = _ha_softphone_state(lab.hass, "casa")
        assert terminal["peer_name"] == remote_name
        assert not lab.runtime.rtp_port_pool.get("used") and not lab.calls.endpoint_claims_snapshot()
        (tmp_path / "incoming-forward-identity.json").write_text(json.dumps({
            "caller": remote_name, "caller_number": caller_number,
            "received_from": received_from, "ringing": ringing, "connected": connected,
            "terminal": terminal, "statuses": [message.status_code for message in messages],
        }, indent=2) + "\n")
    finally:
        transport.close()
