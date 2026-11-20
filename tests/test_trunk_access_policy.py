"""Behavioral policy tests independent of Home Assistant and live SIP sockets."""

import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def policy(monkeypatch):
    name = "_trunk_access_policy_test"
    package = ModuleType(name)
    package.__path__ = [str(Path(__file__).parents[1] / "custom_components/voip_stack")]
    monkeypatch.setitem(sys.modules, name, package)
    return importlib.import_module(f"{name}.trunk_policy")


def test_no_restrictions_preserves_default_access(policy):
    session = SimpleNamespace(metadata={"source_endpoint_id": "restricted"})
    assert policy.external_call_denied({}, "restricted", session=session) == ""
    assert policy.external_call_denied({policy.EXTERNAL_CALL_BLOCKED_ENDPOINTS: []}, "restricted") == ""


@pytest.mark.parametrize(("requested", "phone", "expected"), [
    ("work", "home", "work"), ("", "work", "work"), ("", "", "home"),
])
def test_trunk_selection_has_explicit_phone_default_precedence(policy, requested, phone, expected):
    configs = {identity: {"trunk_enabled": True} for identity in ("home", "work")}
    assert policy.select_trunk_id(configs, "home", requested_id=requested, phone_trunk_id=phone) == expected


@pytest.mark.parametrize("requested", ["missing", "disabled"])
def test_explicit_invalid_trunk_never_falls_back_to_default(policy, requested):
    with pytest.raises(ValueError):
        policy.select_trunk_id({"home": {"trunk_enabled": True}, "disabled": {"trunk_enabled": False}},
                               "home", requested_id=requested)


def test_static_trunk_source_overlap_requires_same_transport(policy):
    first = {"trunk_enabled": True, "trunk_register": False, "trunk_transport": "udp",
             "trunk_allowed_ips": ["192.0.2.0/24", "2001:db8::/64"]}
    second = {**first, "trunk_allowed_ips": ["192.0.2.10"]}
    with pytest.raises(ValueError, match="non-overlapping"):
        policy.validate_static_trunk_sources({"one": first, "two": second})
    policy.validate_static_trunk_sources({"one": first, "two": {**second, "trunk_transport": "tcp"}})
    policy.validate_static_trunk_sources({"one": first, "two": {**second, "trunk_enabled": False}})
    with pytest.raises(ValueError, match="non-overlapping"):
        policy.validate_static_trunk_sources({"one": first, "two": {**second, "trunk_allowed_ips": ["2001:db8::1"]}})


def test_inbound_trunk_identity_uses_owned_flow_or_unique_trusted_source(policy):
    clients = {identity: SimpleNamespace(ready=True, accepts_inbound_source=lambda *_args: True)
               for identity in ("home", "work")}
    with pytest.raises(ValueError, match="multiple trunks"):
        policy.identify_inbound_trunk(clients, "192.0.2.10", 5060, "UDP")
    assert policy.identify_inbound_trunk(clients, "192.0.2.10", 5060, "UDP", flow_trunk_id="work") == "work"
    clients["work"].ready = False
    assert policy.identify_inbound_trunk(clients, "192.0.2.10", 5060, "UDP", flow_trunk_id="work") == ""
    assert policy.identify_inbound_trunk(clients, "192.0.2.10", 5060, "UDP") == "home"


@pytest.mark.parametrize("key", ["source_endpoint_id", "dest_endpoint_id", "endpoint_id"])
def test_authoritative_endpoint_identity_preserves_restriction(policy, key):
    config = {policy.EXTERNAL_CALL_BLOCKED_ENDPOINTS: ["stable-phone"]}
    session = SimpleNamespace(metadata={key: "stable-phone", "caller": "New display name"})
    assert policy.external_call_denied(config, "allowed-phone", session=session) == "stable-phone"


@pytest.mark.parametrize("key", ["caller", "callee", "name", "extension", "username", "sip_from"])
def test_display_text_and_sip_headers_are_not_endpoint_permissions(policy, key):
    config = {policy.EXTERNAL_CALL_BLOCKED_ENDPOINTS: ["stable-phone"]}
    session = SimpleNamespace(metadata={key: "stable-phone", "source_endpoint_id": "allowed"})
    assert policy.external_call_denied(config, session=session) == ""


def test_explicit_actor_and_parent_call_restrictions_are_both_checked(policy):
    sessions = {
        "child": SimpleNamespace(metadata={"source_endpoint_id": "group-phone", "source_call_id": "parent"}),
        "parent": SimpleNamespace(metadata={"source_endpoint_id": "original-phone"}),
    }
    lookups = []

    def get_session(call_id):
        lookups.append(call_id)
        return sessions.get(call_id)

    runtime = SimpleNamespace(
        transport_config={policy.EXTERNAL_CALL_BLOCKED_ENDPOINTS: ["original-phone", "acting-phone"]},
        sip=SimpleNamespace(get_session=get_session),
    )
    assert policy.call_external_call_denied(runtime, "child", "acting-phone") == "acting-phone"
    assert lookups == []
    assert policy.call_external_call_denied(runtime, "child", "allowed-phone") == "original-phone"
    assert lookups == ["child", "parent"]


