"""Native HA routing decisions precede browser SIP media and branch creation."""

import asyncio
from unittest.mock import AsyncMock, Mock
from dataclasses import replace
from types import SimpleNamespace

import pytest
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.script import Script, async_validate_actions_config
from homeassistant.helpers.trigger import TriggerConfig
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components import voip_stack
from custom_components.voip_stack import services, softphone_originate
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
from custom_components.voip_stack.roster import dump_roster_json
from custom_components.voip_stack.trigger import VoipCallTrigger
from custom_components.voip_stack.trunk_policy import EXTERNAL_CALL_BLOCKED_ENDPOINTS

from .test_trunk_calls_ha import trunks as trunks, lab as lab
from .test_sip_phone_transfers_ha import registered_phones as registered_phones

pytestmark = pytest.mark.ha


@pytest.fixture
async def browser_route_lab(trunks, monkeypatch):
    lab = trunks
    source = PhoneEndpoint("route-browser", "Calling account", "browser", username="351123456",
        extension="351123456", device_id="route-browser-device")
    lab.runtime.endpoints.register(source)
    for identity in ("sip:bedroom0", "sip:bedroom1"):
        lab.runtime.endpoints.update(identity, availability="available")
    lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    await services.async_register_services(lab.hass, {
        "call": voip_stack._handle_sip_call_target_service,
        "hangup": voip_stack._handle_sip_hangup_service,
        "select_inbound_destination": voip_stack._handle_select_inbound_destination_service,
    })
    return lab, source


async def attach_route(lab, source, *, matching_weekday=True, destination="External", ingress="extension", trunk=""):
    weekdays = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    today = dt_util.now().weekday()
    chosen = weekdays[today if matching_weekday else (today + 1) % 7]
    action = lambda line: {"action": "voip_stack.select_inbound_destination",
                           "data": {"destination": destination, "trunk_id": line}}
    actions = [{"choose": [{"conditions": [
        {"condition": "template", "value_template": "{{ trigger.call.caller_number.startswith('351') }}"},
        {"condition": "time", "weekday": [chosen]},
    ], "sequence": [action("work")]}], "default": [action("home")]}]
    script = Script(lab.hass, await async_validate_actions_config(lab.hass, cv.SCRIPT_SCHEMA(actions)),
                    "Native PBX route", "automation", script_mode="parallel")
    snapshots, tasks = [], []

    def run(payload, _description, context):
        snapshots.append(payload["call"])
        task = lab.hass.async_create_task(script.async_run({"trigger": payload}, context))
        tasks.append(task)
        return task

    options = {"ingress": ingress, "caller": source.name}
    if trunk:
        options["trunk"] = trunk
    native = VoipCallTrigger(lab.hass, TriggerConfig("voip_stack.route_requested", options=options))
    return await native.async_attach_runner(run), snapshots, tasks


@pytest.mark.parametrize("matching_weekday,index", [(True, 1), (False, 0)])
async def test_browser_native_cid_and_weekday_choose_outbound_line_before_sip(browser_route_lab, matching_weekday, index):
    lab, source = browser_route_lab
    detach, snapshots, tasks = await attach_route(lab, source, matching_weekday=matching_weekday)
    call_id = ""
    try:
        await lab.hass.services.async_call("voip_stack", "call", {
            "device_id": source.device_id, "destination": "External",
        }, blocking=True)
        await asyncio.gather(*tasks)
        assert len(snapshots) == 1
        assert snapshots[0]["caller_number"] == "351123456"
        assert snapshots[0]["ingress"] == "extension"
        call_id = snapshots[0]["call_id"]
        assert lab.phones[index].invites[-1].header("Call-ID") == call_id
        assert not lab.phones[1 - index].invites
        assert lab.calls.get_session(call_id).metadata["outbound_trunk_id"] == ("work" if index else "home")
    finally:
        call_id = call_id or lab.runtime.endpoints.get(source.endpoint_id).active_call_id
        if call_id:
            await lab.hass.services.async_call("voip_stack", "hangup", {
                "device_id": source.device_id, "call_id": call_id,
            }, blocking=True)
        detach()
    assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")


