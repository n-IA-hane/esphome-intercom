"""Native config-subentry identity survives draft edits, migration and rename."""

from copy import deepcopy

import pytest
from homeassistant.config_entries import ConfigSubentry
from pytest_homeassistant.common import MockConfigEntry

from .test_managed_phones_ha import (
    custom_integration_path as custom_integration_path,
    phone_lab as phone_lab,
)

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("unique_id", ["phone:sip:stable", "sip:stable", None])
async def test_native_unique_identity_or_legacy_fallback_owns_the_runtime_id(phone_lab, unique_id):
    from custom_components.voip_stack.phone_config import (
        endpoint_from_subentry, phone_subentry_by_endpoint_id, update_phone_subentry,
    )

    lab = phone_lab
    identity = "sip:stable"
    sub = ConfigSubentry(subentry_type="phone", title="Desk", unique_id=unique_id,
        data={"endpoint_id": identity,
              "kind": "sip_account", "name": "Desk", "username": "desk",
              "password": "fixture-kept-password", "extension": "301"})
    lab.hass.config_entries.async_add_subentry(lab.entry, sub)
    assert endpoint_from_subentry(lab.entry, sub).endpoint_id == identity
    assert phone_subentry_by_endpoint_id(lab.entry, identity) is sub
    update_phone_subentry(lab.hass, lab.entry, identity, {"name": "Renamed desk", "username": "renamed"})
    assert sub.unique_id == unique_id
    if unique_id:
        assert "endpoint_id" not in sub.data
    else:
        assert sub.data["endpoint_id"] == identity
    assert sub.data["password"] == "fixture-kept-password"
    assert lab.runtime.endpoints.resolve("renamed").endpoint_id == identity


async def test_account_draft_binding_reference_is_the_committed_native_identity(phone_lab):
    from custom_components.voip_stack.phone_config import (
        _add_phone_subentry, draft_phone_subentry, phone_endpoint_id,
    )

    lab = phone_lab
    draft = draft_phone_subentry({"kind": "sip_account", "name": "Draft"})
    bound_reference = phone_endpoint_id(draft)
    completed = {"kind": "sip_account", "name": "Desk", "username": "desk",
                 "password": "fixture-draft-password", "extension": "301"}
    edited = draft_phone_subentry(completed, previous=draft, title="Desk")
    saved = _add_phone_subentry(lab.hass, lab.entry, data=edited.data, title="Desk", draft=edited)
    assert saved.subentry_id == draft.subentry_id
    assert saved.unique_id == "phone:sip:" + saved.subentry_id
    assert phone_endpoint_id(saved) == bound_reference and "endpoint_id" not in saved.data
    assert len(lab.entry.subentries) == 1


async def test_legacy_data_allocator_materialises_the_same_native_subentry_id(phone_lab):
    from custom_components.voip_stack.phone_config import _add_phone_subentry, phone_endpoint_id, sip_account_phone_data

    lab = phone_lab
    data = sip_account_phone_data({"username": "desk", "password": "fixture-data-password"})
    saved = _add_phone_subentry(lab.hass, lab.entry, data=data, title="Desk")
    assert saved.unique_id == "phone:sip:" + saved.subentry_id
    assert phone_endpoint_id(saved) == "sip:" + saved.subentry_id
    assert "endpoint_id" not in saved.data


