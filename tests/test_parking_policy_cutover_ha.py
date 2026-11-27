"""A concurrent parking policy edit cannot strand the original browser call."""

import asyncio

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.voip_stack.local_call_media import LocalCallMedia
from custom_components.voip_stack.local_softphone_runtime import (
    async_setup_local_softphone_bridge,
    async_shutdown_local_softphone_bridge,
    start_local_softphone_call,
)
from custom_components.voip_stack.parking import get_parking_lot
from custom_components.voip_stack.store import config_entry

from .test_trunk_route_outcomes_ha import (
    initial_ha_account,
    lab as lab,
    route_outcome_runtime as route_outcome_runtime,
)

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("policy_edit,timeout", [
    pytest.param("missing", None, id="missing"),
    pytest.param("zero", 0, id="zero"),
    pytest.param("valid", 61, id="valid"),
    pytest.param("too_large", 3601, id="too-large"),
    pytest.param("boolean", True, id="boolean"),
    pytest.param("string", "61", id="string"),
])
async def test_parking_rechecks_policy_before_committing_prepared_media(
    lab, monkeypatch, policy_edit, timeout,
):
    first = await initial_ha_account(lab, "parking-a")
    second = await initial_ha_account(lab, "parking-b")
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry, data={
        **entry.data, "parking": {**entry.data["parking"], "timeout": 47},
    })
    bridge = async_setup_local_softphone_bridge(lab.hass)
    call = start_local_softphone_call(
        lab.hass, first.endpoint_id, second.endpoint_id, caller_owner_id="owner-a",
    )
    bridge.answer(call.call_id, second.endpoint_id, "owner-b")
    session = lab.calls.get_session(call.call_id)
    original_media = lab.calls.resource_for(call.call_id, "softphone_media")
    original_owner = session.owner
    lot = get_parking_lot(lab.hass)
    entered, release = asyncio.Event(), asyncio.Event()
    original_start = LocalCallMedia.start
    armed = asyncio.Event()
    original_arm_return = lot.arm_return

    async def start(media):
        await original_start(media)
        entered.set()
        await release.wait()

    def arm_return(*args):
        original_arm_return(*args)
        armed.set()

    monkeypatch.setattr(LocalCallMedia, "start", start)
    monkeypatch.setattr(lot, "arm_return", arm_return)
    task = asyncio.create_task(lab.hass.services.async_call(
        "voip_stack", "park", {"device_id": first.device_id}, blocking=True,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        settings = dict(entry.data["parking"])
        if policy_edit == "missing":
            settings.pop("timeout")
        else:
            settings["timeout"] = timeout
        lab.hass.config_entries.async_update_entry(entry, data={
            **entry.data, "parking": settings,
        })
        release.set()
        if policy_edit not in {"zero", "valid"}:
            with pytest.raises(ServiceValidationError):
                await task
            assert not lot.calls
            assert not armed.is_set()
            assert not lab.runtime.rtp_port_pool["used"]
            assert lab.calls.resource_for(call.call_id, "relay") is None
            assert lab.calls.resource_for(call.call_id, "softphone_media") is original_media
            assert original_media["local_bridge"]
            assert bridge.get_call(call.call_id) is not None
            assert session.live and session.owner == original_owner
            source = bridge.acquire_media(call.call_id, first.endpoint_id, "owner-a")
            target = bridge.acquire_media(call.call_id, second.endpoint_id, "owner-b")
            pcm = bytes([20, 0]) * 480
            bridge.send_audio(call.call_id, first.endpoint_id, source.token, pcm)
            assert bridge.receive_audio_nowait(call.call_id, second.endpoint_id, target.token) == pcm
        else:
            await task
            application = lab.calls.resource_for(call.call_id, "parking")
            assert application is not None
            assert session.live and session.owner == "parking"
            assert application.settings["timeout"] == timeout
            # Observe the real policy application even when zero creates no timer.
            await asyncio.wait_for(armed.wait(), 2)
            assert lot.calls == {application.parked.slot: application.parked}
            if policy_edit == "zero":
                assert application.parked.timer is None
                assert application.parked.deadline is None
            else:
                assert application.parked.timer is not None
                assert not application.parked.timer.done()
                assert application.parked.deadline is not None
            deadline = application.parked.deadline
            lab.hass.config_entries.async_update_entry(entry, data={
                **entry.data, "parking": {**settings, "timeout": 89},
            })
            assert application.settings["timeout"] == timeout
            assert application.parked.deadline == deadline
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await lot.close()
        if session.live:
            await lab.calls.terminate_call_wait(call.call_id, reason="test_cleanup")
        async_shutdown_local_softphone_bridge(lab.hass)
