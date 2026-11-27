"""Policy durations are persisted at input boundaries and required at runtime."""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.core import Context, ServiceCall
from homeassistant.exceptions import ServiceValidationError
import pytest

from custom_components.voip_stack import paging, parking, phonebook_services
from custom_components.voip_stack.contact_config import (
    contact_dicts,
    materialize_contact_timeout,
    replace_contacts,
)
from custom_components.voip_stack.duration_config import initialize_duration_config
from custom_components.voip_stack.paging_config import normalize_paging_group
from custom_components.voip_stack.parking_config import normalize_parking_config, parking_addresses, parking_settings
from custom_components.voip_stack.parking_lot import ParkingLot
from custom_components.voip_stack.roster import RosterError, parse_roster_json
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime

from .test_contact_profile_ha import custom_integrations as custom_integrations, entry as entry

pytestmark = pytest.mark.ha


@pytest.fixture
def duration_entry(hass, entry):
    data = initialize_duration_config(entry.data)
    data["service_timeouts"].update(automation_timeout=47, announcement_timeout=83)
    data["parking"]["timeout"] = 217
    hass.config_entries.async_update_entry(entry, data=data)
    return entry


def automation_record(identity, **metadata):
    return {"id": identity, "name": identity,
            "metadata": {"virtual_endpoint": "automation", **metadata}}


@pytest.mark.parametrize("value", [None, 0, True, float("nan"), float("inf"), "30"])
def test_persisted_policy_records_reject_missing_or_invalid_durations(value):
    contact = automation_record("Automation")
    page = {"name": "Page"}
    park = {}
    if value is not None:
        contact["metadata"]["automation_timeout"] = value
        page["timeout"] = value
        park["timeout"] = value
    with pytest.raises(RosterError):
        parse_roster_json([contact])
    with pytest.raises(ValueError):
        normalize_paging_group(page)
    if value == 0 and not isinstance(value, bool):
        assert normalize_parking_config(park)["timeout"] == 0
    else:
        with pytest.raises(ValueError):
            normalize_parking_config(park)
    with pytest.raises(ServiceValidationError):
        paging.validate_announcement({"targets": ["desk"], "media": "media-source://test", **park})


def test_materialization_is_a_copy_and_preserves_explicit_contact_values():
    missing = automation_record("Missing")
    before = deepcopy(missing)
    filled = materialize_contact_timeout(missing, 47)
    assert missing == before
    assert filled["metadata"]["automation_timeout"] == 47
    assert filled["metadata"] is not missing["metadata"]
    explicit = automation_record("Explicit", automation_timeout=23)
    assert materialize_contact_timeout(explicit, 47) == explicit
    invalid = automation_record("Invalid", automation_timeout=0)
    assert materialize_contact_timeout(invalid, 47) == invalid
    with pytest.raises(RosterError):
        parse_roster_json([materialize_contact_timeout(invalid, 47)])
    assert materialize_contact_timeout({"id": "Physical"}, 47) == {"id": "Physical"}
    assert materialize_contact_timeout({"type": "automation", "timeout": 19}, 47)["metadata"]["automation_timeout"] == 19


def test_parking_namespace_can_validate_legacy_addresses_without_creating_a_duration():
    settings = parking_addresses({"access": "750", "slots": ["751", "752"]})
    assert settings == {"access": "750", "slots": ["751", "752"]}
    with pytest.raises(ValueError):
        normalize_parking_config(settings)


async def test_replacement_seeds_legacy_missing_values_before_any_registry_update(hass, entry):
    missing, explicit = automation_record("Missing"), automation_record("Explicit", automation_timeout=23)
    replace_contacts(hass, entry, [missing, explicit], automation_timeout=47)
    assert "automation_timeout" not in missing["metadata"]
    assert {item["id"]: item["metadata"]["automation_timeout"] for item in contact_dicts(entry)} == {
        "Missing": 47, "Explicit": 23,
    }
    before = deepcopy(contact_dicts(entry))
    with pytest.raises(ValueError):
        replace_contacts(hass, entry, [automation_record("New"), automation_record("Bad", automation_timeout=True)],
                         automation_timeout=47)
    assert contact_dicts(entry) == before


async def test_new_contact_form_prefills_the_saved_owner_and_edit_preserves_its_value(hass, duration_entry):
    entry = duration_entry
    form = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "contact"), context={"source": "user"})
    form = await hass.config_entries.subentries.async_configure(form["flow_id"], {"type": "automation"})
    values = form["data_schema"]({"name": "Door automation"})
    assert values["timeout"] == 47
    result = await hass.config_entries.subentries.async_configure(form["flow_id"], values)
    assert result["type"] == "create_entry"
    child = next(iter(entry.subentries.values()))
    assert child.data["metadata"]["automation_timeout"] == 47
    hass.config_entries.async_update_entry(entry, data={**entry.data,
        "service_timeouts": {**entry.data["service_timeouts"], "automation_timeout": 61}})
    form = await hass.config_entries.subentries.async_init((entry.entry_id, "contact"),
        context={"source": "reconfigure", "subentry_id": child.subentry_id})
    values = form["data_schema"]({})
    assert values["timeout"] == 47
    result = await hass.config_entries.subentries.async_configure(form["flow_id"], values)
    assert result["type"] == "abort"
    assert entry.subentries[child.subentry_id].data["metadata"]["automation_timeout"] == 47
    form = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "contact"), context={"source": "user"})
    form = await hass.config_entries.subentries.async_configure(form["flow_id"], {"type": "automation"})
    assert form["data_schema"]({"name": "Another automation"})["timeout"] == 61


