"""Concurrent public HA hangups release every browser conference account."""

import asyncio
import socket
import struct
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import callback

from custom_components.voip_stack import endpoint_runtime, invite_router, peer_snapshot, softphone_originate
from custom_components.voip_stack.conference import conference_manager, CONFERENCE_FORMAT
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.ha_bindings import HaCardBindings
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.roster import RosterEntry, dump_roster_json
from custom_components.voip_stack.runtime_diagnostics import runtime_resource_snapshot
from custom_components.voip_stack.websocket_api import HA_SOFTPHONE_STATE_EVENT, _ha_softphone_state

from .test_automation_originate_ha import lab as lab, assert_call_resources_released

pytestmark = pytest.mark.ha


async def test_three_browser_accounts_finish_concurrent_conference_hangups_and_can_be_reused(
    lab, monkeypatch, unused_tcp_port_factory,
):
    """Host calls, two browser accounts answer, all hangup services finish twice."""
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
    room_name = "Service conference"
    accounts = []
    entry = lab.hass.config_entries.async_entries("voip_stack")[0]
    for role in ("host", "first", "second"):
        username = "conference-" + role
        endpoint = PhoneEndpoint(endpoint_id="sip:" + username, username=username,
            name="Conference " + role, device_id=username + "-device", kind=EndpointKind.SIP_ACCOUNT,
            availability="available", capabilities=frozenset({"audio", "dtmf"}),
            conference_group=room_name, conference_ring=True)
        accounts.append(endpoint)
        lab.runtime.endpoints.register(endpoint)
        lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(subentry_type="phone",
            title=endpoint.name, unique_id="phone:" + endpoint.endpoint_id, data={
                "name": endpoint.name, "kind": endpoint.kind.value, "username": username,
                "conference_group": room_name, "conference_ring": True,
                "ha_bindings": [{"source": "migration", "reference": "legacy_ha_phone:" + role}],
            }))
        lab.roster.append(RosterEntry(username, name=endpoint.name, metadata={
            "endpoint_id": endpoint.endpoint_id, "endpoint_kind": endpoint.kind.value, "local_ha": True}))
    bindings = lab.runtime.ha_bindings = HaCardBindings(lab.hass, entry, lab.runtime, lambda _changed: None)
    await bindings.async_start()
    assert all(bindings.has_binding(account.endpoint_id) for account in accounts)
    members = [account.username for account in accounts]
    lab.roster.append(RosterEntry("service-conference", name=room_name, ha_bridge=True,
        metadata={"group_type": "conference", "members": members, "ring_members": members}))
    lab.runtime.directory.replace(lab.roster)
    lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
    monkeypatch.setattr(peer_snapshot, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", AsyncMock(return_value=[]))
    monkeypatch.setattr("custom_components.voip_stack.endpoint_group_calls.roster_from_peers", lambda *_args: lab.roster)
    monkeypatch.setattr(invite_router, "_roster_from_peers", lambda *_args: lab.roster)
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    assert await endpoint_runtime.async_start_sip_endpoint(lab.hass)
    manager = conference_manager(lab.hass, local_ip="127.0.0.1")
    ringing = asyncio.Event()

    @callback
    def changed(_event):
        if all(_ha_softphone_state(lab.hass, account.endpoint_id)["state"] == "ringing" for account in accounts[1:]):
            ringing.set()

    unsubscribe = lab.hass.bus.async_listen(HA_SOFTPHONE_STATE_EVENT, changed)
    seen_call_ids = set()
    try:
        for cycle in range(2):
            ringing.clear()
            await lab.hass.services.async_call("voip_stack", "call", {
                "device_id": accounts[0].device_id, "destination": room_name,
                "media_client_id": "host-browser-owner",
            }, blocking=True)
            await asyncio.wait_for(ringing.wait(), 2)
            states = [_ha_softphone_state(lab.hass, account.endpoint_id) for account in accounts]
            assert states[0]["state"] == "in_call" and states[0]["direction"] == "outgoing"
            assert all(state["direction"] == "incoming" and state["can_answer"] for state in states[1:])
            call_ids = [state["call_id"] for state in states]
            assert len(set(call_ids)) == 3 and not seen_call_ids.intersection(call_ids)
            assert all(call_id.startswith("conference:") for call_id in call_ids)
            seen_call_ids.update(call_ids)
            await asyncio.gather(*(lab.hass.services.async_call("voip_stack", "answer", {
                "device_id": account.device_id, "call_id": call_id,
                "media_client_id": account.username + "-browser-owner",
            }, blocking=True) for account, call_id in zip(accounts[1:], call_ids[1:], strict=True)))
            assert all(_ha_softphone_state(lab.hass, account.endpoint_id)["state"] == "in_call" for account in accounts)
            room = manager.rooms[room_name]
            mixer = room._task
            assert set(room.legs) == set(call_ids) and mixer is not None and not mixer.done()
            sessions = [lab.calls.get_session(call_id) for call_id in call_ids]

            # Use the actual browser conference queues, with an independent
            # N-1 PCM oracle proving that this room's mixer is running.
            samples = [1000 + cycle, 2000 + cycle, 3000 + cycle]
            for call_id, sample in zip(call_ids, samples, strict=True):
                manager.push_ha_audio(call_id, struct.pack("<h", sample) * CONFERENCE_FORMAT.nominal_frame_samples)
            for call_id, own in zip(call_ids, samples, strict=True):
                queue = lab.calls.resource_for(call_id, "softphone_media")["conference_queue"]
                async with asyncio.timeout(2):
                    while True:
                        frame = await queue.get()
                        if any(frame):
                            assert frame == struct.pack("<h", sum(samples) - own) * CONFERENCE_FORMAT.nominal_frame_samples
                            break

            release = asyncio.Event()

            async def hangup(account, call_id):
                await release.wait()
                await lab.hass.services.async_call("voip_stack", "hangup", {
                    "device_id": account.device_id, "call_id": call_id,
                }, blocking=True)

            hangups = [asyncio.create_task(hangup(account, call_id))
                for account, call_id in zip(accounts, call_ids, strict=True)]
            release.set()
            async with asyncio.timeout(3):
                await asyncio.gather(*hangups)
                await asyncio.gather(*(session.terminated.wait() for session in sessions))
            assert all(task.done() and task.exception() is None for task in hangups)
            assert room._closed and not room.legs and mixer.done() and room._task is None
            assert room._close_task is not None and room._close_task.done()
            assert not manager.rooms and not manager.ha_calls
            assert_call_resources_released(lab)
            registry = lab.calls.snapshot()
            assert registry["sessions"] == registry["active_sessions"] == 0
            assert all(count == 0 for count in registry["resource_counts"].values()), registry
            assert not registry["call_ids"] and not registry["pending_call_ids"] and not registry["media_call_ids"]
            assert not registry["bridge_call_ids"] and not registry["endpoint_claims"]
            resources = runtime_resource_snapshot(lab.calls, detailed=True,
                rtp_port_pool=lab.runtime.rtp_port_pool, call_artifacts=lab.calls,
                browser_media=lab.runtime.media, runtime_tasks=lab.runtime.tasks)
            assert resources["call_scoped_quiescent"]
            assert all(count == 0 for count in resources["resource_counts"].values()), resources
            assert not resources["allocated_rtp_ports"]
            assert all(not identities for identities in resources["call_ids"].values()), resources
            assert all(_ha_softphone_state(lab.hass, account.endpoint_id)["state"] == "idle" for account in accounts)
            # Duplicate requests are harmless after completed cleanup, before
            # these same accounts originate and accept their next call.
            async with asyncio.timeout(2):
                await asyncio.gather(*(lab.hass.services.async_call("voip_stack", "hangup", {
                    "device_id": account.device_id, "call_id": call_id,
                }, blocking=True) for account, call_id in zip(accounts, call_ids, strict=True)))
        assert len(seen_call_ids) == 6
        assert not any(phone.invites for phone in lab.phones)

        # The same host and participant can immediately make an ordinary
        # point-to-point call, with no conference owner or media left behind.
        from custom_components.voip_stack.core.audio_format import HA_SIP_PCM_FORMATS
        from custom_components.voip_stack.local_softphone_runtime import async_setup_local_softphone_bridge
        bridge = async_setup_local_softphone_bridge(lab.hass)
        await lab.hass.services.async_call("voip_stack", "call", {
            "device_id": accounts[0].device_id, "destination": accounts[1].name,
            "media_client_id": "host-browser-owner",
        }, blocking=True)
        ordinary_id = _ha_softphone_state(lab.hass, accounts[0].endpoint_id)["call_id"]
        assert ordinary_id and not ordinary_id.startswith("conference:")
        await lab.hass.services.async_call("voip_stack", "answer", {
            "device_id": accounts[1].device_id, "call_id": ordinary_id,
            "media_client_id": accounts[1].username + "-browser-owner",
        }, blocking=True)
        leases = [bridge.acquire_media(ordinary_id, account.endpoint_id, owner)
            for account, owner in zip(accounts[:2],
                ("host-browser-owner", accounts[1].username + "-browser-owner"), strict=True)]
        for source, destination, sample in ((0, 1, 4321), (1, 0, -2345)):
            pcm = struct.pack("<h", sample) * HA_SIP_PCM_FORMATS[0].nominal_frame_samples
            assert not bridge.send_audio(ordinary_id, accounts[source].endpoint_id, leases[source].token, pcm)
            async with asyncio.timeout(2):
                assert await bridge.receive_audio(ordinary_id, accounts[destination].endpoint_id,
                                                   leases[destination].token) == pcm
        ordinary_session = lab.calls.get_session(ordinary_id)
        await lab.hass.services.async_call("voip_stack", "hangup", {
            "device_id": accounts[0].device_id, "call_id": ordinary_id,
        }, blocking=True)
        async with asyncio.timeout(2):
            await ordinary_session.terminated.wait()
        assert not manager.rooms and not manager.ha_calls
        assert_call_resources_released(lab)
        assert all(_ha_softphone_state(lab.hass, account.endpoint_id)["state"] == "idle" for account in accounts)
    finally:
        unsubscribe()
        for session in tuple(lab.calls.sessions.values()):
            await lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
        await bindings.async_close()
        await endpoint_runtime.async_stop_sip_endpoint(lab.hass)
