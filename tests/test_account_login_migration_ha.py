"""Repair generated browser logins without changing their native account owners."""

from copy import deepcopy
import json
import secrets

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from .test_managed_phones_ha import (
    custom_integration_path as custom_integration_path,
    phone_lab as phone_lab,
)

pytestmark = pytest.mark.ha


def add_account(hass, entry, identity, *, name="Casa", username="ha_browser_legacy"):
    data = {
        "kind": "sip_account", "name": name, "username": username,
        "password": secrets.token_urlsafe(24), "extension": "221",
        "enabled": True, "auto_answer": True, "send_video": True,
        "ha_bindings": [{"source": "migration", "reference": "legacy_ha_phone:" + identity},
                        {"source": "popup", "reference": "kept-popup"}],
        "call_policy": {"ringing_timeout": 25, "no_answer": {"kind": "voicemail", "target": identity}},
        "future_unknown": {"keep": ["literal", 7]},
    }
    subentry = ConfigSubentry(subentry_type="phone", title=name,
        unique_id="phone:" + identity, data=data)
    hass.config_entries.async_add_subentry(entry, subentry)
    return subentry


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


@pytest.mark.parametrize("version,minor", [(6, 4), (7, 0), (7, 1)])
@pytest.mark.parametrize("name", ["Casa", "Piano Terra", 'Caffè "Nord"'])
async def test_generated_login_upgrade_preserves_every_other_account_field(
    hass, socket_enabled, version, minor, name
):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.phone_config import phone_endpoint_id
    from custom_components.voip_stack.store import sip_accounts
    from .test_linphone_registrar_wire import check_account_registration

    entry = MockConfigEntry(domain="voip_stack", version=version, minor_version=minor,
        data={"initial_phone_created": True, "future_parent": {"keep": True}},
        options={"future_option": ["keep"]})
    entry.add_to_hass(hass)
    identity = "browser:legacy"
    subentry = add_account(hass, entry, identity, name=name)
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(config_entry_id=entry.entry_id,
        config_subentry_id=subentry.subentry_id,
        identifiers={("voip_stack", "phone_endpoint:" + identity)}, name="Kept device")
    entity = er.async_get(hass).async_get_or_create("sensor", "voip_stack", "kept-call-state",
        config_entry=entry, config_subentry_id=subentry.subentry_id, device_id=device.id)
    before = deepcopy(dict(subentry.data))
    parent, options = encoded(dict(entry.data)), encoded(dict(entry.options))
    assert await async_migrate_entry(hass, entry)
    assert subentry.data["username"] == name
    assert encoded({key: value for key, value in subentry.data.items() if key != "username"}) == encoded(
        {key: value for key, value in before.items() if key != "username"})
    assert entry.version == 7 and entry.minor_version == 2
    assert phone_endpoint_id(subentry) == identity and subentry.unique_id == "phone:" + identity
    assert encoded(dict(entry.data)) == parent and encoded(dict(entry.options)) == options
    assert devices.async_get(device.id).identifiers == device.identifiers
    assert er.async_get(hass).async_get(entity.entity_id).id == entity.id
    account, = sip_accounts(hass)
    assert account.password == before["password"]
    await check_account_registration("UDP", "outbound", account)
    saved = encoded(subentry.as_dict())
    assert await async_migrate_entry(hass, entry)
    assert encoded(subentry.as_dict()) == saved


@pytest.mark.parametrize("name,taken", [("Casa", "casa"), ("Straße", "STRASSE"), ("☎", "☎")])
@pytest.mark.parametrize("version,minor", [(6, 4), (7, 0), (7, 1)])
async def test_repair_conflict_reports_existing_issue_before_any_account_write(hass, name, taken, version, minor):
    from homeassistant.helpers import issue_registry as ir
    from custom_components.voip_stack import async_migrate_entry

    entry = MockConfigEntry(domain="voip_stack", version=version, minor_version=minor,
        data={"initial_phone_created": True})
    entry.add_to_hass(hass)
    first = add_account(hass, entry, "browser:first", name="Reception", username="ha_browser_first")
    other = add_account(hass, entry, "sip:manual", name="Desk", username=taken)
    second = add_account(hass, entry, "browser:legacy", name=name)
    hass.config_entries.async_update_subentry(entry, first, data={**first.data, "extension": "222"})
    hass.config_entries.async_update_subentry(entry, other, data={**other.data, "extension": "223"})
    before = [encoded(sub.as_dict()) for sub in (first, second, other)]
    assert await async_migrate_entry(hass, entry) is False
    assert entry.version == version and entry.minor_version == minor
    assert [encoded(sub.as_dict()) for sub in (first, second, other)] == before
    issue_id = "account_route_conflict_" + entry.entry_id
    issue = ir.async_get(hass).async_get_issue("voip_stack", issue_id)
    assert issue is not None and issue.translation_placeholders == {"phone": name, "route": name}
    hass.config_entries.async_update_subentry(entry, other, data={**other.data, "username": "desk-login"})
    assert await async_migrate_entry(hass, entry)
    assert first.data["username"] == "Reception" and second.data["username"] == name
    assert ir.async_get(hass).async_get_issue("voip_stack", issue_id) is None


