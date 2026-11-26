"""The caller policy preserves numbering and never doubles as credentials."""

import pytest

from custom_components.voip_stack.outbound_identity import normalized_outbound_identity_config, resolve_trunk_caller
from custom_components.voip_stack.phone_policy import PhonePolicy


@pytest.mark.parametrize("mode,expected", [("caller", "sip:+39061234101@origin.example"),
                                          ("account", "sip:+39061234101@origin.example"),
                                          ("trunk", "sip:+3906123400@provider.example")])
def test_forwarded_originator_and_force_trunk_have_explicit_different_results(mode, expected):
    identity = resolve_trunk_caller({
        "trunk_caller_id_policy": mode, "trunk_username": "registration", "trunk_auth_username": "digest-login",
        "trunk_caller_id": "+3906123400", "trunk_caller_name": "Office",
        "trunk_domain": "provider.example", "trunk_server": "192.0.2.2",
    }, caller_user="+39061234101", caller_name="Remote caller", caller_uri="sip:+39061234101@origin.example")
    assert identity.uri == expected
    assert identity.name == ("Office" if mode == "trunk" else "Remote caller")


def test_outbound_account_number_does_not_change_account_or_trunk_selection():
    policy = PhonePolicy.from_data({"outbound_trunk_id": "office", "outbound_caller_id": "+39061234101", "outbound_caller_name": "Reception"})
    assert policy.as_data() == {"outbound_trunk_id": "office", "outbound_caller_id": "+39061234101", "outbound_caller_name": "Reception"}
    assert not policy.requires_ha_routing


@pytest.mark.parametrize("field,value", [("trunk_caller_id", "sip:101@example.org"),
                                        ("trunk_caller_name", "Reception\r\nContact: bad"),
                                        ("trunk_caller_id_policy", "guess")])
def test_invalid_persisted_identity_is_rejected(field, value):
    with pytest.raises(ValueError):
        normalized_outbound_identity_config({field: value})


def test_legacy_trunk_default_keeps_registration_user_and_domain_fallback():
    selected = resolve_trunk_caller({"trunk_username": "provider-user", "trunk_server": "pbx.example"},
        caller_user="101", caller_name="Reception")
    assert selected.uri == "sip:provider-user@pbx.example"
    assert selected.name == "provider-user"