async def test_forbidden_automation_retarget_never_constructs_media_or_sip(browser_route_lab, monkeypatch):
    from custom_components.voip_stack import sip_client

    lab, source = browser_route_lab
    lab.runtime.transport_config[EXTERNAL_CALL_BLOCKED_ENDPOINTS] = (source.endpoint_id,)
    detach, snapshots, tasks = await attach_route(lab, source)
    clients = Mock(wraps=sip_client.SipCallClient)
    monkeypatch.setattr(sip_client, "SipCallClient", clients)
    try:
        with pytest.raises(ServiceValidationError):
            await lab.hass.services.async_call("voip_stack", "call", {
                "device_id": source.device_id, "destination": "bedroom0",
            }, blocking=True)
        await asyncio.gather(*tasks)
        assert len(snapshots) == 1
        clients.assert_not_called()
        assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")
        assert not any(peer.invites for peer in lab.phones)
    finally:
        detach()


@pytest.mark.parametrize("blocked", [False, True])
async def test_normal_account_mobility_policy_keeps_external_call_permission(browser_route_lab, monkeypatch, blocked):
    from custom_components.voip_stack import sip_client
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.phone_policy import PhonePolicy

    lab, source = browser_route_lab
    lab.runtime.endpoints.update("sip:bedroom0", policy=PhonePolicy.from_data({
        "always": {"kind": "contact", "target": "12345"}}))
    if blocked:
        lab.runtime.transport_config[EXTERNAL_CALL_BLOCKED_ENDPOINTS] = ("sip:bedroom0",)
    clients = Mock(wraps=sip_client.SipCallClient)
    monkeypatch.setattr(sip_client, "SipCallClient", clients)
    try:
        if blocked:
            with pytest.raises(ServiceValidationError):
                await lab.hass.services.async_call("voip_stack", "call", {
                    "device_id": source.device_id, "destination": "bedroom0"}, blocking=True)
            clients.assert_not_called()
            assert not any(peer.invites for peer in lab.phones)
        else:
            await lab.hass.services.async_call("voip_stack", "call", {
                "device_id": source.device_id, "destination": "bedroom0"}, blocking=True)
            assert sip.parse_sip_uri(lab.phones[0].invites[-1].uri).user == "12345"
            session, = lab.calls.sessions.values()
            assert session.metadata["policy_endpoint_ids"] == ("sip:bedroom0",)
    finally:
        active = lab.runtime.endpoints.get(source.endpoint_id).active_call_id
        if active:
            await lab.hass.services.async_call("voip_stack", "hangup", {
                "device_id": source.device_id, "call_id": active}, blocking=True)
    assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")


