"""Shared browser-phone operations retain their native runtime resource owners."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.core import callback

from .test_managed_phones_ha import custom_integration_path as custom_integration_path
from .test_phone_state_ha import phones as phones

pytestmark = pytest.mark.ha


async def test_shutdown_gates_claims_drains_existing_owners_and_preserves_store_identity(hass, phones, tmp_path):
    from custom_components.voip_stack import websocket_api as api
    from custom_components.voip_stack.const import HA_SOFTPHONE_STATE_EVENT
    from custom_components.voip_stack.websocket_owner import (
        MediaWebSocketOwner, WebSocketOwnerBusyError, async_claim_media_owner,
        async_release_media_owner,
    )

    runtime, registry = phones.runtime, phones.calls
    session = registry.upsert("browser-owner-shutdown", state="ringing", owner="router",
        caller="Office", callee="Kitchen", source_endpoint_id="office", dest_endpoint_id="kitchen")
    registry.claim_endpoint(session.call_id, "kitchen", role="destination")
    pending_route = hass.loop.create_future()
    session.artifacts.pending_route = {"future": pending_route}
    stores = runtime.softphones
    store = api._ha_softphone_store(hass, "kitchen")
    store.update(state="ringing", sip_state="ringing", call_id=session.call_id,
                 session_device_id="kitchen-device", video_active=True)
    media = runtime.media
    channels = ("audio", "video")
    maps = {channel: media.owners_for(channel) for channel in channels}
    owners = {channel: MediaWebSocketOwner(websocket=SimpleNamespace(force_close=Mock()))
              for channel in channels}
    key = f"kitchen|{session.call_id}"
    for channel in channels:
        maps[channel][key] = owners[channel]
    capture = hass.loop.create_future()
    media.debug_capture_tasks.add(capture)
    capture.add_done_callback(media.debug_capture_tasks.discard)
    events = []

    @callback
    def observe(event):
        events.append(dict(event.data))

    unsubscribe = hass.bus.async_listen(HA_SOFTPHONE_STATE_EVENT, observe)
    shutdown = asyncio.create_task(api._async_shutdown_all(hass))
    try:
        async with asyncio.timeout(1):
            await asyncio.gather(*(owner.handoff_requested.wait() for owner in owners.values()))
        assert media.shutdown.is_set() and pending_route.cancelled()
        assert not shutdown.done()
        with pytest.raises(WebSocketOwnerBusyError):
            await async_claim_media_owner(maps["audio"], media.owner_lock_for("audio"),
                "late-call", MediaWebSocketOwner(), timeout=.1, shutdown_event=media.shutdown)
        for channel in channels:
            owners[channel].websocket.force_close.assert_called_once_with()
            await async_release_media_owner(maps[channel], media.owner_lock_for(channel), key, owners[channel])
        assert not shutdown.done(), "A pending atomic capture must remain part of shutdown"
        capture.set_result(None)
        await shutdown
        await hass.async_block_till_done()
        assert runtime.softphones is stores and api._ha_softphone_store(hass, "kitchen") is store
        assert registry.get_session(session.call_id) is session
        assert store["state"] == "idle" and store["sip_state"] == "idle"
        assert not store.get("call_id") and not store.get("video_active")
        assert store["last_sip_event"] == "shutdown"
        assert all(media.owners_for(channel) is maps[channel] and not maps[channel] for channel in channels)
        assert all(owner.released.is_set() for owner in owners.values())
        assert not media.debug_capture_tasks
        assert [(event["endpoint_id"], event["state"], event["last_sip_event"]) for event in events] == [
            ("kitchen", "idle", "shutdown")]
        (tmp_path / "browser-phone-shutdown.json").write_text(json.dumps({
            "maps_preserved": True, "store_preserved": True, "canonical_session_preserved": True,
            "shutdown_gate_set": media.shutdown.is_set(), "pending_route_cancelled": pending_route.cancelled(),
            "owner_revokes": {channel: owners[channel].websocket.force_close.call_count for channel in channels},
            "capture_drained": not media.debug_capture_tasks,
            "events": [{key: event[key] for key in ("endpoint_id", "state", "last_sip_event")} for event in events],
        }, indent=2))
    finally:
        if not capture.done():
            capture.set_result(None)
        for channel in channels:
            await async_release_media_owner(maps[channel], media.owner_lock_for(channel), key, owners[channel])
        await shutdown
        unsubscribe()
