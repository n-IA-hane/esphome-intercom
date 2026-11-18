"""Exercise first-boot enrollment through the actual PBX socket and HA HTTP view."""

import asyncio
import ipaddress
from unittest.mock import AsyncMock
from xml.etree import ElementTree

import pytest
from aiohttp import ClientSession
from ifaddr import get_adapters as get_host_adapters
from homeassistant.components import network
from homeassistant.config_entries import ConfigEntryStore
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import flush_store

from custom_components.voip_stack.physical_phone_runtime import async_reconcile_physical_phone_runtime
from .test_managed_phones_ha import phone_lab as phone_lab, custom_integration_path as custom_integration_path
from .test_physical_phone_dhcp_ha import MAC, native_cache
from .test_physical_phone_pnp_ha import Peer, assert_no_notify, pnp_listener as pnp_listener, pnp_request
from .test_physical_phone_runtime_ha import (
    add, disable_http_server as disable_http_server, hass_config_dir as hass_config_dir,
    hass_storage as hass_storage, phone,
)

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("disable_http_server", [True], indirect=True)
async def test_admin_retry_survives_reload_and_first_valid_http_download_closes_dhcp_enrollment(
        pnp_listener, monkeypatch, unused_tcp_port, disable_http_server):
    lab = pnp_listener
    # A real local private source lets the native DHCP matcher keep its loopback exclusion.
    # The kernel loops both ends of this exchange back to the current test host.
    # Core's mock_network fixture patches the module attribute with a fictitious host.
    addresses = [ipaddress.ip_interface(f"{address.ip}/{address.network_prefix}")
                 for adapter in get_host_adapters() for address in adapter.ips if address.is_IPv4]
    local = next((ip for ip in addresses if ip.ip.is_private and not ip.ip.is_loopback and ip.network.prefixlen <= 29), None)
    if local is None:
        pytest.skip("A local private IPv4 is required for this wire test")
    source = str(local.ip)
    simulated_ha = next(str(ip) for ip in local.network.hosts() if ip != local.ip)
    monkeypatch.setattr(network, "async_get_adapters", AsyncMock(return_value=[{"enabled": True,
        "ipv4": [{"address": simulated_ha, "network_prefix": local.network.prefixlen}]}]))
    monkeypatch.setattr(network, "async_get_source_ip", AsyncMock(return_value="127.0.0.1"))
    # Join the existing local test PBX socket, avoiding a bind on the host's standard port.
    pbx = ("127.0.0.1", lab.runtime.transport_config["sip_port"])
    monkeypatch.setattr("custom_components.voip_stack.physical_phone_discovery.PNP_PORT", pbx[1])
    origin = f"http://127.0.0.1:{unused_tcp_port}"
    assert await async_setup_component(lab.hass, "http", {"http": {"server_host": ["127.0.0.1"], "server_port": unused_tcp_port}})
    await lab.hass.http.start()
    transport, peer = await lab.hass.loop.create_datagram_endpoint(lambda: Peer(MAC), local_addr=(source, 0))
    try:
        lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
            "phone_provisioning": {"enabled": True, "base_url": origin, "allow_insecure_http": True}})
        data = phone(mac=MAC, profile_id="snom/360", lines=[])
        record = add(lab, data)
        watcher = native_cache(lab)
        watcher.async_process_client(source, "snom360", MAC)
        await async_reconcile_physical_phone_runtime(lab.hass, lab.entry)

        async def announce(call_id):
            transport.sendto(pnp_request(peer, call_id, "snom"), pbx)
            assert (await peer.response(call_id, method="SUBSCRIBE")).status_code == 200

        async def persisted_enrollment():
            await flush_store(lab.hass.config_entries._store)
            stored = await ConfigEntryStore(lab.hass).async_load()
            entry = next(item for item in stored["entries"] if item["entry_id"] == lab.entry.entry_id)
            handset = next(item for item in entry["subentries"] if item["subentry_id"] == record.subentry_id)
            return handset["data"]["pnp_enrollment"]

        await announce("old-inventory")
        await assert_no_notify(peer)
        await lab.hass.services.async_call("voip_stack", "provision_phone", {"phone_id": data["phone_id"]}, blocking=True)
        assert record.data["pnp_enrollment"]["host"] == source
        assert await persisted_enrollment() == record.data["pnp_enrollment"]
        await lab.runtime.phone_discovery.async_close()
        await lab.runtime.physical_phones.async_close()
        lab.runtime.phone_discovery = lab.runtime.physical_phones = None
        await async_reconcile_physical_phone_runtime(lab.hass, lab.entry)
        assert not lab.runtime.phone_discovery.candidates
        await announce("fresh-boot")
        notice = await asyncio.wait_for(peer.notifies.get(), 3)
        url = notice.body.decode()
        assert url.startswith(origin + "/api/voip_stack/phone_provisioning/") and url.endswith("/" + MAC + ".xml")
        async with ClientSession() as client:
            async with client.get(url) as response:
                assert response.status == 200
                assert ElementTree.fromstring(await response.read()).findtext("phone-settings/setting_server") == url
        assert record.data["pnp_enrollment"] == {} and await persisted_enrollment() == {}
        await announce("already-downloaded")
        await assert_no_notify(peer)
    finally:
        transport.close()
        await lab.hass.http.stop()
