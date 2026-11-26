"""Shared discovery never makes a registrar Contact or endpoint policy stale."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from homeassistant.core import Context, ServiceCall
from homeassistant.exceptions import ServiceValidationError

from custom_components.voip_stack import automation_originate, automation_routing
from custom_components.voip_stack.core import sip
from custom_components.voip_stack.core.sip_auth import build_digest_authorization
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.inbound_routing import automation as route_automation
from custom_components.voip_stack.peer import Peer
from custom_components.voip_stack.roster import RosterEntry
from .test_automation_originate_ha import lab as lab, PHONE_PASSWORD, assert_call_resources_released

pytestmark = pytest.mark.ha


async def register_contact(lab, *, contact, address, expires):
    """Apply an authenticated REGISTER through the real production registrar."""
    registrar = lab.calls.component("registrar")
    uri = f"sip:127.0.0.1:{lab.runtime.transport_config['sip_port']}"
    identity = "fresh-contact-" + uuid4().hex

    async def exchange(cseq, authorization=""):
        headers = [("Via", f"SIP/2.0/UDP {address[0]}:{address[1]};branch=z9hG4bK-{identity}-{cseq};rport"),
            ("From", "<sip:bedroom0@127.0.0.1>;tag=fresh"), ("To", "<sip:bedroom0@127.0.0.1>"),
            ("Call-ID", identity), ("CSeq", f"{cseq} REGISTER"), ("Contact", contact), ("Expires", str(expires))]
        if authorization:
            headers.append(("Authorization", authorization))
        return await registrar.handle_register(sip.parse_message(sip.build_request("REGISTER", uri, headers)), address, "UDP")

    challenge = await exchange(1)
    assert challenge.status == 401
    header = next(value for name, value in challenge.headers if name.casefold() == "www-authenticate")
    authorization = build_digest_authorization(challenge_header=header, username="bedroom0",
        password=PHONE_PASSWORD, method="REGISTER", uri=uri)
    assert (await exchange(2, authorization)).status == 200


async def prepared_call(lab):
    for phone in lab.phones:
        phone.direction = "recvonly"
    preparation = await automation_originate.async_prepare_application_calls(lab.hass)
    endpoint = lab.runtime.endpoints.by_username("bedroom0")
    address = lab.phones[0].transport.get_extra_info("sockname")
    # Exercise stale physical discovery too, not just the saved roster row.
    peer = Peer(name=endpoint.name, host=address[0], sip_port=address[1],
        endpoint_id=endpoint.endpoint_id, endpoint_kind="sip_account", sip_uri_user=endpoint.username,
        contact_uri=f"sip:bedroom0@{address[0]}:{address[1]}")
    return replace(preparation, peers=(peer,))


async def originate(lab, preparation):
    return await automation_originate.async_originate_application_call(
        ServiceCall(lab.hass, "voip_stack", "call", {"destination": "bedroom0", "answer_timeout": 1}, context=Context()),
        source=RosterEntry("paging:test", name="Announcement", metadata={"virtual_endpoint": "paging"}),
        answer_mode="Auto", audio_direction="sendonly", preparation=preparation)


@pytest.mark.parametrize("when", ["after_prepare", "during_route_window"])
async def test_prepared_paging_dials_current_contact_after_registration_moves(lab, monkeypatch, when):
    preparation = await prepared_call(lab)
    old_address = lab.phones[0].transport.get_extra_info("sockname")
    new_address = lab.phones[1].transport.get_extra_info("sockname")

    async def rebind():
        await register_contact(lab, contact="*", address=old_address, expires=0)
        await register_contact(lab, contact=f"<sip:bedroom0@{new_address[0]}:{new_address[1]}>",
                               address=new_address, expires=120)

    if when == "during_route_window":
        monkeypatch.setattr(automation_routing, "has_native_route_window", lambda *_: True)
        async def decide(**_kwargs):
            await asyncio.sleep(0)
            await rebind()
            return route_automation.AutomationRoute()
        monkeypatch.setattr(route_automation, "request_route_override", decide)
    else:
        await rebind()
    token = await originate(lab, preparation)
    try:
        assert not lab.phones[0].invites
        assert len(lab.phones[1].invites) == 1
        actual = sip.parse_sip_uri(lab.phones[1].invites[0].uri)
        assert (actual.host, actual.port) == new_address
        endpoint = lab.runtime.endpoints.by_username("bedroom0")
        assert endpoint.active_call_id == token["call_id"]
    finally:
        await lab.calls.terminate_call_wait(token["call_id"], intent=TerminationIntent("test_done"))
    assert_call_resources_released(lab)


@pytest.mark.parametrize("change", ["expired_contact", "dnd", "busy", "removed_endpoint"])
async def test_prepared_paging_rejects_current_unreachable_or_disabled_target(lab, change):
    preparation = await prepared_call(lab)
    endpoint = lab.runtime.endpoints.by_username("bedroom0")
    if change == "expired_contact":
        address = lab.phones[0].transport.get_extra_info("sockname")
        await register_contact(lab, contact="*", address=address, expires=0)
    elif change == "dnd":
        lab.runtime.endpoints.update(endpoint.endpoint_id, dnd=True)
    elif change == "busy":
        lab.runtime.endpoints.claim_call(endpoint.endpoint_id, "other-owner")
    else:
        lab.runtime.endpoints.remove(endpoint.endpoint_id)
    with pytest.raises(ServiceValidationError):
        await originate(lab, preparation)
    assert all(not phone.invites for phone in lab.phones)
    if change == "busy":
        assert lab.runtime.endpoints.get(endpoint.endpoint_id).active_call_id == "other-owner"
        lab.runtime.endpoints.release_call(endpoint.endpoint_id, "other-owner")
    assert_call_resources_released(lab)
