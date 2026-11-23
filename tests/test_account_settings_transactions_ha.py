"""Canonical account writes remain atomic while HA projections lag behind."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigSubentry, SOURCE_USER
from pytest_homeassistant.common import MockConfigEntry

from .test_managed_phones_ha import (
    custom_integration_path as custom_integration_path,
    phone_lab as phone_lab,
)

pytestmark = pytest.mark.ha


async def account(lab, identity, username, extension, **settings):
    from custom_components.voip_stack.phone_config import endpoint_from_subentry
    from custom_components.voip_stack.websocket_api import _async_load_ha_softphone_store

    data = {"endpoint_id": identity, "kind": "sip_account", "name": username.title(),
            "username": username, "extension": extension,
            "password": "fixture-account-password", "enabled": True, **settings}
    sub = ConfigSubentry(subentry_type="phone", title=data["name"],
                         unique_id="phone:" + identity, data=data)
    lab.hass.config_entries.async_add_subentry(lab.entry, sub)
    lab.runtime.endpoints.register(endpoint_from_subentry(lab.entry, sub))
    await _async_load_ha_softphone_store(lab.hass, lab.entry, endpoint_id=identity,
                                        endpoint_data=dict(sub.data))
    return sub


def isolate_listener_integrations(monkeypatch):
    """Keep this configuration test away from network/provisioning work."""
    from custom_components.voip_stack import (
        config_entry_runtime, mailbox, phone_services, physical_phone_runtime,
    )

    monkeypatch.setattr(config_entry_runtime, "async_refresh_directory", AsyncMock())
    monkeypatch.setattr(mailbox, "async_refresh_mailbox_state", AsyncMock())
    monkeypatch.setattr(phone_services, "async_reconcile_phone_services", AsyncMock())
    monkeypatch.setattr(physical_phone_runtime, "async_reconcile_physical_phone_runtime", AsyncMock())


def capture_applied_signatures(lab):
    from custom_components.voip_stack.phone_config import phone_endpoint_id, phone_subentry_data
    from custom_components.voip_stack.config_entry_runtime import (
        entry_phone_signature, entry_runtime_signature,
    )

    lab.runtime.entry_phone_signature = entry_phone_signature(lab.entry)
    lab.runtime.entry_phone_records = {
        phone_endpoint_id(sub): phone_subentry_data(sub) for sub in lab.entry.subentries.values()
        if sub.subentry_type == "phone"
    }
    lab.runtime.entry_runtime_signature = entry_runtime_signature(lab.entry)
    lab.runtime.entry_contacts_signature = ()


@pytest.mark.parametrize("field", ["extension", "ring_group", "conference_group"])
async def test_rejected_text_edit_keeps_saved_runtime_and_client_state(phone_lab, field):
    from custom_components.voip_stack.phone_config import endpoint_from_subentry
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    from custom_components.voip_stack.text import PhoneEndpointSettingText, _SETTINGS

    lab = phone_lab
    first = await account(lab, "sip:a", "201", "201")
    await account(lab, "sip:b", "202", "202")
    before = deepcopy(dict(first.data))
    projection = deepcopy(lab.runtime.softphones["sip:a"])
    endpoint = lab.runtime.endpoints.require("sip:a")
    entity = PhoneEndpointSettingText(lab.hass, endpoint, lab.runtime.endpoints,
                                      setting=next(item for item in _SETTINGS if item.key == field))
    entity.hass = lab.hass
    with pytest.raises(ValueError):
        await entity.async_set_value("202")
    assert dict(first.data) == before
    assert lab.runtime.endpoints.require("sip:a") == endpoint
    assert lab.runtime.softphones["sip:a"] == projection
    rebuilt = EndpointRegistry()
    for sub in lab.entry.subentries.values():
        rebuilt.register(endpoint_from_subentry(lab.entry, sub))
    assert rebuilt.resolve("201").endpoint_id == "sip:a"
    assert rebuilt.resolve("202").endpoint_id == "sip:b"


@pytest.mark.parametrize("preference", ["auto_answer", "send_video", "dnd"])
async def test_preference_patch_preserves_native_edit_while_listener_waits(phone_lab, monkeypatch, preference):
    from custom_components.voip_stack.config_entry_runtime import async_config_entry_updated
    from custom_components.voip_stack.switch import (
        PhoneEndpointAutoAnswerSwitch, PhoneEndpointDndSwitch, PhoneEndpointSendVideoSwitch,
    )

    lab = phone_lab
    sub = await account(lab, "sip:a", "201", "201", send_video=True, dnd=True)
    capture_applied_signatures(lab)
    isolate_listener_integrations(monkeypatch)
    await lab.runtime.trunk_update_lock.acquire()
    listener = None
    try:
        lab.hass.config_entries.async_update_subentry(lab.entry, sub,
            data={**sub.data, "extension": "301", "send_video": False, "dnd": False})
        listener = lab.hass.async_create_task(async_config_entry_updated(lab.hass, lab.entry))
        for _ in range(4):
            await asyncio.sleep(0)
        assert not listener.done()
        assert lab.runtime.softphones["sip:a"]["extension"] == "201"
        kind = {"auto_answer": PhoneEndpointAutoAnswerSwitch,
                "send_video": PhoneEndpointSendVideoSwitch, "dnd": PhoneEndpointDndSwitch}[preference]
        switch = kind(lab.hass, lab.runtime.endpoints.require("sip:a"), lab.runtime.endpoints)
        switch.hass = lab.hass
        await switch.async_turn_on()
        assert sub.data["extension"] == "301"
        assert sub.data["send_video"] is (preference == "send_video")
        assert sub.data["dnd"] is (preference == "dnd")
        assert bool(sub.data.get("auto_answer")) is (preference == "auto_answer")
        assert sub.data["password"] == "fixture-account-password"
    finally:
        lab.runtime.trunk_update_lock.release()
        if listener is not None:
            await listener
    assert lab.runtime.endpoints.require("sip:a").extension == "301"


async def test_competing_account_alias_edits_commit_only_one_candidate(phone_lab):
    from custom_components.voip_stack.phone_config import phone_endpoint_id
    from custom_components.voip_stack.websocket_api import async_set_ha_softphone_settings

    lab = phone_lab
    first = await account(lab, "sip:a", "201", "201")
    second = await account(lab, "sip:b", "202", "202")
    outcomes = await asyncio.gather(*(
        async_set_ha_softphone_settings(lab.hass, endpoint_id=identity, extension="301")
        for identity in ("sip:a", "sip:b")), return_exceptions=True)
    assert sum(isinstance(result, ValueError) for result in outcomes) == 1
    assert sorted(sub.data["extension"] for sub in (first, second)).count("301") == 1
    for sub in (first, second):
        assert lab.runtime.endpoints.require(phone_endpoint_id(sub)).extension == sub.data["extension"]


@pytest.mark.parametrize("owner", ["contact", "ring", "queue", "paging", "assist", "esphome", "mailbox"])
async def test_partial_account_edits_share_every_existing_route_namespace(phone_lab, owner):
    from custom_components.voip_stack.phone_config import update_phone_subentry
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint

    lab = phone_lab
    sub = await account(lab, "sip:a", "201", "201")
    target = "301"
    if owner in {"ring", "queue", "paging", "contact"}:
        data = {"name": "Other", "extension": target}
        if owner in {"ring", "queue"}:
            kind, data = "group", {**data, "group_id": "other", "kind": owner}
        elif owner == "paging":
            kind, data = "paging", {**data, "paging_group_id": "other"}
        else:
            kind, data = "contact", {**data, "id": "other"}
        lab.hass.config_entries.async_add_subentry(lab.entry, ConfigSubentry(
            subentry_type=kind, title="Other", unique_id="other:" + owner, data=data))
    elif owner == "assist":
        lab.hass.config_entries.async_update_entry(lab.entry,
            data={**lab.entry.data, "assist_endpoint_enabled": True, "assist_extension": target})
    elif owner == "esphome":
        lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="esp:other", kind="esphome",
            name="Other", extension=target))
    else:
        target = "voicemail.201"
    before = deepcopy(dict(sub.data))
    with pytest.raises(ValueError):
        update_phone_subentry(lab.hass, lab.entry, "sip:a", {"extension": target})
    assert dict(sub.data) == before
    assert lab.runtime.endpoints.require("sip:a").extension == "201"


async def test_existing_ring_group_is_reused_as_membership_but_not_as_account_alias(phone_lab):
    from custom_components.voip_stack.phone_config import update_phone_subentry

    lab = phone_lab
    sub = await account(lab, "sip:a", "201", "201")
    lab.hass.config_entries.async_add_subentry(lab.entry, ConfigSubentry(subentry_type="group",
        title="Staff", unique_id="group:staff", data={"group_id": "staff", "kind": "ring", "name": "Staff"}))
    update_phone_subentry(lab.hass, lab.entry, "sip:a", {"ring_group": "Staff"})
    assert sub.data["ring_group"] == "Staff"
    with pytest.raises(ValueError):
        update_phone_subentry(lab.hass, lab.entry, "sip:a", {"username": "staff"})
    assert sub.data["username"] == "201"


async def test_clearing_conference_membership_clears_ring_without_call_events(phone_lab):
    from custom_components.voip_stack.websocket_api import CALL_EVENT, async_set_ha_softphone_settings

    lab = phone_lab
    sub = await account(lab, "sip:a", "201", "201", conference_group="Staff", conference_ring=True)
    events = []
    unsubscribe = lab.hass.bus.async_listen(CALL_EVENT, lambda event: events.append(event.data))
    try:
        await async_set_ha_softphone_settings(lab.hass, endpoint_id="sip:a", conference_group="")
        await lab.hass.async_block_till_done()
    finally:
        unsubscribe()
    assert sub.data["conference_group"] == "" and sub.data["conference_ring"] is False
    assert lab.runtime.endpoints.require("sip:a").conference_ring is False
    assert events == []


async def test_v7_uses_the_softphone_name_once_and_preserves_its_groups(hass):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.phone_config import phone_subentry_data

    entry = MockConfigEntry(domain="voip_stack", version=6, minor_version=4,
                            data={"initial_phone_created": True})
    entry.add_to_hass(hass)
    data = {"endpoint_id": "default", "kind": "browser", "name": "Casa", "extension": "221",
            "ring_group": "HA Phone Default", "send_video": True}
    sub = ConfigSubentry(subentry_type="phone", title="Casa", unique_id="phone:default", data=data)
    hass.config_entries.async_add_subentry(entry, sub)
    assert await async_migrate_entry(hass, entry)
    assert sub.data["username"] == "Casa"
    assert all(phone_subentry_data(sub)[key] == value for key, value in data.items() if key != "kind")
    saved = deepcopy(dict(sub.data))
    assert await async_migrate_entry(hass, entry)
    assert dict(sub.data) == saved and entry.version == 7


async def test_recreate_waits_for_deleted_accounts_existing_aliases_to_drain(phone_lab):
    from custom_components.voip_stack.phone_config import (
        async_setup_endpoint_registry, sync_registry_from_entry,
    )
    from .support.ha_frontend import prepare_frontend_dependency

    lab = phone_lab
    old = await account(lab, "sip:old", "alice", "201")
    lab.runtime.endpoints = async_setup_endpoint_registry(lab.hass, lab.entry)
    lab.runtime.endpoints.claim_call("sip:old", "existing-call-token")
    lab.hass.config_entries.async_remove_subentry(lab.entry, old.subentry_id)
    sync_registry_from_entry(lab.hass, lab.entry)
    lab.hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    prepare_frontend_dependency(lab.hass)
    form = await lab.hass.config_entries.subentries.async_init(
        (lab.entry.entry_id, "phone"), context={"source": SOURCE_USER})
    values = {"name": "Alice", "username": "alice", "extension": "201",
              "password": "fixture-new-account-password", "enabled": True}
    rejected = await lab.hass.config_entries.subentries.async_configure(form["flow_id"], values)
    assert rejected["type"] == "form" and rejected["errors"]
    assert not lab.entry.subentries
    assert lab.runtime.endpoints.resolve("alice").active_call_id == "existing-call-token"
    lab.runtime.endpoints.release_call("sip:old", "existing-call-token")
    await lab.hass.async_block_till_done()
    created = await lab.hass.config_entries.subentries.async_configure(form["flow_id"], values)
    assert created["type"] == "abort" and created["reason"] == "phone_configured"
    sync_registry_from_entry(lab.hass, lab.entry)
    current = lab.runtime.endpoints.resolve("alice")
    assert current.endpoint_id != "sip:old" and not current.active_call_id


@pytest.mark.parametrize("busy", [False, True])
async def test_native_delete_reserves_aliases_before_parent_listener_reconciles(phone_lab, monkeypatch, busy):
    from custom_components.voip_stack.config_entry_runtime import async_config_entry_updated
    from custom_components.voip_stack.phone_config import async_setup_endpoint_registry
    from .support.ha_frontend import prepare_frontend_dependency

    lab = phone_lab
    old = await account(lab, "sip:old", "alice", "201")
    lab.runtime.endpoints = async_setup_endpoint_registry(lab.hass, lab.entry)
    if busy:
        lab.runtime.endpoints.claim_call("sip:old", "existing-call-token")
    capture_applied_signatures(lab)
    isolate_listener_integrations(monkeypatch)
    entered = asyncio.Event()
    listeners = []

    async def actual_update(hass, entry):
        listeners.append(asyncio.current_task())
        entered.set()
        await async_config_entry_updated(hass, entry)

    unsubscribe = lab.entry.add_update_listener(actual_update)
    await lab.runtime.trunk_update_lock.acquire()
    try:
        lab.hass.config_entries.async_remove_subentry(lab.entry, old.subentry_id)
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(0)
        assert not listeners[0].done()
        assert "sip:old" not in lab.runtime.endpoints.pending_removals
        lab.hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
        prepare_frontend_dependency(lab.hass)
        form = await lab.hass.config_entries.subentries.async_init(
            (lab.entry.entry_id, "phone"), context={"source": SOURCE_USER})
        values = {"name": "Alice", "username": "alice", "extension": "201",
                  "password": "fixture-new-account-password", "enabled": True}
        rejected = await lab.hass.config_entries.subentries.async_configure(form["flow_id"], values)
        assert rejected["type"] == "form" and rejected["errors"]
        assert not lab.entry.subentries
        assert lab.runtime.endpoints.resolve("alice").endpoint_id == "sip:old"
    finally:
        lab.runtime.trunk_update_lock.release()
        await asyncio.gather(*listeners)
        unsubscribe()
    if busy:
        assert lab.runtime.endpoints.require("sip:old").active_call_id == "existing-call-token"
        lab.runtime.endpoints.release_call("sip:old", "existing-call-token")
        await lab.hass.async_block_till_done()
    assert lab.runtime.endpoints.get("sip:old") is None
    created = await lab.hass.config_entries.subentries.async_configure(form["flow_id"], values)
    assert created["type"] == "abort" and created["reason"] == "phone_configured"
    assert len(lab.entry.subentries) == 1


@pytest.mark.parametrize("writer", ["text", "native_flow"])
async def test_alias_handover_waits_for_native_edit_to_reconcile_before_save(phone_lab, monkeypatch, writer):
    from homeassistant.config_entries import SOURCE_RECONFIGURE
    from custom_components.voip_stack.config_entry_runtime import async_config_entry_updated
    from custom_components.voip_stack.phone_config import async_setup_endpoint_registry
    from custom_components.voip_stack.text import PhoneEndpointSettingText, _SETTINGS
    from .support.ha_frontend import prepare_frontend_dependency

    lab = phone_lab
    first = await account(lab, "sip:a", "201", "201", name="First")
    second = await account(lab, "sip:b", "202", "202", name="Second")
    lab.runtime.endpoints = async_setup_endpoint_registry(lab.hass, lab.entry)
    capture_applied_signatures(lab)
    isolate_listener_integrations(monkeypatch)
    entered = asyncio.Event()
    listeners = []

    async def actual_update(hass, entry):
        listeners.append(asyncio.current_task())
        entered.set()
        await async_config_entry_updated(hass, entry)

    entity = PhoneEndpointSettingText(lab.hass, lab.runtime.endpoints.require("sip:a"), lab.runtime.endpoints,
        setting=next(item for item in _SETTINGS if item.key == "extension"))
    entity.hass = lab.hass
    lab.hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    prepare_frontend_dependency(lab.hass)
    form = await lab.hass.config_entries.subentries.async_init((lab.entry.entry_id, "phone"),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": first.subentry_id})
    values = {"name": "First", "username": "201", "extension": "202", "enabled": True}
    unsubscribe = lab.entry.add_update_listener(actual_update)
    await lab.runtime.trunk_update_lock.acquire()
    before = deepcopy(dict(first.data))
    try:
        lab.hass.config_entries.async_update_subentry(lab.entry, second,
            data={**second.data, "username": "203", "extension": "203"})
        await asyncio.wait_for(entered.wait(), 1)
        assert not listeners[0].done()
        assert lab.runtime.endpoints.require("sip:b").username == "202"
        if writer == "text":
            with pytest.raises(ValueError):
                await entity.async_set_value("202")
        else:
            rejected = await lab.hass.config_entries.subentries.async_configure(form["flow_id"], values)
            assert rejected["type"] == "form" and rejected["errors"]
        assert dict(first.data) == before
        assert lab.runtime.endpoints.require("sip:a").extension == "201"
    finally:
        lab.runtime.trunk_update_lock.release()
        await asyncio.gather(*listeners)
        unsubscribe()
    assert lab.runtime.endpoints.require("sip:b").username == "203"
    if writer == "text":
        await entity.async_set_value("202")
    else:
        accepted = await lab.hass.config_entries.subentries.async_configure(form["flow_id"], values)
        assert accepted["type"] == "abort"
    assert first.data["extension"] == "202"
    # The candidate's own live identity remains excluded for ordinary edits.
    await entity.async_set_value("204")
    assert first.data["extension"] == "204"
    assert first.data["password"] == "fixture-account-password"


async def test_failed_registry_sync_does_not_mark_phone_configuration_applied(phone_lab, monkeypatch):
    from custom_components.voip_stack.config_entry_runtime import async_config_entry_updated, entry_phone_signature
    from custom_components.voip_stack.phone_config import async_setup_endpoint_registry, sync_registry_from_entry

    lab = phone_lab
    old = await account(lab, "sip:old", "alice", "201")
    lab.runtime.endpoints = async_setup_endpoint_registry(lab.hass, lab.entry)
    capture_applied_signatures(lab)
    original_signature = lab.runtime.entry_phone_signature
    original_records = deepcopy(lab.runtime.entry_phone_records)
    lab.runtime.endpoints.claim_call("sip:old", "existing-call-token")
    lab.hass.config_entries.async_remove_subentry(lab.entry, old.subentry_id)
    sync_registry_from_entry(lab.hass, lab.entry)
    replacement = ConfigSubentry(subentry_type="phone", title="Alice", unique_id="phone:sip:new",
        data={**old.data, "endpoint_id": "sip:new"})
    lab.hass.config_entries.async_add_subentry(lab.entry, replacement)
    isolate_listener_integrations(monkeypatch)
    with pytest.raises(ValueError):
        await async_config_entry_updated(lab.hass, lab.entry)
    assert lab.runtime.entry_phone_signature == original_signature
    assert lab.runtime.entry_phone_records == original_records
    lab.runtime.endpoints.release_call("sip:old", "existing-call-token")
    await lab.hass.async_block_till_done()
    await async_config_entry_updated(lab.hass, lab.entry)
    assert lab.runtime.entry_phone_signature == entry_phone_signature(lab.entry)
    assert lab.runtime.endpoints.resolve("alice").endpoint_id == "sip:new"


async def test_configured_settings_use_the_canonical_entry_before_projection_bootstrap(phone_lab):
    from custom_components.voip_stack.phone_config import _add_phone_subentry, sync_registry_from_entry
    from custom_components.voip_stack.websocket_api import async_set_ha_softphone_settings

    lab = phone_lab
    sub = _add_phone_subentry(lab.hass, lab.entry, title="Desk", data={"endpoint_id": "sip:desk",
        "kind": "sip_account", "name": "Desk", "username": "desk", "password": "fixture-kept-password"})
    sync_registry_from_entry(lab.hass, lab.entry)
    assert "sip:desk" not in lab.runtime.softphones
    await async_set_ha_softphone_settings(lab.hass, endpoint_id="sip:desk", auto_answer=True)
    assert sub.data["auto_answer"] is True
    assert sub.data["password"] == "fixture-kept-password"
    assert lab.runtime.softphones["sip:desk"]["auto_answer"] is True


async def test_unconfigured_settings_have_a_service_error_and_create_no_hidden_configuration(phone_lab):
    from homeassistant.exceptions import ServiceValidationError
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
    from custom_components.voip_stack.websocket_api import async_set_ha_softphone_settings

    lab = phone_lab
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="sip:retired", name="Retired", kind="sip_account",
                                                username="retired"))
    with pytest.raises(ServiceValidationError, match="configured"):
        await async_set_ha_softphone_settings(lab.hass, endpoint_id="sip:retired", auto_answer=True)
    assert not lab.entry.subentries
    assert "sip:retired" not in lab.runtime.softphones
