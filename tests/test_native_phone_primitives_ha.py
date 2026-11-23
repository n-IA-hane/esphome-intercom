"""Native phone selection, entity lifetime and action translations."""

from datetime import timedelta
import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform

from .test_managed_phones_ha import custom_integration_path as custom_integration_path, phone_lab as phone_lab

pytestmark = pytest.mark.ha


async def test_native_services_expose_all_registered_actions_in_each_language(hass):
    from homeassistant.helpers.translation import async_get_translations
    from homeassistant.setup import async_setup_component

    from custom_components.voip_stack import _async_register_services
    from custom_components.voip_stack.physical_phone_services import register_physical_phone_services
    from custom_components.voip_stack.sip_capture_service import async_register_sip_capture
    from .test_config_hardening import _leaf_values

    assert await async_setup_component(hass, "http", {})
    await _async_register_services(hass)
    await async_register_sip_capture(hass)
    register_physical_phone_services(hass)
    base = Path(__file__).resolve().parents[1] / "custom_components" / "voip_stack"
    source = json.loads((base / "strings.json").read_text())["services"]
    assert set(hass.services.async_services()["voip_stack"]) == set(source)
    for language in ("en", "it", "cs", "de", "pt-BR"):
        document = json.loads((base / "translations" / f"{language}.json").read_text())
        expected = {
            f"component.voip_stack.services.{path}": value
            for path, value in _leaf_values(document["services"]).items()
        }
        translated = await async_get_translations(hass, language, "services", {"voip_stack"})
        assert translated == expected


def platform_for(lab, domain):
    platform = EntityPlatform(hass=lab.hass, logger=logging.getLogger(__name__), domain=domain,
        platform_name="voip_stack", platform=None, scan_interval=timedelta(seconds=30), entity_namespace=None)
    platform.config_entry = lab.entry
    return platform


@pytest.mark.parametrize("configured,default,bound,expected", [
    ("device-other", True, {"default"}, "other"),
    ("missing-device", True, {"other"}, "default"),
    ("missing-device", False, {"other"}, "other"),
    ("", False, {"other", "third"}, None),
    ("", False, set(), None),
])
async def test_default_selection_agrees_across_setup_runtime_and_service(phone_lab, configured, default, bound, expected):
    from custom_components.voip_stack import _preferred_phone_device_id
    from custom_components.voip_stack.phone_control import PhoneAdapterRegistry, PhoneOperation
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
    from custom_components.voip_stack.runtime_data import preferred_browser_phone

    lab = phone_lab
    for identity in (("default", "other", "third") if default else ("other", "third")):
        lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id=identity, kind="sip_account",
            name=identity, username=identity, device_id="device-" + identity))
    lab.runtime.preferred_phone_device_id = configured
    lab.runtime.ha_bindings = SimpleNamespace(has_binding=lambda identity: identity in bound)
    selected_device = _preferred_phone_device_id(lab.runtime.endpoints, configured, ha_bound_endpoint_ids=bound)
    runtime_phone = preferred_browser_phone(lab.hass)
    assert selected_device == ("device-" + expected if expected else "")
    assert (runtime_phone.endpoint_id if runtime_phone else None) == expected
    adapters = PhoneAdapterRegistry(lab.hass, lab.runtime.endpoints, preferred_phone_device_id=configured)
    call = ServiceCall(lab.hass, "voip_stack", "call", {})
    if expected is None:
        with pytest.raises(ServiceValidationError) as error:
            await adapters.resolve_source(call, PhoneOperation.ORIGINATE)
        assert error.value.translation_key == "phone_selection_required"
    else:
        assert (await adapters.resolve_source(call, PhoneOperation.ORIGINATE)).endpoint_id == expected


