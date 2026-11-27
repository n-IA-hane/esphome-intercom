"""The copied-storage gate covers the same upgrade boundary as runtime."""

from copy import deepcopy
import json

import pytest

pytestmark = pytest.mark.ha


def storage(minor, data=None, subentries=None):
    return {"domain": "voip_stack", "entry_id": "migration-copy", "version": 7,
            "minor_version": minor, "data": data or {}, "subentries": subentries or []}


@pytest.mark.parametrize("minor", [5, 6])
def test_preflight_accepts_supported_source_schemas_without_mutating_values(minor):
    from tools.preflight_pbx_migrations import preflight_store

    raw = storage(minor, {"default_ringing_timeout": 0,
        "service_timeouts": {"media_timeout": 91}, "unrelated": {"password": "secret-copy-only"}}, [{
            "subentry_id": "provider", "subentry_type": "trunk", "title": "Provider",
            "data": {"trunk_id": "provider", "trunk_register": False,
                     "trunk_allowed_ips": ["192.0.2.10"], "trunk_inbound_default_target": "Desk",
                     "trunk_inbound_unknown_target": "reject", "trunk_inbound_mode": "dtmf",
                     "trunk_dtmf_timeout_ms": 3000}}])
    before = deepcopy(raw)
    result = preflight_store(raw)
    assert result["status"] == "passed"
    assert result["entries"][0]["prerequisite_migrations_required"] is False
    assert result["entries"][0]["migration_required"] is True
    assert "secret-copy-only" not in json.dumps(result)
    assert raw == before


def test_preflight_rejects_residual_parent_even_at_current_numeric_schema():
    from custom_components.voip_stack.duration_config import initialize_duration_config
    from tools.preflight_pbx_migrations import preflight_store

    raw = storage(7, initialize_duration_config({"trunk_enabled": True,
        "trunk_register": False, "trunk_allowed_ips": ["192.0.2.10"],
        "trunk_inbound_default_target": "Desk"}))
    before = deepcopy(raw)
    result = preflight_store(raw)
    assert result["status"] == "blocked"
    entry = result["entries"][0]
    assert entry["stored_postconditions_satisfied"] is False
    assert entry["parent_trunk_move_required"] is True
    assert entry["migration_required"] is True
    assert entry["recovery_required"] == "Settings > System > Repairs"
    assert raw == before


@pytest.mark.parametrize("minor", [5, 6])
def test_preflight_reports_residual_parent_move_for_older_source(minor):
    from custom_components.voip_stack.duration_config import initialize_duration_config
    from tools.preflight_pbx_migrations import preflight_store

    raw = storage(minor, initialize_duration_config({"trunk_enabled": True,
        "trunk_register": False, "trunk_allowed_ips": ["192.0.2.10"]}))
    before = deepcopy(raw)
    result = preflight_store({"data": {"entries": [raw]}})
    assert result["status"] == "passed"
    assert result["entries"][0]["parent_trunk_move_required"] is True
    assert result["entries"][0]["stored_postconditions_satisfied"] is False
    assert raw == before


def test_preflight_keeps_earlier_identity_migrations_outside_its_claim():
    from tools.preflight_pbx_migrations import preflight_store

    result = preflight_store(storage(4))
    assert result["status"] == "blocked"
    assert result["entries"][0]["prerequisite_migrations_required"] is True


@pytest.mark.parametrize("invalid", [0, -1, "secret-invalid-duration"])
def test_preflight_still_blocks_invalid_saved_policies_without_disclosing_them(invalid):
    from tools.preflight_pbx_migrations import preflight_store

    raw = storage(5, {"service_timeouts": {"media_timeout": invalid}})
    before = deepcopy(raw)
    result = preflight_store(raw)
    assert result["status"] == "blocked"
    assert "secret-invalid-duration" not in json.dumps(result)
    assert raw == before
