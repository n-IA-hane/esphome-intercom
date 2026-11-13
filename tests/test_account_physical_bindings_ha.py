"""Physical association edits share one inventory and never retain old secrets."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from .test_physical_phone_flow_ha import (
    identity, physical_flow as physical_flow, start, submit,
)
from .test_physical_phone_runtime_ha import (
    cached_physical_config as cached_physical_config,
    custom_integration_path as custom_integration_path,
    hass_config_dir as hass_config_dir,
    hass_storage as hass_storage,
    phone_lab as phone_lab,
)

pytestmark = pytest.mark.ha


def test_unassigned_inventory_has_no_implicit_line_or_account():
    from custom_components.voip_stack.physical_phone import normalize_physical_phone

    data = normalize_physical_phone({"name": "Unassigned", "mac": "001122334455",
                                     "profile_id": "fanvil/x3u-pro", "lines": [], "keys": []})
    assert data["lines"] == [] and data["keys"] == []
    assert data["phone_id"] and data["provisioning_token"]


async def test_discovery_editor_can_save_unassigned_without_creating_an_account(physical_flow):
    state = physical_flow
    before = set(state.lab.entry.subentries)
    form = await start(state)
    assert form["description_placeholders"]["profile_pin"] == "a" * 40
    form = await submit(state, form, identity())
    saved = await submit(state, form, {"lines": [], "keys": []})
    assert saved["type"] == "create_entry"
    added = [item for key, item in state.lab.entry.subentries.items() if key not in before]
    assert len(added) == 1 and added[0].subentry_type == "physical_phone"
    assert added[0].data["lines"] == []


async def test_last_unbind_invalidates_old_credentials_and_serves_owned_disable_only(cached_physical_config):
    from custom_components.voip_stack.physical_phone import normalize_physical_phone

    state = cached_physical_config
    manager, old = state.manager, state.data
    profile, _ = await manager.catalog.async_profile(old["profile_id"], commit=old["catalog_commit"])
    profile["capabilities"]["disable_lines"] = True
    template = {"templates/config.jinja2":
        "{% for line in lines %}sip.password={{ line.password }}\n{% endfor %}"
        "{% for slot in retired['lines'] %}line{{ slot }}.enable=0\n{% endfor %}"}
    manager.catalog.async_profile.return_value = (profile, template)
    # Persisted historical ownership, independent of whether the phone downloaded it.
    manager.states[old["phone_id"]]["managed_slots"] = {
        "mac": old["mac"], "keys": [], "lines": {"1": ["sip:first"]}}
    proposed = normalize_physical_phone({**old, "lines": [], "keys": []}, previous=old)
    assert proposed["provisioning_token"] == old["provisioning_token"]
    state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=proposed)
    filename = old["mac"] + ".cfg"
    assert await manager.async_file(old["phone_id"], old["provisioning_token"], filename) is None
    await manager.async_prepare(proposed)
    assert manager.authentication_owner(proposed) == ()
    body = await manager.async_file(old["phone_id"], old["provisioning_token"], filename)
    assert body == b"line1.enable=0\n"
    assert b"password" not in body and b"fixture-only-password" not in body
    assert manager.public_state(old["phone_id"])["state"] == "unassigned"
    assert manager.public_state(old["phone_id"])["lines"] == []


async def test_unassigned_hardware_context_needs_no_registrar_or_ldap(cached_physical_config, monkeypatch):
    state = cached_physical_config
    phone = {**state.data, "lines": [], "keys": []}
    profile, _ = await state.manager.catalog.async_profile(phone["profile_id"], commit=phone["catalog_commit"])
    profile["capabilities"]["functions"].append("ldap")
    monkeypatch.setattr(state.manager.runtime, "sip", None)
    context = await state.manager.async_context(phone, profile)
    assert context["lines"] == [] and context["directory"] == {}
    assert state.manager.authentication_owner(phone) == ()


async def test_inverse_account_edits_are_staged_and_keep_other_account_slots(physical_flow):
    from homeassistant.config_entries import ConfigSubentry
    from custom_components.voip_stack.physical_phone import (
        account_physical_bindings, apply_account_bindings, async_prepare_account_bindings,
        normalize_physical_phone,
    )

    state = physical_flow
    phone = normalize_physical_phone({"name": "Desk", "mac": "001122334455",
        "profile_id": "fanvil/x3u-pro", "catalog_commit": "a" * 40,
        "lines": [{"slot": "1", "endpoint_id": "sip:office"}], "keys": []})
    sub = ConfigSubentry(subentry_type="physical_phone", title="Desk", unique_id=phone["phone_id"], data=phone)
    state.lab.hass.config_entries.async_add_subentry(state.lab.entry, sub)
    state.lab.runtime.physical_phones = SimpleNamespace(catalog=state.catalog)
    rows = [{"phone_id": phone["phone_id"], "slot": "2", "transport": "udp"}]
    prepared = await async_prepare_account_bindings(state.lab.hass, state.lab.entry, "sip:front", rows)
    assert list(sub.data["lines"]) == phone["lines"]
    assert account_physical_bindings(state.lab.entry, "sip:front") == []
    apply_account_bindings(state.lab.hass, state.lab.entry, prepared)
    assert account_physical_bindings(state.lab.entry, "sip:front") == rows
    assert sub.data["lines"][0] == phone["lines"][0]


async def test_inverse_account_edit_cannot_overwrite_another_account_slot(physical_flow):
    from homeassistant.config_entries import ConfigSubentry
    from custom_components.voip_stack.physical_phone import async_prepare_account_bindings, normalize_physical_phone

    state = physical_flow
    phone = normalize_physical_phone({"name": "Desk", "mac": "001122334455",
        "profile_id": "fanvil/x3u-pro", "catalog_commit": "a" * 40,
        "lines": [{"slot": "1", "endpoint_id": "sip:office"}], "keys": []})
    sub = ConfigSubentry(subentry_type="physical_phone", title="Desk", unique_id=phone["phone_id"], data=phone)
    state.lab.hass.config_entries.async_add_subentry(state.lab.entry, sub)
    state.lab.runtime.physical_phones = SimpleNamespace(catalog=state.catalog)
    before = deepcopy(dict(sub.data))
    with pytest.raises(ValueError, match="phone_line_occupied"):
        await async_prepare_account_bindings(state.lab.hass, state.lab.entry, "sip:front",
            [{"phone_id": phone["phone_id"], "slot": "1", "transport": "udp"}])
    assert dict(sub.data) == before
