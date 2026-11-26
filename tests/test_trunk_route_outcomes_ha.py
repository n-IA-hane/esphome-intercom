"""Real trunk ingress preserves the selected route and its final SIP outcome."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from homeassistant.helpers.trigger import TriggerConfig

from custom_components.voip_stack.config import normalized_trunk_config
from custom_components.voip_stack.core import sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_trunk import SipTrunkClient
from custom_components.voip_stack.trunk_runtime import trunk_client_config

from .test_automation_originate_ha import lab as lab, assert_call_resources_released

pytestmark = pytest.mark.ha


@pytest.fixture(autouse=True)
async def route_outcome_runtime(lab, monkeypatch):
    from custom_components.voip_stack.ha_bindings import HaCardBindings
    from custom_components.voip_stack.store import config_entry

    bindings = HaCardBindings(lab.hass, config_entry(lab.hass), lab.runtime, lambda _ids: None)
    lab.runtime.ha_bindings = bindings
    try:
        yield bindings
    finally:
        await bindings.async_close()


async def initial_ha_account(lab, endpoint_id="default", *, bound=True):
    """Represent the canonical initial SIP account's configured HA adapter."""
    from homeassistant.config_entries import ConfigSubentry
    from custom_components.voip_stack.account_runtime import recompute_account_availability
    from custom_components.voip_stack.directory_runtime import compose_directory_entries
    from custom_components.voip_stack.store import config_entry

    name = lab.hass.config.location_name if endpoint_id == "default" else "Other HA"
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(
        subentry_type="phone", title=name, unique_id="phone:" + endpoint_id,
        data={"kind": "sip_account", "name": name, "username": endpoint_id},
    ))
    endpoint = PhoneEndpoint(endpoint_id, name,
        EndpointKind.SIP_ACCOUNT, username=endpoint_id, device_id="ha-" + endpoint_id)
    lab.runtime.endpoints.register(endpoint)
    if bound:
        await lab.runtime.ha_bindings.async_set_popup_binding("fixture-user", "ha-account-" + endpoint_id, endpoint_id, True)
    recompute_account_availability(lab.hass)
    automation = [row for row in lab.roster if row.metadata.get("virtual_endpoint") == "automation"]
    lab.roster[:] = [*compose_directory_entries(lab.hass, [],
        lab.calls.component("registrar").registered_roster_entries()), *automation]
    lab.runtime.directory.replace(lab.roster)
    return endpoint


@asynccontextmanager
async def incoming(lab, transport, *, destination=None, dtmf=False, outbound_port=None):
    config = normalized_trunk_config({
        "trunk_enabled": True, "trunk_register": False,
        "trunk_server": "127.0.0.1", "trunk_allowed_ips": ["127.0.0.1/32"],
        "trunk_transport": transport.lower(),
        "trunk_inbound_mode": "dtmf" if dtmf else "direct",
        "trunk_dtmf_enabled": dtmf, "trunk_dtmf_timeout_ms": 100 if dtmf else 0,
    })
    # Absence is the untouched initial HA destination; a configured route must
    # never silently retry that account after failing.
    if destination is not None:
        config["trunk_inbound_default_target"] = destination
    if outbound_port is not None:
        config["trunk_port"] = outbound_port
    provider = SipTrunkClient(config=trunk_client_config(config), local_ip="127.0.0.1",
        local_sip_port=lab.runtime.transport_config["sip_port"], trunk_id="outcomes")
    lab.runtime.trunk_configs["outcomes"] = config
    lab.runtime.trunk_config = config
    lab.runtime.default_trunk_id = "outcomes"
    lab.calls.adopt_component("trunk:outcomes", provider, closer=provider.stop)
    caller = SipCallClient(local_ip="127.0.0.1", local_name="Provider caller", local_uri_user="101",
        local_sip_port=0, local_rtp_port=36540,
        supported_formats=[AudioFormat(16000, frame_ms=20)], signaling_transport=transport)
    try:
        yield caller, config
    finally:
        await caller.terminate(timeout=1)
        await caller.close()
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(.01)
        assert_call_resources_released(lab)


