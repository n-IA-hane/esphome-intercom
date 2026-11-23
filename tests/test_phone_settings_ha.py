"""Native HA action and config persistence for per-phone routing defaults."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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
from custom_components.voip_stack.phone_queue_membership import async_set_phone_memberships_service


pytestmark = pytest.mark.ha


@pytest.fixture
async def phones(hass, monkeypatch):
    from homeassistant.helpers import entity_registry as er

    entry = MockConfigEntry(domain="voip_stack", data={})
    entry.add_to_hass(hass)
    registry = EndpointRegistry()
    entry.runtime_data = VoipStackRuntime(
        transport_config={}, assist_config={}, trunk_config={}, endpoints=registry,
        phones=PhoneAdapterRegistry(hass, registry),
    )
    for name in ("Kitchen", "Bedroom", 'ESP'):
        phone_config._add_phone_subentry(hass, entry, data={
            "endpoint_id": name.lower(), 'username': name.lower(), 'password': 'fixture-phone-password',
            "kind": "sip_account", "name": name,
        }, title=name)
    phone_config.sync_registry_from_entry(hass, entry)
    from custom_components.voip_stack.group_config import reconcile_group_records
    reconcile_group_records(hass, entry)
    er.async_get(hass).async_get_or_create("event", "voip_stack", "voip_stack_call_event")
    for name in ("Kitchen", "Bedroom", 'ESP'):
        registry.update(name.lower(), device_id="device-" + name.lower(),
            entity_ids={"sensor." + name.lower() + "_state"})
    push = AsyncMock()
    monkeypatch.setattr(voip_stack, "_refresh_directory", push)
    await services.async_register_services(hass, {
        "set_phone_settings": voip_stack._handle_set_phone_settings_service,
        "set_phone_memberships": async_set_phone_memberships_service,
    })
    return SimpleNamespace(entry=entry, registry=registry, push=push)


async def settings(hass, device="device-kitchen", **values):
    await hass.services.async_call("voip_stack", "set_phone_settings", {
        "device_id": device, **values,
    }, blocking=True)


@pytest.mark.parametrize("selector", ["Banco@Hotel", "Banco%40Hotel", "office", "204", "device-kitchen", "kitchen"])
async def test_phone_settings_resolves_the_selected_phone(hass, phones, selector):
    from custom_components.voip_stack.sip_account_identity import async_migrate_account_identities
    row = phone_config.phone_subentry_by_endpoint_id(phones.entry, 'kitchen')
    await async_migrate_account_identities(hass, phones.entry,
        account_updates={row.subentry_id: {**row.data, 'name': 'Banco@Hotel', 'username': 'office', 'extension': '204'}})
    if selector == 'kitchen':
        with pytest.raises(ServiceValidationError):
            await settings(hass, device=selector, forward_to='Bedroom')
        return
    await settings(hass, device=selector, forward_to="Bedroom")
    assert phones.registry.require("office").policy.always.target == "bedroom"


async def test_native_device_selector_wins_over_another_phones_name(hass, phones):
    device_id = "a52832282eca4f82a000cfacaca56d0c"
    phones.registry.update("kitchen", name=device_id)
    phones.registry.update("bedroom", device_id=device_id)
    await settings(hass, device=device_id, forward_to="ESP")
    assert phones.registry.require("bedroom").policy.always.target == "esp"
    assert phones.registry.require("kitchen").policy == PhonePolicy()


async def test_legacy_source_reference_rejects_cross_namespace_ambiguity(hass, phones):
    phones.registry.update('bedroom', extension='shared-reference')
    phones.registry.update('kitchen', extension='shared-reference')
    with pytest.raises(ServiceValidationError, match="[Aa]mbiguous"):
        await settings(hass, device='shared-reference', ringing_timeout=20, no_answer_action='hangup')
    assert all(phones.registry.require(key).policy == PhonePolicy() for key in ("kitchen", "bedroom"))
    phones.push.assert_not_awaited()


async def test_forward_destination_keeps_public_alias_over_another_device_id(hass, phones):
    phones.registry.update("bedroom", name="Public destination")
    phones.registry.update("esp", device_id="Public destination")
    await settings(hass, forward_to="Public destination")
    assert phones.registry.require("kitchen").policy.always.target == "bedroom"


async def test_forward_destination_rejects_contact_phone_alias_ambiguity(hass, phones):
    from custom_components.voip_stack.phone_directory import PhoneDirectory
    from custom_components.voip_stack.roster import RosterEntry

    phones.entry.runtime_data.directory = PhoneDirectory([
        RosterEntry("external", name="External", number="Bedroom"),
    ])
    with pytest.raises(ServiceValidationError, match="[Aa]mbiguous"):
        await settings(hass, forward_to="Bedroom")
    assert phones.registry.require("kitchen").policy == PhonePolicy()
    phones.push.assert_not_awaited()


async def test_duplicate_source_extension_leaves_both_phones_unchanged(hass, phones):
    for endpoint_id in ("kitchen", "bedroom"):
        phones.registry.update(endpoint_id, extension="204")
    with pytest.raises(ServiceValidationError, match="[Aa]mbiguous"):
        await settings(hass, device="204", ringing_timeout=20)
    assert all(phones.registry.require(key).policy == PhonePolicy() for key in ("kitchen", "bedroom"))
    phones.push.assert_not_awaited()


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
    row = phone_config.phone_subentry_by_endpoint_id(phones.entry, 'esp')
    saved = phone_config.persisted_phone_policy(row.data)
    assert saved.unavailable.target == "bedroom"
    assert phones.registry.require("esp").policy == saved
    assert set(phones.entry.subentries) == before
    await settings(hass, "device-esp", unavailable_to="")
    assert phone_config.persisted_phone_policy(row.data) == PhonePolicy()


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
        "endpoint_id": "old-desk", "kind": "sip_account", "name": "Old desk",
        "username": "old-desk", "offline_policy": "forward",
        "offline_forward_target": "Bedroom",
    }, title="Old desk")
    phone_config.sync_registry_from_entry(hass, phones.entry)
    phones.registry.update("old-desk", device_id="device-old-desk")
    assert phones.registry.require("old-desk").policy.unavailable.target == "Bedroom"
    await settings(hass, "device-old-desk", unavailable_to="")
    stored = phone_config.phone_subentry_by_endpoint_id(phones.entry, "old-desk")
    reloaded = phone_config.endpoint_from_subentry(phones.entry, stored)
    assert reloaded.policy.unavailable is None
    assert reloaded.offline_policy.value == "unavailable"
    assert not reloaded.offline_forward_target


@pytest.mark.parametrize("selector", ["Kitchen", "device-kitchen"])
async def test_read_only_user_cannot_change_phone_routing(hass, phones, hass_read_only_user, selector):
    from homeassistant.core import Context
    from homeassistant.exceptions import Unauthorized

    with pytest.raises(Unauthorized):
        await hass.services.async_call("voip_stack", "set_phone_settings", {
            "device_id": selector, "forward_to": "Bedroom",
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


def add_paging(hass, phones, identity, members, **extra):
    from homeassistant.config_entries import ConfigSubentry
    from custom_components.voip_stack.paging_config import normalize_paging_group

    data = {**normalize_paging_group({"name": identity, "extension": "402",
        "members": members, "answer_timeout": 7, "timeout": 41}, identity=identity), **extra}
    record = ConfigSubentry(subentry_type="paging", title=identity,
        unique_id="paging:" + identity, data=data)
    hass.config_entries.async_add_subentry(phones.entry, record)
    return record


async def paging_memberships(hass, selected, *, phone="device-kitchen", context=None):
    await hass.services.async_call("voip_stack", "set_phone_memberships", {
        "phone": phone, "field": "paging_groups", "value": selected,
    }, blocking=True, context=context)


async def test_paging_membership_updates_canonical_groups_and_one_projection(hass, phones, hass_admin_user):
    from homeassistant.core import Context
    from custom_components.voip_stack.paging_config import paging_group_entries

    first = add_paging(hass, phones, "first", ["bedroom", "kitchen"], retained="setting")
    second = add_paging(hass, phones, "second", ["bedroom"])
    solo = add_paging(hass, phones, "solo", ["kitchen"])
    originals = {record.subentry_id: dict(record.data) for record in (first, second, solo)}
    observer = SimpleNamespace(refresh=Mock())
    phones.entry.runtime_data.sip = SimpleNamespace(component=lambda name: observer if name == "phone_state" else None)
    admin = Context(user_id=hass_admin_user.id)

    await paging_memberships(hass, ["first", "second"], context=admin)
    assert first.data["members"] == ["bedroom", "kitchen"]
    assert second.data["members"] == ["bedroom", "kitchen"]
    assert solo.data["members"] == []
    for record in (first, second, solo):
        assert {key: value for key, value in record.data.items() if key != "members"} == {
            key: value for key, value in originals[record.subentry_id].items() if key != "members"}
    assert not next(row for row in paging_group_entries(phones.entry) if row.id == "paging:solo").enabled
    observer.refresh.assert_called_once_with()
    phones.push.assert_not_awaited()  # Native entry listener owns the phonebook refresh.
    assert "paging_groups" not in phone_config.phone_subentry_by_endpoint_id(phones.entry, "kitchen").data

    await paging_memberships(hass, ["first", "second"], context=admin)
    observer.refresh.assert_called_once_with()  # Idempotent selection.
    await paging_memberships(hass, [], context=admin)
    assert first.data["members"] == second.data["members"] == ["bedroom"]
    assert observer.refresh.call_count == 2
    # An offline ESP uses the same server membership owner, without another phone record.
    previous = set(phones.entry.subentries)
    await paging_memberships(hass, ["solo"], phone="device-esp", context=admin)
    assert solo.data["members"] == ["esp"]
    assert set(phones.entry.subentries) == previous


async def test_paging_membership_rejects_whole_batch_before_mutation(hass, phones):
    first = add_paging(hass, phones, "first", ["kitchen"])
    full = add_paging(hass, phones, "full", [f"phone:{index}" for index in range(16)])
    with pytest.raises(ServiceValidationError, match="existing paging groups"):
        await paging_memberships(hass, ["missing"])
    with pytest.raises(ServiceValidationError, match="sixteen"):
        await paging_memberships(hass, ["full"])
    assert first.data["members"] == ["kitchen"]
    assert len(full.data["members"]) == 16
    phones.push.assert_not_awaited()


async def test_paging_membership_requires_admin_even_for_own_phone(hass, phones, hass_admin_user, hass_read_only_user):
    from homeassistant.auth.const import GROUP_ID_USER
    from homeassistant.core import Context
    from homeassistant.exceptions import Unauthorized

    hass_user = await hass.auth.async_create_user("Phone user", group_ids=[GROUP_ID_USER])
    assert not hass_user.is_admin
    record = add_paging(hass, phones, "group", ["bedroom"])
    await hass.services.async_call("voip_stack", "set_phone_memberships", {
        "phone": "device-kitchen", "field": "ring_group", "value": "Existing control",
    }, blocking=True, context=Context(user_id=hass_user.id))
    for user in (hass_user, hass_read_only_user):
        with pytest.raises(Unauthorized):
            await paging_memberships(hass, ["group"], context=Context(user_id=user.id))
    assert record.data["members"] == ["bedroom"]
    # Existing internal HA automations retain the native administrative service contract.
    await paging_memberships(hass, ["group"])
    assert record.data["members"] == ["bedroom", "kitchen"]


async def test_paging_memberships_are_projected_from_canonical_groups(hass, phones):
    from custom_components.voip_stack.phone_state import phone_states, phone_state_dto
    from custom_components.voip_stack.websocket_api import _ha_softphone_state

    first = add_paging(hass, phones, "first", ["kitchen", "esp"])
    add_paging(hass, phones, "second", ["kitchen"])
    states = phone_states(hass, dialogs={})
    assert states["kitchen"]["paging_groups"] == ["first", "second"]
    assert states["esp"]["paging_groups"] == ["first"]
    assert states["bedroom"]["paging_groups"] == []
    dto = phone_state_dto(states["esp"], peer_readable=lambda _: True)
    assert dto["paging_groups"] == ["first"]
    dto["paging_groups"].clear()
    assert first.data["members"] == ["kitchen", "esp"]
    assert _ha_softphone_state(hass, "kitchen")["paging_groups"] == ["first", "second"]
    await paging_memberships(hass, [])
    assert _ha_softphone_state(hass, "kitchen")["paging_groups"] == []


async def test_paging_batch_uses_one_native_directory_refresh(hass, phones, monkeypatch):
    from custom_components.voip_stack import config_entry_runtime as updates
    from custom_components.voip_stack import trunk_runtime, phone_services, mailbox, physical_phone_runtime
    from custom_components.voip_stack.group_config import group_records, membership_signature
    from custom_components.voip_stack.paging_config import paging_group_configs

    add_paging(hass, phones, "first", ["kitchen"])
    add_paging(hass, phones, "second", ["bedroom"])
    runtime = phones.entry.runtime_data
    runtime.entry_runtime_signature = updates.entry_runtime_signature(phones.entry)
    runtime.entry_phone_signature = updates.entry_phone_signature(phones.entry)
    runtime.entry_contacts_signature = ()
    runtime.entry_group_records = group_records(phones.entry)
    runtime.entry_paging_records = paging_group_configs(phones.entry)
    runtime.entry_membership_signature = membership_signature(hass, phones.entry)
    refresh = AsyncMock()
    monkeypatch.setattr(updates, "async_refresh_directory", refresh)
    for module, name in ((trunk_runtime, "async_reconcile_trunks"),
                         (phone_services, "async_reconcile_phone_services"),
                         (mailbox, "async_refresh_mailbox_state"),
                         (physical_phone_runtime, "async_reconcile_physical_phone_runtime")):
        monkeypatch.setattr(module, name, AsyncMock())
    remove = phones.entry.add_update_listener(updates.async_config_entry_updated)
    try:
        await paging_memberships(hass, ["second"])
        await hass.async_block_till_done()
        refresh.assert_awaited_once_with(hass)
        assert runtime.entry_paging_records == paging_group_configs(phones.entry)
    finally:
        remove()


async def test_existing_phone_observer_emits_one_paging_delta_for_mirror(hass, phones):
    from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
    from custom_components.voip_stack.phone_state import phone_state_observer

    add_paging(hass, phones, "first", ["bedroom"])
    calls = SipEndpointRuntime(allow_dark_sessions=True)
    calls.activate()
    calls.bind_endpoint_registry(phones.registry)
    phones.entry.runtime_data.sip = calls
    seen = []
    observer = phone_state_observer(hass)
    unsubscribe = observer.subscribe(seen.append, kind="projection")
    try:
        await paging_memberships(hass, ["first"], phone="device-esp")
        assert len(seen) == 1
        assert set(seen[0].phones) == {"esp"}
        assert seen[0].phones["esp"]["paging_groups"] == ["first"]
        await paging_memberships(hass, ["first"], phone="device-esp")
        assert len(seen) == 1
    finally:
        unsubscribe()
        await calls.shutdown()
