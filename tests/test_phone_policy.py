"""Phone policy validation and decisions, without HA or SIP transport mocks."""

from dataclasses import FrozenInstanceError
import importlib.util
from pathlib import Path
import sys

import pytest


SPEC = importlib.util.spec_from_file_location(
    "phone_policy_contract",
    Path(__file__).resolve().parents[1] / "custom_components/voip_stack/phone_policy.py",
)
policy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = policy
SPEC.loader.exec_module(policy)


def test_unconfigured_phone_has_no_timeout_or_routing_work():
    configured = policy.PhonePolicy.from_data(None)
    assert configured.ringing_timeout is None
    assert not configured.requires_ha_routing
    assert configured.as_data() == {}
    for busy in (False, True):
        for unavailable in (False, True):
            assert configured.before_ringing(busy=busy, unavailable=unavailable) is None


@pytest.mark.parametrize("timeout", [-1, True, "30", float("nan"), float("inf"), 3601])
def test_reject_invalid_timeout(timeout):
    with pytest.raises(ValueError):
        policy.PhonePolicy.from_data({
            "ringing_timeout": timeout, "no_answer": {"kind": "hangup"},
        })


@pytest.mark.parametrize("data", [
    {"always": {}},
    {"always": {"kind": "unknown", "target": "Kitchen"}},
    {"always": {"kind": "hangup", "target": "Kitchen"}},
    {"always": {"kind": "contact", "target": 101}},
    {"always": {"kind": "contact", "target": "Kitchen", "typo": True}},
    {"ringing_timout": 25},
    {"outbound_trunk_id": 12},
    [],
])
def test_reject_incomplete_or_misspelled_settings(data):
    with pytest.raises(ValueError):
        policy.PhonePolicy.from_data(data)


def test_roundtrip_preserves_stable_references_and_explicit_hangup():
    data = {
        "always": {"kind": "contact", "target": "sip:opaque-identity"},
        "busy": {"kind": "voicemail", "target": "browser:opaque-identity"},
        "unavailable": {"kind": "contact", "target": "Welcome"},
        "ringing_timeout": 25,
        "no_answer": {"kind": "hangup", "target": ""},
        "outbound_trunk_id": "trunk:opaque-identity",
    }
    configured = policy.PhonePolicy.from_data(data)
    assert configured.as_data() == data
    assert policy.PhonePolicy.from_data(configured.as_data()) == configured
    assert configured.requires_ha_routing
    with pytest.raises(FrozenInstanceError):
        configured.ringing_timeout = 50


def test_policy_is_independent_from_mutable_input():
    raw = {"always": {"kind": "contact", "target": "Kitchen"}}
    configured = policy.PhonePolicy.from_data(raw)
    raw["always"]["target"] = "Other"
    assert configured.always.target == "Kitchen"


def test_unconditional_forward_precedes_busy_and_unavailable():
    configured = policy.PhonePolicy.from_data({
        "always": {"target": "Reception"},
        "busy": {"target": "Busy"},
        "unavailable": {"target": "Unavailable"},
    })
    assert configured.before_ringing(busy=True, unavailable=True).target == "Reception"


def test_busy_and_unavailable_remain_distinct():
    configured = policy.PhonePolicy.from_data({"unavailable": {"target": "Reception"}})
    assert configured.before_ringing(busy=True, unavailable=True) is None
    assert configured.before_ringing(busy=False, unavailable=True).target == "Reception"
    assert configured.before_ringing(busy=False, unavailable=False) is None


def test_group_does_not_trigger_member_voicemail():
    configured = policy.PhonePolicy.from_data({
        "always": {"kind": "voicemail", "target": "browser:member"},
    })
    assert configured.before_ringing(busy=False, unavailable=False, group_member=True) is None


def test_explicitly_clearing_timeout_and_destination_disables_both():
    configured = policy.PhonePolicy.from_data({
        "ringing_timeout": 25, "no_answer": {"kind": "hangup"},
    })
    data = configured.as_data()
    data.update(ringing_timeout=None, no_answer=None)
    cleared = policy.PhonePolicy.from_data(data)
    assert cleared == policy.PhonePolicy()
