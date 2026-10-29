"""HA mailbox truth reaches the physical ESP through its native API owner."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.esphome.const import CLIENT_INFO
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.voip_stack import esphome_mailbox as module
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.mailbox import (
    EVENT_VOICEMAIL_CHANGED,
    async_refresh_mailbox_state,
    async_remove_mailbox,
    async_setup_mailboxes,
    get_archive,
)
from custom_components.voip_stack.store import config_entry

from .test_voicemail_ha import (
    configured_tts_provider as configured_tts_provider,
    lab as lab,
    mailbox_lab as mailbox_lab,
)

pytestmark = pytest.mark.ha


@pytest.fixture
async def esp_mailbox(mailbox_lab, monkeypatch):
    lab = mailbox_lab
    hass = lab.hass
    native = MockConfigEntry(domain="esphome", title="Physical API identity", data={
        "host": "192.0.2.80", "device_name": "physical_node",
    })
    native.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=native.entry_id,
        identifiers={("esphome", "84c7bb000001")},
        connections={(dr.CONNECTION_NETWORK_MAC, "84:c7:bb:00:00:01")},
        name="Phone",
    )
    entities = er.async_get(hass)
    endpoint = entities.async_get_or_create("sensor", "esphome", "mac_voip_endpoint",
        config_entry=native, device_id=device.id, suggested_object_id="renamed_capabilities")
    state = entities.async_get_or_create("sensor", "esphome", "mac_voip_state",
        config_entry=native, device_id=device.id, suggested_object_id="renamed_status")
    hass.states.async_set(endpoint.entity_id,
        "Phone|203.0.113.60|5060|40000|full_duplex|16000:s16le:1:16|16000:s16le:1:16|sip_udp|101")
    hass.states.async_set(state.entity_id, "idle")
    identity = f"esphome:{device.id}"
    entry = config_entry(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data,
        "esphome_mailboxes": {identity: {"enabled": True}}})
    source_ip = AsyncMock(return_value="192.0.2.1")
    monkeypatch.setattr(module.network, "async_get_source_ip", source_ip)
    received = []
    control = SimpleNamespace(fail=False, entered=None, release=None)

    async def delivered(call):
        if control.entered is not None:
            control.entered.set()
            await control.release.wait()
        if control.fail:
            raise HomeAssistantError("API connection unavailable")
        received.append(dict(call.data))

    # These are the existing action surface, discovered by the native node
    # name even though SIP advertises a different public/proxy address.
    for action in ("start_call", "answer_call", "decline_call", "hangup_call"):
        hass.services.async_register("esphome", f"physical_node_{action}", lambda _: None)
    hass.services.async_register("esphome", "physical_node_set_message_waiting", delivered)
    await hass.async_block_till_done()
    await async_setup_mailboxes(hass, entry)
    await hass.async_block_till_done(wait_background_tasks=True)
    obj = SimpleNamespace(**vars(lab), device=device, native=native, state=state,
        endpoint_id=identity, received=received, source_ip=source_ip, control=control,
        delivered=delivered, bridge=lab.runtime.esphome_mailbox)
    yield obj
    await obj.bridge.async_close()
    await hass.async_block_till_done(wait_background_tasks=True)


async def changed(lab, identity=None, **extra):
    lab.hass.bus.async_fire(EVENT_VOICEMAIL_CHANGED,
        {"endpoint_id": identity or lab.endpoint_id, **extra})
    await lab.hass.async_block_till_done(wait_background_tasks=True)


async def deposit(lab):
    archive = await get_archive(lab.hass)
    writer = await archive.begin(lab.endpoint_id)
    assert writer.offer(bytes(640), AudioFormat(16000, frame_ms=20))
    message = await writer.finish()
    await changed(lab)
    return archive, message["id"]


async def test_initial_snapshot_uses_native_api_owner_not_sip_address(esp_mailbox):
    lab = esp_mailbox
    assert lab.received == [{
        "available": True, "waiting": False, "new_messages": 0, "old_messages": 0,
        "source_client_info": CLIENT_INFO, "source_client_address": "192.0.2.1",
    }]
    lab.source_ip.assert_awaited_with(lab.hass, target_ip="192.0.2.80")
    device = lab.runtime.endpoints.get(lab.endpoint_id)
    assert device.device_id == lab.device.id


async def test_unread_read_and_deleted_messages_follow_persistent_archive(esp_mailbox):
    lab = esp_mailbox
    archive, first = await deposit(lab)
    _, second = await deposit(lab)
    assert lab.received[-1]["new_messages"] == 2
    assert lab.received[-1]["waiting"] is True
    await archive.mark_read(first, lab.endpoint_id)
    await changed(lab)
    assert (lab.received[-1]["new_messages"], lab.received[-1]["old_messages"]) == (1, 1)
    await archive.delete(second, lab.endpoint_id)
    await changed(lab)
    assert lab.received[-1]["waiting"] is False
    assert (lab.received[-1]["new_messages"], lab.received[-1]["old_messages"]) == (0, 1)


async def test_mailbox_removed_sends_unavailable_without_touching_other_phones(esp_mailbox):
    lab = esp_mailbox
    await deposit(lab)
    await async_remove_mailbox(lab.hass, lab.endpoint_id)
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert lab.received[-1]["available"] is False
    assert lab.received[-1]["waiting"] is False
    assert lab.received[-1]["new_messages"] == lab.received[-1]["old_messages"] == 0


async def test_config_refresh_disables_and_reenables_same_mailbox(esp_mailbox):
    lab = esp_mailbox
    await deposit(lab)
    entry = config_entry(lab.hass)
    original = dict(entry.data)
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "esphome_mailboxes": {lab.endpoint_id: {"enabled": False}}})
    await async_refresh_mailbox_state(lab.hass)
    assert lab.received[-1]["available"] is False
    lab.hass.config_entries.async_update_entry(entry, data=original)
    await async_refresh_mailbox_state(lab.hass)
    assert lab.received[-1]["available"] is True
    assert lab.received[-1]["waiting"] is True
    assert lab.received[-1]["new_messages"] == 1


async def test_no_change_other_endpoint_and_call_states_do_not_repeat_snapshot(esp_mailbox):
    lab = esp_mailbox
    await changed(lab)
    await changed(lab, "a-different-mailbox")
    lab.hass.states.async_set(lab.state.entity_id, "ringing")
    lab.hass.services.async_register("esphome", "irrelevant_set_roster_json", lambda _: None)
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert len(lab.received) == 1


async def test_renamed_state_entity_reconnect_resends_identical_snapshot(esp_mailbox):
    lab = esp_mailbox
    lab.hass.states.async_set(lab.state.entity_id, "unavailable")
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert len(lab.received) == 1
    lab.hass.states.async_set(lab.state.entity_id, "idle")
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert len(lab.received) == 2
    assert lab.received[0] == lab.received[1]


async def test_native_action_registered_again_resends_cached_snapshot(esp_mailbox):
    lab = esp_mailbox
    lab.hass.services.async_remove("esphome", "physical_node_set_message_waiting")
    lab.hass.services.async_register("esphome", "physical_node_set_message_waiting", lab.delivered)
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert len(lab.received) == 2
    assert lab.received[0] == lab.received[1]


async def test_reconnect_during_inflight_delivery_cannot_poison_cache(esp_mailbox):
    lab = esp_mailbox
    archive = await get_archive(lab.hass)
    writer = await archive.begin(lab.endpoint_id)
    assert writer.offer(bytes(640), AudioFormat(16000, frame_ms=20))
    await writer.finish()
    lab.control.entered, lab.control.release = asyncio.Event(), asyncio.Event()
    push = asyncio.create_task(lab.bridge.async_push())
    await asyncio.wait_for(lab.control.entered.wait(), 2)
    lab.hass.states.async_set(lab.state.entity_id, "unavailable")
    lab.hass.states.async_set(lab.state.entity_id, "idle")
    # Observe the actual public state event before releasing the older write.
    seen = asyncio.Event()
    @callback
    def event_seen(_event):
        seen.set()
    unsubscribe = lab.hass.bus.async_listen_once("bridge-test-barrier", event_seen)
    lab.hass.bus.async_fire("bridge-test-barrier")
    await asyncio.wait_for(seen.wait(), 2)
    unsubscribe()
    lab.control.release.set()
    await push
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert len(lab.received) == 3
    assert lab.received[-2] == lab.received[-1]


async def test_failed_delivery_is_not_cached_and_retries_on_next_change(esp_mailbox):
    lab = esp_mailbox
    lab.control.fail = True
    await deposit(lab)
    assert len(lab.received) == 1
    lab.control.fail = False
    await changed(lab)
    assert len(lab.received) == 2
    assert lab.received[-1]["waiting"] is True


async def test_old_firmware_without_native_action_is_quiet_and_no_network_probe(esp_mailbox):
    lab = esp_mailbox
    lab.hass.services.async_remove("esphome", "physical_node_set_message_waiting")
    lab.source_ip.reset_mock()
    await changed(lab)
    lab.source_ip.assert_not_awaited()
    assert len(lab.received) == 1


async def test_stop_clears_badge_before_sip_owner_stops_and_unsubscribes(esp_mailbox, monkeypatch):
    from custom_components import voip_stack
    from custom_components.voip_stack import websocket_api

    lab = esp_mailbox
    await deposit(lab)
    shutdown = AsyncMock()
    async def stop(_hass):
        assert lab.received[-1]["available"] is False
        assert lab.runtime.esphome_mailbox is None
    stop_sip = AsyncMock(side_effect=stop)
    monkeypatch.setattr(websocket_api, "_async_shutdown_all", shutdown)
    monkeypatch.setattr(voip_stack, "_async_stop_sip_endpoint", stop_sip)
    await voip_stack._async_stop_runtime(lab.hass)
    assert shutdown.await_count == stop_sip.await_count == 1
    count = len(lab.received)
    await changed(lab)
    lab.hass.states.async_set(lab.state.entity_id, "unavailable")
    lab.hass.states.async_set(lab.state.entity_id, "idle")
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert len(lab.received) == count
    assert not lab.runtime.tasks


async def test_concurrent_stop_waits_for_the_same_final_delivery(esp_mailbox):
    lab = esp_mailbox
    lab.control.entered, lab.control.release = asyncio.Event(), asyncio.Event()
    first = asyncio.create_task(lab.bridge.async_close())
    await asyncio.wait_for(lab.control.entered.wait(), 2)
    second = asyncio.create_task(lab.bridge.async_close())
    # Queue a real loop callback after the second caller, without a timing sleep.
    ready = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(ready.set_result, None)
    await ready
    assert not first.done() and not second.done()
    lab.control.release.set()
    await asyncio.gather(first, second)
    assert len(lab.received) == 2
    assert lab.received[-1]["available"] is False


async def test_capabilities_arriving_after_state_resynchronize_on_reconnect(esp_mailbox):
    lab = esp_mailbox
    entities = er.async_get(lab.hass)
    capability = entities.async_get_entity_id("sensor", "esphome", "mac_voip_endpoint")
    original = lab.hass.states.get(capability).state
    lab.hass.states.async_set(capability, "unavailable")
    lab.hass.states.async_set(lab.state.entity_id, "unavailable")
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    lab.hass.states.async_set(lab.state.entity_id, "idle")
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert len(lab.received) == 1
    lab.hass.states.async_set(capability, original)
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert len(lab.received) == 2
    assert lab.received[0] == lab.received[1]
