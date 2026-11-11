"""Physical discovery starts and stops around the real SIP listener owner."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component

from .test_queue_agent_wire_ha import Peer
from .test_trunk_calls_ha import lab as lab, trunks as trunks

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_missing_ipv4_discovery_is_nonfatal_and_releases_partial_resources(trunks, monkeypatch, hass_client):
    from custom_components.voip_stack import physical_phone_provisioning as module
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery
    lab = trunks
    entry = lab.hass.config_entries.async_entries("voip_stack")[0]
    original = lab.runtime.sip
    assert await async_setup_component(lab.hass, "http", {})
    await hass_client()
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "phone_provisioning": {"enabled": True}})
    monkeypatch.setattr(PhoneDiscovery, "async_start", AsyncMock(side_effect=HomeAssistantError("No source IPv4")))
    try:
        await module.async_reconcile_physical_phones(lab.hass, entry)
        manager = lab.runtime.physical_phones
        assert manager is not None and manager.discovery is None
        assert manager.discovery_error == "phone_discovery_unavailable"
        assert lab.runtime.sip is original
        lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "phone_provisioning": {"enabled": False}})
        await module.async_reconcile_physical_phones(lab.hass, entry)
        assert lab.runtime.physical_phones is None and not manager._unsubscribers
        assert not lab.hass.services.has_service("voip_stack", "provision_phone")
    finally:
        if lab.runtime.physical_phones is not None:
            await lab.runtime.physical_phones.async_close()
            lab.runtime.physical_phones = None


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("path", ["pbx", "discovery"])
async def test_real_pnp_wire_follows_feature_toggle_without_reloading_sip_transport(trunks, monkeypatch, hass_client, path):
    from custom_components.voip_stack import physical_phone_provisioning as module
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery
    from homeassistant.components import network
    lab = trunks
    entry = lab.hass.config_entries.async_entries("voip_stack")[0]
    original = lab.runtime.sip
    endpoint = original.component("udp_listener")
    udp = endpoint.udp_server.transport
    listener = ("127.0.0.1", lab.runtime.transport_config["sip_port"])
    assert await async_setup_component(lab.hass, "http", {})
    await hass_client()
    monkeypatch.setattr(network, "async_get_source_ip", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(PhoneDiscovery, "_start_flow", AsyncMock())
    transport, peer = await lab.hass.loop.create_datagram_endpoint(lambda: Peer("001122334455"), local_addr=("127.0.0.4", 0))
    event = 'ua-profile;vendor="Fanvil";model="X3U Pro"'
    extra = (("Event", event), ("Expires", "0"))
    try:
        transport.sendto(peer.request("SUBSCRIBE", "provision", "off-before", extra=extra), listener)
        assert (await peer.response("off-before", method="SUBSCRIBE")).status_code == 403
        assert peer.notifies.empty()
        lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "phone_provisioning": {
            "enabled": True, "base_url": "https://ha.example.test"}})
        await module.async_reconcile_physical_phones(lab.hass, entry)
        manager = lab.runtime.physical_phones
        discovery = manager.discovery
        assert discovery is not None
        target = listener if path == "pbx" else ("127.0.0.1", discovery._transport.get_extra_info("sockname")[1])
        transport.sendto(peer.request("SUBSCRIBE", "provision", "on", extra=extra), target)
        assert (await peer.response("on", method="SUBSCRIBE")).status_code == 200
        notify = await asyncio.wait_for(peer.notifies.get(), 3)
        assert notify.header("Event") == event and notify.header("Content-Type") == "application/url"
        assert bool(notify.body)
        assert len(discovery.candidates) == 1
        assert not lab.calls.sessions
        lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "phone_provisioning": {"enabled": False}})
        await module.async_reconcile_physical_phones(lab.hass, entry)
        assert lab.runtime.physical_phones is None
        assert discovery._transport is None and discovery._membership is None and not discovery._tasks
        assert not discovery.candidates and not manager._unsubscribers and not manager.files
        assert lab.runtime.sip is original and endpoint.udp_server.transport is udp
        transport.sendto(peer.request("SUBSCRIBE", "provision", "off-after", extra=extra), listener)
        assert (await peer.response("off-after", method="SUBSCRIBE")).status_code == 403
        assert peer.notifies.empty() and not lab.calls.sessions
    finally:
        transport.close()
        if lab.runtime.physical_phones is not None:
            await lab.runtime.physical_phones.async_close()
            lab.runtime.physical_phones = None


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_disable_during_discovery_start_does_not_attach_late_callbacks(trunks, monkeypatch, hass_client):
    from custom_components.voip_stack import physical_phone_provisioning as module
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery
    lab = trunks
    entry = lab.hass.config_entries.async_entries("voip_stack")[0]
    assert await async_setup_component(lab.hass, "http", {})
    await hass_client()
    entered, release = asyncio.Event(), asyncio.Event()

    async def start(_discovery):
        entered.set()
        await release.wait()

    monkeypatch.setattr(PhoneDiscovery, "async_start", start)
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "phone_provisioning": {"enabled": True}})
    enabling = asyncio.create_task(module.async_reconcile_physical_phones(lab.hass, entry))
    manager = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        manager = lab.runtime.physical_phones
        lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "phone_provisioning": {"enabled": False}})
        await module.async_reconcile_physical_phones(lab.hass, entry)
        assert lab.runtime.physical_phones is None and not manager._unsubscribers
        release.set()
        await asyncio.wait_for(enabling, 2)
        assert not manager._unsubscribers
        assert manager.discovery is None or manager.discovery._closed
    finally:
        release.set()
        await asyncio.gather(enabling, return_exceptions=True)
        if manager is not None:
            await manager.async_close()
        lab.runtime.physical_phones = None


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("stage", ["network", "transport"])
async def test_retired_discovery_never_keeps_a_late_udp_transport(trunks, monkeypatch, stage):
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery
    from custom_components.voip_stack.physical_phone_provisioning import PhysicalPhoneProvisioner
    from homeassistant.components import network
    lab = trunks
    entry = lab.hass.config_entries.async_entries("voip_stack")[0]
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "phone_provisioning": {"enabled": True}})
    owner = PhysicalPhoneProvisioner(lab.hass, entry)
    lab.runtime.physical_phones = owner
    discovery = owner.discovery = PhoneDiscovery(owner)
    entered, release = asyncio.Event(), asyncio.Event()
    created = []
    loop = asyncio.get_running_loop()
    original_create = loop.create_datagram_endpoint

    async def source_ip(*_args, **_kwargs):
        if stage == "network":
            entered.set()
            await release.wait()
        return "127.0.0.1"

    async def create(*args, **kwargs):
        result = await original_create(*args, **kwargs)
        created.append(result[0])
        if stage == "transport":
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(network, "async_get_source_ip", source_ip)
    monkeypatch.setattr(loop, "create_datagram_endpoint", create)
    starting = asyncio.create_task(discovery.async_start())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await discovery.async_close()
        release.set()
        await asyncio.wait_for(starting, 2)
        assert discovery._transport is None
        assert all(transport.is_closing() for transport in created)
        if stage == "network":
            assert not created
    finally:
        release.set()
        await asyncio.gather(starting, return_exceptions=True)
        for transport in created:
            transport.close()
        await discovery.async_close()
        lab.runtime.physical_phones = None
