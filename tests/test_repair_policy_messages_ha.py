"""Repairs identify the affected setting and explain the saved routing policy."""

from copy import deepcopy

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.translation import async_get_translations
from pytest_homeassistant.common import MockConfigEntry

from .test_duration_migration_repair_ha import blocked_repair, edit_durations, owner_choice
from .test_managed_phones_ha import custom_integration_path as custom_integration_path

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("payload", [None, {}])
async def test_native_repair_initial_metadata_is_not_an_owner_selection(hass, monkeypatch, payload):
    from homeassistant.data_entry_flow import InvalidData

    entry, _, manager, _, issue_id = await blocked_repair(hass, monkeypatch,
        {"service_timeouts": {"media_timeout": 0}})
    initial = await manager.async_init("voip_stack", context={"issue_id": issue_id}, data=payload)
    assert initial["step_id"] == "init"
    assert not initial.get("errors")
    before = deepcopy(entry.as_dict())
    with pytest.raises(InvalidData):
        await manager.async_configure(initial["flow_id"], {"owner": "missing-owner"})
    assert entry.as_dict() == before


@pytest.mark.parametrize("language", ["it", "en"])
async def test_repair_section_labels_follow_native_frontend_translation_paths(hass, monkeypatch, language):
    hass.config.language = language
    _, _, manager, initial, _ = await blocked_repair(hass, monkeypatch,
        {"service_timeouts": {"media_timeout": 0, "dtmf_timeout": 14}})
    form = await manager.async_configure(initial["flow_id"], {"owner": owner_choice(initial)})
    section = next(iter(form["data_schema"].schema.values()))
    translations = await async_get_translations(hass, language, "issues", {"voip_stack"})
    prefix = "component.voip_stack.issues.duration_configuration_invalid.fix_flow.step.edit_durations."
    # Native expandable forms pass options.path, whereas this HA's Repairs
    # renderer checks options.prefix. Its supported fallback is data.<field>,
    # also used for the section heading. Keep one label per field there.
    assert translations.get(prefix + "data.advanced")
    for key in section.schema.schema:
        assert translations.get(prefix + "data." + key.schema)
    assert section.options["collapsed"] is True


@pytest.mark.parametrize("language,label", [("it", "Riproduzione audio"), ("en", "Audio playback")])
async def test_duration_repair_names_the_field_and_range_in_user_language(hass, monkeypatch, language, label):
    hass.config.language = language
    entry, _, manager, initial, _ = await blocked_repair(hass, monkeypatch,
        {"service_timeouts": {"media_timeout": 0, "dtmf_timeout": 12}})
    selector = next(iter(initial["data_schema"].schema.values()))
    assert "service_timeouts" not in selector.config["options"][0]["label"]
    form = await manager.async_configure(initial["flow_id"], {"owner": owner_choice(initial)})
    detail = form["description_placeholders"]["error"]
    assert label in detail and "1" in detail and "600" in detail
    assert "media_timeout" not in detail and "ValueError" not in detail
    section = next(iter(form["data_schema"].schema.values()))
    assert section.options["collapsed"] is True
    assert next(key.default() for key in section.schema.schema if key.schema == "dtmf_timeout") == 12
    result = await edit_durations(manager, form["flow_id"], {"media_timeout": 45})
    assert result["type"] == "create_entry"
    assert entry.data["service_timeouts"]["dtmf_timeout"] == 12


async def test_expiry_relationship_has_human_labels_and_preserves_failed_submission(hass, monkeypatch):
    hass.config.language = "it"
    entry, _, manager, initial, _ = await blocked_repair(hass, monkeypatch,
        {"registrar_min_expires": 120, "registrar_max_expires": 60})
    form = await manager.async_configure(initial["flow_id"], {"owner": owner_choice(initial)})
    before = deepcopy(entry.as_dict())
    invalid = await edit_durations(manager, form["flow_id"],
        {"registrar_min_expires": 130, "registrar_max_expires": 70})
    detail = invalid["description_placeholders"]["error"]
    assert "Durata minima" in detail and "Durata massima" in detail
    assert "registrar_" not in detail and "must not exceed" not in detail
    assert entry.as_dict() == before
    fields = next(iter(invalid["data_schema"].schema.values())).schema.schema
    assert {key.schema: key.default() for key in fields} == {
        "registrar_min_expires": 130, "registrar_max_expires": 70}


