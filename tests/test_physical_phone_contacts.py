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


def test_external_directory_key_provisions_the_current_public_number():
    from dataclasses import replace
    entry = contact()
    current = runtime(entry)
    key, = profile_module.resolve_phone_keys(current, phone(), PROFILE)
    assert key["dial_target"] == "003905512345"
    assert key["label"] == "Customer" and key["event_package"] == ""
    changed = replace(entry, name="Renamed customer", number="0000123456")
    current.directory.replace([changed])
    updated, = profile_module.resolve_phone_keys(current, phone(), PROFILE)
    assert updated["dial_target"] == "0000123456" and updated["label"] == "Renamed customer"
    decision = router.resolve_ha_router(updated["dial_target"], list(current.directory.snapshot()), trunk_ready=True)
    assert decision.action is router.RouteAction.TRUNK and decision.target == "0000123456"
    custom, = profile_module.resolve_phone_keys(current, phone(label="My label"), PROFILE)
    assert custom["label"] == "My label"


def test_sip_contact_key_dials_its_pbx_extension_or_public_name():
    from dataclasses import replace

    entry = contact(sip_uri="sip:operator@external.example", extension="230")
    current = runtime(entry)
    key, = profile_module.resolve_phone_keys(current, phone(), PROFILE)
    assert key["dial_target"] == "230"
    current.directory.replace([replace(entry, name="Customer desk", extension="")])
    key, = profile_module.resolve_phone_keys(current, phone(), PROFILE)
    assert key["dial_target"] == "Customer%20desk"
    decision = router.resolve_ha_router("Customer desk", list(current.directory.snapshot()))
    assert decision.action is router.RouteAction.FORWARD and decision.sip_uri == entry.sip_uri


def test_legacy_contact_with_a_number_shadowed_by_a_group_uses_its_name():
    entry = contact()
    current = runtime(entry)
    group = RosterEntry(id="Office", extension=entry.number, metadata={"group_type": "ring"})
    assert next(current.directory.dial_users(entry)) == entry.number
    current.directory.upsert(group)
    key, = profile_module.resolve_phone_keys(current, phone(), PROFILE)
    assert key["dial_target"] == "Customer"
    assert router.resolve_ha_router(key["dial_target"], [group, entry], trunk_ready=True).entry == entry
    current.directory.remove(group.id)
    assert next(current.directory.dial_users(entry)) == entry.number


def test_queue_key_uses_the_public_name_within_the_slot_budget():
    current = runtime(contact())
    queue = SimpleNamespace(queue_id="q1", name="Support desk", extension="", members={"sip:line"})
    current.queues = SimpleNamespace(resolve=lambda ref: queue)
    config = phone(function="queue_login")
    config["keys"][0]["target"] = queue.queue_id
    profile = {"capabilities": {"key_slots": [{"slot": "1", "functions": ["queue_login"], "max_value_bytes": 31}]}}
    key, = profile_module.resolve_phone_keys(current, config, profile)
    assert key["dial_target"] == key["subscription_target"] == "queue-login+Support%20desk"
    queue.name = "Very long reception desk name"
    with pytest.raises(ValueError, match="phone_key_target_too_long"):
        profile_module.resolve_phone_keys(current, config, profile)


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


@pytest.mark.parametrize("extension,username,name,expected", [
    ("201", "ha_browser_long_public_identity", "Casa", "201"),
    ("", "Casa", "Casa display", "Casa"),
    ("", "ha_browser_f0fa47b74fb6441eb1ec5a97b88be460", "Casa", "Casa"),
    ("", "a" * 30, "Casa", "Casa"),
])
def test_qualified_fanvil_blf_pickup_uses_the_same_callable_public_route(extension, username, name, expected):
    from copy import deepcopy

    endpoint = SimpleNamespace(endpoint_id="browser:opaque-stable-id", extension=extension,
                               sip_uri_user=username, name=name)
    current = SimpleNamespace(endpoints=SimpleNamespace(resolve=lambda value: endpoint
        if value == endpoint.endpoint_id else None), directory=None, queues=None)
    profile = deepcopy(PROFILE)
    profile.update(id="fanvil/x3u-pro", vendor="fanvil")
    profile["capabilities"]["separate_pickup_target"] = True
    physical = {"lines": [{"slot": "1", "endpoint_id": "sip:picker"}], "keys": [
        {"slot": "1", "line": "1", "function": "blf", "target": endpoint.endpoint_id}]}

    key, = profile_module.resolve_phone_keys(current, physical, profile)

    assert key["dial_target"] == key["subscription_target"] == expected
    assert key["pickup_target"] == "*8" + expected
    assert "opaque-stable-id" not in key["pickup_target"]
    assert key["event_package"] == "dialog"


def test_unqualified_fanvil_blf_does_not_gain_a_pickup_mapping():
    endpoint = SimpleNamespace(endpoint_id="browser:casa", extension="", sip_uri_user="Casa", name="Casa")
    current = SimpleNamespace(endpoints=SimpleNamespace(resolve=lambda _: endpoint), directory=None, queues=None)
    physical = {"lines": [{"slot": "1", "endpoint_id": "sip:picker"}], "keys": [
        {"slot": "1", "line": "1", "function": "blf", "target": endpoint.endpoint_id}]}
    profile = {**PROFILE, "vendor": "fanvil"}
    key, = profile_module.resolve_phone_keys(current, physical, profile)
    assert key["pickup_target"] == ""