@pytest.mark.parametrize("kind", ["text", "dnd", "conference", "answer", "video", "state", "event"])
async def test_native_entity_delete_unlinks_only_its_own_id_and_keeps_the_call(phone_lab, kind):
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
    from custom_components.voip_stack.sensor import PhoneEndpointCallStateSensor
    from custom_components.voip_stack.event import PhoneEndpointCallEvent
    from custom_components.voip_stack.switch import (PhoneEndpointDndSwitch, PhoneEndpointConferenceRingSwitch,
        PhoneEndpointAutoAnswerSwitch, PhoneEndpointSendVideoSwitch)
    from custom_components.voip_stack.text import PhoneEndpointSettingText, _SETTINGS

    lab = phone_lab
    endpoint = PhoneEndpoint(endpoint_id="desk", kind="sip_account", name="Desk", username="desk",
        active_call_id="existing-call", entity_ids={"sensor.sibling"})
    lab.runtime.endpoints.register(endpoint)
    factories = {"text": ("text", lambda *args: PhoneEndpointSettingText(*args, setting=_SETTINGS[0])),
        "dnd": ("switch", PhoneEndpointDndSwitch), "conference": ("switch", PhoneEndpointConferenceRingSwitch),
        "answer": ("switch", PhoneEndpointAutoAnswerSwitch), "video": ("switch", PhoneEndpointSendVideoSwitch),
        "state": ("sensor", PhoneEndpointCallStateSensor), "event": ("event", PhoneEndpointCallEvent)}
    domain, factory = factories[kind]
    platform = platform_for(lab, domain)
    entity = factory(lab.hass, endpoint, lab.runtime.endpoints)
    await platform.async_add_entities([entity])
    stable = entity.unique_id
    try:
        er.async_get(lab.hass).async_update_entity(entity.entity_id,
            new_entity_id=domain + ".user_named", name="My custom label")
        await lab.hass.async_block_till_done()
        assert entity.unique_id == stable
        assert er.async_get(lab.hass).async_get(entity.entity_id).name == "My custom label"
        er.async_get(lab.hass).async_remove(entity.entity_id)
        await lab.hass.async_block_till_done()
        current = lab.runtime.endpoints.require("desk")
        assert current.entity_ids == frozenset({"sensor.sibling"})
        assert current.active_call_id == "existing-call"
    finally:
        await platform.async_reset()


async def test_subentry_delete_uses_native_registry_cleanup_and_keeps_pbx_cleanup_separate(phone_lab, monkeypatch):
    from custom_components.voip_stack.endpoint_entity_manager import EndpointEntityManager
    from custom_components.voip_stack.endpoint_lifecycle import call_registry
    from custom_components.voip_stack.phone_config import async_setup_endpoint_registry, sync_registry_from_entry
    from custom_components.voip_stack.sensor import PhoneEndpointCallStateSensor

    lab = phone_lab
    child = ConfigSubentry(subentry_type="phone", title="Desk", unique_id="phone:sip:desk",
        data={"kind": "sip_account", "name": "Desk", "username": "desk"})
    lab.hass.config_entries.async_add_subentry(lab.entry, child)
    lab.runtime.endpoints = async_setup_endpoint_registry(lab.hass, lab.entry)
    platform = platform_for(lab, "sensor")
    def add_entities(entities, *, config_subentry_id=None):
        lab.entry.async_create_task(lab.hass,
            platform.async_add_entities(entities, config_subentry_id=config_subentry_id), "test native entity add")
    manager = EndpointEntityManager(lab.hass, lab.entry, add_entities, PhoneEndpointCallStateSensor)
    manager.async_setup()
    await lab.hass.async_block_till_done()
    entity = manager.entities["sip:desk"]
    remove = AsyncMock(wraps=entity.async_remove)
    monkeypatch.setattr(entity, "async_remove", remove)
    calls = call_registry(lab.hass)
    session = calls.upsert("existing-call", state="in_call", owner="router", source_endpoint_id="sip:desk")
    calls.claim_endpoint(session.call_id, "sip:desk", role="source")
    try:
        lab.hass.config_entries.async_remove_subentry(lab.entry, child.subentry_id)
        sync_registry_from_entry(lab.hass, lab.entry)
        await lab.hass.async_block_till_done()
        assert calls.get_session(session.call_id) is session and session.live
        assert lab.runtime.endpoints.require("sip:desk").active_call_id == session.call_id
        assert er.async_get(lab.hass).async_get(entity.entity_id) is None
        await calls.terminate_call_wait(session.call_id, reason="test_done")
        await lab.hass.async_block_till_done()
        assert lab.runtime.endpoints.get("sip:desk") is None
        assert not manager.entities
        assert remove.await_count == 1
    finally:
        await platform.async_reset()


async def test_phonebook_failed_start_detaches_registry_and_service_listeners(phone_lab, monkeypatch):
    from homeassistant.const import EVENT_SERVICE_REGISTERED
    from custom_components.voip_stack import peer_snapshot
    from custom_components.voip_stack.phonebook_coordinator import entry_phonebook

    lab = phone_lab
    coordinator = entry_phonebook(lab.hass, lab.runtime)
    events = ("entity_registry_updated", EVENT_SERVICE_REGISTERED)
    baseline = {name: lab.hass.bus.async_listeners().get(name, 0) for name in events}
    monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", AsyncMock(side_effect=RuntimeError("initial snapshot failed")))
    with pytest.raises(RuntimeError, match="initial snapshot failed"):
        await coordinator.async_start()
    assert coordinator.closed
    assert baseline == {name: lab.hass.bus.async_listeners().get(name, 0) for name in events}


