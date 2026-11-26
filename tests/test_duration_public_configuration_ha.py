"""Persisted technical policies use collapsed native forms and safe preflight."""
from copy import deepcopy
import json

import pytest
from pytest_homeassistant.common import MockConfigEntry

pytestmark = pytest.mark.ha


def test_copied_storage_preflight_detects_incomplete_target_schema_without_values():
    from tools.preflight_pbx_migrations import preflight_store
    from custom_components.voip_stack.duration_config import initialize_duration_config
    raw = {"domain": "voip_stack", "entry_id": "public-fixture", "version": 7, "minor_version": 6,
           "data": {"service_timeouts": {"media_timeout": 91}, "unrelated": {"password": "fixture-secret"}},
           "subentries": []}
    before = deepcopy(raw)
    planned = preflight_store(raw)
    assert planned["status"] == "passed" and raw == before
    assert planned["entries"][0]["migration_required"] is True
    raw["minor_version"] = 7
    assert preflight_store(raw)["status"] == "blocked"
    raw["data"] = initialize_duration_config(raw["data"])
    complete = preflight_store(raw)
    assert complete["status"] == "passed" and complete["entries"][0]["stored_postconditions_satisfied"]
    assert "fixture-secret" not in json.dumps(complete)


async def test_new_pbx_technical_settings_share_one_collapsed_section(hass):
    from custom_components.voip_stack.config_flow import VoipStackConfigFlow, _apply_pbx_advanced
    from custom_components.voip_stack.duration_config import initialize_duration_config
    from custom_components.voip_stack.duration_policy import SERVICE_TIMEOUT_LIMITS
    from custom_components.voip_stack.sip_registration_policy import (REGISTRAR_POLICY_FIELDS, AUDIO_RTCP_MIN_INTERVAL,
        LOCAL_REDIRECT_MAX_HOPS)
    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=7,
                            data=initialize_duration_config({}))
    entry.add_to_hass(hass)
    flow = VoipStackConfigFlow()
    flow.hass = hass
    flow._current_entry_data = lambda: (entry, entry.data)
    form = await flow.async_step_user()
    sections = {key.schema: value for key, value in form["data_schema"].schema.items()}
    advanced = sections["advanced"]
    assert advanced.options["collapsed"] is True
    fields = {key.schema for key in advanced.schema.schema}
    assert set(SERVICE_TIMEOUT_LIMITS) | set(REGISTRAR_POLICY_FIELDS) | {AUDIO_RTCP_MIN_INTERVAL, LOCAL_REDIRECT_MAX_HOPS} <= fields
    assert not set(SERVICE_TIMEOUT_LIMITS).intersection(sections)
    values = {"advanced": {"media_timeout": 99, "conference_sip_ringing_timeout": 0,
                            "registrar_contact_limit": 8, AUDIO_RTCP_MIN_INTERVAL: 7, LOCAL_REDIRECT_MAX_HOPS: 9}}
    _apply_pbx_advanced(values, entry.data)
    assert "advanced" not in values
    assert values["service_timeouts"]["media_timeout"] == 99
    assert values["service_timeouts"]["conference_sip_ringing_timeout"] == 0
    assert values["registrar_contact_limit"] == 8 and values[AUDIO_RTCP_MIN_INTERVAL] == 7
    from custom_components.voip_stack.config import entry_transport_config
    projected = entry_transport_config(type("Stored", (), {"data": {**entry.data, **values}})())
    assert projected[LOCAL_REDIRECT_MAX_HOPS] == 9


async def test_trunk_advanced_interval_is_collapsed_and_zero_disables_probes(hass):
    from custom_components.voip_stack.config_flow import TrunkFormMixin, VoipStackConfigFlow
    class Form(TrunkFormMixin, VoipStackConfigFlow):
        pass
    flow = Form()
    flow.hass = hass
    flow._current_entry_data = lambda: (None, {})
    form = await flow.async_step_trunk_mode({"trunk_register": False})
    advanced = next(value for key, value in form["data_schema"].schema.items() if key.schema == "advanced")
    assert advanced.options["collapsed"] is True
    assert advanced.schema({"trunk_options_interval": 0})["trunk_options_interval"] == 0
