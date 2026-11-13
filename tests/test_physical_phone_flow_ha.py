"""Physical inventory uses native HA subentries and existing SIP account owners."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.config_entries import ConfigSubentry, SOURCE_IGNORE, SOURCE_INTEGRATION_DISCOVERY, SOURCE_USER
from homeassistant.data_entry_flow import InvalidData
import pytest

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
    reload_entry = AsyncMock()
    monkeypatch.setattr(lab.hass.config_entries, "async_reload", reload_entry)
    return SimpleNamespace(lab=lab, catalog=catalog, reload=reload_entry)


async def start(state, *, data=None):
    return await state.lab.hass.config_entries.subentries.async_init(
        (state.lab.entry.entry_id, "physical_phone"), context={"source": SOURCE_USER}, data=data)


async def submit(state, result, data):
    return await state.lab.hass.config_entries.subentries.async_configure(result["flow_id"], data)


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
    form = await submit(state, form, identity())
    assert form["step_id"] == "bindings"
    selectors = {key.schema: value for key, value in form["data_schema"].schema.items()}
    for name in ("lines", "keys"):
        config = selectors[name].serialize()["selector"]["object"]
        assert config["multiple"] is True and config["fields"]
    assert "fixture-owner-secret" not in repr(form)
    result = await submit(state, form, bindings(lines=[
        {"slot": "2", "endpoint_id": "sip:office", "transport": "tcp"},
        {"slot": "1", "endpoint_id": "sip:front", "transport": "udp"},
    ]))
    assert result["type"] == "create_entry"
    data = result["data"]
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
    form = await submit(state, await start(state), identity())
    selectors = {key.schema: value for key, value in form["data_schema"].schema.items()}
    options = selectors["keys"].serialize()["selector"]["object"]["fields"]["target"]["selector"]["select"]["options"]
    assert {"value": reference, "label": "External client"} in options
    complete = await submit(state, form, bindings(keys=[{"slot": "1", "line": "1",
        "function": "speed_dial", "target": reference, "label": ""}]))
    assert complete["type"] == "create_entry"
    assert complete["data"]["keys"][0]["target"] == reference


@pytest.mark.parametrize("kind,capability", [("keys", "clear_keys"), ("lines", "disable_lines")])
async def test_removal_is_saved_only_when_the_selected_profile_can_clear_it(physical_flow, kind, capability):
    state = physical_flow
    initial = bindings(lines=[{"slot": "1", "endpoint_id": "sip:front", "transport": "udp"},
                              {"slot": "2", "endpoint_id": "sip:office", "transport": "udp"}])
    form = await submit(state, await start(state), identity())
    result = await submit(state, form, initial)
    item = next(row for row in state.lab.entry.subentries.values()
                if row.subentry_type == "physical_phone" and row.data["phone_id"] == result["data"]["phone_id"])
    replacement = deepcopy(initial)
    replacement[kind] = [] if kind == "keys" else initial["lines"][:1]
    for supported in (False, True):
        state.catalog.profiles[PROFILE][0]["capabilities"][capability] = supported
        form = await state.lab.hass.config_entries.subentries.async_init(
            (state.lab.entry.entry_id, "physical_phone"),
            context={"source": "reconfigure", "subentry_id": item.subentry_id})
        form = await submit(state, form, identity(update_profile=supported))
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
    form = await submit(state, await start(state), identity())
    tls_bindings = bindings(lines=[{"slot": "1", "endpoint_id": "sip:front", "transport": "tls"}])
    with pytest.raises(InvalidData):
        await submit(state, form, tls_bindings)
    selectors = {key.schema: value for key, value in form["data_schema"].schema.items()}
    transport = selectors["lines"].serialize()["selector"]["object"]["fields"]["transport"]
    assert transport["selector"]["select"]["options"] == ["udp", "tcp"]
    with pytest.raises(ValueError, match="invalid_phone_transport"):
        PhysicalPhoneSubentryFlowHandler()._canonical_bindings(tls_bindings)
    assert not any(item.subentry_type == "physical_phone" for item in state.lab.entry.subentries.values())

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
    result = await submit(state, form, bindings())
    assert result["data"]["phone_id"] == "discovered-id"
    assert result["data"]["provisioning_token"] == "private-discovery-token"
    assert result["data"]["host"] == "192.0.2.10"
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
    form = await submit(state, await start(state), identity())
    try:
        result = await submit(state, form, bindings(**bad))
    except InvalidData:
        # Unsupported choices are rejected by the actual native selector schema.
        pass
    else:
        assert result["type"] == "form" and result["step_id"] == "bindings" and result["errors"]
    assert not any(item.subentry_type == "physical_phone" for item in state.lab.entry.subentries.values())


async def test_queue_alias_is_saved_as_stable_queue_identity(physical_flow):
    state = physical_flow
    queue = SimpleNamespace(queue_id="stable-queue", name="Support", extension="700", members={"sip:front"})
    state.lab.runtime.queues = SimpleNamespace(resolve=lambda value: queue if value in {queue.queue_id, queue.name} else None)
    form = await submit(state, await start(state), identity())
    result = await submit(state, form, bindings(keys=[{
        "slot": "2", "function": "queue_login", "line": "1", "target": "Support", "label": "",
    }]))
    assert result["type"] == "create_entry"
    assert result["data"]["keys"][0]["target"] == queue.queue_id


async def test_language_override_uses_supported_bcp_choice(physical_flow):
    state = physical_flow
    form = await submit(state, await start(state), identity(follow_ha_language=False))
    assert form["step_id"] == "language"
    with pytest.raises(InvalidData):
        await submit(state, form, {"language": "not-a-supported-language"})
    form = await submit(state, form, {"language": "it"})
    result = await submit(state, form, bindings())
    assert result["data"]["language"] == "it" and result["data"]["follow_ha_language"] is False


async def test_unsupported_ha_language_requests_an_explicit_override(physical_flow):
    state = physical_flow
    state.lab.hass.config.language = "nl"
    form = await submit(state, await start(state), identity())
    assert form["step_id"] == "language" and form["errors"]["base"] == "phone_language_unsupported"
    form = await submit(state, form, {"language": "it"})
    result = await submit(state, form, bindings())
    assert result["data"]["follow_ha_language"] is False


async def test_profile_without_language_omits_language_only(physical_flow):
    state = physical_flow
    form = await submit(state, await start(state), identity(profile_id=state.catalog.empty_language))
    assert form["step_id"] == "bindings"
    result = await submit(state, form, bindings(keys=[]))
    assert result["data"]["language"] == "" and result["data"]["follow_ha_language"] is True
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
    form = await submit(state, form, identity(name="Renamed", update_profile=refresh))
    result = await submit(state, form, bindings())
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    saved = state.lab.entry.subentries[item.subentry_id].data
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
    form = await submit(state, await start(state), identity())
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
    assert result["type"] == "create_entry"
    account = next(item.data for item in state.lab.entry.subentries.values()
                   if item.subentry_type == "phone" and item.data["username"] == "new")
    assert result["data"]["lines"][0]["endpoint_id"] == account["endpoint_id"]
    assert account["password"] == "fixture-new-secret"
    assert "password" not in result["data"]["lines"][0]


async def test_disabling_provisioning_mid_flow_aborts_without_persisting(physical_flow):
    state = physical_flow
    form = await submit(state, await start(state), identity())
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
    form = await submit(state, form, identity(name="Renamed"))
    assert form["step_id"] == "bindings"
    result = await submit(state, form, bindings())
    assert result["type"] == "abort"
    saved = state.lab.entry.subentries[item.subentry_id].data
    assert saved["language"] == "it" and saved["follow_ha_language"] is True


@pytest.mark.parametrize("status", ["protocol_verified", "hardware_qualified"])
async def test_verified_catalog_statuses_remain_selectable(physical_flow, status):
    state = physical_flow
    state.catalog.profiles[PROFILE][0]["status"] = status
    form = await submit(state, await start(state), identity())
    assert form["step_id"] == "bindings"
    result = await submit(state, form, bindings())
    assert result["type"] == "create_entry" and result["data"]["profile_id"] == PROFILE


def cache_candidate(state):
    from custom_components.voip_stack.physical_phone_discovery import PhoneCandidate
    candidate = PhoneCandidate("candidate-id", "001122334455", "192.0.2.10",
                               "Fanvil", "X3U Pro", token="private-candidate-credential")
    state.lab.runtime.physical_phones = SimpleNamespace(catalog=state.catalog,
        discovery=SimpleNamespace(candidates={candidate.mac: candidate}))
    return candidate


async def test_main_native_discovery_reuses_editor_without_child_flow_or_second_pbx(physical_flow):
    state = physical_flow
    candidate = cache_candidate(state)
    assert "provisioning_token" not in candidate.public() and "token" not in candidate.public()
    result = await state.lab.hass.config_entries.flow.async_init("voip_stack",
        context={"source": SOURCE_INTEGRATION_DISCOVERY}, data={"discovery_candidate": candidate.public()})
    assert result["type"] == "form" and result["step_id"] == "phone_discovered"
    assert candidate.token not in repr(result)
    result = await state.lab.hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "form" and result["step_id"] == "phone_user"
    assert candidate.token not in repr(result)
    assert not state.lab.hass.config_entries.subentries.async_progress()
    form = await state.lab.hass.config_entries.flow.async_configure(result["flow_id"], result["data_schema"]({}))
    assert form["step_id"] == "phone_bindings"
    created = await state.lab.hass.config_entries.flow.async_configure(form["flow_id"], bindings())
    assert created["type"] == "abort" and created["reason"] == "phone_configured"
    physical = next(item for item in state.lab.entry.subentries.values() if item.subentry_type == "physical_phone")
    assert physical.data["phone_id"] == candidate.phone_id
    assert physical.data["provisioning_token"] == candidate.token
    assert len([entry for entry in state.lab.hass.config_entries.async_entries("voip_stack")
                if entry.source != SOURCE_IGNORE]) == 1


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


async def test_main_discovery_requires_the_current_cached_candidate(physical_flow):
    state = physical_flow
    candidate = cache_candidate(state)
    state.lab.runtime.physical_phones.discovery.candidates.clear()
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
    from pytest_homeassistant_custom_component.common import MockConfigEntry
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
    state = physical_flow
    candidate = cache_candidate(state)
    form = await state.lab.hass.config_entries.flow.async_init("voip_stack",
        context={"source": SOURCE_INTEGRATION_DISCOVERY}, data={"discovery_candidate": candidate.public()})

    async def main(data):
        nonlocal form
        form = await state.lab.hass.config_entries.flow.async_configure(form["flow_id"], data)
        return form

    await main({})
    await main(form["data_schema"]({"follow_ha_language": False}))
    assert form["step_id"] == "phone_language"
    await main({"language": "it"})
    assert form["step_id"] == "phone_bindings"
    account_ids = {item.subentry_id for item in state.lab.entry.subentries.values() if item.subentry_type == "phone"}
    await main({"lines": [{"slot": "1", "endpoint_id": CREATE_ACCOUNT}], "keys": []})
    assert form["step_id"] == "phone_account"
    await main({"name": "Discovery line", "username": "discovery-line", "password": "fixture-draft-secret",
                "extension": "484", "enabled": True})
    assert form["step_id"] == "phone_bindings"
    assert {item.subentry_id for item in state.lab.entry.subentries.values() if item.subentry_type == "phone"} == account_ids
    await main(form["data_schema"]({}))
    assert form["type"] == "abort" and form["reason"] == "phone_configured"
    physical = next(item.data for item in state.lab.entry.subentries.values() if item.subentry_type == "physical_phone")
    account = next(item.data for item in state.lab.entry.subentries.values()
                   if item.subentry_type == "phone" and item.data["username"] == "discovery-line")
    assert physical["phone_id"] == candidate.phone_id and physical["provisioning_token"] == candidate.token
    assert physical["language"] == "it" and physical["follow_ha_language"] is False
    assert physical["lines"][0]["endpoint_id"] == account["endpoint_id"]
    assert not state.lab.hass.config_entries.subentries.async_progress()
    state.reload.assert_not_awaited()
