"""Physical inventory uses native HA subentries and existing SIP account owners."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.config_entries import ConfigSubentry, SOURCE_IGNORE, SOURCE_INTEGRATION_DISCOVERY, SOURCE_USER
from homeassistant.data_entry_flow import InvalidData
import pytest
import voluptuous as vol

from .test_managed_phones_ha import (
    custom_integration_path as custom_integration_path,
    phone_lab as phone_lab,
)

pytestmark = pytest.mark.ha
PIN, NEW_PIN = "a" * 40, "b" * 40
PROFILE = "fanvil/x3u-pro"


class AssetCatalog:
    """Recorded catalog capability data, without downloads or vendor actions."""

    def __init__(self):
        # Projection limits copied from the maintained X3U Pro/H2U/X3U assets.
        # The flow consumes catalog data; it never executes bundle code.
        functions = ["line", "speed_dial", "blf", "queue_login", "queue_pause", "park"]
        caps = {"max_lines": 6, "key_slots": [{"slot": str(slot), "max_value_bytes": 31,
                 "max_label_bytes": 16, "functions": functions} for slot in range(1, 4)],
                "functions": [*functions, "sip_account", "ldap", "language"],
                "languages": {"en": "en", "it": "it"}}
        self.profiles = {}
        for model in ("x3u-pro", "h2u-v2", "x3u"):
            profile = {"id": "fanvil/" + model, "vendor": "fanvil", "model": model,
                       "status": "software_validated", "capabilities": deepcopy(caps)}
            if model == "h2u-v2":
                profile["capabilities"].update(max_lines=1, key_slots=caps["key_slots"][:1])
            if model == "x3u":
                profile["capabilities"].update(max_lines=1, key_slots=[], languages={})
            self.profiles[profile["id"]] = (profile, {})
        self.empty_language = "fanvil/x3u"
        self.manifest = {"profiles": [data[0] for data in self.profiles.values()]}
        self.commit = PIN
        self.calls = []

    async def async_manifest(self, *, refresh=False, commit=""):
        self.calls.append(("manifest", commit, refresh))
        if refresh:
            self.commit = NEW_PIN
        return deepcopy(self.manifest)

    async def async_profile(self, identity, *, commit=""):
        self.calls.append(("profile", identity, commit))
        if identity not in self.profiles:
            raise ValueError("phone_profile_not_ready")
        return deepcopy(self.profiles[identity])


async def test_account_binding_validation_overlays_existing_account_draft(physical_flow):
    from custom_components.voip_stack.physical_phone import (
        normalize_physical_phone, async_prepare_account_bindings, account_physical_bindings,
    )
    from custom_components.voip_stack.phone_config import phone_subentry_by_endpoint_id, phone_subentry_data

    state = physical_flow
    data = normalize_physical_phone({**identity(), **bindings(), "catalog_commit": PIN})
    saved = ConfigSubentry(subentry_type="physical_phone", title=data["name"], data=data,
        unique_id="physical_phone:" + data["phone_id"])
    state.lab.hass.config_entries.async_add_subentry(state.lab.entry, saved)
    state.lab.runtime.physical_phones = SimpleNamespace(catalog=state.catalog)
    original = phone_subentry_by_endpoint_id(state.lab.entry, "sip:front")
    proposal = {**phone_subentry_data(original), "name": "Renamed front"}
    before = dict(saved.data)
    prepared = await async_prepare_account_bindings(state.lab.hass, state.lab.entry, "sip:front",
        account_physical_bindings(state.lab.entry, "sip:front"), account_data=proposal)
    assert prepared[0][1]["lines"] == before["lines"]
    assert dict(saved.data) == before and original.data["name"] == "Front desk"


@pytest.fixture
def physical_flow(phone_lab, monkeypatch):
    from custom_components.voip_stack import physical_phone_flow
    from custom_components.voip_stack.phone_config import (
        _add_phone_subentry, sip_account_phone_data, sync_registry_from_entry,
    )

    lab = phone_lab
    from .support.ha_frontend import prepare_frontend_dependency
    lab.hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    prepare_frontend_dependency(lab.hass)
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        "phone_provisioning": {"enabled": True}})
    for identity, name, extension in [("sip:front", "Front desk", "481"), ("sip:office", "Office", "482")]:
        data = sip_account_phone_data({"username": extension, "password": "fixture-owner-secret",
                                      "display_name": name, "extension": extension}, endpoint_id=identity)
        _add_phone_subentry(lab.hass, lab.entry, data=data, title=name)
    sync_registry_from_entry(lab.hass, lab.entry)
    catalog = AssetCatalog()
    monkeypatch.setattr(physical_phone_flow, "PhoneProfileCatalog", lambda _hass: catalog)
    monkeypatch.setattr("custom_components.voip_stack.phone_profile_catalog.PhoneProfileCatalog", lambda _hass: catalog)
    reload_entry = AsyncMock()
    monkeypatch.setattr(lab.hass.config_entries, "async_reload", reload_entry)
    return SimpleNamespace(lab=lab, catalog=catalog, reload=reload_entry)


async def start(state, *, data=None):
    return await state.lab.hass.config_entries.subentries.async_init(
        (state.lab.entry.entry_id, "physical_phone"), context={"source": SOURCE_USER}, data=data)


async def submit(state, result, data):
    if result.get("step_id") in {"bindings", "phone_bindings"}:
        # Existing scenarios describe canonical records; exercise the actual UI wire values.
        data = deepcopy(data)
        fixture_choices = {"sip:front": "Front desk (481)", "sip:office": "Office (482)",
                           "__create_account__": "Create SIP account"}
        for line in data.get("lines", []):
            line["endpoint_id"] = fixture_choices.get(line.get("endpoint_id"), line.get("endpoint_id"))
    if result.get("step_id") in {"user", "phone_user"}:
        data = dict(data)
        advanced = dict(data.get("advanced") or {})
        for key in ("area_id", "person", "follow_ha_language", "enabled"):
            if key in data:
                advanced[key] = data.pop(key)
        if advanced:
            data["advanced"] = advanced
    return await state.lab.hass.config_entries.subentries.async_configure(result["flow_id"], data)


def physical_phone_data(record):
    from custom_components.voip_stack.physical_phone import physical_phone_data as data
    return data(record)


def physical_record(state):
    return next(item for item in state.lab.entry.subentries.values() if item.subentry_type == "physical_phone")


async def reconfigure(state, step, record=None):
    item = record or physical_record(state)
    menu = await state.lab.hass.config_entries.subentries.async_init(
        (state.lab.entry.entry_id, "physical_phone"),
        context={"source": "reconfigure", "subentry_id": item.subentry_id})
    assert menu["type"] == "menu" and step in menu["menu_options"]
    return await submit(state, menu, {"next_step_id": step})


async def binding_editor(state):
    created = await submit(state, await start(state), identity())
    assert created["type"] == "abort" and created["reason"] == "phone_configured"
    return await reconfigure(state, "bindings")


def identity(**values):
    return {"name": "Desk phone", "mac": "00:11:22:33:44:55", "profile_id": PROFILE,
            "follow_ha_language": True, "enabled": True, **values}


def bindings(**values):
    return {"lines": [{"slot": "1", "endpoint_id": "sip:front", "transport": "udp"}],
            "keys": [{"slot": "1", "function": "blf", "line": "1", "target": "sip:office", "label": ""}],
            **values}


async def test_manual_editor_has_native_multiple_objects_and_creates_only_inventory(physical_flow):
    state = physical_flow
    before = {endpoint.endpoint_id for endpoint in state.lab.runtime.endpoints.endpoints}
    form = await start(state)
    assert form["type"] == "form" and form["step_id"] == "user"
    created = await submit(state, form, identity())
    assert created["type"] == "abort" and created["reason"] == "phone_configured"
    form = await reconfigure(state, "bindings")
    selectors = {key.schema: value for key, value in form["data_schema"].schema.items()}
    for name in ("lines", "keys"):
        config = selectors[name].serialize()["selector"]["object"]
        assert config["multiple"] is True and config["fields"]
    assert "fixture-owner-secret" not in repr(form)
    result = await submit(state, form, bindings(lines=[
        {"slot": "2", "endpoint_id": "sip:office", "transport": "tcp"},
        {"slot": "1", "endpoint_id": "sip:front", "transport": "udp"},
    ]))
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    data = physical_phone_data(physical_record(state))
    assert [line["slot"] for line in data["lines"]] == ["2", "1"]
    assert data["mac"] == "001122334455" and data["catalog_commit"] == PIN
    assert data["follow_ha_language"] is True and data["language"] == "en"
    assert data["keys"][0]["target"] == "sip:office"
    assert len(data["provisioning_token"]) >= 32
    assert {endpoint.endpoint_id for endpoint in state.lab.runtime.endpoints.endpoints} == before
    state.reload.assert_not_awaited()


async def test_editor_selects_external_contact_created_through_the_contact_flow(physical_flow):
    state = physical_flow
    created = await state.lab.hass.config_entries.subentries.async_init(
        (state.lab.entry.entry_id, "contact"), context={"source": SOURCE_USER})
    created = await submit(state, created, {"type": "contact"})
    created = await submit(state, created, {"name": "External client", "number": "003905512345"})
    assert created["type"] == "create_entry"
    reference = "contact:" + created["data"]["id"]
    form = await binding_editor(state)
    selectors = {key.schema: value for key, value in form["data_schema"].schema.items()}
    options = selectors["keys"].serialize()["selector"]["object"]["fields"]["target"]["selector"]["select"]["options"]
    assert {"value": reference, "label": "External client"} in options
    complete = await submit(state, form, bindings(keys=[{"slot": "1", "line": "1",
        "function": "speed_dial", "target": reference, "label": ""}]))
    assert complete["type"] == "abort" and complete["reason"] == "reconfigure_successful"
    assert physical_phone_data(physical_record(state))["keys"][0]["target"] == reference


@pytest.mark.parametrize("kind,capability", [("keys", "clear_keys"), ("lines", "disable_lines")])
async def test_removal_is_saved_only_when_the_selected_profile_can_clear_it(physical_flow, kind, capability):
    state = physical_flow
    initial = bindings(lines=[{"slot": "1", "endpoint_id": "sip:front", "transport": "udp"},
                              {"slot": "2", "endpoint_id": "sip:office", "transport": "udp"}])
    form = await binding_editor(state)
    result = await submit(state, form, initial)
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    item = physical_record(state)
    replacement = deepcopy(initial)
    replacement[kind] = [] if kind == "keys" else initial["lines"][:1]
    for supported in (False, True):
        state.catalog.profiles[PROFILE][0]["capabilities"][capability] = supported
        form = await state.lab.hass.config_entries.subentries.async_init(
            (state.lab.entry.entry_id, "physical_phone"),
            context={"source": "reconfigure", "subentry_id": item.subentry_id})
        form = await submit(state, form, {"next_step_id": "user"})
        properties = await submit(state, form, identity(update_profile=supported))
        assert properties["type"] == "abort"
        form = await reconfigure(state, "bindings", item)
        updated = await submit(state, form, replacement)
        if supported:
            assert updated["type"] == "abort" and updated["reason"] == "reconfigure_successful"
            assert state.lab.entry.subentries[item.subentry_id].data[kind] == replacement[kind]
        else:
            assert updated["type"] == "form"
            assert updated["errors"]["base"] == "phone_" + capability + "_unsupported"
            assert state.lab.entry.subentries[item.subentry_id].data[kind] == initial[kind]
            state.lab.hass.config_entries.subentries.async_abort(updated["flow_id"])


async def test_managed_lines_reject_tls_without_a_local_tls_listener(physical_flow):
    from custom_components.voip_stack.physical_phone import normalize_physical_phone
    from custom_components.voip_stack.physical_phone_flow import PhysicalPhoneSubentryFlowHandler
    from custom_components.voip_stack.physical_phone_provisioning import PhysicalPhoneProvisioner
    from custom_components.voip_stack.sip_registrar import SipAccount

    state = physical_flow
    form = await binding_editor(state)
    tls_bindings = bindings(lines=[{"slot": "1", "endpoint_id": "sip:front", "transport": "tls"}])
    with pytest.raises(InvalidData):
        await submit(state, form, tls_bindings)
    selectors = {key.schema: value for key, value in form["data_schema"].schema.items()}
    transport = selectors["lines"].serialize()["selector"]["object"]["fields"]["transport"]
    assert transport["selector"]["select"]["options"] == ["udp", "tcp"]
    with pytest.raises(ValueError, match="invalid_phone_transport"):
        PhysicalPhoneSubentryFlowHandler()._canonical_bindings(tls_bindings)
    assert physical_phone_data(physical_record(state))["lines"] == []

    # Stored/imported data must pass the same guard before generating credentials.
    data = normalize_physical_phone({**identity(), **tls_bindings})
    endpoint = state.lab.runtime.endpoints.get("sip:front")
    state.lab.registrar.update_accounts([SipAccount(endpoint.username, endpoint.name, "fixture-secret")])
    manager = PhysicalPhoneProvisioner(state.lab.hass, state.lab.entry)
    try:
        with pytest.raises(ValueError, match="invalid_phone_transport"):
            await manager.async_context(data, state.catalog.profiles[PROFILE][0])
    finally:
        await manager.async_close()


async def test_discovery_source_user_data_is_initialization_and_token_stays_private(physical_flow):
    state = physical_flow
    form = await start(state, data={"discovery_candidate": {
        "phone_id": "discovered-id", "mac": "00:11:22:33:44:55", "host": "192.0.2.10",
        "vendor": "Fanvil", "model": "X3U Pro", "provisioning_token": "private-discovery-token",
    }})
    assert form["type"] == "form" and form["step_id"] == "user"
    assert "private-discovery-token" not in repr(form)
    form = await submit(state, form, form["data_schema"]({}))
    result = form
    assert result["type"] == "abort" and result["reason"] == "phone_configured"
    saved = physical_phone_data(physical_record(state))
    assert saved["phone_id"] == physical_record(state).subentry_id
    assert saved["phone_id"] != "discovered-id"
    assert saved["provisioning_token"] == "private-discovery-token"
    assert saved["host"] == ""
    assert not any(call[0] == "manifest" and call[2] for call in state.catalog.calls)


@pytest.mark.parametrize("bad", [
    {"lines": [{"slot": "1", "endpoint_id": "sip:front"}, {"slot": "1", "endpoint_id": "sip:office"}]},
    {"lines": [{"slot": "7", "endpoint_id": "sip:front"}]},
    {"lines": [{"slot": "0", "endpoint_id": "sip:front"}]},
    {"lines": [{"slot": "1", "endpoint_id": "browser:missing"}]},
    {"keys": [{"slot": "4", "function": "line", "line": "1"}]},
    {"keys": [{"slot": "1", "function": "line", "line": "2"}]},
    {"keys": [{"slot": "1", "function": "blf", "line": "1", "target": "missing"}]},
    {"keys": [{"slot": "1", "function": "speed_dial", "line": "1", "target": "1" * 32}]},
    {"keys": [{"slot": "1", "function": "line", "line": "1", "label": "x" * 17}]},
])
async def test_binding_validation_uses_profile_and_existing_owners(physical_flow, bad):
    state = physical_flow
    form = await binding_editor(state)
    try:
        result = await submit(state, form, bindings(**bad))
    except InvalidData:
        # Unsupported choices are rejected by the actual native selector schema.
        pass
    else:
        assert result["type"] == "form" and result["step_id"] == "bindings" and result["errors"]
    saved = physical_phone_data(physical_record(state))
    assert saved["lines"] == [] and saved["keys"] == []


async def test_queue_alias_is_saved_as_stable_queue_identity(physical_flow):
    state = physical_flow
    queue = SimpleNamespace(queue_id="stable-queue", name="Support", extension="700", members={"sip:front"})
    state.lab.runtime.queues = SimpleNamespace(resolve=lambda value: queue if value in {queue.queue_id, queue.name} else None)
    form = await binding_editor(state)
    result = await submit(state, form, bindings(keys=[{
        "slot": "2", "function": "queue_login", "line": "1", "target": "Support", "label": "",
    }]))
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    assert physical_phone_data(physical_record(state))["keys"][0]["target"] == queue.queue_id


async def test_language_override_uses_supported_bcp_choice(physical_flow):
    state = physical_flow
    form = await submit(state, await start(state), identity(follow_ha_language=False))
    assert form["step_id"] == "language"
    with pytest.raises(InvalidData):
        await submit(state, form, {"language": "not-a-supported-language"})
    form = await submit(state, form, {"language": "it"})
    assert form["type"] == "abort" and form["reason"] == "phone_configured"
    saved = physical_phone_data(physical_record(state))
    assert saved["language"] == "it" and saved["follow_ha_language"] is False


async def test_unsupported_ha_language_requests_an_explicit_override(physical_flow):
    state = physical_flow
    state.lab.hass.config.language = "nl"
    form = await submit(state, await start(state), identity())
    assert form["step_id"] == "language" and form["errors"]["base"] == "phone_language_unsupported"
    form = await submit(state, form, {"language": "it"})
    assert form["type"] == "abort" and form["reason"] == "phone_configured"
    assert physical_phone_data(physical_record(state))["follow_ha_language"] is False


async def test_profile_without_language_omits_language_only(physical_flow):
    state = physical_flow
    form = await submit(state, await start(state), identity(profile_id=state.catalog.empty_language))
    assert form["type"] == "abort" and form["reason"] == "phone_configured"
    saved = physical_phone_data(physical_record(state))
    assert saved["language"] == "" and saved["follow_ha_language"] is True
    form = await submit(state, await start(state), identity(mac="00:11:22:33:44:66",
        profile_id=state.catalog.empty_language, follow_ha_language=False))
    assert form["step_id"] == "user" and form["errors"]["base"] == "phone_language_unsupported"


@pytest.mark.parametrize("refresh", [False, True])
async def test_reconfigure_preserves_id_token_data_and_pin_unless_explicit(physical_flow, refresh):
    from custom_components.voip_stack.physical_phone import normalize_physical_phone
    state = physical_flow
    previous = normalize_physical_phone({**identity(), **bindings(), "catalog_commit": PIN})
    previous["future_option"] = "retained"
    item = ConfigSubentry(subentry_type="physical_phone", title=previous["name"], data=previous,
                          unique_id="physical_phone:" + previous["phone_id"])
    state.lab.hass.config_entries.async_add_subentry(state.lab.entry, item)
    state.catalog.commit = NEW_PIN
    form = await state.lab.hass.config_entries.subentries.async_init(
        (state.lab.entry.entry_id, "physical_phone"),
        context={"source": "reconfigure", "subentry_id": item.subentry_id})
    form = await submit(state, form, {"next_step_id": "user"})
    result = await submit(state, form, identity(name="Renamed", update_profile=refresh))
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    saved = physical_phone_data(state.lab.entry.subentries[item.subentry_id])
    assert "phone_id" not in state.lab.entry.subentries[item.subentry_id].data
    assert saved["phone_id"] == previous["phone_id"] and saved["provisioning_token"] == previous["provisioning_token"]
    assert saved["future_option"] == "retained" and saved["name"] == "Renamed"
    assert saved["catalog_commit"] == (NEW_PIN if refresh else PIN)
    state.reload.assert_not_awaited()


async def test_duplicate_mac_rejected_even_for_matching_discovery_identity(physical_flow):
    from custom_components.voip_stack.physical_phone import normalize_physical_phone
    state = physical_flow
    previous = normalize_physical_phone({**identity(), **bindings(), "catalog_commit": PIN})
    state.lab.hass.config_entries.async_add_subentry(state.lab.entry, ConfigSubentry(
        subentry_type="physical_phone", title=previous["name"], data=previous,
        unique_id="physical_phone:" + previous["phone_id"]))
    form = await start(state, data={"discovery_candidate": {"phone_id": previous["phone_id"],
        "mac": previous["mac"], "provisioning_token": "different-private-token"}})
    result = await submit(state, form, identity())
    assert result["errors"]["base"] == "phone_mac_exists"


async def test_assisted_account_stays_draft_until_final_inventory_validation(physical_flow):
    from custom_components.voip_stack.physical_phone_flow import CREATE_ACCOUNT
    state = physical_flow
    accounts_before = {item.subentry_id for item in state.lab.entry.subentries.values() if item.subentry_type == "phone"}
    form = await binding_editor(state)
    form = await submit(state, form, bindings(lines=[{"slot": "1", "endpoint_id": CREATE_ACCOUNT}], keys=[]))
    assert form["step_id"] == "account"
    rejected = await submit(state, form, {"name": "New account", "username": "new", "password": ""})
    assert rejected["errors"]["password"] == "phone_password_required"
    form = await submit(state, rejected, {"name": "New account", "username": "new", "password": "fixture-new-secret",
                                         "extension": "483", "enabled": True})
    assert form["step_id"] == "bindings"
    assert {item.subentry_id for item in state.lab.entry.subentries.values() if item.subentry_type == "phone"} == accounts_before
    values = form["data_schema"]({})
    assert "fixture-new-secret" not in repr(values)
    with pytest.raises(InvalidData):
        await submit(state, form, {**values, "keys": [{"slot": "99", "function": "line", "line": "1"}]})
    assert len([item for item in state.lab.entry.subentries.values() if item.subentry_type == "phone"]) == len(accounts_before)
    result = await submit(state, form, {**values, "keys": []})
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    from custom_components.voip_stack.phone_config import phone_subentry_data
    account = next(phone_subentry_data(item) for item in state.lab.entry.subentries.values()
                   if item.subentry_type == "phone" and item.data["username"] == "new")
    saved = physical_phone_data(physical_record(state))
    assert saved["lines"][0]["endpoint_id"] == account["endpoint_id"]
    assert account["password"] == "fixture-new-secret"
    assert "password" not in saved["lines"][0]


async def test_disabling_provisioning_mid_flow_aborts_without_persisting(physical_flow):
    state = physical_flow
    form = await binding_editor(state)
    state.lab.hass.config_entries.async_update_entry(state.lab.entry, data={**state.lab.entry.data,
        "phone_provisioning": {"enabled": False}})
    result = await submit(state, form, bindings())
    assert result["type"] == "abort" and result["reason"] == "phone_provisioning_disabled"


async def test_edit_preserves_valid_language_fallback_after_ha_locale_changes(physical_flow):
    from custom_components.voip_stack.physical_phone import normalize_physical_phone
    state = physical_flow
    previous = normalize_physical_phone({**identity(), **bindings(), "catalog_commit": PIN, "language": "it"})
    item = ConfigSubentry(subentry_type="physical_phone", title=previous["name"], data=previous,
                          unique_id="physical_phone:" + previous["phone_id"])
    state.lab.hass.config_entries.async_add_subentry(state.lab.entry, item)
    state.lab.hass.config.language = "nl"
    form = await state.lab.hass.config_entries.subentries.async_init(
        (state.lab.entry.entry_id, "physical_phone"),
        context={"source": "reconfigure", "subentry_id": item.subentry_id})
    form = await submit(state, form, {"next_step_id": "user"})
    result = await submit(state, form, identity(name="Renamed"))
    assert result["type"] == "abort"
    saved = state.lab.entry.subentries[item.subentry_id].data
    assert saved["language"] == "it" and saved["follow_ha_language"] is True


@pytest.mark.parametrize("status", ["protocol_verified", "hardware_qualified"])
async def test_verified_catalog_statuses_remain_selectable(physical_flow, status):
    state = physical_flow
    state.catalog.profiles[PROFILE][0]["status"] = status
    form = await submit(state, await start(state), identity())
    assert form["type"] == "abort" and form["reason"] == "phone_configured"
    assert physical_phone_data(physical_record(state))["profile_id"] == PROFILE


def cache_candidate(state, *, vendor="Fanvil", model="X3U Pro"):
    from custom_components.voip_stack.physical_phone_discovery import PhoneCandidate
    candidate = PhoneCandidate("", "001122334455", "192.0.2.10", vendor, model)
    state.lab.runtime.physical_phones = SimpleNamespace(catalog=state.catalog)
    state.lab.runtime.phone_discovery = SimpleNamespace(candidates={candidate.mac: candidate}, _closed=False)
    return candidate


async def discover(state, candidate):
    return await state.lab.hass.config_entries.flow.async_init("voip_stack",
        context={"source": SOURCE_INTEGRATION_DISCOVERY}, data={"discovery_candidate": candidate.public()})


async def test_main_native_discovery_reuses_editor_without_child_flow_or_second_pbx(physical_flow):
    state = physical_flow
    candidate = cache_candidate(state)
    assert "provisioning_token" not in candidate.public() and "token" not in candidate.public()
    result = await discover(state, candidate)
    assert result["type"] == "form" and result["step_id"] == "phone_discovered"
    assert not state.catalog.calls and candidate.native_subentry is None and not candidate.token
    result = await state.lab.hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "form" and result["step_id"] == "phone_user"
    from homeassistant.helpers.translation import async_get_translations

    for language in ("en", "it", "cs", "de", "pt-BR"):
        translations = await async_get_translations(state.lab.hass, language, "config", {"voip_stack"})
        description = translations["component.voip_stack.config.step.phone_user.description"]
        assert description.format_map(result.get("description_placeholders") or {})
    assert candidate.token not in repr(result)
    assert not state.lab.hass.config_entries.subentries.async_progress()
    created = await state.lab.hass.config_entries.flow.async_configure(result["flow_id"], result["data_schema"]({}))
    assert created["type"] == "abort" and created["reason"] == "phone_configured"
    physical = next(item for item in state.lab.entry.subentries.values() if item.subentry_type == "physical_phone")
    assert physical_phone_data(physical)["phone_id"] == candidate.phone_id
    assert physical.data["provisioning_token"] == candidate.token
    assert physical.data["lines"] == []
    assert len([entry for entry in state.lab.hass.config_entries.async_entries("voip_stack")
                if entry.source != SOURCE_IGNORE]) == 1


async def test_confirming_native_dhcp_card_arms_bounded_enrollment_for_that_phone(physical_flow):
    from homeassistant.config_entries import SOURCE_DHCP
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery
    from .test_physical_phone_dhcp_ha import HOST, MAC, info, native_cache

    state = physical_flow
    profile = {**deepcopy(state.catalog.profiles[PROFILE][0]), "id": "snom/360", "vendor": "snom", "model": "360"}
    state.catalog.profiles[profile["id"]] = (profile, {})
    state.catalog.manifest["profiles"].append(profile)
    lab = state.lab
    lab.runtime.phone_discovery = PhoneDiscovery(lab.hass, lab.entry)
    watcher = native_cache(lab)
    watcher.async_process_client(HOST, "snom360", MAC)
    flow = lab.hass.config_entries.flow
    try:
        preview = await flow.async_init("voip_stack", context={"source": SOURCE_DHCP}, data=info())
        assert preview["step_id"] == "phone_discovered"
        editor = await flow.async_configure(preview["flow_id"], {})
        assert editor["step_id"] == "phone_user"
        assert all(s.subentry_type != "physical_phone" for s in lab.entry.subentries.values())
        created = await flow.async_configure(editor["flow_id"], editor["data_schema"]({}))
        assert created["reason"] == "phone_configured"
        saved = physical_record(state)
        assert saved.data["pnp_enrollment"]["host"] == HOST
        assert saved.data["host"] == "" and saved.data["profile_id"] == "snom/360"
    finally:
        await lab.runtime.phone_discovery.async_close()
        lab.runtime.phone_discovery = None


@pytest.mark.parametrize("enabled", [False, True])
async def test_discovery_preview_is_informative_without_catalog_or_credentials(physical_flow, enabled):
    state = physical_flow
    state.lab.hass.config_entries.async_update_entry(state.lab.entry, data={**state.lab.entry.data,
        "phone_provisioning": {"enabled": enabled}})
    candidate = cache_candidate(state, vendor="snom", model="snom360")
    before = set(state.lab.entry.subentries)
    preview = await discover(state, candidate)
    assert preview["type"] == "form" and preview["step_id"] == "phone_discovered"
    assert preview["data_schema"]({}) == {} and not preview["errors"]
    assert preview["description_placeholders"] == {
        "name": "Snom 360", "mac": "00:11:22:33:44:55", "host": "192.0.2.10",
        "vendor": "snom", "model": "snom360"}
    pending = state.lab.hass.config_entries.flow.async_get(preview["flow_id"])
    assert pending["context"]["title_placeholders"] == {"name": "Snom 360"}
    assert candidate.native_subentry is None and not candidate.phone_id and not candidate.token
    assert not state.catalog.calls and set(state.lab.entry.subentries) == before
    assert state.lab.entry.data["phone_provisioning"]["enabled"] is enabled
    assert not state.lab.hass.config_entries.subentries.async_progress()


async def test_disabled_preview_can_continue_after_separate_provisioning_opt_in(physical_flow):
    state = physical_flow
    state.lab.hass.config_entries.async_update_entry(state.lab.entry, data={**state.lab.entry.data,
        "phone_provisioning": {"enabled": False}})
    candidate = cache_candidate(state)
    preview = await discover(state, candidate)
    blocked = await state.lab.hass.config_entries.flow.async_configure(preview["flow_id"], {})
    assert blocked["type"] == "form" and blocked["step_id"] == "phone_discovered"
    assert blocked["errors"] == {"base": "phone_provisioning_disabled"}
    assert not state.lab.entry.data["phone_provisioning"]["enabled"]
    assert candidate.native_subentry is None and not state.catalog.calls
    state.lab.hass.config_entries.async_update_entry(state.lab.entry, data={**state.lab.entry.data,
        "phone_provisioning": {"enabled": True}})
    editor = await state.lab.hass.config_entries.flow.async_configure(blocked["flow_id"], {})
    assert editor["type"] == "form" and editor["step_id"] == "phone_user"
    assert candidate.native_subentry is not None and candidate.token
    assert state.catalog.calls == [("manifest", "", False)]
    assert candidate.token not in repr(editor)


@pytest.mark.parametrize("change", ["disabled", "removed", "expired", "replaced"])
async def test_discovery_rechecks_live_candidate_before_entering_editor(physical_flow, change):
    state = physical_flow
    candidate = cache_candidate(state)
    before = set(state.lab.entry.subentries)
    preview = await discover(state, candidate)
    if change == "disabled":
        state.lab.hass.config_entries.async_update_entry(state.lab.entry, data={**state.lab.entry.data,
            "phone_provisioning": {"enabled": True, "discovery_enabled": False}})
    elif change == "removed":
        state.lab.runtime.phone_discovery.candidates.clear()
    elif change == "expired":
        candidate.seen -= 86400
    else:
        state.lab.runtime.phone_discovery = None
    result = await state.lab.hass.config_entries.flow.async_configure(preview["flow_id"], {})
    assert result["type"] == "abort" and result["reason"] == "phone_discovery_expired"
    assert candidate.native_subentry is None and not state.catalog.calls
    assert set(state.lab.entry.subentries) == before


@pytest.mark.parametrize("change", ["disabled", "removed", "provisioning_disabled", "changed_mac"])
async def test_discovery_editor_rechecks_candidate_before_saving(physical_flow, change):
    state = physical_flow
    candidate = cache_candidate(state)
    before = set(state.lab.entry.subentries)
    preview = await discover(state, candidate)
    editor = await state.lab.hass.config_entries.flow.async_configure(preview["flow_id"], {})
    values = editor["data_schema"]({})
    reason = "phone_discovery_expired"
    if change == "disabled":
        state.lab.hass.config_entries.async_update_entry(state.lab.entry, data={**state.lab.entry.data,
            "phone_provisioning": {"enabled": True, "discovery_enabled": False}})
    elif change == "removed":
        state.lab.runtime.phone_discovery.candidates.clear()
    elif change == "provisioning_disabled":
        state.lab.hass.config_entries.async_update_entry(state.lab.entry, data={**state.lab.entry.data,
            "phone_provisioning": {"enabled": False}})
        reason = "phone_provisioning_disabled"
    else:
        values["mac"] = "00:11:22:33:44:66"
        reason = "invalid_phone_discovery"
    result = await state.lab.hass.config_entries.flow.async_configure(editor["flow_id"], values)
    assert result["type"] == "abort" and result["reason"] == reason
    assert set(state.lab.entry.subentries) == before
    assert not any(call[0] == "profile" for call in state.catalog.calls)


async def test_discovery_candidate_removed_during_catalog_await_is_not_committed(physical_flow, monkeypatch):
    state = physical_flow
    candidate = cache_candidate(state)
    before = set(state.lab.entry.subentries)
    preview = await discover(state, candidate)
    editor = await state.lab.hass.config_entries.flow.async_configure(preview["flow_id"], {})
    original = state.catalog.async_profile
    async def load_then_expire(*args, **kwargs):
        result = await original(*args, **kwargs)
        state.lab.runtime.phone_discovery.candidates.clear()
        return result
    monkeypatch.setattr(state.catalog, "async_profile", load_then_expire)
    result = await state.lab.hass.config_entries.flow.async_configure(editor["flow_id"], editor["data_schema"]({}))
    assert result["type"] == "abort" and result["reason"] == "phone_discovery_expired"
    assert set(state.lab.entry.subentries) == before


@pytest.mark.parametrize("override", ["", "desk-admin.local"])
async def test_manual_matching_mac_keeps_native_identity_and_only_stores_explicit_host(physical_flow, override):
    state = physical_flow
    candidate = cache_candidate(state)
    form = await start(state)
    assert candidate.native_subentry is None and not candidate.phone_id and not candidate.token
    result = await submit(state, form, identity(advanced={"host": override}))
    assert result["type"] == "abort" and result["reason"] == "phone_configured"
    saved = physical_record(state)
    assert candidate.native_subentry is not None and saved.subentry_id == candidate.phone_id
    assert saved.data["provisioning_token"] == candidate.token and saved.data["host"] == override


@pytest.mark.parametrize(("vendor", "model", "matches"), [
    ("snom", "snom360", True), ("Snom", "360", True),
    ("snom", "snom3600", False), ("snom", "snomD360", False), ("other", "360", False),
])
async def test_discovery_model_preselection_requires_exact_verified_vendor_and_model(
    physical_flow, vendor, model, matches
):
    state = physical_flow
    profile = {**deepcopy(state.catalog.profiles[PROFILE][0]), "id": "snom/360", "vendor": "snom", "model": "360"}
    state.catalog.profiles[profile["id"]] = (profile, {})
    state.catalog.manifest["profiles"].append(profile)
    candidate = cache_candidate(state, vendor=vendor, model=model)
    preview = await discover(state, candidate)
    editor = await state.lab.hass.config_entries.flow.async_configure(preview["flow_id"], {})
    profile_field = next(key for key in editor["data_schema"].schema if key.schema == "profile_id")
    selector = editor["data_schema"].schema[profile_field]
    assert {"value": "snom/360", "label": "Snom 360"} in selector.config["options"]
    if matches:
        assert editor["data_schema"]({})["profile_id"] == "snom/360"
        assert editor["data_schema"]({})["name"] == "Snom 360"
    else:
        assert profile_field.default is vol.UNDEFINED
    assert state.catalog.calls == [("manifest", "", False)]


async def test_native_ignore_keeps_inventory_and_accounts_unmodified(physical_flow):
    state = physical_flow
    candidate = cache_candidate(state)
    before = set(state.lab.entry.subentries)
    discovered = await state.lab.hass.config_entries.flow.async_init("voip_stack",
        context={"source": SOURCE_INTEGRATION_DISCOVERY}, data={"discovery_candidate": candidate.public()})
    assert discovered["type"] == "form"
    ignored = await state.lab.hass.config_entries.flow.async_init("voip_stack", context={"source": SOURCE_IGNORE},
        data={"unique_id": "physical_phone:" + candidate.mac, "title": "Discovered phone"})
    assert ignored["type"] == "create_entry" and ignored["result"].source == SOURCE_IGNORE
    result = await state.lab.hass.config_entries.flow.async_init("voip_stack",
        context={"source": SOURCE_INTEGRATION_DISCOVERY}, data={"discovery_candidate": candidate.public()})
    assert result["type"] == "abort" and result["reason"] == "already_configured"
    assert set(state.lab.entry.subentries) == before
    assert not state.catalog.calls and candidate.native_subentry is None and not candidate.token


async def test_main_opt_out_and_provisioning_error_have_no_catalog_io_or_reload(physical_flow):
    from custom_components.voip_stack.config_flow import VoipStackConfigFlow
    state = physical_flow
    state.lab.hass.config_entries.async_update_entry(state.lab.entry, data={**state.lab.entry.data,
        "phone_provisioning": {"enabled": False}})
    assert "physical_phone" not in VoipStackConfigFlow.async_get_supported_subentry_types(state.lab.entry)
    form = await state.lab.hass.config_entries.flow.async_init("voip_stack",
        context={"source": "reconfigure", "entry_id": state.lab.entry.entry_id})
    assert "phone_provisioning" in form["menu_options"]
    form = await state.lab.hass.config_entries.flow.async_configure(form["flow_id"], {"next_step_id": "phone_provisioning"})
    assert form["step_id"] == "phone_provisioning" and not state.catalog.calls
    rejected = await state.lab.hass.config_entries.flow.async_configure(form["flow_id"], {
        "enabled": True, "base_url": "http://ha.example.test", "allow_insecure_http": False})
    assert rejected["errors"]["base"] == "phone_provisioning_url_invalid" and not state.catalog.calls
    result = await state.lab.hass.config_entries.flow.async_configure(rejected["flow_id"], {
        "enabled": False, "base_url": "", "allow_insecure_http": False})
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    assert not state.catalog.calls
    state.reload.assert_not_awaited()


@pytest.mark.parametrize("enabled", [False, True])
async def test_discovery_preference_only_does_not_validate_url_or_load_catalog(physical_flow, monkeypatch, enabled):
    state = physical_flow
    config = {"enabled": enabled, "base_url": "", "allow_insecure_http": False}
    state.lab.hass.config_entries.async_update_entry(state.lab.entry, data={**state.lab.entry.data,
        "phone_provisioning": config})
    validate_url = Mock(side_effect=AssertionError("discovery must not validate a provisioning URL"))
    monkeypatch.setattr("custom_components.voip_stack.physical_phone_provisioning.provisioning_base_url", validate_url)
    form = await state.lab.hass.config_entries.flow.async_init("voip_stack",
        context={"source": "reconfigure", "entry_id": state.lab.entry.entry_id})
    form = await state.lab.hass.config_entries.flow.async_configure(form["flow_id"],
        {"next_step_id": "phone_provisioning"})
    values = form["data_schema"]({})
    assert values["discovery_enabled"] is True
    result = await state.lab.hass.config_entries.flow.async_configure(form["flow_id"],
        {**values, "discovery_enabled": False})
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    assert state.lab.entry.data["phone_provisioning"] == {**config, "discovery_enabled": False}
    assert not state.catalog.calls
    validate_url.assert_not_called()


async def test_main_enable_reports_catalog_failure_without_persisting(physical_flow, monkeypatch):
    state = physical_flow
    monkeypatch.setattr(state.catalog, "async_manifest", AsyncMock(side_effect=ValueError("invalid_fixture_catalog")))
    before = dict(state.lab.entry.data)
    form = await state.lab.hass.config_entries.flow.async_init("voip_stack",
        context={"source": "reconfigure", "entry_id": state.lab.entry.entry_id})
    form = await state.lab.hass.config_entries.flow.async_configure(form["flow_id"], {"next_step_id": "phone_provisioning"})
    result = await state.lab.hass.config_entries.flow.async_configure(form["flow_id"], {
        "enabled": True, "base_url": "https://ha.example.test", "allow_insecure_http": False})
    assert result["type"] == "form" and result["errors"]["base"] == "phone_catalog_unavailable"
    assert dict(state.lab.entry.data) == before


@pytest.mark.parametrize(("base_url", "allow_http"), [
    ("", True), ("https://phone-access.example.test", False),
])
async def test_main_enable_resolves_provisional_url_before_catalog_and_accepts_correction(
    physical_flow, base_url, allow_http
):
    """The actual HA URL owner, rather than saved settings, validates the draft."""
    state = physical_flow
    state.lab.hass.config.internal_url = "http://ha.example.test:8123"
    state.lab.hass.config.external_url = None
    state.lab.hass.config.api = None
    state.lab.hass.config_entries.async_update_entry(state.lab.entry, data={**state.lab.entry.data,
        "phone_provisioning": {"enabled": False, "base_url": "https://previous.example.test"}})
    before = deepcopy(dict(state.lab.entry.data))
    form = await state.lab.hass.config_entries.flow.async_init("voip_stack",
        context={"source": "reconfigure", "entry_id": state.lab.entry.entry_id})
    form = await state.lab.hass.config_entries.flow.async_configure(form["flow_id"],
        {"next_step_id": "phone_provisioning"})
    rejected = await state.lab.hass.config_entries.flow.async_configure(form["flow_id"], {
        "enabled": True, "base_url": "", "allow_insecure_http": False})
    assert rejected["type"] == "form"
    assert rejected["errors"] == {"base": "phone_provisioning_url_unavailable"}
    assert dict(state.lab.entry.data) == before and not state.catalog.calls
    corrected = await state.lab.hass.config_entries.flow.async_configure(rejected["flow_id"], {
        "enabled": True, "base_url": base_url, "allow_insecure_http": allow_http})
    assert corrected["type"] == "abort" and corrected["reason"] == "reconfigure_successful"
    assert state.lab.entry.data["phone_provisioning"] == {
        "enabled": True, "discovery_enabled": True, "base_url": base_url, "allow_insecure_http": allow_http}
    assert state.catalog.calls == [("manifest", "", False)]
    state.reload.assert_not_awaited()


@pytest.mark.parametrize("url", [
    "https://ha.example.test:invalid", "https://[broken", "https://ha.example.test:0",
    "https://private-user:private-password@ha.example.test", "https://ha.example.test/?private=query",
])
async def test_main_enable_rejects_malformed_url_without_disclosing_it_or_loading_catalog(physical_flow, url):
    state = physical_flow
    before = deepcopy(dict(state.lab.entry.data))
    form = await state.lab.hass.config_entries.flow.async_init("voip_stack",
        context={"source": "reconfigure", "entry_id": state.lab.entry.entry_id})
    form = await state.lab.hass.config_entries.flow.async_configure(form["flow_id"],
        {"next_step_id": "phone_provisioning"})
    result = await state.lab.hass.config_entries.flow.async_configure(form["flow_id"], {
        "enabled": True, "base_url": url, "allow_insecure_http": True})
    assert result["errors"] == {"base": "phone_provisioning_url_invalid"}
    assert not state.catalog.calls and dict(state.lab.entry.data) == before


async def test_main_discovery_requires_the_current_cached_candidate(physical_flow):
    state = physical_flow
    candidate = cache_candidate(state)
    state.lab.runtime.phone_discovery.candidates.clear()
    result = await state.lab.hass.config_entries.flow.async_init("voip_stack",
        context={"source": SOURCE_INTEGRATION_DISCOVERY}, data={"discovery_candidate": candidate.public()})
    assert result["type"] == "abort" and result["reason"] == "phone_discovery_expired"


async def test_main_user_still_enforces_one_pbx(physical_flow):
    state = physical_flow
    result = await state.lab.hass.config_entries.flow.async_init("voip_stack", context={"source": SOURCE_USER})
    assert result["type"] == "abort" and result["reason"] == "single_instance_allowed"
    assert len([entry for entry in state.lab.hass.config_entries.async_entries("voip_stack")
                if entry.source != SOURCE_IGNORE]) == 1


async def test_ignored_discovery_metadata_is_not_the_parent_pbx(physical_flow, monkeypatch):
    from custom_components.voip_stack.config_flow import VoipStackConfigFlow
    from pytest_homeassistant.common import MockConfigEntry
    state = physical_flow
    ignored = MockConfigEntry(domain="voip_stack", source=SOURCE_IGNORE,
                              unique_id="physical_phone:ignored", data={})
    ignored.add_to_hass(state.lab.hass)
    flow = VoipStackConfigFlow()
    flow.hass = state.lab.hass
    flow.context = {"source": SOURCE_USER}
    # Both are actual HA entries; force metadata-first enumeration to exercise
    # the integration's parent-selection predicate independently of insertion order.
    monkeypatch.setattr(flow, "_async_current_entries", lambda: [ignored, state.lab.entry])
    assert flow._current_entry() is state.lab.entry


async def test_main_adapter_language_and_account_keep_one_editor_draft(physical_flow):
    from custom_components.voip_stack.physical_phone_flow import CREATE_ACCOUNT
    from custom_components.voip_stack.phone_config import phone_subentry_data
    state = physical_flow
    candidate = cache_candidate(state)
    form = await state.lab.hass.config_entries.flow.async_init("voip_stack",
        context={"source": SOURCE_INTEGRATION_DISCOVERY}, data={"discovery_candidate": candidate.public()})
    assert form["step_id"] == "phone_discovered"
    form = await state.lab.hass.config_entries.flow.async_configure(form["flow_id"], {})
    assert form["step_id"] == "phone_user"
    form = await state.lab.hass.config_entries.flow.async_configure(form["flow_id"],
        form["data_schema"]({"advanced": {"follow_ha_language": False}}))
    assert form["step_id"] == "phone_language"
    result = await state.lab.hass.config_entries.flow.async_configure(form["flow_id"], {"language": "it"})
    assert result["type"] == "abort" and result["reason"] == "phone_configured"
    physical = physical_phone_data(physical_record(state))
    assert physical["phone_id"] == candidate.phone_id and physical["provisioning_token"] == candidate.token
    assert physical["language"] == "it" and physical["follow_ha_language"] is False

    # Adoption is finished. Advanced line/account editing keeps its own staged
    # account until the same physical inventory passes final validation.
    form = await reconfigure(state, "bindings")
    account_ids = {item.subentry_id for item in state.lab.entry.subentries.values() if item.subentry_type == "phone"}
    form = await submit(state, form, {"lines": [{"slot": "1", "endpoint_id": CREATE_ACCOUNT}], "keys": []})
    assert form["step_id"] == "account"
    form = await submit(state, form, {"name": "Discovery line", "username": "discovery-line", "password": "fixture-draft-secret",
                "extension": "484", "enabled": True})
    assert form["step_id"] == "bindings"
    assert {item.subentry_id for item in state.lab.entry.subentries.values() if item.subentry_type == "phone"} == account_ids
    result = await submit(state, form, form["data_schema"]({}))
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    physical = physical_phone_data(physical_record(state))
    account = next(phone_subentry_data(item) for item in state.lab.entry.subentries.values()
                   if item.subentry_type == "phone" and item.data["username"] == "discovery-line")
    assert physical["phone_id"] == candidate.phone_id and physical["provisioning_token"] == candidate.token
    assert physical["lines"][0]["endpoint_id"] == account["endpoint_id"]
    assert not state.lab.hass.config_entries.subentries.async_progress()
    state.reload.assert_not_awaited()


async def test_line_account_form_roundtrip_uses_readable_choices_and_saves_only_canonical_ids(physical_flow):
    state = physical_flow
    initial = await start(state)
    profile_selector = next(value for key, value in initial['data_schema'].schema.items() if key.schema == 'profile_id')
    assert {'value': PROFILE, 'label': 'Fanvil X3U Pro'} in profile_selector.config['options']
    await submit(state, initial, identity())
    form = await reconfigure(state, 'bindings')
    line_selector = next(value for key, value in form['data_schema'].schema.items() if key.schema == 'lines')
    choices = line_selector.config['fields']['endpoint_id']['selector'].config['options']
    values = [choice['value'] for choice in choices]
    assert 'Front desk (481)' in values and 'Office (482)' in values
    assert len(values) == len(set(values))
    assert not {'sip:front', 'sip:office'} & set(values)
    # This is exactly the updated value returned by HA's nested object edit dialog.
    await submit(state, form, {'lines': [{'slot': '1', 'endpoint_id': 'Office (482)', 'transport': 'udp'}], 'keys': []})
    record = physical_record(state)
    assert record.data['lines'] == [{'slot': '1', 'endpoint_id': 'sip:office', 'transport': 'udp'}]
    assert record.data['catalog_commit'] == PIN
    form = await reconfigure(state, 'bindings')
    values = form['data_schema']({})
    assert values['lines'][0]['endpoint_id'] == 'Office (482)'
    values['lines'][0]['endpoint_id'] = 'Front desk (481)'
    await submit(state, form, values)
    assert physical_record(state).data['lines'][0]['endpoint_id'] == 'sip:front'
    state.reload.assert_not_awaited()


async def test_unmapped_line_account_form_value_is_rejected_without_saving(physical_flow):
    state = physical_flow
    form = await binding_editor(state)
    before = deepcopy(dict(physical_record(state).data))
    try:
        result = await submit(state, form, {'lines': [{'slot': '1', 'endpoint_id': 'Unlisted (999)', 'transport': 'udp'}], 'keys': []})
    except InvalidData:
        pass
    else:
        assert result['type'] == 'form' and result['errors']['base'] == 'invalid_phone_bindings'
    assert physical_record(state).data == before
    state.reload.assert_not_awaited()


async def test_line_account_without_extension_uses_its_unique_username_in_the_form(physical_flow):
    from custom_components.voip_stack.phone_config import (
        _add_phone_subentry, sip_account_phone_data, sync_registry_from_entry,
    )
    state = physical_flow
    account = sip_account_phone_data({'username': 'guest-alpha', 'password': 'fixture-guest-secret',
        'display_name': 'Guest desk'}, endpoint_id='sip:guest')
    _add_phone_subentry(state.lab.hass, state.lab.entry, data=account, title='Guest desk')
    sync_registry_from_entry(state.lab.hass, state.lab.entry)
    form = await binding_editor(state)
    line_selector = next(value for key, value in form['data_schema'].schema.items() if key.schema == 'lines')
    choices = line_selector.config['fields']['endpoint_id']['selector'].config['options']
    assert {'value': 'Guest desk (guest-alpha)', 'label': 'Guest desk (guest-alpha)'} in choices
    result = await submit(state, form, {'lines': [{'slot': '1', 'endpoint_id': 'Guest desk (guest-alpha)'}], 'keys': []})
    assert result['type'] == 'abort' and result['reason'] == 'reconfigure_successful'
    assert physical_record(state).data['lines'][0]['endpoint_id'] == 'sip:guest'
    assert 'fixture-guest-secret' not in repr(form)
