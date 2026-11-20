"""Browser origination to a local application keeps the caller's perspective."""

import asyncio
import socket
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError

from custom_components.voip_stack import endpoint_runtime, invite_router, peer_snapshot, softphone_originate
from custom_components.voip_stack.automation_call import automation_call
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.ha_bindings import HaCardBindings
from custom_components.voip_stack.local_softphone_runtime import async_setup_local_softphone_bridge
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.roster import RosterEntry, dump_roster_json
from custom_components.voip_stack.websocket_api import CALL_EVENT, HA_SOFTPHONE_STATE_EVENT, _ha_softphone_state

from .test_automation_originate_ha import lab as lab, assert_call_resources_released

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("handled", [False, True])
async def test_account_calls_automation_contact_then_accepts_an_independent_incoming_call(
    lab, monkeypatch, unused_tcp_port_factory, handled,
):
    """Exercise the card's public service, real SIP loopback and application owner."""
    await lab.calls.shutdown()
    lab.runtime.sip = lab.calls = SipEndpointRuntime(allow_dark_sessions=True)
    for _ in range(16):
        port = unused_tcp_port_factory()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.bind(("127.0.0.1", port))
        except OSError:
            continue
        break
    else:
        raise RuntimeError("No shared UDP/TCP port available")
    lab.runtime.transport_config.update(sip_port=port, sip_registrar_enabled=True)
    caller = PhoneEndpoint(endpoint_id="sip:account-caller", name="Account caller",
        username="account-caller", device_id="account-caller-device", kind=EndpointKind.SIP_ACCOUNT,
        availability="available", capabilities=frozenset({"audio", "dtmf"}))
    next_caller = PhoneEndpoint(endpoint_id="next-caller", name="Next caller",
        device_id="next-caller-device", kind=EndpointKind.BROWSER,
        availability="available", capabilities=frozenset({"audio", "dtmf"}))
    for endpoint in (caller, next_caller):
        lab.runtime.endpoints.register(endpoint)
        lab.roster.append(RosterEntry(endpoint.name, metadata={"local_ha": True,
            "endpoint_id": endpoint.endpoint_id, "endpoint_kind": endpoint.kind.value}))
    entry = lab.hass.config_entries.async_entries("voip_stack")[0]
    lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(subentry_type="phone",
        title=caller.name, unique_id="phone:" + caller.endpoint_id, data={
            "name": caller.name, "kind": caller.kind.value, "username": caller.username,
            "ha_bindings": [{"source": "migration", "reference": "legacy_ha_phone:caller"}],
        }))
    bindings = lab.runtime.ha_bindings = HaCardBindings(lab.hass, entry, lab.runtime, lambda _changed: None)
    await bindings.async_start()
    assert bindings.has_binding(caller.endpoint_id)
    contact = RosterEntry("office-announcements", name="Announcements", ha_bridge=True,
        metadata={"virtual_endpoint": "automation", "automation_timeout": 30})
    lab.roster.append(contact)
    lab.runtime.directory.replace(lab.roster)
    lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
    monkeypatch.setattr(peer_snapshot, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", AsyncMock(return_value=[]))
    monkeypatch.setattr("custom_components.voip_stack.endpoint_group_calls.roster_from_peers", lambda *_args: lab.roster)
    monkeypatch.setattr(invite_router, "_roster_from_peers", lambda *_args: lab.roster)
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    assert await endpoint_runtime.async_start_sip_endpoint(lab.hass)
    async_setup_local_softphone_bridge(lab.hass)
    requested = asyncio.Event()
    connected = asyncio.Event()
    events, snapshots = [], []

    @callback
    def observe(event):
        data = dict(event.data)
        if event.event_type == CALL_EVENT:
            events.append(data)
            if data.get("event_type") == "automation_requested":
                requested.set()
        elif data.get("endpoint_id") == caller.endpoint_id:
            snapshots.append(data)
            if data.get("state") == "in_call":
                connected.set()

    unsubs = [lab.hass.bus.async_listen(kind, observe) for kind in (CALL_EVENT, HA_SOFTPHONE_STATE_EVENT)]
    try:
        await lab.hass.services.async_call("voip_stack", "call", {
            "device_id": caller.device_id, "destination": contact.name,
            "media_client_id": "caller-browser-owner",
        }, blocking=True)
        await asyncio.wait_for(requested.wait(), 2)
        state = _ha_softphone_state(lab.hass, caller.endpoint_id)
        call_id = state["call_id"]
        session = lab.calls.get_session(call_id)
        application = automation_call(lab.hass, call_id)
        assert session is not None and application is not None
        assert session.state == "ringing" and session.owner == "automation"
        assert (state["state"], state["direction"], state["role"], state["can_answer"]) == (
            "remote_ringing", "outgoing", "caller", False)
        assert snapshots and all(item["state"] != "ringing" for item in snapshots)
        requested_events = [event for event in events if event.get("event_type") == "automation_requested"]
        assert len(requested_events) == 1
        assert requested_events[0]["direction"] == "incoming"
        assert requested_events[0]["scope"] == "sip_bridge"
        assert requested_events[0]["route_kind"] == "automation"
        assert requested_events[0]["call_id"] == call_id
        assert not any(event.get("scope") == "session" and event.get("direction") == "incoming"
            for event in events)
        with pytest.raises(ServiceValidationError, match="handled by an application"):
            await lab.hass.services.async_call("voip_stack", "answer", {
                "device_id": caller.device_id, "call_id": call_id,
                "media_client_id": "caller-browser-owner",
            }, blocking=True)
        assert not application.answered and session.owner == "automation"
        if handled:
            # The receiving application may answer its own real SIP leg.
            await application.answer()
            await asyncio.wait_for(connected.wait(), 2)
            state = _ha_softphone_state(lab.hass, caller.endpoint_id)
            assert state["state"] == "in_call" and state["direction"] == "outgoing"
            assert not state["can_answer"]
        await lab.hass.services.async_call("voip_stack", "hangup", {
            "device_id": caller.device_id, "call_id": call_id,
        }, blocking=True)
        await asyncio.wait_for(session.terminated.wait(), 2)
        assert_call_resources_released(lab)

        # A subsequent independent caller still offers Answer to this account.
        await lab.hass.services.async_call("voip_stack", "call", {
            "device_id": next_caller.device_id, "destination": caller.name,
            "media_client_id": "next-browser-owner",
        }, blocking=True)
        incoming = _ha_softphone_state(lab.hass, caller.endpoint_id)
        assert incoming["call_id"] != call_id
        assert (incoming["state"], incoming["direction"], incoming["role"], incoming["can_answer"]) == (
            "ringing", "incoming", "callee", True)
        await lab.hass.services.async_call("voip_stack", "answer", {
            "device_id": caller.device_id, "call_id": incoming["call_id"],
            "media_client_id": "caller-browser-owner",
        }, blocking=True)
        assert _ha_softphone_state(lab.hass, caller.endpoint_id)["state"] == "in_call"
        incoming_session = lab.calls.get_session(incoming["call_id"])
        await lab.hass.services.async_call("voip_stack", "hangup", {
            "device_id": caller.device_id, "call_id": incoming["call_id"],
        }, blocking=True)
        await asyncio.wait_for(incoming_session.terminated.wait(), 2)
        assert_call_resources_released(lab)
        assert not any(phone.invites for phone in lab.phones)
    finally:
        for unsubscribe in unsubs:
            unsubscribe()
        for session in tuple(lab.calls.sessions.values()):
            await lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
        await bindings.async_close()
        await endpoint_runtime.async_stop_sip_endpoint(lab.hass)
