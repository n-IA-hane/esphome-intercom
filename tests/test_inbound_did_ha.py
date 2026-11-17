"""Native HA routing consumes original DID identity from real SIP datagrams."""

import asyncio
from dataclasses import replace
import socket
from types import SimpleNamespace

import pytest
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.script import Script, async_validate_actions_config
from homeassistant.helpers.trigger import TriggerConfig

from custom_components import voip_stack
from custom_components.voip_stack import services, trigger
from custom_components.voip_stack.config import normalized_trunk_config
from custom_components.voip_stack.core import sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.endpoint_runtime import _classify_trunk_invite
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.inbound_identity import apply_inbound_identity
from custom_components.voip_stack.inbound_identity_form import did_form_fields, validate_did_form
from custom_components.voip_stack.inbound_routing.automation import request_route_override
from custom_components.voip_stack.router import RouteAction, RouteDecision
from custom_components.voip_stack.sip_listener import SipInviteResult, SipUdpEndpoint
from custom_components.voip_stack.sip_trunk import SipTrunkClient
from custom_components.voip_stack.trunk_runtime import trunk_client_config

from .test_automation_originate_ha import lab as lab

pytestmark = pytest.mark.ha


async def test_native_line_device_selector_keeps_trunk_identity_after_rename(lab):
    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.helpers import device_registry as dr
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.trunk_config import sync_trunk_devices

    entry = config_entry(lab.hass)
    line = ConfigSubentry(subentry_type="trunk", title="Public line", unique_id="trunk:provider-a",
                         data={"trunk_id": "provider-a"})
    lab.hass.config_entries.async_add_subentry(entry, line)
    sync_trunk_devices(lab.hass, entry)
    device = next(item for item in dr.async_entries_for_config_entry(dr.async_get(lab.hass), entry.entry_id)
                  if ("voip_stack", "trunk:provider-a") in item.identifiers)
    validated = await trigger.VoipCallTrigger.async_validate_config(lab.hass, {"options": {"trunk": device.id}})
    assert validated["options"]["trunk"] == "provider-a"
    lab.hass.config_entries.async_update_subentry(entry, line, title="Renamed line", data=dict(line.data))
    sync_trunk_devices(lab.hass, entry)
    selected = trigger.VoipCallTrigger(lab.hass, TriggerConfig("voip_stack.route_requested", options={"trunk": device.id}))
    assert selected._options["trunk"] == "provider-a"


def _wire(did, call_id, *, caller="001111", body=True):
    offer = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=DID wire\r\n"
             "c=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 35000 RTP/AVP 0\r\n"
             "a=rtpmap:0 PCMU/8000\r\na=ptime:20\r\na=sendrecv\r\n").encode() if body else b""
    return sip.build_request("INVITE", "sip:account-contact@127.0.0.1", [
        ("Via", f"SIP/2.0/UDP 127.0.0.1:5090;branch=z9hG4bK-{call_id}"),
        ("From", f'"Fixture caller" <sip:{caller}@caller.example>;tag=caller'),
        ("To", f"<sip:{did}@provider.example>"),
        ("P-Called-Party-ID", "<sip:unselected-number@provider.example>"),
        ("Call-ID", call_id), ("CSeq", "1 INVITE"),
        ("Contact", "<sip:caller@127.0.0.1:5090>"),
        *(([("Content-Type", "application/sdp")]) if body else []),
    ], offer)


def test_did_native_form_preserves_string_numbers_and_omitted_reconfigure_fields():
    import voluptuous as vol
    existing = {"trunk_did_source": "to", "trunk_did_fixed": "000123",
                "trunk_did_prefix_rules": [{"prefix": "+00", "replacement": "00"}]}
    values = vol.Schema(did_form_fields(existing))({})
    assert values["trunk_did_fixed"] == "000123"
    assert not validate_did_form(values, existing)
    saved = normalized_trunk_config(values)
    assert saved["trunk_did_source"] == "to"
    assert saved["trunk_did_prefix_rules"] == existing["trunk_did_prefix_rules"]
    partial = {"trunk_did_source": "fixed"}
    assert not validate_did_form(partial, saved)
    assert partial["trunk_did_fixed"] == "000123"
    assert validate_did_form({"trunk_did_source": "custom_header"}, {}) == {
        "trunk_did_source": "trunk_did_invalid",
    }


