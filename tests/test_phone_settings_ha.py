"""Native HA action and config persistence for per-phone routing defaults."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant.common import MockConfigEntry

from custom_components import voip_stack
from custom_components.voip_stack import phone_config, services
from custom_components.voip_stack.endpoint_registry import EndpointRegistry
from custom_components.voip_stack.phone_control import PhoneAdapterRegistry
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
from custom_components.voip_stack.phone_policy import PhonePolicy
from custom_components.voip_stack.runtime_data import VoipStackRuntime


pytestmark = pytest.mark.ha


@pytest.fixture
async def phones(hass, monkeypatch):
    entry = MockConfigEntry(domain="voip_stack", data={})
    entry.add_to_hass(hass)
    registry = EndpointRegistry()
    entry.runtime_data = VoipStackRuntime(
        transport_config={}, assist_config={}, trunk_config={}, endpoints=registry,
        phones=PhoneAdapterRegistry(hass, registry),
    )
    for name in ("Kitchen", "Bedroom"):
        phone_config._add_phone_subentry(hass, entry, data={
            "endpoint_id": name.lower(), "kind": "browser", "name": name,
        }, title=name)
    phone_config.sync_registry_from_entry(hass, entry)
    for name in ("Kitchen", "Bedroom"):
        registry.update(name.lower(), device_id="device-" + name.lower())
    registry.register(PhoneEndpoint(
        endpoint_id="esphome:stable", name="ESP", kind="esphome", device_id="device-esp",
    ))
    push = AsyncMock()
    monkeypatch.setattr(voip_stack, "_refresh_and_push_phonebook", push)
    await services.async_register_services(hass, {
        "set_phone_settings": voip_stack._handle_set_phone_settings_service,
    })
    return SimpleNamespace(entry=entry, registry=registry, push=push)


async def settings(hass, device="device-kitchen", **values):
    await hass.services.async_call("voip_stack", "set_phone_settings", {
        "device_id": device, **values,
    }, blocking=True)


async def test_native_action_persists_defaults_and_keeps_active_call(hass, phones):
    phones.registry.claim_call("kitchen", "already-active")
    await settings(hass, forward_to="Bedroom")
    kitchen = phones.registry.require("kitchen")
    assert kitchen.active_call_id == "already-active"
    assert kitchen.policy.always.target == "bedroom"
    stored = phone_config.phone_subentry_by_endpoint_id(phones.entry, "kitchen")
    loaded = phone_config.endpoint_from_subentry(phones.entry, stored)
    assert loaded.policy == kitchen.policy
    phones.push.assert_awaited_once()


@pytest.mark.parametrize("destination", ["", "Bedroom"])
async def test_no_answer_voicemail_uses_an_enabled_mailbox(hass, phones, destination):
    hass.config_entries.async_update_entry(phones.entry, data={"voicemail": {
        "enabled": True, "tts_entity_id": "tts.test", "language": "en",
    }})
    for name in ("kitchen", "bedroom"):
        phone_config.update_phone_subentry(hass, phones.entry, name, {"voicemail": {"enabled": True}})
    await settings(hass, ringing_timeout=30, no_answer_action="voicemail", no_answer_to=destination)
    policy = phones.registry.require("kitchen").policy
    assert policy.no_answer.kind.value == "voicemail"
    assert policy.no_answer.target == ("bedroom" if destination else "kitchen")
    assert policy.ringing_timeout == 30
    await settings(hass, ringing_timeout=0)
    assert phones.registry.require("kitchen").policy.no_answer is None


async def test_no_answer_voicemail_rejects_unconfigured_service_and_mailbox(hass, phones):
    with pytest.raises(ServiceValidationError, match="Enable voicemail"):
        await settings(hass, ringing_timeout=30, no_answer_action="voicemail")
    hass.config_entries.async_update_entry(phones.entry, data={"voicemail": {
        "enabled": True, "tts_entity_id": "tts.test", "language": "en",
    }})
    with pytest.raises(ServiceValidationError, match="destination phone"):
        await settings(hass, ringing_timeout=30, no_answer_action="voicemail", no_answer_to="Bedroom")
    assert phones.registry.require("kitchen").policy.ringing_timeout is None


async def test_partial_update_preserves_other_rules_and_empty_clears(hass, phones):
    await settings(hass, forward_to="Bedroom", busy_to="Bedroom")
    await settings(hass, forward_to="")
    policy = phones.registry.require("kitchen").policy
    assert policy.always is None
    assert policy.busy.target == "bedroom"


async def test_esp_policies_do_not_create_duplicate_phone_subentries(hass, phones):
    before = set(phones.entry.subentries)
    await settings(hass, "device-esp", unavailable_to="Bedroom")
    saved = phone_config.esphome_phone_policy(phones.entry, "esphome:stable")
    assert saved.unavailable.target == "bedroom"
    assert phones.registry.require("esphome:stable").policy == saved
    assert set(phones.entry.subentries) == before
    await settings(hass, "device-esp", unavailable_to="")
    assert phone_config.esphome_phone_policy(phones.entry, "esphome:stable") == PhonePolicy()


async def test_self_and_chained_loops_are_rejected_before_persistence(hass, phones):
    with pytest.raises(ServiceValidationError, match="itself"):
        await settings(hass, forward_to="Kitchen")
    assert phones.registry.require("kitchen").policy == PhonePolicy()
    await settings(hass, forward_to="Bedroom")
    with pytest.raises(ServiceValidationError, match="loop"):
        await settings(hass, "device-bedroom", forward_to="Kitchen")
    assert phones.registry.require("bedroom").policy == PhonePolicy()


async def test_explicit_phone_selection_is_required(hass, phones):
    with pytest.raises(ServiceValidationError, match="existing VoIP phone"):
        await settings(hass, "unknown-device", forward_to="Bedroom")
    phones.push.assert_not_awaited()


async def test_esp_policy_update_does_not_reload_sip_transport(hass, phones, monkeypatch):
    from custom_components.voip_stack.config_entry_runtime import (
        async_config_entry_updated, entry_runtime_signature, entry_phone_signature,
    )
    runtime = phones.entry.runtime_data
    runtime.entry_runtime_signature = entry_runtime_signature(phones.entry)
    runtime.entry_phone_signature = entry_phone_signature(phones.entry)
    runtime.entry_contacts_signature = ()
    reload_entry = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_reload", reload_entry)
    await settings(hass, "device-esp", forward_to="Bedroom")
    await async_config_entry_updated(hass, phones.entry)
    reload_entry.assert_not_awaited()


async def test_legacy_offline_forward_is_visible_and_can_be_cleared(hass, phones):
    phone_config._add_phone_subentry(hass, phones.entry, data={
        "endpoint_id": "sip:legacy", "kind": "sip_account", "name": "Old desk",
        "username": "old-desk", "offline_policy": "forward",
        "offline_forward_target": "Bedroom",
    }, title="Old desk")
    phone_config.sync_registry_from_entry(hass, phones.entry)
    phones.registry.update("sip:legacy", device_id="device-old-desk")
    assert phones.registry.require("sip:legacy").policy.unavailable.target == "Bedroom"
    await settings(hass, "device-old-desk", unavailable_to="")
    stored = phone_config.phone_subentry_by_endpoint_id(phones.entry, "sip:legacy")
    reloaded = phone_config.endpoint_from_subentry(phones.entry, stored)
    assert reloaded.policy.unavailable is None
    assert reloaded.offline_policy.value == "unavailable"
    assert not reloaded.offline_forward_target


async def test_read_only_user_cannot_change_phone_routing(hass, phones, hass_read_only_user):
    from homeassistant.core import Context
    from homeassistant.exceptions import Unauthorized

    with pytest.raises(Unauthorized):
        await hass.services.async_call("voip_stack", "set_phone_settings", {
            "device_id": "device-kitchen", "forward_to": "Bedroom",
        }, blocking=True, context=Context(user_id=hass_read_only_user.id))
    assert phones.registry.require("kitchen").policy == PhonePolicy()
    phones.push.assert_not_awaited()


async def test_no_answer_requires_explicit_outcome_and_can_be_disabled(hass, phones):
    with pytest.raises(ServiceValidationError, match="set together"):
        await settings(hass, ringing_timeout=25)
    assert phones.registry.require("kitchen").policy.ringing_timeout is None
    await settings(hass, ringing_timeout=25, no_answer_action="forward", no_answer_to="Bedroom")
    policy = phones.registry.require("kitchen").policy
    assert policy.ringing_timeout == 25
    assert policy.no_answer.target == "bedroom"
    await settings(hass, ringing_timeout=0)
    policy = phones.registry.require("kitchen").policy
    assert policy.ringing_timeout is None
    assert policy.no_answer is None


async def test_no_answer_can_explicitly_terminate_without_a_destination(hass, phones):
    await settings(hass, ringing_timeout=25, no_answer_action="hangup")
    assert phones.registry.require("kitchen").policy.no_answer.kind.value == "hangup"