def test_parent_cycles_terminate_and_restrictions_are_read_on_every_attempt(policy):
    sessions = {
        "a": SimpleNamespace(metadata={"source_call_id": "b"}),
        "b": SimpleNamespace(metadata={"source_call_id": "a", "dest_endpoint_id": "phone"}),
    }
    runtime = SimpleNamespace(transport_config={}, sip=SimpleNamespace(get_session=sessions.get))
    assert policy.call_external_call_denied(runtime, "a") == ""
    runtime.transport_config[policy.EXTERNAL_CALL_BLOCKED_ENDPOINTS] = ["phone"]
    assert policy.call_external_call_denied(runtime, "a") == "phone"
    runtime.transport_config[policy.EXTERNAL_CALL_BLOCKED_ENDPOINTS] = []
    assert policy.call_external_call_denied(runtime, "a") == ""


def test_long_forwarding_chain_preserves_original_phone_restriction(policy):
    sessions = {
        str(index): SimpleNamespace(metadata={"source_call_id": str(index + 1)})
        for index in range(12)
    }
    sessions["12"] = SimpleNamespace(metadata={"source_endpoint_id": "original-phone"})
    runtime = SimpleNamespace(
        transport_config={policy.EXTERNAL_CALL_BLOCKED_ENDPOINTS: ["original-phone"]},
        sip=SimpleNamespace(get_session=sessions.get),
    )
    assert policy.call_external_call_denied(runtime, "0") == "original-phone"


def test_forwarded_call_retains_registration_only_origin_without_other_blocked_endpoints(policy):
    sessions = {"inner": SimpleNamespace(metadata={"source_call_id": "original"}),
                "original": SimpleNamespace(metadata={"registration_only_caller": "legacy"})}
    runtime = SimpleNamespace(transport_config={}, sip=SimpleNamespace(get_session=sessions.get))
    assert policy.call_external_call_denied(runtime, "inner") == "legacy"
    assert policy.call_external_call_denied(runtime, "original") == "legacy"
    assert policy.call_external_call_denied(runtime, "unrelated") == ""


@pytest.mark.parametrize("uri,config,target,expected", [
    ("sip:123@provider.example", {"trunk_server": "provider.example"}, None, True),
    ("sip:123@PROVIDER.EXAMPLE.:5070;transport=tcp", {"trunk_server": "provider.example", "trunk_port": 5070}, None, True),
    ("sip:123@provider.example:5060", {"trunk_server": "provider.example", "trunk_port": 5070}, None, False),
    ("sip:123@proxy.example:5080", {"trunk_server": "provider.example", "trunk_outbound_proxy": "sip:proxy.example:5080;transport=tcp"}, None, True),
    ("sip:123@198.51.100.7:5090", {"trunk_server": "provider.example"}, ("198.51.100.7", 5090), True),
    ("sip:123@198.51.100.7", {"trunk_server": "provider.example"}, ("198.51.100.7", 5090), False),
    ("sip:desk@192.0.2.20", {"trunk_server": "provider.example"}, None, False),
    ("sip:123@provider.example.attacker.example", {"trunk_server": "provider.example"}, None, False),
    ("sip:provider.example@other.example", {"trunk_server": "provider.example"}, None, False),
    ("sip:123@[2001:db8::1]:5070", {"trunk_server": "[2001:db8::1]", "trunk_port": 5070}, None, True),
    ("sips:123@provider.example", {"trunk_server": "provider.example", "trunk_port": 5061}, None, True),
    ("sips:123@provider.example", {"trunk_server": "sips:provider.example"}, None, True),
    ("sips:123@proxy.example", {"trunk_server": "provider.example", "trunk_outbound_proxy": "sips:proxy.example"}, None, True),
    ("sips:123@proxy.example:5060", {"trunk_server": "provider.example", "trunk_outbound_proxy": "sips:proxy.example:5060"}, None, True),
    ("sip:123@[2001:0db8:0:0:0:0:0:1]", {"trunk_server": "[2001:db8::1]"}, None, True),
    ("sip:123@[2001:db8::1]", {"trunk_server": "provider.example"}, ("2001:0db8:0:0:0:0:0:1", 5060), True),
    ("invalid uri", {"trunk_server": "provider.example"}, None, False),
    ("sip:123@provider.example", {}, None, False),
])
def test_trunk_recognition_uses_routing_authority_not_caller_text(policy, uri, config, target, expected):
    assert policy.is_trunk_uri(uri, config, target) is expected
