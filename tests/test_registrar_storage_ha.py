"""Restart restores valid datagram leases without reviving stream ownership."""

from .support.sip_policy import configured_registrar_policy

from dataclasses import replace
import time
from unittest.mock import AsyncMock

import pytest

from custom_components.voip_stack.registrar_storage import RegistrarLeaseStore
from custom_components.voip_stack.sip_registrar import SipAccount, SipRegistrar, SipRegistration

pytestmark = pytest.mark.ha


def registrar(password="test-only-password"):
    return SipRegistrar(policy=configured_registrar_policy(), enabled=True, accounts=[SipAccount("Desk", "Desk", password)],
                        local_ip="127.0.0.1", local_sip_port=5060)


@pytest.mark.parametrize("password,expected", [("test-only-password", 1), ("rotated-password", 0)])
async def test_restart_keeps_only_unexpired_udp_leases_for_the_same_credentials(hass, password, expected):
    original = registrar()
    lease = SipRegistration("Desk", "sip:Desk@127.0.0.2:5062", "127.0.0.2", 5062,
                            "UDP", time.time() + 120, call_id="registered-before-restart")
    original.registrations.update({
        "Desk": lease,
        "Desk#2": replace(lease, transport="TCP", source_port=5063),
        "Desk#3": replace(lease, expires_at=time.time() - 1),
    })
    prior = RegistrarLeaseStore(hass, original, "prior")
    data = prior.snapshot()
    assert len(data["bindings"]) == 1
    restored = registrar(password)
    state = RegistrarLeaseStore(hass, restored, "new")
    state.store.async_load = AsyncMock(return_value=data)
    state.store.async_save = AsyncMock()
    await state.async_restore()
    try:
        contacts = restored.registered_contacts("Desk")
        assert len(contacts) == expected
        if contacts:
            assert contacts[0].contact_uri == lease.contact_uri
            assert contacts[0].source_port == lease.source_port
            assert contacts[0].capability_generation != lease.capability_generation
        assert not restored.nonces
    finally:
        await state.async_close()
    assert not restored._contact_observers
    assert not restored._account_observers


@pytest.mark.parametrize("bindings", [None, "invalid", [None, {}, {"binding": {"username": "Desk"}}]])
async def test_invalid_stored_data_does_not_prevent_a_fresh_registration(hass, bindings):
    current = registrar()
    state = RegistrarLeaseStore(hass, current, "invalid")
    state.store.async_load = AsyncMock(return_value={"bindings": bindings})
    state.store.async_save = AsyncMock()
    await state.async_restore()
    assert not current.registrations
    await state.async_close()