async def test_structural_repair_explains_next_step_without_exception_or_storage(hass, monkeypatch, caplog):
    hass.config.language = "it"
    _, _, _, initial, _ = await blocked_repair(hass, monkeypatch,
        {"parking": ["do-not-display-secret"]})
    assert initial["step_id"] == "confirm"
    detail = initial["description_placeholders"]["error"]
    assert "struttura" in detail and "log" in detail
    assert "ValueError" not in detail and "do-not-display-secret" not in repr(initial)
    assert "PBX migration structure needs review" in caplog.text
    assert "Invalid parking configuration" in caplog.text
    assert "do-not-display-secret" not in caplog.text


@pytest.mark.parametrize("value", ["never-display-this-secret", float("nan"), True])
async def test_non_numeric_saved_value_is_blank_editable_and_not_reflected(hass, monkeypatch, value):
    entry, _, manager, initial, _ = await blocked_repair(hass, monkeypatch,
        {"service_timeouts": {"media_timeout": value, "dtmf_timeout": 14}})
    form = await manager.async_configure(initial["flow_id"], {"owner": owner_choice(initial)})
    fields = next(iter(form["data_schema"].schema.values())).schema.schema
    invalid = next(key for key in fields if key.schema == "media_timeout")
    import voluptuous as vol
    assert invalid.default is vol.UNDEFINED
    assert "never-display-this-secret" not in repr(form)
    assert "Audio playback" in form["description_placeholders"]["error"]
    result = await edit_durations(manager, form["flow_id"], {"media_timeout": 40})
    assert result["type"] == "create_entry"
    assert entry.data["service_timeouts"]["media_timeout"] == 40
    assert entry.data["service_timeouts"]["dtmf_timeout"] == 14


@pytest.mark.parametrize("destination,kind,display", [
    ("", "trunk_dtmf_retired_reject", None),
    ("desk", "trunk_dtmf_retired", "Telefono ufficio"),
    (None, "trunk_dtmf_retired", "Telefono casa"),
])
async def test_retired_menu_notice_matches_actual_destination(hass, destination, kind, display):
    from custom_components.voip_stack.repairs import async_report_trunk_menu_retired

    entry = MockConfigEntry(domain="voip_stack", title="Casa", data={})
    entry.add_to_hass(hass)
    for identity, name in [("default", "Telefono casa"), ("desk", "Telefono ufficio")]:
        hass.config_entries.async_add_subentry(entry, ConfigSubentry(subentry_type="phone", title=name,
            unique_id="phone:" + identity, data={"endpoint_id": identity, "username": identity}))
    data = {} if destination is None else {"trunk_inbound_default_target": destination}
    record = ConfigSubentry(subentry_type="trunk", title="Fusion ufficio", unique_id="trunk:fusion", data=data)
    hass.config_entries.async_add_subentry(entry, record)
    async_report_trunk_menu_retired(hass, entry, record)
    issue = ir.async_get(hass).async_get_issue("voip_stack", "trunk_dtmf_retired_" + record.subentry_id)
    assert issue.translation_key == kind
    assert issue.translation_placeholders["trunk"] == "Fusion ufficio"
    if display is not None:
        assert issue.translation_placeholders["destination"] == display
    texts = await async_get_translations(hass, "it", "issues", {"voip_stack"})
    rendered = texts["component.voip_stack.issues." + kind + ".description"].format(**issue.translation_placeholders)
    assert "Fusion ufficio" in rendered and "Automazione" in rendered
    assert "Impostazioni" in rendered
    assert "No destination" not in rendered
    if destination == "":
        assert "rifiutat" in rendered and "Telefono casa" not in rendered
