"""Runtime policy owners require explicit persisted policy values."""

from types import SimpleNamespace

import pytest

from .voip_phase1_support import _load_intercom_module, sip_registrar, sip_trunk


policy_module = _load_intercom_module("sip_registration_policy")


def test_registrar_policy_cannot_invent_unstored_values():
    with pytest.raises(TypeError):
        policy_module.RegistrarPolicy()


@pytest.mark.parametrize("policy_argument", [{}, {"policy": None}])
def test_registrar_requires_a_configured_policy(policy_argument):
    with pytest.raises((TypeError, ValueError)):
        sip_registrar.SipRegistrar(
            enabled=True, accounts=[], local_ip="127.0.0.1", local_sip_port=5060,
            **policy_argument,
        )


def test_trunk_cannot_invent_an_unstored_probe_interval():
    with pytest.raises(TypeError):
        sip_trunk.SipTrunkConfig(
            enabled=True, transport="udp", server="127.0.0.1", port=5060,
            domain="", username="", auth_username="", password="", expires=300,
        )


def test_absent_entry_has_no_runtime_registration_or_probe_policy():
    config = _load_intercom_module("config")
    transport = config.entry_transport_config(None)
    assert not set(policy_module.REGISTRAR_POLICY_FIELDS).intersection(transport)
    assert policy_module.AUDIO_RTCP_MIN_INTERVAL not in transport
    assert policy_module.LOCAL_REDIRECT_MAX_HOPS not in transport
    assert policy_module.TRUNK_OPTIONS_INTERVAL not in config.entry_trunk_config(None)
    with pytest.raises(KeyError):
        policy_module.RegistrarPolicy.from_mapping(transport)


def test_incomplete_saved_entry_is_not_treated_as_an_absent_entry():
    config = _load_intercom_module("config")
    with pytest.raises(KeyError):
        config.entry_transport_config(SimpleNamespace(data={}))


def test_explicit_configured_policy_values_and_disabled_probe_are_retained():
    saved = {
        "registrar_auth_failure_threshold": 4,
        "registrar_auth_failure_window": 17,
        "registrar_auth_block_duration": 42,
        "registrar_min_expires": 73,
        "registrar_max_expires": 902,
        "registrar_contact_limit": 7,
    }
    policy = policy_module.RegistrarPolicy.from_mapping(saved)
    registrar = sip_registrar.SipRegistrar(
        enabled=True, accounts=[], local_ip="127.0.0.1", local_sip_port=5060,
        policy=policy,
    )
    assert registrar.policy is policy
    assert {
        "registrar_" + key: getattr(policy, key)
        for key in policy.__dataclass_fields__
    } == saved
    trunk = sip_trunk.SipTrunkConfig(
        enabled=True, transport="udp", server="127.0.0.1", port=5060,
        domain="", username="", auth_username="", password="", expires=300,
        options_interval=0,
    )
    assert trunk.options_interval == 0