async def test_phonebook_close_cancels_active_refresh_and_queued_state_event(phone_lab, monkeypatch):
    from custom_components.voip_stack import peer_snapshot
    from custom_components.voip_stack.phonebook_coordinator import entry_phonebook

    lab = phone_lab
    role = er.async_get(lab.hass).async_get_or_create("text", "esphome", "board-voip_ring_groups")
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def snapshot(_hass):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    source = AsyncMock(side_effect=snapshot)
    monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", source)
    coordinator = entry_phonebook(lab.hass, lab.runtime)
    adding = asyncio.create_task(coordinator.async_start())
    await asyncio.wait_for(entered.wait(), 1)
    lab.hass.states.async_set(role.entity_id, "Reception")
    await asyncio.sleep(0)
    await coordinator.async_close()
    with pytest.raises(asyncio.CancelledError):
        await adding
    assert cancelled.is_set()
    lab.hass.states.async_set(role.entity_id, "Later state")
    await lab.hass.async_block_till_done()
    source.assert_awaited_once_with(lab.hass)


async def test_phonebook_tracks_availability_edges_without_sensor_or_call_state_refreshes(phone_lab, monkeypatch):
    from custom_components.voip_stack import peer_snapshot
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
    from custom_components.voip_stack.phonebook_coordinator import entry_phonebook

    lab = phone_lab
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="Board", kind="sip_account",
        name="Board", username="Board"))
    state = er.async_get(lab.hass).async_get_or_create("sensor", "esphome", "board-voip_state")
    source = AsyncMock(return_value=[])
    monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", source)
    owner = entry_phonebook(lab.hass, lab.runtime)
    await owner.async_start()
    try:
        source.reset_mock()
        initial = owner.roster_json
        for status in ("unknown", "unavailable", "idle", "ringing", "in_call", "unavailable"):
            lab.hass.states.async_set(state.entity_id, status)
            await lab.hass.async_block_till_done()
            source.assert_not_awaited()
            assert owner.roster_json == initial, status
        # SIP registration and HA bindings invalidate the canonical account
        # snapshot; ESPHome sensor values do not own account availability.
        lab.runtime.endpoints.update("Board", availability="available")
        owner.request_refresh()
        await lab.hass.async_block_till_done()
        source.assert_awaited_once_with(lab.hass)
        assert lab.runtime.directory.get("Board").metadata["availability"] == "available"
        assert lab.hass.states.get("sensor.voip_phonebook") is None
    finally:
        await owner.async_close()
    lab.hass.states.async_set(state.entity_id, "idle")
    await lab.hass.async_block_till_done()
    assert source.await_count == 1


async def test_phonebook_recomputes_when_endpoint_metadata_changes(phone_lab, monkeypatch):
    from custom_components.voip_stack import peer_snapshot
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
    from custom_components.voip_stack.phonebook_coordinator import entry_phonebook

    lab = phone_lab
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="Board", kind="sip_account",
        name="Board", username="Board", extension="51"))
    state = er.async_get(lab.hass).async_get_or_create("sensor", "esphome", "board-voip_endpoint")
    source = AsyncMock(return_value=[])
    monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", source)
    owner = entry_phonebook(lab.hass, lab.runtime)
    await owner.async_start()
    try:
        lab.hass.states.async_set(state.entity_id, "ready", {"endpoint": {"extension": "51"}})
        await lab.hass.async_block_till_done()
        source.reset_mock()
        lab.hass.states.async_set(state.entity_id, "ready", {"endpoint": {"extension": "51"}, "other": True})
        await lab.hass.async_block_till_done()
        source.assert_not_awaited()
        lab.hass.states.async_set(state.entity_id, "ready", {"endpoint": {"extension": "52"}})
        await lab.hass.async_block_till_done()
        source.assert_not_awaited()
        assert lab.runtime.directory.get("Board").extension == "51"
        lab.runtime.endpoints.update("Board", extension="52")
        owner.request_refresh()
        await lab.hass.async_block_till_done()
        source.assert_awaited_once_with(lab.hass)
        assert lab.runtime.directory.get("Board").extension == "52"
    finally:
        await owner.async_close()