async def invite(lab, caller, target="450"):
    state = await caller.invite(target=target, remote_host="127.0.0.1",
        remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
    return state


async def final_outcome(caller, state):
    if state == "ringing":
        state = await caller.wait_for_final(timeout=3)
    assert state not in {"in_call", "ringing", "timeout"}, (state, caller.last_sip_status_code)


@pytest.mark.parametrize("transport", ["UDP", "TCP"])
@pytest.mark.parametrize("dtmf", [False, True], ids=["direct", "dtmf"])
async def test_untouched_trunk_rings_initial_ha_account(lab, transport, dtmf):
    initial = await initial_ha_account(lab)
    async with incoming(lab, transport, dtmf=dtmf) as (caller, config):
        assert config["trunk_inbound_default_target"] == "HA"
        assert await invite(lab, caller) == "ringing"
        async with asyncio.timeout(3):
            while lab.runtime.softphones.get(initial.endpoint_id, {}).get("state") != "ringing":
                await asyncio.sleep(.01)
        assert lab.runtime.softphones[initial.endpoint_id]["state"] == "ringing"
        assert lab.runtime.endpoints.require(initial.endpoint_id).active_call_id
        assert not any(phone.invites for phone in lab.phones)


@pytest.mark.parametrize("transport", ["UDP", "TCP"])
@pytest.mark.parametrize("dtmf", [False, True], ids=["direct", "dtmf"])
async def test_configured_missing_target_ends_404_without_ringing_initial_ha(lab, transport, dtmf):
    await initial_ha_account(lab)
    async with incoming(lab, transport, destination="Deleted destination", dtmf=dtmf) as (caller, _):
        await final_outcome(caller, await invite(lab, caller))
        assert (caller.last_sip_status_code, caller.last_sip_reason) == (404, "Not Found")
        assert not lab.runtime.endpoints.require("default").active_call_id
        assert lab.runtime.softphones.get("default", {}).get("state") != "ringing"
        assert not any(phone.invites for phone in lab.phones)


@pytest.mark.parametrize("transport", ["UDP", "TCP"])
@pytest.mark.parametrize("dtmf", [False, True], ids=["direct", "dtmf"])
async def test_disabled_selected_account_preserves_unavailable_instead_of_404(lab, transport, dtmf):
    from custom_components.voip_stack.account_runtime import recompute_account_availability
    from custom_components.voip_stack.phone_config import phone_subentry_by_endpoint_id
    from custom_components.voip_stack.store import config_entry

    await initial_ha_account(lab)
    entry = config_entry(lab.hass)
    subentry = phone_subentry_by_endpoint_id(entry, "bedroom0")
    lab.hass.config_entries.async_update_subentry(entry, subentry, data={**subentry.data, "enabled": False})
    recompute_account_availability(lab.hass, "bedroom0")
    lab.roster[:] = [replace(row, enabled=False) if row.id == "bedroom0" else row for row in lab.roster]
    lab.runtime.directory.replace(lab.roster)
    async with incoming(lab, transport, destination="bedroom0", dtmf=dtmf) as (caller, _):
        await final_outcome(caller, await invite(lab, caller))
        assert (caller.last_sip_status_code, caller.last_sip_reason) == (480, "Temporarily Unavailable")
        assert not any(phone.invites for phone in lab.phones)
        assert not lab.runtime.endpoints.require("default").active_call_id


@pytest.mark.parametrize("transport", ["UDP", "TCP"])
@pytest.mark.parametrize("dtmf", [False, True], ids=["direct", "dtmf"])
async def test_deleted_initial_account_does_not_select_another_ha_phone(lab, transport, dtmf):
    await initial_ha_account(lab, "other")
    assert lab.runtime.endpoints.get("default") is None
    assert lab.runtime.ha_bindings.has_binding("other")
    async with incoming(lab, transport, dtmf=dtmf) as (caller, _):
        await final_outcome(caller, await invite(lab, caller))
        assert caller.last_sip_status_code in {404, 480}
        assert not lab.runtime.endpoints.require("other").active_call_id
        assert lab.runtime.softphones.get("other", {}).get("state") != "ringing"
        assert not any(phone.invites for phone in lab.phones)


@asynccontextmanager
async def outgoing_provider_wire(lab, monkeypatch, transport):
    from custom_components.voip_stack.sip_tcp_io import read_sip_stream_message

    provider_phone = lab.phones[1]
    def reject():
        request = provider_phone.last_request
        headers = [(key, request.header(key)) for key in ("Via", "From", "To", "Call-ID", "CSeq")]
        provider_phone.transport.sendto(sip.build_response(486, "Provider busy", headers), provider_phone.remote)
    monkeypatch.setattr(provider_phone, "answer", reject)
    port = provider_phone.transport.get_extra_info("sockname")[1]
    handlers = set()
    async def tcp_provider(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            while raw := await read_sip_stream_message(reader):
                request = sip.parse_message(raw)
                if request.method == "INVITE":
                    provider_phone.invites.append(request)
                    headers = [(key, request.header(key)) for key in ("Via", "From", "To", "Call-ID", "CSeq")]
                    writer.write(sip.build_response(486, "Provider busy", headers))
                    await writer.drain()
        finally:
            handlers.discard(task)
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(tcp_provider, "127.0.0.1", 0) if transport == "TCP" else None
    if server is not None:
        port = server.sockets[0].getsockname()[1]
    try:
        yield port, provider_phone
    finally:
        if server is not None:
            server.close()
            await server.wait_closed()
        remaining = tuple(handlers)
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)


@pytest.mark.parametrize("transport", ["UDP", "TCP"])
async def test_unknown_inbound_public_number_does_not_use_default_outgoing_provider(lab, monkeypatch, transport):
    await initial_ha_account(lab)
    async with outgoing_provider_wire(lab, monkeypatch, transport) as (port, provider_phone):
        async with incoming(lab, transport, destination="", outbound_port=port) as (caller, _):
            await final_outcome(caller, await invite(lab, caller, "00390123456789"))
            assert not provider_phone.invites, "An inbound called number must not implicitly choose the default outgoing trunk"
            assert caller.last_sip_status_code == 404
            assert not lab.runtime.endpoints.require("default").active_call_id


@pytest.mark.parametrize("transport", ["UDP", "TCP"])
@pytest.mark.parametrize("dtmf", [False, True], ids=["direct", "dtmf"])
@pytest.mark.parametrize("selection", ["configured", "automation"])
async def test_explicit_public_number_destination_can_use_outgoing_provider(lab, monkeypatch, transport, dtmf, selection):
    from custom_components import voip_stack
    from custom_components.voip_stack import services
    from custom_components.voip_stack.trigger import VoipCallTrigger

    await initial_ha_account(lab)
    destination = "00390123456789"
    await services.async_register_services(lab.hass, {
        "select_inbound_destination": voip_stack._handle_select_inbound_destination_service,
    })
    tasks = []
    async def choose(context):
        await lab.hass.services.async_call("voip_stack", "select_inbound_destination",
            {"destination": destination}, blocking=True, context=context)
    def run(_payload, _description, context):
        task = lab.hass.async_create_task(choose(context))
        tasks.append(task)
        return task
    trigger = VoipCallTrigger(lab.hass, TriggerConfig("voip_stack.route_requested", options={"trunk": "outcomes"}))
    detach = await trigger.async_attach_runner(run) if selection == "automation" else lambda: None
    try:
        async with outgoing_provider_wire(lab, monkeypatch, transport) as (port, provider_phone):
            async with incoming(lab, transport, dtmf=dtmf, outbound_port=port,
                destination=destination if selection == "configured" else "") as (caller, _):
                await final_outcome(caller, await invite(lab, caller))
                await asyncio.gather(*tasks)
                assert len(provider_phone.invites) == 1, (caller.last_sip_status_code, caller.last_sip_reason)
                assert sip.parse_sip_uri(provider_phone.invites[0].uri).user == destination
                assert (caller.last_sip_status_code, caller.last_sip_reason) == (486, "Provider busy")
                assert not lab.phones[0].invites
                assert not lab.runtime.endpoints.require("default").active_call_id
    finally:
        detach()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("existing", [{"trunk_inbound_default_target": "HA"}, {"trunk_inbound_default_target": ""}])
async def test_trunk_form_preserves_default_when_initial_account_is_deleted(lab, existing):
    from custom_components.voip_stack.config_flow import _trunk_target_choices
    from custom_components.voip_stack.store import config_entry

    await initial_ha_account(lab, "other")
    assert lab.runtime.ha_bindings.has_binding("other")
    _choices, selected = _trunk_target_choices(lab.hass, config_entry(lab.hass), existing)
    assert selected == existing["trunk_inbound_default_target"]


@pytest.mark.parametrize("transport", ["UDP", "TCP"])
@pytest.mark.parametrize("dtmf", [False, True], ids=["direct", "dtmf"])
@pytest.mark.parametrize("selection", ["automation", "configured", "account"])
@pytest.mark.parametrize("provisional", [False, True], ids=["immediate", "after180"])
@pytest.mark.parametrize("status,reason", [(404, "Peer route does not exist"), (486, "Peer busy"),
    (603, "Peer declined"), (480, "Peer temporarily unavailable")])
async def test_selected_destination_preserves_peer_failure(
    lab, monkeypatch, transport, dtmf, selection, provisional, status, reason,
):
    from custom_components import voip_stack
    from custom_components.voip_stack import services
    from custom_components.voip_stack.trigger import VoipCallTrigger

    await initial_ha_account(lab)
    target = lab.phones[0]
    port = target.transport.get_extra_info("sockname")[1]
    destination = f"sip:outside-directory@127.0.0.1:{port};transport=udp"
    assert not any(row.sip_uri == destination for row in lab.roster)
    # The independent peer sends the actual final response over its UDP socket.
    # The inbound provider leg independently exercises both UDP and TCP.
    def reject():
        request = target.last_request
        headers = [(key, request.header(key)) for key in ("Via", "From", "To", "Call-ID", "CSeq")]
        target.transport.sendto(sip.build_response(status, reason, headers), target.remote)
    monkeypatch.setattr(target, "answer", reject)
    respond = target.respond
    def optionally_send_provisional(request, address, response_status, body=b""):
        if response_status != 180 or provisional:
            respond(request, address, response_status, body)
    monkeypatch.setattr(target, "respond", optionally_send_provisional)
    await services.async_register_services(lab.hass, {
        "select_inbound_destination": voip_stack._handle_select_inbound_destination_service,
    })
    tasks, snapshots = [], []
    async def choose(context):
        await lab.hass.services.async_call("voip_stack", "select_inbound_destination",
            {"destination": destination}, blocking=True, context=context)
    def run(payload, _description, context):
        snapshots.append(payload["call"])
        task = lab.hass.async_create_task(choose(context))
        tasks.append(task)
        return task
    trigger = VoipCallTrigger(lab.hass, TriggerConfig("voip_stack.route_requested",
        options={"trunk": "outcomes"}))
    detach = await trigger.async_attach_runner(run) if selection == "automation" else lambda: None
    try:
        configured_destination = {"configured": destination, "account": "bedroom0"}.get(selection)
        async with incoming(lab, transport, dtmf=dtmf,
            destination=configured_destination) as (caller, _):
            await final_outcome(caller, await invite(lab, caller))
            await asyncio.gather(*tasks)
            assert len(snapshots) == (1 if selection == "automation" else 0)
            assert len(target.invites) == 1, (caller.last_sip_status_code, caller.last_sip_reason)
            expected_uri = destination if selection != "account" else next(
                row.sip_uri for row in lab.roster if row.id == "bedroom0")
            assert target.invites[0].uri == expected_uri
            assert (caller.last_sip_status_code, caller.last_sip_reason) == (status, reason)
            assert not lab.phones[1].invites
            assert not lab.runtime.endpoints.require("default").active_call_id
            assert lab.runtime.softphones.get("default", {}).get("state") != "ringing"
    finally:
        detach()
        await asyncio.gather(*tasks, return_exceptions=True)
