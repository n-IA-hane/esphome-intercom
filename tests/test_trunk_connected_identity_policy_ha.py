"""A retained external SIP leg keeps its selected caller policy after transfer."""

import pytest

from custom_components.voip_stack.core import sip
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.roster import RosterEntry

from .test_bridge_connected_identity_wire_ha import _identity_phone
from .test_queue_call_ha import wait_until
from .test_trunk_calls_ha import lab as lab, trunks as trunks


pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("mode", ["trunk", "caller", "account"])
async def test_transferred_trunk_leg_respects_forced_from_on_update_and_bye(trunks, monkeypatch, mode):
    runtime = trunks
    provider, target = runtime.phones
    _identity_phone(monkeypatch, provider, "External party")
    _identity_phone(monkeypatch, target, "Connected reception")
    runtime.runtime.trunk_configs["home"].update(
        trunk_caller_id_policy=mode, trunk_caller_id="+3906123400", trunk_caller_name="Main office",
    )
    account = PhoneEndpoint(endpoint_id="101", username="101", name="Reception",
        device_id="identity-transfer-card", kind=EndpointKind.SIP_ACCOUNT, availability="available")
    runtime.runtime.endpoints.register(account)
    runtime.roster.append(RosterEntry(account.username, name=account.name,
        metadata={"local_ha": True, "endpoint_id": account.endpoint_id}))
    runtime.runtime.directory.replace(runtime.roster)
    try:
        await runtime.hass.services.async_call("voip_stack", "call", {
            "device_id": account.device_id, "destination": "External",
            "media_client_id": "identity-transfer-owner",
        }, blocking=True)
        call_id = runtime.runtime.endpoints.require(account.endpoint_id).active_call_id
        await wait_until(lambda: runtime.calls.get_session(call_id).state == "in_call")
        source_client = runtime.calls.sip_client_for(call_id)
        contact = source_client.local_contact_uri
        credential = (source_client.username, source_client.auth_username, source_client.password)
        original_dialog_id = source_client.dialog.call_id
        initial = provider.invites[-1]
        expected_initial = "+3906123400" if mode == "trunk" else "101"
        assert sip.parse_sip_uri(sip.name_addr_uri(initial.header("From"))).user == expected_initial

        result = await runtime.hass.services.async_call("voip_stack", "transfer", {
            "device_id": account.device_id, "call_id": call_id, "destination": "bedroom1",
        }, blocking=True, return_response=True)
        assert result["success"], result
        assert runtime.calls.source_sip_client_for(call_id) is source_client
        destination_id = runtime.calls.bridge_link_for(call_id)
        session = runtime.calls.get_session(call_id)
        await wait_until(lambda: f"voip-bridge-identity-{destination_id}" not in session.named_tasks)

        expected = "+3906123400" if mode == "trunk" else "bedroom1"
        assert sip.parse_sip_uri(source_client.dialog.local_uri).user == expected
        identity_updates = [message for message in provider.requests if message.method == "UPDATE" and not message.body]
        if mode != "trunk":
            assert identity_updates
            assert sip.parse_sip_uri(identity_updates[-1].header("From")).user == "bedroom1"
        else:
            assert all(sip.parse_sip_uri(message.header("From")).user == expected for message in identity_updates)
            assert source_client.local_name == "Main office"
        assert source_client.dialog.call_id == original_dialog_id
        assert source_client.local_contact_uri == contact
        assert (source_client.username, source_client.auth_username, source_client.password) == credential
        assert len(provider.invites) == 1 and len(target.invites) == 1
        assert runtime.calls.resource_for(call_id, "relay") is not None

        await runtime.calls.request_termination(call_id, TerminationIntent("test_complete"))
        await wait_until(lambda: not runtime.calls.sessions)
        bye = next(message for message in reversed(provider.requests) if message.method == "BYE")
        assert sip.parse_sip_uri(bye.header("From")).user == expected
        assert not runtime.runtime.rtp_port_pool.get("used")
    finally:
        for session in tuple(runtime.calls.sessions.values()):
            if session.live:
                await runtime.calls.request_termination(session.call_id, TerminationIntent("test_cleanup"))