async def test_interrupted_login_repair_retries_without_rotating_credentials(hass, monkeypatch):
    from custom_components.voip_stack import async_migrate_entry

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=1,
        data={"initial_phone_created": True})
    entry.add_to_hass(hass)
    first = add_account(hass, entry, "browser:first", name="Reception", username="ha_browser_first")
    second = add_account(hass, entry, "browser:legacy")
    hass.config_entries.async_update_subentry(entry, first, data={**first.data, "extension": "222"})
    saved = [deepcopy(dict(sub.data)) for sub in (first, second)]
    update = hass.config_entries.async_update_subentry
    writes = 0

    def interrupted(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise RuntimeError("fixture interrupted between login writes")
        return update(*args, **kwargs)

    monkeypatch.setattr(hass.config_entries, "async_update_subentry", interrupted)
    with pytest.raises(RuntimeError, match="interrupted between login writes"):
        await async_migrate_entry(hass, entry)
    assert entry.minor_version == 1
    assert first.data["username"] == "Reception" and second.data["username"] == "ha_browser_legacy"
    monkeypatch.setattr(hass.config_entries, "async_update_subentry", update)
    assert await async_migrate_entry(hass, entry)
    for subentry, original, username in zip((first, second), saved, ("Reception", "Casa"), strict=True):
        assert dict(subentry.data) == {**original, "username": username}


@pytest.mark.parametrize("username,binding,expected", [
    ("custom-login", "browser:legacy", "custom-login"),
    ("ha_browser_legacy", "browser:other", "ha_browser_legacy"),
    ("ha_browser_legacy", None, "ha_browser_legacy"),
    ("ha_phone_legacy_2", "browser:legacy", "Casa"),
])
async def test_only_proven_generated_logins_are_repaired(hass, username, binding, expected):
    from custom_components.voip_stack import async_migrate_entry

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=1,
        data={"initial_phone_created": True})
    entry.add_to_hass(hass)
    subentry = add_account(hass, entry, "browser:legacy", username=username)
    associations = [{"source": "migration"}]
    if binding is not None:
        associations[0]["reference"] = "legacy_ha_phone:" + binding
    hass.config_entries.async_update_subentry(entry, subentry,
        data={**subentry.data, "ha_bindings": associations})
    before = deepcopy(dict(subentry.data))
    assert await async_migrate_entry(hass, entry)
    assert dict(subentry.data) == {**before, "username": expected}
    # Once the upgrade is complete, an explicit later login is user-owned.
    hass.config_entries.async_update_subentry(entry, subentry,
        data={**subentry.data, "username": "ha_browser_legacy"})
    assert await async_migrate_entry(hass, entry)
    assert subentry.data["username"] == "ha_browser_legacy"


async def test_fresh_browser_upgrade_keeps_an_explicit_generated_shaped_login(hass):
    from custom_components.voip_stack import async_migrate_entry

    entry = MockConfigEntry(domain="voip_stack", version=6, minor_version=4,
        data={"initial_phone_created": True})
    entry.add_to_hass(hass)
    subentry = add_account(hass, entry, "browser:legacy")
    hass.config_entries.async_update_subentry(entry, subentry,
        data={**subentry.data, "kind": "browser", "ha_bindings": []})
    password = subentry.data["password"]
    assert await async_migrate_entry(hass, entry)
    assert subentry.data["username"] == "ha_browser_legacy"
    assert subentry.data["password"] == password
    assert subentry.data["kind"] == "sip_account"


async def test_repaired_login_reaches_existing_provisioning_line_without_rebinding(phone_lab):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.phone_config import async_setup_endpoint_registry, sync_registry_from_entry
    from custom_components.voip_stack.physical_phone import normalize_physical_phone, physical_phone_data
    from custom_components.voip_stack.physical_phone_provisioning import PhysicalPhoneProvisioner
    from custom_components.voip_stack.phone_profile import render_phone_bundle
    from custom_components.voip_stack.store import sip_accounts
    from .test_physical_phone_runtime_ha import cached_profile

    lab = phone_lab
    lab.hass.config_entries.async_update_entry(lab.entry, version=7, minor_version=1)
    account = add_account(lab.hass, lab.entry, "browser:legacy")
    hardware = normalize_physical_phone({"name": "Bound phone", "mac": "001122334455",
        "profile_id": "fanvil/x3u-pro", "lines": [{"slot": "1", "endpoint_id": "browser:legacy"}], "keys": []})
    physical = ConfigSubentry(subentry_type="physical_phone", title=hardware["name"],
        unique_id="physical_phone:" + hardware["phone_id"], data=hardware)
    lab.hass.config_entries.async_add_subentry(lab.entry, physical)
    before = encoded(physical.as_dict())
    password = account.data["password"]
    lab.runtime.endpoints = async_setup_endpoint_registry(lab.hass, lab.entry)
    assert await async_migrate_entry(lab.hass, lab.entry)
    assert account.data["username"] == "Casa"
    sync_registry_from_entry(lab.hass, lab.entry)
    lab.registrar.update_accounts(sip_accounts(lab.hass))
    manager = PhysicalPhoneProvisioner(lab.hass, lab.entry)
    lab.runtime.physical_phones = manager
    profile, templates = cached_profile()
    context = await manager.async_context(physical_phone_data(physical), profile)
    assert context["lines"][0]["username"] == context["lines"][0]["auth_username"] == "Casa"
    assert context["lines"][0]["password"] == password
    files, _ = render_phone_bundle(profile, templates, context)
    assert files["001122334455.cfg"].splitlines()[0] == b"sip.username=Casa"
    assert encoded(physical.as_dict()) == before