async def test_application_caller_uses_the_same_native_window_before_its_sip_leg(browser_route_lab):
    from homeassistant.core import Context
    from .test_automation_originate_ha import dial

    lab, _phone = browser_route_lab
    lab.roster[0] = replace(lab.roster[0], extension="351777")
    source = SimpleNamespace(name=lab.roster[0].display_name)
    detach, snapshots, tasks = await attach_route(lab, source)
    context = Context()
    try:
        await dial(lab, context, destination="External")
        await asyncio.gather(*tasks)
        assert len(snapshots) == 1
        assert lab.phones[1].invites[-1].header("Call-ID") == snapshots[0]["call_id"]
        assert not lab.phones[0].invites
    finally:
        if lab.calls.sessions:
            await lab.hass.services.async_call("voip_stack", "hangup", {}, blocking=True, context=context)
        detach()
    assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_sip_group_gate_runs_before_any_member_offer(trunks, registered_phones):
    from custom_components.voip_stack.roster import RosterEntry

    lab = trunks
    lab.roster.append(RosterEntry("GatedGroup", ha_bridge=True,
        metadata={"group_type": "ring", "members": ["transfer_b", "transfer_c"]}))
    await services.async_register_services(lab.hass, {
        "select_inbound_destination": voip_stack._handle_select_inbound_destination_service,
    })
    snapshots, tasks = [], []

    def run(payload, _description, context):
        snapshots.append(payload["call"])
        task = lab.hass.async_create_task(lab.hass.services.async_call("voip_stack", "select_inbound_destination",
            {"destination": "transfer_c"}, blocking=True, context=context))
        tasks.append(task)
        return task

    native = VoipCallTrigger(lab.hass, TriggerConfig("voip_stack.route_requested", options={"caller": "transfer_a"}))
    detach = await native.async_attach_runner(run)
    caller, first, second = registered_phones
    try:
        await asyncio.to_thread(caller.dial, "GatedGroup")
        await asyncio.gather(*tasks)
        assert len(snapshots) == 1
        assert "Incoming call" not in first.read()
        assert "Call established" in second.read()
    finally:
        await asyncio.to_thread(caller.hangup)
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(.01)
        detach()
    assert not lab.runtime.rtp_port_pool.get("used")


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_dtmf_fallback_native_cid_did_and_selected_line_reach_the_outbound_client(trunks, unused_udp_port_factory, tmp_path, caplog):
    import socket
    import json
    from custom_components.voip_stack.const import CALL_EVENT
    from custom_components.voip_stack.config import normalized_trunk_config
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.sip_client import SipCallClient
    from custom_components.voip_stack.sip_trunk import SipTrunkClient
    from custom_components.voip_stack.trunk_runtime import trunk_client_config

    lab = trunks
    events = []
    unlisten = lab.hass.bus.async_listen(CALL_EVENT, lambda event: events.append(dict(event.data)))
    config = normalized_trunk_config({"trunk_enabled": True, "trunk_register": False,
        "trunk_server": "127.0.0.42", "trunk_allowed_ips": ["127.0.0.42/32"],
        "trunk_inbound_mode": "dtmf", "trunk_dtmf_enabled": True,
        "trunk_dtmf_timeout_ms": 100, "trunk_inbound_default_target": "12345"})
    lab.runtime.trunk_configs["inbound"] = config
    provider = SipTrunkClient(config=trunk_client_config(config), local_ip="127.0.0.1",
        local_sip_port=lab.runtime.transport_config["sip_port"], trunk_id="inbound")
    lab.calls.adopt_component("trunk:inbound", provider, closer=provider.stop)
    await services.async_register_services(lab.hass, {
        "select_inbound_destination": voip_stack._handle_select_inbound_destination_service,
    })
    detach, snapshots, tasks = await attach_route(lab, SimpleNamespace(name="Provider caller"),
        destination="12345", ingress="trunk", trunk="inbound")
    signaling = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    signaling.bind(("127.0.0.42", 0))
    signaling.setblocking(False)
    responses = asyncio.Queue()
    listener = ("127.0.0.1", lab.runtime.transport_config["sip_port"])

    async def receive():
        while True:
            raw, address = await lab.hass.loop.sock_recvfrom(signaling, 16384)
            await responses.put((raw, address))

    receiver = asyncio.create_task(receive())
    caller = SipCallClient(local_ip="127.0.0.42", local_name="Provider caller", local_uri_user="351987654",
        local_sip_port=signaling.getsockname()[1], local_rtp_port=unused_udp_port_factory(),
        supported_formats=[AudioFormat(16000, frame_ms=20)])
    caller.use_reused_signaling_flow(send=lambda raw: bool(signaling.sendto(raw, listener)),
                                     responses=responses, close=signaling.close)
    try:
        result = await caller.invite(target="450", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=15)
        if result == "ringing":
            result = await caller.wait_for_final(timeout=15)
        await asyncio.gather(*tasks)
        assert len(snapshots) == 1
        assert snapshots[0]["caller_number"] == "351987654"
        assert snapshots[0]["called_number"] == "450"
        assert snapshots[0]["inbound_trunk_id"] == "inbound"
        assert result == "in_call"
        assert lab.phones[1].invites and not lab.phones[0].invites
        assert lab.calls.get_session(caller.dialog_ids.call_id).metadata["outbound_trunk_id"] == "work"
    finally:
        await caller.close()
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(.01)
        detach()
        unlisten()
        (tmp_path / "events.json").write_text(json.dumps(events, default=str, indent=2))
        (tmp_path / "logs.txt").write_text(caplog.text)
    assert not lab.runtime.rtp_port_pool.get("used")