async def test_v7_1_identity_migration_keeps_legacy_identity_credentials_and_native_records(hass):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.phone_config import phone_endpoint_id

    entry = MockConfigEntry(domain="voip_stack", data={"initial_phone_created": True}, version=7, minor_version=0)
    entry.add_to_hass(hass)
    first = ConfigSubentry(subentry_type="phone", title="First", unique_id="phone:sip:first",
        data={"endpoint_id": "sip:first", "kind": "sip_account", "name": "First",
              "username": "first", "password": "fixture-first-password", "extension": "301"})
    second = ConfigSubentry(subentry_type="phone", title="Second", unique_id=None,
        data={"endpoint_id": "sip:second", "kind": "sip_account", "name": "Second",
              "username": "second", "password": "fixture-second-password", "extension": "302"})
    for sub in (first, second):
        hass.config_entries.async_add_subentry(entry, sub)
    native_ids = (first.subentry_id, second.subentry_id)
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 7 and entry.minor_version == 1
    assert (first.subentry_id, second.subentry_id) == native_ids
    assert phone_endpoint_id(first) == "sip:first" and phone_endpoint_id(second) == "sip:second"
    assert second.unique_id == "phone:sip:second"
    assert all("endpoint_id" not in sub.data for sub in (first, second))
    assert first.unique_id == "phone:sip:first" and first.data["password"] == "fixture-first-password"
    saved = [deepcopy(dict(sub.data)) for sub in (first, second)]
    assert await async_migrate_entry(hass, entry)
    assert [dict(sub.data) for sub in (first, second)] == saved
    from custom_components.voip_stack.phone_queue_membership import async_set_phone_memberships, iter_phone_memberships
    await async_set_phone_memberships(hass, entry, "sip:second", "queues", ["Support"])
    assert dict(iter_phone_memberships(hass, entry, "queue"))["sip:second"] == ("Support",)
    assert "endpoint_id" not in second.data


async def test_mismatched_identity_reports_repair_before_any_migration_write(hass):
    from custom_components.voip_stack import async_migrate_entry
    from homeassistant.helpers import issue_registry as ir

    entry = MockConfigEntry(domain="voip_stack", data={"initial_phone_created": True}, version=7, minor_version=0)
    entry.add_to_hass(hass)
    first = ConfigSubentry(subentry_type="phone", title="First", unique_id="phone:sip:first",
        data={"endpoint_id": "sip:first", "kind": "sip_account", "name": "First", "username": "first"})
    second = ConfigSubentry(subentry_type="phone", title="Second", unique_id="phone:sip:canonical",
        data={"endpoint_id": "sip:legacy", "kind": "sip_account", "name": "Second", "username": "second"})
    for sub in (first, second):
        hass.config_entries.async_add_subentry(entry, sub)
    before = [deepcopy(sub.as_dict()) for sub in (first, second)]
    assert await async_migrate_entry(hass, entry) is False
    assert entry.minor_version == 0
    assert [sub.as_dict() for sub in (first, second)] == before
    issue = ir.async_get(hass).async_get_issue("voip_stack", "phone_identity_conflict_" + entry.entry_id)
    assert issue is not None
    assert issue.translation_placeholders == {"phone": "Second", "canonical": "sip:canonical", "legacy": "sip:legacy"}
    hass.config_entries.async_update_subentry(entry, second,
        data={**second.data, "endpoint_id": "sip:canonical"})
    assert await async_migrate_entry(hass, entry)
    assert ir.async_get(hass).async_get_issue("voip_stack", "phone_identity_conflict_" + entry.entry_id) is None


async def test_physical_identity_conflict_blocks_account_backfill_while_provisioning_is_off(hass, monkeypatch):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.phone_profile_catalog import PhoneProfileCatalog
    from homeassistant.helpers import issue_registry as ir
    from unittest.mock import AsyncMock

    entry = MockConfigEntry(domain="voip_stack", data={"initial_phone_created": True}, version=7, minor_version=0)
    entry.add_to_hass(hass)
    account = ConfigSubentry(subentry_type="phone", title="Desk", unique_id=None,
        data={"endpoint_id": "sip:desk", "kind": "sip_account", "name": "Desk", "username": "desk",
              "password": "fixture-kept-password"})
    physical = ConfigSubentry(subentry_type="physical_phone", title="Hardware",
        unique_id="physical_phone:canonical", data={"phone_id": "legacy", "name": "Hardware"})
    for sub in (account, physical):
        hass.config_entries.async_add_subentry(entry, sub)
    download = AsyncMock(side_effect=AssertionError("identity migration must not load a profile"))
    monkeypatch.setattr(PhoneProfileCatalog, "async_profile", download)
    before = [deepcopy(sub.as_dict()) for sub in (account, physical)]
    assert await async_migrate_entry(hass, entry) is False
    assert [sub.as_dict() for sub in (account, physical)] == before
    assert entry.minor_version == 0
    download.assert_not_awaited()
    issue = ir.async_get(hass).async_get_issue("voip_stack", "phone_identity_conflict_" + entry.entry_id)
    assert issue is not None and issue.translation_placeholders["phone"] == "Hardware"
