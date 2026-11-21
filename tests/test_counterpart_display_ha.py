"""The dialed route and the displayed connected identity have separate contracts."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from custom_components.voip_stack.core import sip
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
from custom_components.voip_stack.roster import RosterEntry, dump_roster_json

from .test_automation_originate_ha import lab as lab

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
    lab.runtime.endpoints.update("sip:bedroom0", name=name or "204", extension="204", availability="available")
    entry = replace(lab.roster[1], id="204", name=name, extension="204")
    lab.roster[1] = entry
    lab.runtime.directory.replace([*lab.roster, RosterEntry("Remote reception", name="Unrelated directory contact")])
    lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    phone = lab.phones[0]
    def identity_response(request, address, status, body=b""):
        to = request.header("To")
        if remote_name and status == 200 and request.method == "INVITE":
            to = f'"{remote_name}" <{sip.name_addr_uri(to)}>'
        headers = [(key, request.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
        headers += [("To", to if ";tag=" in to else to + ";tag=bedroom"), ("Supported", "from-change")]
        if body:
            port = phone.transport.get_extra_info("sockname")[1]
            headers += [("Content-Type", "application/sdp"), ("Contact", f"<sip:bedroom@127.0.0.1:{port}>")]
        reason = {180: "Ringing", 200: "OK", 486: "Busy Here", 487: "Request Terminated"}[status]
        phone.transport.sendto(sip.build_response(status, reason, headers, body), address)

    monkeypatch.setattr(phone, "respond", identity_response)
    await lab.hass.services.async_call("voip_stack", "call", {
        "device_id": source.device_id, "destination": "204",
    }, blocking=True)
    async with asyncio.timeout(2):
        while _ha_softphone_state(lab.hass, source.endpoint_id)["state"] != "in_call":
            await asyncio.sleep(.01)
    current = _ha_softphone_state(lab.hass, source.endpoint_id)
    session = lab.calls.get_session(current["call_id"])
    assert phone.invites[-1].uri == entry.sip_uri
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
        assert snapshot(lab.runtime, source.endpoint_id)[0].peer_name == updated
        assert _ha_softphone_state(lab.hass, source.endpoint_id)["dialed_target"] == "204"
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
