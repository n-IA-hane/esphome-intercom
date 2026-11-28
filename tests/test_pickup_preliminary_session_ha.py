"""Pickup retires only its native route-window session, retaining the real call."""

import asyncio
import struct

import pytest
from unittest.mock import AsyncMock

from homeassistant.exceptions import ServiceValidationError

from .test_automation_originate_ha import lab as lab

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("mode,fail,fault", [
    ("parking", False, None), ("directed", False, None),
    ("parking", True, None), ("directed", True, None),
    ("parking", False, "cancel"), ("directed", False, "cancel"),
    ("parking", False, "new_generation"), ("directed", False, "new_generation"),
    ("directed", False, "canonical_offline"),
    ("directed", False, "canonical_dnd"),
    ("directed", False, "canonical_disabled"),
    ("directed", False, "canonical_stale_claim"),
])
async def test_native_pickup_retires_preliminary_session_and_preserves_parallel_call(lab, monkeypatch, mode, fail, fault):
    from custom_components.voip_stack import softphone_originate
    from custom_components.voip_stack.endpoint_session import TerminationIntent
    from custom_components.voip_stack.local_softphone_runtime import (
        async_setup_local_softphone_bridge, async_shutdown_local_softphone_bridge,
        start_local_softphone_call,
    )
    from custom_components.voip_stack.parking import get_parking_lot
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.roster import RosterEntry
    from custom_components.voip_stack.runtime_data import registration_data
    from custom_components.voip_stack.directory_runtime import _endpoint_entry

    for name in ("route-a", "route-b", "route-c", "parallel-a", "parallel-b"):
        canonical = name == "route-c" and str(fault).startswith("canonical_")
        endpoint = PhoneEndpoint(endpoint_id=name, username=name, name=name,
            device_id="device-" + name, kind=EndpointKind.SIP_ACCOUNT if canonical else EndpointKind.BROWSER,
            availability="unavailable" if canonical and fault == "canonical_disabled" else "offline" if canonical else "available",
            dnd=canonical and fault == "canonical_dnd", capabilities=frozenset({"audio", "dtmf"}))
        lab.runtime.endpoints.register(endpoint)
        lab.roster.append(_endpoint_entry(endpoint) if canonical else
            RosterEntry(name, metadata={"local_ha": True, "endpoint_id": name}))
    lab.runtime.directory.replace(lab.roster)
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    bridge = async_setup_local_softphone_bridge(lab.hass)
    original = start_local_softphone_call(lab.hass, "route-a", "route-b", caller_owner_id="owner-a")
    if mode == "parking":
        bridge.answer(original.call_id, "route-b", "owner-b")
        await lab.hass.services.async_call("voip_stack", "park", {
            "device_id": "device-route-b"}, blocking=True, return_response=True)
        destination, picker = ("702" if fail else "701"), "route-b"
    else:
        destination, picker = "pickup." + ("route-c" if fail else "route-b"), "route-c"
    session = lab.calls.get_session(original.call_id)
    token = session.token
    parallel = start_local_softphone_call(lab.hass, "parallel-a", "parallel-b", caller_owner_id="parallel-owner")
    bridge.answer(parallel.call_id, "parallel-b", "parallel-answer")
    parallel_session = lab.calls.get_session(parallel.call_id)
    parallel_token = parallel_session.token
    filters = registration_data(lab.hass).route_trigger_filters
    key = object()
    filters[key] = {"destination": destination}
    successors = []
    reached = asyncio.Event()
    if fault == "new_generation":
        from custom_components.voip_stack import parking, call_pickup
        module, name = (parking, "pickup_browser_call") if mode == "parking" else (call_pickup, "pickup_browser")
        pickup = getattr(module, name)

        async def replace_preliminary(*args):
            preliminary, = (current for current in lab.calls.sessions.values()
                if current.call_id not in {original.call_id, parallel.call_id})
            previous = preliminary.token
            recovered = await pickup(*args)
            await lab.calls.request_termination(previous.call_id, TerminationIntent("test_route_replaced"),
                generation=previous.generation)
            successor = lab.calls.upsert(previous.call_id, state="connecting", owner="router")
            assert successor.generation != previous.generation
            successors.append(successor)
            return recovered

        monkeypatch.setattr(module, name, replace_preliminary)
    elif fault == "cancel":
        forward = lab.calls.forward_call

        async def pause_handoff(**kwargs):
            reached.set()
            await asyncio.Event().wait()
            return await forward(**kwargs)

        lab.calls.forward_call = pause_handoff
    elif fault == "canonical_stale_claim":
        from custom_components.voip_stack import call_forwarder
        discover = call_forwarder._async_build_peer_snapshot

        async def retire_claim(hass):
            peers = await discover(hass)
            claim = lab.calls.resource_for(token.call_id, "pickup")
            lab.calls.release_resource(token.call_id, "pickup:" + token.call_id,
                value=claim, generation=token.generation)
            return peers

        monkeypatch.setattr(call_forwarder, "_async_build_peer_snapshot", retire_claim)
    rejected = fail or fault in {"canonical_dnd", "canonical_disabled", "canonical_stale_claim"}
    try:
        call = lab.hass.services.async_call("voip_stack", "call", {
            "device_id": "device-" + picker, "destination": destination,
            "media_client_id": "pickup-owner"}, blocking=True)
        if fault == "cancel":
            pending = asyncio.create_task(call)
            await asyncio.wait_for(reached.wait(), 3)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        elif rejected:
            with pytest.raises((ServiceValidationError, ValueError)):
                await call
        else:
            await call
        assert lab.calls.get_session(token.call_id, generation=token.generation) is session
        assert lab.calls.get_session(parallel_token.call_id, generation=parallel_token.generation) is parallel_session
        assert parallel_session.live and bridge.get_call(parallel.call_id).callee_state.value == "in_call"
        # The LAB witness retained an otherwise empty router session here.
        assert set(lab.calls.sessions) == {original.call_id, parallel.call_id, *(s.call_id for s in successors)}
        assert all(successor.live for successor in successors)
        if rejected or fault == "cancel":
            if mode == "parking":
                assert get_parking_lot(lab.hass).calls["701"].session is session
                assert get_parking_lot(lab.hass).calls["701"].claim is None
            else:
                assert bridge.get_call(original.call_id).callee_state.value == "ringing"
        else:
            assert session.live and session.state == "in_call"
            assert session.endpoint_claims == {"route-a": "source", picker: "destination"}
            assert not get_parking_lot(lab.hass).calls
            source = bridge.acquire_media(token.call_id, "route-a", "owner-a")
            target = bridge.acquire_media(token.call_id, picker, "pickup-owner")
            pcm = struct.pack("<480h", *([7000] * 480))
            bridge.send_audio(token.call_id, "route-a", source.token, pcm)
            assert bridge.receive_audio_nowait(token.call_id, picker, target.token) == pcm
            bridge.send_audio(token.call_id, picker, target.token, pcm)
            assert bridge.receive_audio_nowait(token.call_id, "route-a", source.token) == pcm
    finally:
        filters.pop(key)
        for current in tuple(lab.calls.sessions.values()):
            await lab.calls.request_termination(current.call_id, TerminationIntent("test_done"), generation=current.generation)
        async_shutdown_local_softphone_bridge(lab.hass)
