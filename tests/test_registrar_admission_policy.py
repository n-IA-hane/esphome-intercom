"""Configured registrar limits preserve leases and bound authentication state."""
from __future__ import annotations

from types import SimpleNamespace
import time

import pytest

from .voip_phase1_support import sip, sip_registrar
from .test_linphone_registrar_wire import _digest
from custom_components.voip_stack.sip_registration_policy import (
    RegistrarPolicy, registrar_policy_defaults, validate_registrar_policy,
)


def owner(policy=None, **kwargs):
    return sip_registrar.SipRegistrar(enabled=True,
        accounts=[sip_registrar.SipAccount("phone", "Phone", "test-password")],
        local_ip="127.0.0.1", local_sip_port=5060, policy=policy, **kwargs)


def request(registrar, sequence=1, password="test-password", contacts=None, expires=120, challenge=None):
    if challenge is None:
        challenge = registrar._challenge("UDP:192.0.2.10")[1]
    uri = "sip:127.0.0.1"
    headers = [("Via", f"SIP/2.0/UDP 192.0.2.10:5060;branch=z9hG4bK-test-{sequence}"),
        ("From", "<sip:phone@127.0.0.1>;tag=a"), ("To", "<sip:phone@127.0.0.1>"),
        ("Call-ID", "policy-registration"), ("CSeq", f"{sequence} REGISTER"), ("Expires", str(expires)),
        ("Authorization", _digest(challenge, uri, "phone", password, nonce_count=sequence))]
    for contact in contacts if contacts is not None else ["<sip:phone@192.0.2.10:5060>"]:
        headers.append(("Contact", contact))
    return sip.parse_message(sip.build_request("REGISTER", uri, headers))


async def test_expiry_bounds_and_contact_expiry_are_configured_and_atomic():
    registrar = owner(RegistrarPolicy(min_expires=90, max_expires=180))
    bad = request(registrar, contacts=["<sip:first@192.0.2.10>", "<sip:short@192.0.2.10>;expires=89"])
    result = await registrar.handle_register(bad, ("192.0.2.10", 5060), "UDP")
    assert result.status == 423 and result.headers == (("Min-Expires", "90"),)
    assert not registrar.registrations
    result = await registrar.handle_register(request(registrar, 2, expires=86400), ("192.0.2.10", 5060), "UDP")
    assert result.status == 200
    binding, = registrar.registered_contacts("phone")
    assert 179 <= binding.expires_at - time.time() <= 180
    result = await registrar.handle_register(request(registrar, 3, expires=0, contacts=["*"]), ("192.0.2.10", 5060), "UDP")
    assert result.status == 200 and not registrar.registrations


async def test_lowered_contact_limit_preserves_refresh_and_removal_then_rejects_new_excess():
    registrar = owner(RegistrarPolicy(contact_limit=3))
    contacts = [f"<sip:client{n}@192.0.2.10>" for n in range(3)]
    assert (await registrar.handle_register(request(registrar, contacts=contacts), ("192.0.2.10", 5060), "UDP")).status == 200
    before = dict(registrar.registrations)
    registrar.configure_policy(RegistrarPolicy(contact_limit=2))
    assert registrar.registrations == before
    assert (await registrar.handle_register(request(registrar, 2, contacts=contacts), ("192.0.2.10", 5060), "UDP")).status == 200
    mixed = [contacts[0] + ";expires=0", "<sip:new@192.0.2.10>"]
    before = dict(registrar.registrations)
    assert (await registrar.handle_register(request(registrar, 3, contacts=mixed), ("192.0.2.10", 5060), "UDP")).status == 403
    assert registrar.registrations == before
    assert (await registrar.handle_register(request(registrar, 4, contacts=contacts[:2], expires=0), ("192.0.2.10", 5060), "UDP")).status == 200
    assert len(registrar.registrations) == 1
    assert (await registrar.handle_register(request(registrar, 5, contacts=["<sip:new@192.0.2.10>"]), ("192.0.2.10", 5060), "UDP")).status == 200
    assert len(registrar.registrations) == 2