async def test_native_100_did_dictionary_and_explicit_exceptions_from_real_wire(lab):
    """SIP parsing, trunk trust, event snapshot, HA script and existing service."""
    hass, registry = lab.hass, lab.calls
    config = normalized_trunk_config({
        "trunk_enabled": True, "trunk_register": False,
        "trunk_server": "127.0.0.1", "trunk_allowed_ips": ["127.0.0.1/32"],
        "trunk_did_source": "to",
    })
    client = SipTrunkClient(config=trunk_client_config(config), local_ip="127.0.0.1",
                           local_sip_port=5090, trunk_id="provider-a")
    # Reuse the production service schema, authorization and implicit call binding.
    await services.async_register_services(hass, {
        "select_inbound_destination": voip_stack._handle_select_inbound_destination_service,
    })
    routes = {f"009900{index:03}": f"Office {index:03}" for index in range(100)}
    def action(destination):
        return {"action": "voip_stack.select_inbound_destination", "data": {"destination": destination}}
    actions = [{"choose": [
        {"conditions": "{{ trigger.call.caller_number == '009911' }}", "sequence": [action("Security")]},
        {"conditions": "{{ trigger.call.called_number == '009900042' }}", "sequence": [action("Reception")]},
    ], "default": [action("{{ did_routes.get(trigger.call.called_number, 'Fallback') }}")]}]
    script = Script(hass, await async_validate_actions_config(hass, cv.SCRIPT_SCHEMA(actions)),
                    "Provider neutral DID map", "automation", script_mode="parallel", max_runs=110)
    tasks, snapshots, decisions = [], [], asyncio.Queue()

    def runner(payload, _description, context):
        snapshots.append(payload["call"])
        task = hass.async_create_task(script.async_run({"trigger": payload, "did_routes": routes}, context))
        tasks.append(task)
        return task

    native = trigger.VoipCallTrigger(hass, TriggerConfig("voip_stack.route_requested", options={"trunk": "provider-a"}))
    detach = await native.async_attach_runner(runner)

    def identify(request, initial):
        classified = _classify_trunk_invite(initial, enabled=True, trunk=None, trunks={"provider-a": client})
        return apply_inbound_identity(classified, config, request)

    async def incoming(invite):
        registry.upsert(invite.call_id, state="connecting", owner="router")
        route = await request_route_override(
            runtime=SimpleNamespace(hass=hass, ha_peer_name=lambda _: "Fixture"),
            invite=invite, decision=RouteDecision(RouteAction.BRIDGE, target="Default"),
            registered_source=False, caller_is_trusted_endpoint=True,
            automation_routing_enabled=True, trunk_invite=True,
        )
        await decisions.put((invite, route))
        return SipInviteResult(486, "Busy Here")

    owner = SipUdpEndpoint(local_ip="127.0.0.1", local_rtp_port=44000,
                          supported_formats=[AudioFormat(8000, frame_ms=20)],
                          on_invite=incoming, invite_identity_resolver=identify)
    transport, _ = await hass.loop.create_datagram_endpoint(lambda: owner, local_addr=("127.0.0.1", 0))
    source = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    source.bind(("127.0.0.1", 0))
    source.setblocking(False)
    address = transport.get_extra_info("sockname")
    try:
        scenarios = [(did, "001111", "Reception" if did == "009900042" else destination)
                     for did, destination in routes.items()]
        scenarios += [("009900001", "009911", "Security"), ("009999999", "001111", "Fallback")]
        for index, (did, caller, expected) in enumerate(scenarios):
            call_id = f"native-did-{index}"
            await hass.loop.sock_sendto(source, _wire(did, call_id, caller=caller), address)
            invite, selected = await asyncio.wait_for(decisions.get(), 3)
            assert selected.action == "forward" and selected.destination == expected
            assert invite.request_uri.user == "account-contact"
            assert invite.called_number == invite.called_number_raw == did
            assert snapshots[-1]["called_number_source"] == "to"
            assert snapshots[-1]["inbound_trunk_id"] == "provider-a"
            assert snapshots[-1]["caller_name"] == "Fixture caller"
            session = registry.get_session(call_id)
            assert session.callee == expected
            # A subsequent route or forwarded leg cannot overwrite original ingress.
            registry.set_pending_invite(call_id, replace(invite, called_number="changed", caller_name="changed"))
            fields = registry.event_fields(call_id, "connecting")
            assert fields["called_number"] == did and fields["caller_name"] == "Fixture caller"
            while True:
                raw, _ = await asyncio.wait_for(hass.loop.sock_recvfrom(source, 8192), 3)
                response = sip.parse_message(raw)
                if response.status_code == 486:
                    break
            headers = [(key, response.header(key)) for key in ("Via", "From", "To", "Call-ID")]
            headers.append(("CSeq", "1 ACK"))
            await hass.loop.sock_sendto(source, sip.build_request("ACK", "sip:account-contact@127.0.0.1", headers), address)
            await registry.request_termination(call_id, TerminationIntent("test_done"))
            retired = registry.event_fields(call_id, "idle")
            assert retired["called_number"] == did and retired["inbound_trunk_id"] == "provider-a"
        await asyncio.gather(*tasks)
        async with asyncio.timeout(3):
            while owner.pending_invites or owner.completed_invites or owner._invite_tasks:
                await asyncio.sleep(.001)
        assert len(snapshots) == 102
        assert not registry.sessions and not owner.active_dialogs
    finally:
        detach()
        source.close()
        transport.close()
        await owner.wait_closed()
        await client.stop()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_trunk_filter_uses_stable_identity_and_called_number_matches_destination():
    payload = {"caller": "Caller", "callee": "Retargeted", "called_number": "000123",
               "inbound_trunk_id": "line-identity", "outbound_trunk_id": "outbound-line"}
    assert trigger.matches_call(payload, {"trunk": "line-identity", "destination": "000123"})
    assert trigger.matches_call(payload, {"inbound_trunk_id": "line-identity", "outbound_trunk_id": "outbound-line"})
    assert not trigger.matches_call(payload, {"trunk": "a display name"})
    assert trigger.OPTIONS_SCHEMA({"trunk": "line-identity", "called_number": "000123"})


async def test_original_missing_did_and_caller_name_are_not_invented_by_forwarding(lab):
    owner = SipUdpEndpoint(local_ip="127.0.0.1", local_rtp_port=44000,
                          supported_formats=[AudioFormat(8000, frame_ms=20)], on_invite=lambda _: None,
                          trusted_trunk=True, trusted_trunk_id="provider-a",
                          invite_identity_resolver=lambda request, initial: apply_inbound_identity(
                              initial, {"trunk_did_source": "custom_header", "trunk_did_header": "X-Missing"}, request))
    invite = owner._parse_initial_invite(sip.parse_message(_wire("000123", "missing", body=False)), ("127.0.0.1", 5090))
    invite = replace(invite, caller_name="")
    lab.calls.upsert("missing", state="connecting", owner="router")
    lab.calls.set_pending_invite("missing", invite)
    lab.calls.set_pending_invite("missing", replace(invite, called_number="invented", caller_name="Forwarded peer"))
    fields = lab.calls.event_fields("missing", "connecting")
    assert fields["called_number"] == fields["called_number_raw"] == fields["caller_name"] == ""
    await lab.calls.request_termination("missing", TerminationIntent("test_done"))
    assert lab.calls.event_fields("missing", "idle")["called_number"] == ""