async def test_add_and_import_contacts_use_live_initial_values_and_keep_explicit_values(hass, duration_entry):
    handlers = phonebook_services.build_phonebook_service_handlers(AsyncMock())
    for name, fields in (("New", {}), ("Explicit", {"timeout": 19})):
        await handlers["add_contact"](ServiceCall(hass, "voip_stack", "add_contact",
            {"name": name, "type": "automation", **fields}, context=Context()))
    assert {item["id"]: item["metadata"]["automation_timeout"] for item in contact_dicts(duration_entry)} == {
        "New": 47, "Explicit": 19,
    }
    hass.config_entries.async_update_entry(duration_entry, data={**duration_entry.data,
        "service_timeouts": {**duration_entry.data["service_timeouts"], "automation_timeout": 61}})
    contacts = [automation_record("Imported"), automation_record("Imported explicit", automation_timeout=29)]
    await handlers["set_contacts"](ServiceCall(hass, "voip_stack", "set_contacts",
        {"roster_json": json.dumps({"version": 2, "contacts": contacts})}, context=Context()))
    assert {item["id"]: item["metadata"]["automation_timeout"] for item in contact_dicts(duration_entry)} == {
        "Imported": 61, "Imported explicit": 29,
    }


async def test_paging_form_materializes_its_own_limit_and_keeps_the_saved_choice(hass, duration_entry):
    form = await hass.config_entries.subentries.async_init(
        (duration_entry.entry_id, "paging"), context={"source": "user"})
    values = form["data_schema"]({"name": "Paging group"})
    assert values["timeout"] == 120
    result = await hass.config_entries.subentries.async_configure(form["flow_id"], {**values, "timeout": 51})
    assert result["type"] == "create_entry"
    child = next(row for row in duration_entry.subentries.values() if row.subentry_type == "paging")
    assert child.data["timeout"] == 51
    form = await hass.config_entries.subentries.async_init((duration_entry.entry_id, "paging"),
        context={"source": "reconfigure", "subentry_id": child.subentry_id})
    values = form["data_schema"]({})
    assert values["timeout"] == 51
    result = await hass.config_entries.subentries.async_configure(form["flow_id"], values)
    assert result["type"] == "abort"
    assert duration_entry.subentries[child.subentry_id].data["timeout"] == 51


async def test_announcement_resolves_current_owner_or_explicit_value_before_start(hass, duration_entry, monkeypatch):
    started = []
    monkeypatch.setattr(paging, "async_require_service_admin", AsyncMock())
    manager = SimpleNamespace(start=lambda _call, data: (
        started.append(data) or SimpleNamespace(report=lambda: {"state": "queued"})))
    monkeypatch.setattr(paging, "_manager", lambda _hass: manager)
    data = {"targets": ["desk"], "media": "media-source://test", "wait": False}
    call = ServiceCall(hass, "voip_stack", "announce", data, context=Context())
    await paging.async_announce(call)
    assert started[-1]["timeout"] == 83 and "timeout" not in call.data
    hass.config_entries.async_update_entry(duration_entry, data={**duration_entry.data,
        "service_timeouts": {**duration_entry.data["service_timeouts"], "announcement_timeout": 97}})
    await paging.async_announce(call)
    assert started[-1]["timeout"] == 97
    await paging.async_announce(ServiceCall(hass, "voip_stack", "announce", {**data, "timeout": 11}, context=Context()))
    assert started[-1]["timeout"] == 11
    hass.config_entries.async_update_entry(duration_entry, data={**duration_entry.data, "service_timeouts": {}})
    with pytest.raises(ServiceValidationError, match="Missing configured"):
        await paging.async_announce(call)
    assert len(started) == 3


async def test_saved_live_paging_cannot_inherit_an_absent_timeout(hass, duration_entry, monkeypatch):
    registry = SimpleNamespace(transition=Mock())
    monkeypatch.setattr(paging, "call_registry", lambda _hass: registry)
    monkeypatch.setattr(paging, "verified_paging_source", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(paging, "_manager", lambda _hass: SimpleNamespace(entry=duration_entry))
    monkeypatch.setattr(paging, "paging_group_configs", lambda _entry: {"group": {"name": "Saved group"}})
    contact = SimpleNamespace(metadata={"paging_group_id": "group"})
    with pytest.raises(ServiceValidationError):
        paging.start_paging_application(hass, object(), object(), contact, "127.0.0.1")
    registry.transition.assert_not_called()


async def test_parking_arms_the_persisted_residence_and_missing_policy_disables_it(hass, duration_entry, monkeypatch):
    owner = SipEndpointRuntime()
    owner.activate()
    session = owner.upsert("duration-check", state="in_call", owner="parking")
    lot = ParkingLot(("701",))
    parked = lot.reserve(session)
    settings = normalize_parking_config(duration_entry.data["parking"])
    application = SimpleNamespace(answer=AsyncMock(), settings=settings, lot=lot, parked=parked,
                                  _return=AsyncMock(), hass=hass, session=session)
    monkeypatch.setattr(parking, "publish_bridge_projection", lambda *_args, **_kwargs: None)
    start = asyncio.get_running_loop().time()
    try:
        await parking.ParkingCall.run(application)
        assert start + 217 <= parked.deadline <= asyncio.get_running_loop().time() + 217
        assert parked.timer is not None and not parked.timer.done()
    finally:
        await lot.close()
        await owner.shutdown()
    hass.config_entries.async_update_entry(duration_entry, data={**duration_entry.data, "parking": {}})
    assert parking_settings(hass) == {"enabled": False, "unavailable_reason": "invalid_configuration"}