@pytest.mark.parametrize("policy", [RegistrarPolicy(auth_failure_threshold=0), RegistrarPolicy(auth_block_duration=0)])
async def test_zero_threshold_or_duration_disables_counter_and_block(policy):
    registrar = owner(policy)
    for sequence in range(1, 30):
        assert (await registrar.handle_register(request(registrar, sequence, "incorrect"), ("192.0.2.10", 5060), "UDP")).status == 401
    assert not registrar.authentication_failures
    assert (await registrar.handle_register(request(registrar, 30), ("192.0.2.10", 5060), "UDP")).status == 200


async def test_failure_window_stale_nonce_block_expiry_source_isolation_and_notification(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(sip_registrar, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    notifications = []
    registrar = owner(RegistrarPolicy(auth_failure_threshold=2, auth_failure_window=60, auth_block_duration=300),
        on_source_block=lambda source, duration: notifications.append((source, duration)))
    address = ("192.0.2.10", 5060)
    wrong = request(registrar, 1, "incorrect")
    for _ in range(20):
        assert (await registrar.handle_register(wrong, address, "UDP")).status == 401
    state, = registrar.authentication_failures.values()
    assert len(state.failures) == 1
    nonce = next(iter(registrar.nonces))
    registrar.nonces[nonce] = 0
    stale = await registrar.handle_register(wrong, address, "UDP")
    assert stale.status == 401 and "stale=true" in dict(stale.headers)["WWW-Authenticate"]
    assert len(state.failures) == 1
    clock[0] += 61
    assert (await registrar.handle_register(request(registrar, 2, "incorrect"), address, "UDP")).status == 401
    assert (await registrar.handle_register(request(registrar, 3, "incorrect"), address, "TCP")).status == 403
    assert notifications == [(address[0], 300)]
    nonce_count = len(registrar.nonces)
    for _ in range(20):
        result = await registrar.handle_register(request(registrar, 4), address, "UDP")
        assert result.status == 403 and int(dict(result.headers)["Retry-After"]) == 300
    assert len(registrar.nonces) == nonce_count
    assert (await registrar.handle_register(request(registrar, 5), ("192.0.2.11", 5060), "UDP")).status == 200
    clock[0] += 301
    assert (await registrar.handle_register(request(registrar, 6), address, "UDP")).status == 200
    assert not registrar.authentication_failures


async def test_failed_authentication_memory_and_nonce_budgets_remain_bounded():
    registrar = owner(RegistrarPolicy(auth_failure_threshold=2))
    for index in range(sip_registrar.MAX_AUTH_FAILURE_SOURCES + 20):
        source = f"192.0.{index // 254}.{index % 254 + 1}"
        assert (await registrar.handle_register(request(registrar, index + 1, "incorrect"), (source, 5060), "UDP")).status == 401
    assert len(registrar.authentication_failures) == sip_registrar.MAX_AUTH_FAILURE_SOURCES
    assert all(len(state.failures) < registrar.policy.auth_failure_threshold for state in registrar.authentication_failures.values())
    assert len(registrar.nonces) <= sip_registrar.MAX_ACTIVE_NONCES
    assert len(registrar.nonce_uses) <= sip_registrar.MAX_NONCE_USE_RECORDS


@pytest.mark.parametrize("values", [{"registrar_min_expires": 91, "registrar_max_expires": 90},
    {"registrar_contact_limit": 0}, {"registrar_auth_failure_threshold": -1},
    {"registrar_auth_failure_window": 0}, {"registrar_auth_block_duration": -1}])
def test_policy_validates_same_saved_fields_as_forms(values):
    with pytest.raises(ValueError):
        validate_registrar_policy({**registrar_policy_defaults(), **values})


def test_runtime_policy_requires_all_saved_values():
    with pytest.raises(KeyError):
        RegistrarPolicy.from_mapping({})
