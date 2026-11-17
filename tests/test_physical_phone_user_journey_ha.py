"""Native adoption and explicit physical key order have one clear operation."""

from copy import deepcopy

from homeassistant.config_entries import ConfigSubentry
import pytest
import voluptuous as vol

from .test_physical_phone_flow_ha import physical_flow as physical_flow, identity, start, submit
from .test_managed_phones_ha import phone_lab as phone_lab, custom_integration_path as custom_integration_path

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("assigned", [False, True])
async def test_one_form_adoption_uses_native_identity_and_optional_account(physical_flow, assigned):
    from custom_components.voip_stack.physical_phone import physical_phone_data

    state = physical_flow
    before = set(state.lab.entry.subentries)
    form = await start(state)
    result = await submit(state, form, identity(**({"account_id": "sip:front"} if assigned else {})))
    assert result["type"] == "abort" and result["reason"] == "phone_configured"
    (record,) = [value for key, value in state.lab.entry.subentries.items() if key not in before]
    assert record.subentry_type == "physical_phone"
    assert record.unique_id == "physical_phone:" + record.subentry_id
    assert "phone_id" not in record.data
    data = physical_phone_data(record)
    assert data["phone_id"] == record.subentry_id and not data["keys"]
    assert data["lines"] == ([{"slot": "1", "endpoint_id": "sip:front", "transport": "udp"}] if assigned else [])


async def test_key_order_selectors_apply_a_unique_permutation_to_owned_positions(physical_flow):
    from custom_components.voip_stack.physical_phone import normalize_physical_phone, physical_phone_data

    state = physical_flow
    original = normalize_physical_phone({**identity(), "phone_id": "legacy-physical",
        "catalog_commit": "a" * 40, "lines": [{"slot": "1", "endpoint_id": "sip:front"}],
        "keys": [{"slot": "1", "line": "1", "function": "line", "label": "Line"},
                 {"slot": "2", "line": "1", "function": "blf", "target": "sip:front", "label": "Casa"},
                 {"slot": "3", "line": "1", "function": "blf", "target": "sip:office", "label": "P4"}]})
    record = ConfigSubentry(subentry_type="physical_phone", title=original["name"], data=deepcopy(original),
        unique_id="physical_phone:legacy-physical")
    state.lab.hass.config_entries.async_add_subentry(state.lab.entry, record)
    form = await state.lab.hass.config_entries.subentries.async_init(
        (state.lab.entry.entry_id, "physical_phone"),
        context={"source": "reconfigure", "subentry_id": record.subentry_id})
    assert form["type"] == "menu"
    form = await submit(state, form, {"next_step_id": "key_order"})
    selectors = {key.schema: value for key, value in form["data_schema"].schema.items()}
    assert set(selectors) == {"1", "2", "3"}
    assert all("select" in selector.serialize()["selector"] for selector in selectors.values())
    bad = await submit(state, form, {"1": "3", "2": "3", "3": "2"})
    assert bad["errors"]["base"] == "invalid_phone_key_order"
    assert physical_phone_data(state.lab.entry.subentries[record.subentry_id]) == original
    result = await submit(state, bad, {"1": "3", "2": "1", "3": "2"})
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    saved = physical_phone_data(state.lab.entry.subentries[record.subentry_id])
    assert [(key["slot"], key["label"]) for key in saved["keys"]] == [("1", "P4"), ("2", "Line"), ("3", "Casa")]
    for field in ("phone_id", "mac", "provisioning_token", "lines", "catalog_commit"):
        assert saved[field] == original[field]


async def test_admin_password_is_not_prefilled_and_explicit_username_clear_removes_credentials(physical_flow):
    from custom_components.voip_stack.physical_phone import draft_physical_subentry, normalize_physical_phone, physical_phone_data

    state = physical_flow
    data = normalize_physical_phone({**identity(), "catalog_commit": "a" * 40,
        "admin_username": "fixture-admin", "admin_password": "fixture-private"})
    record = draft_physical_subentry(data)
    state.lab.hass.config_entries.async_add_subentry(state.lab.entry, record)
    form = await state.lab.hass.config_entries.subentries.async_init(
        (state.lab.entry.entry_id, "physical_phone"), context={"source": "reconfigure", "subentry_id": record.subentry_id})
    form = await submit(state, form, {"next_step_id": "user"})
    advanced = next(value for key, value in form["data_schema"].schema.items() if key.schema == "advanced")
    fields = {key.schema: key for key in advanced.schema.schema}
    assert fields["admin_username"].default() == "fixture-admin"
    assert fields["admin_password"].default is vol.UNDEFINED
    result = await submit(state, form, identity(advanced={"admin_username": "fixture-admin", "admin_password": ""}))
    assert result["type"] == "abort"
    saved = physical_phone_data(state.lab.entry.subentries[record.subentry_id])
    assert saved["admin_password"] == "fixture-private"

    form = await state.lab.hass.config_entries.subentries.async_init(
        (state.lab.entry.entry_id, "physical_phone"), context={"source": "reconfigure", "subentry_id": record.subentry_id})
    form = await submit(state, form, {"next_step_id": "user"})
    result = await submit(state, form, identity(advanced={"admin_username": "", "admin_password": ""}))
    assert result["type"] == "abort"
    cleared = physical_phone_data(state.lab.entry.subentries[record.subentry_id])
    assert cleared["admin_username"] == cleared["admin_password"] == ""
    for field in ("phone_id", "provisioning_token", "mac", "catalog_commit", "lines", "keys"):
        assert cleared[field] == saved[field]
