"""Directory references on physical keys retain identity across contact edits."""

from hashlib import sha256
from types import SimpleNamespace

import pytest

from .test_groups import _load_module

directory_module = _load_module("phone_directory")
profile_module = _load_module("phone_profile")
router = _load_module("router")
RosterEntry = _load_module("roster").RosterEntry


def contact(**values):
    return RosterEntry(id="customer-one", name="Customer", number="003905512345",
        metadata={"endpoint_id": "contact:customer-one", "endpoint_kind": "contact"}, **values)


def runtime(entry):
    return SimpleNamespace(directory=directory_module.PhoneDirectory([entry]),
        endpoints=SimpleNamespace(resolve=lambda _ref: None), queues=None)


def phone(function="speed_dial", label=""):
    return {"lines": [{"slot": "1", "endpoint_id": "sip:line"}],
        "keys": [{"slot": "1", "line": "1", "function": function,
                  "target": "contact:customer-one", "label": label}]}


PROFILE = {"capabilities": {"functions": ["speed_dial", "blf"], "key_slots": [
    {"slot": "1", "functions": ["speed_dial", "blf"], "max_value_bytes": 31, "max_label_bytes": 31}]}}


def test_external_directory_key_is_stable_and_follows_the_current_contact():
    from dataclasses import replace
    entry = contact()
    current = runtime(entry)
    key, = profile_module.resolve_phone_keys(current, phone(), PROFILE)
    expected = "contact+" + sha256(b"contact:customer-one").hexdigest()[:20]
    assert key["dial_target"] == expected and len(expected) == 28
    assert key["label"] == "Customer" and key["event_package"] == ""
    changed = replace(entry, name="Renamed customer", number="0000123456")
    current.directory.replace([changed])
    updated, = profile_module.resolve_phone_keys(current, phone(), PROFILE)
    assert updated["dial_target"] == expected and updated["label"] == "Renamed customer"
    decision = router.resolve_ha_router(expected, list(current.directory.snapshot()), trunk_ready=True)
    assert decision.action is router.RouteAction.TRUNK and decision.target == "0000123456"
    custom, = profile_module.resolve_phone_keys(current, phone(label="My label"), PROFILE)
    assert custom["label"] == "My label"


def test_external_directory_reference_is_not_a_blf_principal():
    with pytest.raises(ValueError):
        profile_module.resolve_phone_keys(runtime(contact()), phone("blf"), PROFILE)


def test_unknown_directory_reference_is_rejected_instead_of_using_an_old_number():
    current = runtime(contact())
    current.directory.replace([])
    with pytest.raises(ValueError):
        profile_module.resolve_phone_keys(current, phone(), PROFILE)


def test_directory_alias_uses_the_same_canonical_sip_route_as_its_contact():
    entry = contact(sip_uri="sip:desk@example.test")
    alias = "contact+" + sha256(b"contact:customer-one").hexdigest()[:20]
    decision = router.resolve_ha_router(alias, [entry], trunk_ready=True)
    assert decision.action is router.RouteAction.FORWARD
    assert decision.sip_uri == "sip:desk@example.test"


def test_unknown_directory_alias_never_falls_through_to_a_trunk():
    decision = router.resolve_ha_router("contact+" + "0" * 20, [], trunk_ready=True)
    assert decision.action is router.RouteAction.REJECT and decision.status == 404
