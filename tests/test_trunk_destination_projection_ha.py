"""One native trunk destination field projects existing devices and entities."""
from types import SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

pytestmark = pytest.mark.ha


@pytest.fixture
async def projected_accounts(hass):
    from custom_components.voip_stack.duration_config import initialize_duration_config
    from custom_components.voip_stack.endpoint_device import endpoint_device_identifier
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind, EndpointAvailability
    from custom_components.voip_stack.runtime_data import VoipStackRuntime
    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=7, data=initialize_duration_config({}))
    entry.add_to_hass(hass)
    registry = EndpointRegistry()
    devices = dr.async_get(hass)
    native = {}
    for identity, name, kind in [("home-account", "Casa Test", EndpointKind.BROWSER),
                                 ("desk-a", "Desk", EndpointKind.SIP_ACCOUNT),
                                 ("desk-b", "Desk", EndpointKind.SIP_ACCOUNT)]:
        endpoint = PhoneEndpoint(endpoint_id=identity, username=identity, name=name, kind=kind,
                                 availability=EndpointAvailability.AVAILABLE)
        registry.register(endpoint)
        device = devices.async_get_or_create(config_entry_id=entry.entry_id,
            identifiers={("voip_stack", endpoint_device_identifier(identity))}, name=name)
        registry.update(identity, device_id=device.id)
        native[identity] = device
    entry.runtime_data = VoipStackRuntime(transport_config={}, assist_config={}, trunk_config={},
        endpoints=registry, phones=None, preferred_phone_device_id=native["home-account"].id)
    return SimpleNamespace(entry=entry, devices=native, endpoints=registry)


async def test_duplicate_visual_names_keep_distinct_canonical_routes(hass, projected_accounts):
    from custom_components.voip_stack.trunk_destination_selector import trunk_destination_projection, trunk_destination_value
    data = projected_accounts
    options, references = trunk_destination_projection(hass, data.entry, ["Desk"], "")
    first, second = ("device:" + data.devices[name].id for name in ("desk-a", "desk-b"))
    labels = {option["value"]: option["label"] for option in options}
    assert labels[first] != labels[second]
    assert trunk_destination_value(first, references) == "desk-a"
    assert trunk_destination_value(second, references) == "desk-b"


async def test_entity_rename_and_integration_filter_do_not_change_account_identity(hass, projected_accounts):
    from custom_components.voip_stack.trunk_destination_selector import trunk_destination_projection, trunk_destination_value
    data = projected_accounts
    entities = er.async_get(hass)
    owned = entities.async_get_or_create("event", "voip_stack", "desk-native-event", config_entry=data.entry,
        device_id=data.devices["desk-a"].id, suggested_object_id="old_desk")
    unrelated = entities.async_get_or_create("sensor", "unrelated", "not-voip", config_entry=data.entry,
        device_id=data.devices["desk-a"].id, suggested_object_id="not_voip")
    _options, references = trunk_destination_projection(hass, data.entry, [], "")
    assert trunk_destination_value("entity:" + owned.entity_id, references) == "desk-a"
    assert "entity:" + unrelated.entity_id not in references
    renamed = entities.async_update_entity(owned.entity_id, new_entity_id="event.renamed_desk")
    _options, references = trunk_destination_projection(hass, data.entry, [], "")
    assert "entity:event.old_desk" not in references
    assert trunk_destination_value("entity:" + renamed.entity_id, references) == "desk-a"


async def test_manual_targets_and_explicit_empty_are_preserved(hass, projected_accounts):
    from custom_components.voip_stack.config_flow import TrunkFormMixin, VoipStackConfigFlow
    from custom_components.voip_stack.trunk_destination_selector import trunk_destination_projection, trunk_destination_value
    data = projected_accounts
    manual = "sip:450@pbx.example;transport=tcp"
    options, references = trunk_destination_projection(hass, data.entry, [], manual)
    assert {item["value"] for item in options} >= {manual}
    assert trunk_destination_value(manual, references) == manual
    assert trunk_destination_value("", references) == ""
    class Form(TrunkFormMixin, VoipStackConfigFlow):
        pass
    form = Form()
    form.hass = hass
    for existing in ({"trunk_inbound_default_target": ""}, {"trunk_inbound_default_target": manual}, {}):
        form._current_entry_data = lambda existing=existing: (data.entry, existing)
        response = await form.async_step_trunk()
        field = next(key for key in response["data_schema"].schema if key.schema == "trunk_inbound_default_target")
        if existing.get("trunk_inbound_default_target") == "":
            assert field.default is __import__('voluptuous').UNDEFINED
        else:
            assert field.default() == existing.get("trunk_inbound_default_target", "home-account")
            if not existing:
                selector = response["data_schema"].schema[field]
                assert next(option["label"] for option in selector.config["options"]
                            if option["value"] == "home-account") == "Casa Test"


async def test_trunk_device_retains_native_subentry_reconfigure_link(hass, projected_accounts):
    from custom_components.voip_stack.trunk_config import sync_trunk_devices
    entry = projected_accounts.entry
    row = ConfigSubentry(subentry_type="trunk", title="Provider", unique_id="trunk:provider",
                         data={"trunk_id": "provider"})
    hass.config_entries.async_add_subentry(entry, row)
    sync_trunk_devices(hass, entry)
    device = dr.async_get(hass).async_get_device_by_identifier(("voip_stack", "trunk:provider"), entry.entry_id)
    assert device and device.config_entry_id == entry.entry_id and device.config_subentry_id == row.subentry_id


async def test_contact_write_exposes_welcome_as_device_and_event_destination(hass, projected_accounts):
    from custom_components.voip_stack.contact_config import contact_dicts, replace_contacts
    from custom_components.voip_stack.automation_contact_device import (
        automation_contact_device_identifier, automation_contact_event_unique_id)
    from custom_components.voip_stack.roster import parse_roster_json
    from custom_components.voip_stack.router import RouteAction, resolve_ha_router
    from custom_components.voip_stack.trunk_destination_selector import trunk_destination_projection, trunk_destination_value
    entry = projected_accounts.entry
    replace_contacts(hass, entry, [{"id": "welcome-id", "name": "Welcome",
        "metadata": {"virtual_endpoint": "automation", "automation_timeout": 30}}])
    device = dr.async_get(hass).async_get_device_by_identifier(
        ("voip_stack", automation_contact_device_identifier(entry.entry_id, "welcome-id")), entry.entry_id)
    assert device is not None
    row = next(row for row in entry.subentries.values() if row.subentry_type == "contact")
    entity = er.async_get(hass).async_get_or_create("event", "voip_stack",
        automation_contact_event_unique_id(entry.entry_id, "welcome-id"), config_entry=entry,
        config_subentry_id=row.subentry_id, device_id=device.id)
    _options, references = trunk_destination_projection(hass, entry, [], "")
    for value in ("device:" + device.id, "entity:" + entity.entity_id):
        destination = trunk_destination_value(value, references)
        decision = resolve_ha_router(destination, parse_roster_json(contact_dicts(entry)))
        assert decision.action is RouteAction.AUTOMATION and decision.entry.id == "welcome-id"
    assert not any(row.subentry_type == "phone" for row in entry.subentries.values())
