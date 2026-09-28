"""Static trunk lifecycle and literal source ACL behavior."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from .voip_phase1_support import sip_trunk, asyncio


def config(**kwargs):
    return sip_trunk.SipTrunkConfig(
        enabled=True, transport=kwargs.get("transport", "udp"), server="127.0.0.1", port=15062,
        domain="", username="", auth_username="", password="", expires=300,
        register=False, allowed_ips=kwargs.get("allowed_ips", ("127.0.0.2", "2001:db8:1::/64")),
    )


@pytest.mark.parametrize("transport", ["udp", "tcp"])
def test_static_trunk_lifecycle_never_registers_or_allocates_refresh_socket(transport):
    async def run():
        trunk = sip_trunk.SipTrunkClient(config=config(transport=transport), local_ip="127.0.0.1", local_sip_port=15060)
        trunk._send_raw = AsyncMock()
        await trunk.start()
        assert trunk.ready and not trunk.registered
        assert trunk.snapshot()["trunk_registration_enabled"] is False
        assert trunk._refresh_task is None and trunk._receive_task is None
        assert trunk.transport is None and trunk.writer is None
        assert await trunk.register() == "registration_disabled"
        assert trunk.accepts_inbound_source("127.0.0.2", 50000, transport)
        assert trunk.accepts_inbound_source("2001:db8:1::123", 5060, transport)
        assert not trunk.accepts_inbound_source("127.0.0.3", 5060, transport)
        assert not trunk.accepts_inbound_source("2001:db8:2::1", 5060, transport)
        assert not trunk.accepts_inbound_source("invalid", 5060, transport)
        assert not trunk.accepts_inbound_source("127.0.0.2", 5060, "wrong")
        await trunk.stop()
        await trunk.stop()
        assert not trunk.ready
        assert not trunk.accepts_inbound_source("127.0.0.2", 5060, transport)
        trunk._send_raw.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("allowed", [(), ("invalid",), ("0.0.0.0/0",), ("::/0",)])
def test_static_trunk_requires_restricted_literal_sources(allowed):
    with pytest.raises(ValueError):
        sip_trunk.SipTrunkClient(config=config(allowed_ips=allowed), local_ip="127.0.0.1", local_sip_port=15060)


def test_registered_trunk_still_requires_registration_before_trusting_sources():
    trunk = sip_trunk.SipTrunkClient(config=replace(config(), register=True), local_ip="127.0.0.1", local_sip_port=15060)
    trunk._trusted_udp_hosts = frozenset({"127.0.0.2"})
    assert not trunk.ready
    assert not trunk.accepts_inbound_source("127.0.0.2", 5060, "UDP")
    trunk.registered = True
    assert trunk.ready
    assert trunk.accepts_inbound_source("127.0.0.2", 5060, "UDP")


def test_static_trunk_does_not_advertise_an_unavailable_tls_listener():
    with pytest.raises(ValueError, match="Static trunks require UDP or TCP"):
        sip_trunk.SipTrunkClient(config=config(transport="tls"), local_ip="127.0.0.1", local_sip_port=15060)
