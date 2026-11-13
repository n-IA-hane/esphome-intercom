"""Lossless account upgrades and server-owned configured HA reachability."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .test_managed_phones_ha import custom_integration_path as custom_integration_path, phone_lab as phone_lab

pytestmark = pytest.mark.ha


def add_phone(hass, entry, data):
    sub = ConfigSubentry(subentry_type="phone", title=data["name"],
        unique_id="phone:" + data["endpoint_id"], data=deepcopy(data))
    hass.config_entries.async_add_subentry(entry, sub)
    return sub


async def test_v7_upgrade_preserves_every_id_field_preference_and_existing_credentials(hass):
    from custom_components.voip_stack import async_migrate_entry
    entry = MockConfigEntry(domain="voip_stack", version=6, minor_version=4,
        data={"initial_phone_created": True, "preferred_phone_device_id": "placeholder", "unrelated": {"keep": True}},
        options={"unrelated_option": "preserved"})
    entry.add_to_hass(hass)
    before = {"endpoint_id": "default", "kind": "browser", "name": "Casa", "extension": "221",
        "enabled": True, "dnd": True, "auto_answer": True, "send_video": True, "video_enabled": True,
        "ring_group": "Reception", "queues": ["queue-stable"], "conference_group": "Casa CG", "conference_ring": True,
        "call_policy": {"ringing_timeout": 25, "no_answer": {"kind": "voicemail", "target": "default"},
                        "outbound_trunk_id": "trunk-stable"},
        "voicemail": {"enabled": True, "language": "it"},
        "ha_bindings": [{"source": "popup", "reference": "existing-popup"}],
        "future_unknown": {"nested": [1, "keep"]}}
    legacy = add_phone(hass, entry, before)
    opaque = add_phone(hass, entry, {**before, "endpoint_id": "browser:opaque", "name": "Test", "extension": "222",
        "username": "existing-browser-user", "password": "fixture-existing-browser-password"})
    existing = add_phone(hass, entry, {"endpoint_id": "sip:480", "kind": "sip_account", "name": "Desk", "extension": "480",
        "username": "ha_default", "password": "fixture-existing-sip-password", "enabled": True})
    sip_before = dict(existing.data)
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(config_entry_id=entry.entry_id, config_subentry_id=legacy.subentry_id,
        identifiers={("voip_stack", "phone_endpoint:default")}, name="User device name")
    entity = er.async_get(hass).async_get_or_create("sensor", "voip_stack", "phone_endpoint_default_call_state",
        config_entry=entry, config_subentry_id=legacy.subentry_id, device_id=device.id)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "preferred_phone_device_id": device.id})
    options_before, parent_before = dict(entry.options), dict(entry.data)
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 7 and entry.minor_version == 0
    for original, sub in [(before, legacy), ({**before, "endpoint_id": "browser:opaque", "name": "Test", "extension": "222",
            "username": "existing-browser-user", "password": "fixture-existing-browser-password"}, opaque)]:
        assert sub.data["kind"] == "sip_account"
        assert all(sub.data[key] == value for key, value in original.items() if key not in {"kind", "ha_bindings"})
        assert sub.data.get("username") and len(sub.data.get("password", "")) >= 20
        assert {"source": "migration", "reference": "legacy_ha_phone:" + sub.data["endpoint_id"]} in sub.data["ha_bindings"]
        assert original["ha_bindings"][0] in sub.data["ha_bindings"]
    assert legacy.data["username"] != existing.data["username"]
    assert dict(existing.data) == sip_before
    assert dict(entry.data) == parent_before and dict(entry.options) == options_before
    assert devices.async_get(device.id).identifiers == device.identifiers
    assert er.async_get(hass).async_get(entity.entity_id).id == entity.id
    after = {sub.subentry_id: dict(sub.data) for sub in entry.subentries.values()}
    assert await async_migrate_entry(hass, entry)
    assert all(dict(entry.subentries[key].data) == value for key, value in after.items())


async def test_v7_namespace_conflict_does_not_partially_convert_accounts(hass):
    from custom_components.voip_stack import async_migrate_entry
    entry = MockConfigEntry(domain="voip_stack", version=6, minor_version=4, data={"initial_phone_created": True})
    entry.add_to_hass(hass)
    first = add_phone(hass, entry, {"endpoint_id": "default", "kind": "browser", "name": "Casa", "extension": "221"})
    second = add_phone(hass, entry, {"endpoint_id": "browser:other", "kind": "browser", "name": "Other", "extension": "222",
        "username": "taken"})
    add_phone(hass, entry, {"endpoint_id": "sip:existing", "kind": "sip_account", "name": "Desk", "extension": "480",
        "username": "taken", "password": "fixture-existing-password"})
    before = [deepcopy(dict(first.data)), deepcopy(dict(second.data))]
    assert await async_migrate_entry(hass, entry) is False
    assert [dict(first.data), dict(second.data)] == before
    assert entry.version == 6


@pytest.mark.parametrize("registered,bound,enabled,expected", [
    (False, False, True, "offline"), (True, False, True, "available"),
    (False, True, True, "available"), (True, True, True, "available"),
    (True, True, False, "unavailable")])
async def test_account_availability_uses_current_contacts_or_saved_binding(phone_lab, monkeypatch, registered, bound, enabled, expected):
    from custom_components.voip_stack.account_runtime import account_has_ha_binding, recompute_account_availability
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind, EndpointAvailability
    lab = phone_lab
    add_phone(lab.hass, lab.entry, {"endpoint_id": "sip:stable", "kind": "sip_account", "name": "Phone",
        "username": "phone", "password": "fixture-account-password", "enabled": enabled})
    endpoint = PhoneEndpoint(endpoint_id="sip:stable", name="Phone", kind=EndpointKind.SIP_ACCOUNT, username="phone",
        availability=EndpointAvailability.AVAILABLE, device_id="kept-device", entity_ids=frozenset({"sensor.kept"}))
    lab.runtime.endpoints.register(endpoint)
    lab.runtime.endpoints.claim_call(endpoint.endpoint_id, "kept-call")
    lab.runtime.ha_bindings = SimpleNamespace(has_binding=lambda identity: bound and identity == endpoint.endpoint_id)
    monkeypatch.setattr(lab.registrar, "registered_contacts", Mock(return_value=[object()] if registered else []))
    assert account_has_ha_binding(lab.hass, endpoint.endpoint_id) is bound
    recompute_account_availability(lab.hass, endpoint.endpoint_id)
    current = lab.runtime.endpoints.get(endpoint.endpoint_id)
    assert current.availability.value == expected
    assert current.active_call_id == "kept-call" and current.device_id == "kept-device"
    assert current.entity_ids == frozenset({"sensor.kept"})


def test_ha_adapter_is_ephemeral_and_does_not_change_canonical_kind():
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind, OfflinePolicy, ha_client_endpoint
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    endpoint = PhoneEndpoint(endpoint_id="stable", name="Phone", kind=EndpointKind.SIP_ACCOUNT,
        username="phone", offline_policy=OfflinePolicy.FORWARD, offline_forward_target="backup")
    registry = EndpointRegistry()
    registry.register(endpoint)
    assert endpoint.supports_ha_client and not endpoint.kind.is_softphone
    view = ha_client_endpoint(endpoint)
    assert view.kind is EndpointKind.BROWSER and view.endpoint_id == endpoint.endpoint_id
    assert view.policy == endpoint.policy and view.availability == endpoint.availability
    assert registry.get(endpoint.endpoint_id) is endpoint and len(registry.endpoints) == 1
    assert ha_client_endpoint(replace(endpoint, kind=EndpointKind.ESPHOME)) is None


@pytest.mark.parametrize("disabled,expected", [(False, "available"), (True, "unavailable")])
async def test_registered_account_without_ui_record_keeps_registrar_authority(phone_lab, monkeypatch, disabled, expected):
    from custom_components.voip_stack.account_runtime import recompute_account_availability
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind, EndpointAvailability
    from custom_components.voip_stack.sip_registrar import SipAccount
    lab = phone_lab
    lab.registrar.update_accounts([SipAccount("registered", "Registered", "fixture-registration-password")])
    endpoint = PhoneEndpoint(endpoint_id="sip:registered", name="Registered", kind=EndpointKind.SIP_ACCOUNT,
        username="registered", availability=EndpointAvailability.UNAVAILABLE if disabled else EndpointAvailability.OFFLINE)
    lab.runtime.endpoints.register(endpoint)
    monkeypatch.setattr(lab.registrar, "registered_contacts", Mock(return_value=[object()]))
    recompute_account_availability(lab.hass, endpoint.endpoint_id)
    assert lab.runtime.endpoints.get(endpoint.endpoint_id).availability.value == expected


async def test_native_account_editor_has_one_kind_and_preserves_browser_preferences(phone_lab):
    from homeassistant.config_entries import SOURCE_USER
    from .support.ha_frontend import prepare_frontend_dependency
    lab = phone_lab
    lab.hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    prepare_frontend_dependency(lab.hass)
    result = await lab.hass.config_entries.subentries.async_init(
        (lab.entry.entry_id, "phone"), context={"source": SOURCE_USER})
    assert result["type"] == "form" and result["step_id"] == "sip_account"
    fields = {key.schema for key in result["data_schema"].schema}
    assert {"username", "password", "auto_answer", "send_video", "physical_bindings"} <= fields
    assert "kind" not in fields
    created = await lab.hass.config_entries.subentries.async_configure(result["flow_id"], {
        "name": "New account", "username": "new-account", "password": "fixture-long-account-password",
        "enabled": True, "dnd": False, "auto_answer": True, "send_video": True, "physical_bindings": []})
    assert created["type"] == "create_entry"
    account = next(sub for sub in lab.entry.subentries.values() if sub.data.get("username") == "new-account")
    assert account.data["kind"] == "sip_account" and account.data["auto_answer"] and account.data["send_video"]
    assert "physical_bindings" not in account.data


async def test_phone_dispatch_uses_ha_view_but_keeps_real_sip_hangup(phone_lab):
    from homeassistant.core import ServiceCall
    from custom_components.voip_stack.phone_control import PhoneOperation
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    lab = phone_lab
    endpoint = PhoneEndpoint(endpoint_id="sip:account", name="Account", kind=EndpointKind.SIP_ACCOUNT,
        username="account", device_id="account-device")
    lab.runtime.endpoints.register(endpoint)
    call = ServiceCall(lab.hass, "voip_stack", "call", {"device_id": "account-device"})
    outgoing = await lab.runtime.phones.resolve_source(call, PhoneOperation.ORIGINATE)
    assert outgoing.kind is EndpointKind.BROWSER and outgoing.endpoint_id == endpoint.endpoint_id
    assert lab.runtime.endpoints.get(endpoint.endpoint_id).kind is EndpointKind.SIP_ACCOUNT
    physical = ServiceCall(lab.hass, "voip_stack", "hangup", {"device_id": "account-device", "media_client_id": "observer-client-id"})
    handle = await lab.runtime.phones.resolve_source(physical, PhoneOperation.HANGUP)
    assert handle.kind is EndpointKind.SIP_ACCOUNT


async def test_native_reconfigure_removes_only_inherited_ha_reference(phone_lab):
    from homeassistant.config_entries import SOURCE_RECONFIGURE
    from .support.ha_frontend import prepare_frontend_dependency
    lab = phone_lab
    lab.hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    prepare_frontend_dependency(lab.hass)
    data = {"endpoint_id": "default", "kind": "sip_account", "name": "Casa", "extension": "221",
        "username": "casa", "password": "fixture-account-password", "auto_answer": True, "send_video": True,
        "ha_bindings": [{"source": "migration", "reference": "legacy_ha_phone:default"}],
        "future_unknown": {"preserved": True}}
    sub = add_phone(lab.hass, lab.entry, data)
    form = await lab.hass.config_entries.subentries.async_init((lab.entry.entry_id, "phone"),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": sub.subentry_id})
    assert "migration_bindings" in {key.schema for key in form["data_schema"].schema}
    result = await lab.hass.config_entries.subentries.async_configure(form["flow_id"], {
        "name": "Casa renamed", "username": "casa", "password": data["password"], "migration_bindings": []})
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    assert sub.data["ha_bindings"] == []
    assert sub.data["auto_answer"] and sub.data["send_video"] and sub.data["future_unknown"] == data["future_unknown"]
    assert sub.data["endpoint_id"] == "default"


async def test_native_account_creation_commits_physical_inverse_without_parallel_list(phone_lab):
    from homeassistant.config_entries import SOURCE_USER
    from unittest.mock import AsyncMock
    from custom_components.voip_stack.physical_phone import normalize_physical_phone
    from custom_components.voip_stack.physical_phone_provisioning import PhysicalPhoneProvisioner
    from .support.ha_frontend import prepare_frontend_dependency
    lab = phone_lab
    lab.hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    prepare_frontend_dependency(lab.hass)
    hardware = normalize_physical_phone({"name": "Unassigned", "mac": "001122334455",
        "profile_id": "fanvil/x3u-pro", "lines": [], "keys": []})
    sub = ConfigSubentry(subentry_type="physical_phone", title=hardware["name"], data=hardware,
        unique_id="physical_phone:" + hardware["phone_id"])
    lab.hass.config_entries.async_add_subentry(lab.entry, sub)
    manager = PhysicalPhoneProvisioner(lab.hass, lab.entry)
    lab.runtime.physical_phones = manager
    manager.catalog.async_profile = AsyncMock(return_value=({"capabilities": {
        "max_lines": 2, "key_slots": [], "functions": [], "languages": {}}}, {}))
    form = await lab.hass.config_entries.subentries.async_init((lab.entry.entry_id, "phone"), context={"source": SOURCE_USER})
    result = await lab.hass.config_entries.subentries.async_configure(form["flow_id"], {
        "name": "Bound account", "username": "bound-account", "password": "fixture-bound-account-password",
        "physical_bindings": [{"phone_id": hardware["phone_id"], "slot": "1", "transport": "udp"}]})
    assert result["type"] == "abort" and result["reason"] == "phone_configured"
    accounts = [row for row in lab.entry.subentries.values() if row.subentry_type == "phone"]
    assert len(accounts) == 1 and accounts[0].data["kind"] == "sip_account"
    assert "physical_bindings" not in accounts[0].data
    assert sub.data["lines"] == [{"slot": "1", "endpoint_id": accounts[0].data["endpoint_id"], "transport": "udp"}]
    assert sub.data["provisioning_token"] == hardware["provisioning_token"]


async def test_interrupted_v7_registry_writes_retry_without_namespace_or_credential_loss(hass, monkeypatch):
    from custom_components.voip_stack import async_migrate_entry
    entry = MockConfigEntry(domain="voip_stack", version=6, minor_version=4,
        data={"initial_phone_created": True, "preferred_phone_device_id": "kept-device"})
    entry.add_to_hass(hass)
    first = add_phone(hass, entry, {"endpoint_id": "default", "kind": "browser", "name": "Casa", "extension": "221",
        "auto_answer": True, "future_unknown": {"keep": 1}})
    second = add_phone(hass, entry, {"endpoint_id": "browser:second", "kind": "browser", "name": "Test", "extension": "222",
        "send_video": True, "future_unknown": {"keep": 2}})
    original = hass.config_entries.async_update_subentry
    writes = 0

    def interrupted(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise RuntimeError("fixture interrupted between account writes")
        return original(*args, **kwargs)

    monkeypatch.setattr(hass.config_entries, "async_update_subentry", interrupted)
    with pytest.raises(RuntimeError, match="interrupted between account writes"):
        await async_migrate_entry(hass, entry)
    assert entry.version == 6 and first.data["kind"] == "sip_account" and second.data["kind"] == "browser"
    first_saved = deepcopy(dict(first.data))
    monkeypatch.setattr(hass.config_entries, "async_update_subentry", original)
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 7 and dict(first.data) == first_saved
    assert second.data["kind"] == "sip_account" and second.data["send_video"]
    assert second.data["future_unknown"] == {"keep": 2}
    assert first.data["username"] != second.data["username"]
    assert entry.data["preferred_phone_device_id"] == "kept-device"


async def test_entry_update_removing_last_migration_ref_goes_offline_without_manual_reindex(phone_lab):
    from custom_components.voip_stack.account_runtime import recompute_account_availability
    from custom_components.voip_stack.config_entry_runtime import (
        async_config_entry_updated, entry_phone_signature, entry_runtime_signature)
    from custom_components.voip_stack.ha_bindings import HaCardBindings
    from custom_components.voip_stack.phone_config import endpoint_from_subentry
    from custom_components.voip_stack.sip_registrar import SipAccount
    lab = phone_lab
    data = {"endpoint_id": "subject", "kind": "sip_account", "name": "Subject", "username": "subject",
        "password": "fixture-subject-password", "enabled": True,
        "ha_bindings": [{"source": "migration", "reference": "legacy_ha_phone:subject"}]}
    sub = add_phone(lab.hass, lab.entry, data)
    lab.runtime.endpoints.register(endpoint_from_subentry(lab.entry, sub))
    lab.registrar.update_accounts([SipAccount("subject", "Subject", data["password"])])
    owner = HaCardBindings(lab.hass, lab.entry, lab.runtime,
        lambda ids: recompute_account_availability(lab.hass, ids))
    lab.runtime.ha_bindings = owner
    await owner.async_start()
    recompute_account_availability(lab.hass)
    assert owner.has_binding("subject") and lab.runtime.endpoints.get("subject").availability.value == "available"
    lab.runtime.entry_phone_signature = entry_phone_signature(lab.entry)
    lab.runtime.entry_runtime_signature = entry_runtime_signature(lab.entry)
    lab.runtime.entry_contacts_signature = ()
    before = lab.runtime.endpoints.get("subject")
    try:
        lab.hass.config_entries.async_update_subentry(lab.entry, sub, data={**data, "ha_bindings": []})
        assert replace(endpoint_from_subentry(lab.entry, sub), availability=before.availability) == before
        await async_config_entry_updated(lab.hass, lab.entry)
        await lab.hass.async_block_till_done()
        assert not owner.has_binding("subject")
        assert lab.runtime.endpoints.get("subject").availability.value == "offline"
    finally:
        await owner.async_close()
        lab.runtime.ha_bindings = None
