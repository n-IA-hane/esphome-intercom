"""One default-phone policy and HA-owned entity lifecycle, without live calls."""

from datetime import timedelta
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from pytest_homeassistant.common import MockConfigEntry

from .test_managed_phones_ha import custom_integration_path as custom_integration_path, phone_lab as phone_lab

pytestmark = pytest.mark.ha


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


async def test_phonebook_aborted_add_releases_its_registry_subscription(hass, monkeypatch):
    from custom_components.voip_stack.sensor import VoipPhonebookSensor

    entry = MockConfigEntry(domain="voip_stack", data={})
    entry.add_to_hass(hass)
    platform = EntityPlatform(hass=hass, logger=logging.getLogger(__name__), domain="sensor",
        platform_name="voip_stack", platform=None, scan_interval=timedelta(seconds=30), entity_namespace=None)
    platform.config_entry = entry
    sensor = VoipPhonebookSensor(hass)
    sensor.add_to_platform_start(hass, platform, None)
    before = hass.bus.async_listeners().get("entity_registry_updated", 0)
    monkeypatch.setattr(sensor, "_refresh_tracked_entities", AsyncMock(side_effect=RuntimeError("initial snapshot failed")))
    try:
        with pytest.raises(RuntimeError, match="initial snapshot failed"):
            await sensor.async_added_to_hass()
        sensor.add_to_platform_abort()
        assert hass.bus.async_listeners().get("entity_registry_updated", 0) == before
    finally:
        if remove := getattr(sensor, "_unsub_registry", None):
            remove()


async def test_phonebook_aborted_add_cancels_recompute_requeued_by_a_real_state_event(hass, monkeypatch):
    from custom_components.voip_stack.sensor import VoipPhonebookSensor

    entry = MockConfigEntry(domain="voip_stack", data={})
    entry.add_to_hass(hass)
    platform = EntityPlatform(hass=hass, logger=logging.getLogger(__name__), domain="sensor",
        platform_name="voip_stack", platform=None, scan_interval=timedelta(seconds=30), entity_namespace=None)
    platform.config_entry = entry
    role = er.async_get(hass).async_get_or_create("text", "esphome", "board-voip_ring_groups")
    sensor = VoipPhonebookSensor(hass)
    sensor.add_to_platform_start(hass, platform, None)
    first_started, first_release, second_started, second_release = [asyncio.Event() for _ in range(4)]
    producer_tasks = []

    async def snapshot():
        producer_tasks.append(asyncio.current_task())
        if len(producer_tasks) == 1:
            first_started.set()
            await first_release.wait()
            raise RuntimeError("initial snapshot failed")
        second_started.set()
        await second_release.wait()

    monkeypatch.setattr(sensor, "_recompute", snapshot)
    adding = asyncio.create_task(sensor.async_added_to_hass())
    try:
        await asyncio.wait_for(first_started.wait(), 1)
        hass.states.async_set(role.entity_id, "Reception")
        await asyncio.sleep(0)
        first_release.set()
        with pytest.raises(RuntimeError, match="initial snapshot failed"):
            await adding
        await asyncio.wait_for(second_started.wait(), 1)
        pending = producer_tasks[-1]
        assert not pending.done()
        sensor.add_to_platform_abort()
        await asyncio.sleep(0)
        assert pending.done()
        assert all(task.done() for task in producer_tasks)
        hass.states.async_set(role.entity_id, "Later state")
        await asyncio.sleep(0)
        assert len(producer_tasks) == 2
    finally:
        first_release.set()
        second_release.set()
        for task in producer_tasks:
            task.cancel()
        await asyncio.gather(adding, *producer_tasks, return_exceptions=True)
        for name in ("_unsub_state", "_unsub_registry"):
            if remove := getattr(sensor, name, None):
                remove()
                setattr(sensor, name, None)
