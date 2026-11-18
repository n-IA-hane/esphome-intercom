"""Native DHCP hints share the physical inventory proposal, never PnP authority."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components import dhcp
from homeassistant.components.dhcp import WatcherBase, async_index_integration_matchers
from homeassistant.components.dhcp.models import DATA_DHCP, DHCPData
from homeassistant.config_entries import SOURCE_DHCP, SOURCE_IGNORE
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from custom_components.voip_stack.core import sip
from custom_components.voip_stack.physical_phone import physical_phone_records
from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery, dhcp_identity

from .test_managed_phones_ha import phone_lab as phone_lab, custom_integration_path as custom_integration_path
from .test_physical_phone_pnp_ha import adopt_candidate
from .support.ha_frontend import prepare_frontend_dependency

pytestmark = pytest.mark.ha
MAC = "000413000001"
HOST = "192.168.10.67"


def info(**updates):
    return DhcpServiceInfo(**{"ip": HOST, "hostname": "snom360-000001", "macaddress": MAC, **updates})


@pytest.fixture
async def discovery_lab(phone_lab, monkeypatch):
    lab = phone_lab
    lab.hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    prepare_frontend_dependency(lab.hass)
    collector = lab.runtime.phone_discovery = PhoneDiscovery(lab.hass, lab.entry)
    catalog = Mock(side_effect=AssertionError("DHCP discovery constructed the catalog"))
    monkeypatch.setattr("custom_components.voip_stack.phone_profile_catalog.PhoneProfileCatalog", catalog)
    lab.collector, lab.catalog_guard = collector, catalog
    yield lab
    if lab.runtime.phone_discovery is not None:
        await lab.runtime.phone_discovery.async_close()
        lab.runtime.phone_discovery = None
    lab.runtime.physical_phones = None


def native_cache(lab, *, matched=False):
    matchers = []
    if matched:
        manifest = json.loads((Path(__file__).parents[1] / "custom_components/voip_stack/manifest.json").read_text())
        matchers = [{**matcher, "domain": "voip_stack"} for matcher in manifest["dhcp"]]
    data = DHCPData(integration_matchers=async_index_integration_matchers(matchers))
    lab.hass.data[DATA_DHCP] = data  # Native Core fixture state, consumed through its public API.
    lab.hass.config.components.add("dhcp")
    return WatcherBase(lab.hass, data)


def proposals(lab):
    return lab.hass.config_entries.flow.async_progress_by_handler("voip_stack")


@pytest.mark.parametrize("updates", [
    {"macaddress": "001122000001"}, {"macaddress": "000413000001-tail"},
    {"hostname": "otherphone-000001"}, {"ip": "127.0.0.1"}, {"ip": "0.0.0.0"},
    {"hostname": "snom3600"}, {"hostname": "snom360-other".replace("-", "")}, {"hostname": "snom360-"},
    {"ip": "224.0.1.75"}, {"ip": "8.8.8.8"}, {"ip": "fe80::1"},
])
def test_dhcp_identity_rejects_nonmatching_or_unsafe_hints(updates):
    with pytest.raises(ValueError, match="invalid_phone_discovery"):
        dhcp_identity(info(**updates))


@pytest.mark.parametrize(("other", "expected"), [
    (None, HOST), (info(), HOST), (info(ip="192.168.10.68"), ""),
    (info(macaddress="001122334455"), ""), (SimpleNamespace(ip=HOST), ""),
    (SimpleNamespace(macaddress=MAC), ""), (info(hostname="unknown"), ""),
])
def test_native_address_requires_an_unambiguous_model_mac_and_ip(monkeypatch, other, expected):
    from custom_components.voip_stack.physical_phone_enrollment import phone_dhcp_host

    hass = SimpleNamespace(config=SimpleNamespace(components={"dhcp"}))
    monkeypatch.setattr(dhcp, "async_discovered_service_info", lambda _hass: [info(), other])
    assert phone_dhcp_host(hass, {"mac": MAC, "profile_id": "snom/360"}) == expected


@pytest.mark.parametrize("model", (
    "T23G T23P T27G T27P T29G T31G T33G T40G T40P T41P T41S T42G T42S T42U T43U "
    "T46G T46S T46U T48G T48S T52S T53 T53W T54S T57W W70B"
).split())
def test_yealink_dhcp_option_12_uses_documented_model(model):
    hostname = model if model == "W70B" else f"SIP-{model}"
    assert dhcp_identity(info(macaddress="805e0c000001", hostname=hostname)) == (
        "805e0c000001", HOST, "yealink", model)


@pytest.mark.parametrize(("mac", "hostname"), [
    ("000413000001", "SIP-T46U"), ("805e0c000001", "snom360-000001"),
    ("805e0c000001", "SIP-T46U-spare"), ("805e0c000001", "SIP-T46"),
    ("805e0c000001", "SIP-T21P_E2"), ("805e0c000001", "SIP-W70B"),
    ("805e0c000001", "W52P"), ("805e0c000001", "W80B"),
    ("80:5e-0c:00:00:01", "SIP-T46U"), ("000b82000001", "GRP2614"),
    ("0c383e000001", "Fanvil"), ("805e0c000001", "meeting-room"),
])
def test_dhcp_requires_an_unambiguous_known_vendor_and_model(mac, hostname):
    with pytest.raises(ValueError, match="invalid_phone_discovery"):
        dhcp_identity(SimpleNamespace(macaddress=mac, hostname=hostname, ip=HOST))


async def test_native_yealink_dhcp_dispatches_known_models_without_granting_delivery(discovery_lab):
    lab = discovery_lab
    watcher = native_cache(lab, matched=True)
    if not lab.hass.is_running:
        await lab.hass.async_start()
    watcher.async_process_client("192.168.10.61", "SIP-T46U", "805e0c000001")
    watcher.async_process_client("192.168.10.62", "W70B", "001565000002")
    watcher.async_process_client("192.168.10.63", "SIP-T46U-spare", "805e0c000003")
    watcher.async_process_client("192.168.10.64", "SIP-T46U", "000413000004")
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    flows = proposals(lab)
    assert {flow["context"]["title_placeholders"]["name"] for flow in flows} == {"Yealink T46U", "Yealink W70B"}
    assert all(flow["context"]["source"] == SOURCE_DHCP for flow in flows)
    assert set(lab.collector.candidates) == {"805e0c000001", "001565000002"}
    assert all(not candidate.token and not candidate.pnp_host for candidate in lab.collector.candidates.values())
    assert not physical_phone_records(lab.entry) and not lab.sent
    lab.catalog_guard.assert_not_called()


async def test_native_dhcp_matcher_creates_lazy_proposal_without_taking_existing_device(discovery_lab):
    lab = discovery_lab
    registry = dr.async_get(lab.hass)
    foreign = registry.async_get_or_create(config_entry_id=lab.native.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, "00:04:13:00:00:01")}, name="Existing DHCP device")
    before = foreign.config_entry_id
    watcher = native_cache(lab, matched=True)
    if not lab.hass.is_running:
        await lab.hass.async_start()
    watcher.async_process_client(HOST, "snom360-000001", MAC)
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    flows = proposals(lab)
    assert len(flows) == 1 and flows[0]["step_id"] == "phone_discovered"
    assert flows[0]["context"]["source"] == SOURCE_DHCP
    assert flows[0]["context"]["title_placeholders"]["name"] == "Snom 360"
    candidate = lab.collector.candidates[MAC]
    assert candidate.host == HOST and candidate.flow_id == flows[0]["flow_id"]
    assert not candidate.token and not candidate.phone_id and candidate.native_subentry is None
    assert not candidate.pnp_host and not physical_phone_records(lab.entry)
    assert lab.runtime.physical_phones is None and not lab.sent and not lab.events
    assert registry.async_get(foreign.id).config_entry_id == before
    lab.catalog_guard.assert_not_called()
    await lab.collector.async_close()
    with pytest.raises(UnknownFlow):
        lab.hass.config_entries.flow.async_get(flows[0]["flow_id"])


async def test_known_dhcp_is_backfilled_once_then_updated_by_native_flow_and_reloaded(discovery_lab, monkeypatch):
    lab = discovery_lab
    watcher = native_cache(lab)
    watcher.async_process_client(HOST, "snom360-000001", MAC)
    assert dhcp.async_discovered_service_info(lab.hass) == [info()]
    read_cache = Mock(wraps=dhcp.async_discovered_service_info)
    monkeypatch.setattr(dhcp, "async_discovered_service_info", read_cache)
    await lab.collector.async_start()
    await lab.hass.async_block_till_done()
    await lab.collector.async_start()
    read_cache.assert_called_once_with(lab.hass)
    original = proposals(lab)[0]
    result = await lab.hass.config_entries.flow.async_init("voip_stack", context={"source": SOURCE_DHCP},
        data=info(ip="192.168.10.68"))
    assert result["type"] == "abort" and result["reason"] == "already_in_progress"
    assert len(proposals(lab)) == 1 and proposals(lab)[0]["flow_id"] == original["flow_id"]
    assert lab.collector.candidates[MAC].host == "192.168.10.68"
    # The native DHCP cache already contains the lease before a PBX reload.
    watcher.async_process_client("192.168.10.68", "snom360-000001", MAC)
    await lab.collector.async_close()
    lab.collector = lab.runtime.phone_discovery = PhoneDiscovery(lab.hass, lab.entry)
    await lab.collector.async_start()
    await lab.hass.async_block_till_done()
    assert read_cache.call_count == 2 and len(proposals(lab)) == 1
    assert lab.collector.candidates[MAC].host == "192.168.10.68"
    assert not lab.collector.candidates[MAC].pnp_host
    lab.catalog_guard.assert_not_called()


@pytest.mark.parametrize("pnp_before_adoption", [False, True])
async def test_dhcp_never_grants_pnp_delivery_without_an_actual_pre_adoption_request(discovery_lab, pnp_before_adoption):
    lab = discovery_lab
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        "phone_provisioning": {"enabled": True, "base_url": "https://ha.example.test"}})
    result = await lab.hass.config_entries.flow.async_init("voip_stack", context={"source": SOURCE_DHCP}, data=info())
    assert result["step_id"] == "phone_discovered"
    candidate = lab.collector.candidates[MAC]
    uri = f"sip:MAC%3a{MAC}@localhost"
    request = sip.SipMessage(method="SUBSCRIBE", uri=uri, headers=(
        ("From", f"<{uri}>;tag=one"), ("Contact", f"<sip:{HOST}:5060>"),
        ("Event", 'ua-profile;vendor="snom";model="snom360"')))
    if pnp_before_adoption:
        assert (await lab.collector.handle(request, (HOST, 5060), "UDP")).follow_up is None
        await lab.hass.async_block_till_done()
        assert len(proposals(lab)) == 1
    assert candidate.pnp_host == (HOST if pnp_before_adoption else "")
    adopt_candidate(lab.hass, lab.entry, candidate, "snom")
    manager = lab.runtime.physical_phones = SimpleNamespace(owned=lambda: True, schedule=Mock(),
        async_provisioning_url=AsyncMock(return_value=f"https://ha.example.test/{candidate.token}/{MAC}.xml"))
    response = await lab.collector.handle(request, (HOST, 5060), "UDP")
    assert response.status == 200
    assert (response.follow_up is not None) is pnp_before_adoption
    if pnp_before_adoption:
        assert candidate.token.encode() in response.follow_up.body
        manager.schedule.assert_called_once_with({candidate.phone_id})
    else:
        manager.schedule.assert_not_called()
        assert not candidate.pnp_host
    # Later DHCP changes never authorize or redirect an adopted phone's URL.
    assert lab.collector.observe_dhcp(info(ip="192.168.10.69")) is None
    assert candidate.host == HOST and candidate.pnp_host == (HOST if pnp_before_adoption else "")


async def test_admin_enrollment_follows_unique_dhcp_within_same_lan_and_rechecks_after_render(
        discovery_lab, monkeypatch):
    from homeassistant.components import network
    from custom_components.voip_stack.physical_phone_enrollment import arm_dhcp_enrollment
    from .test_physical_phone_runtime_ha import add, phone

    lab = discovery_lab
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        "phone_provisioning": {"enabled": True, "base_url": "https://ha.example.test"}})
    watcher = native_cache(lab)
    watcher.async_process_client(HOST, "snom360", MAC)
    data = phone(profile_id="snom/360", mac=MAC)
    arm_dhcp_enrollment(lab.hass, data)
    record = add(lab, data)
    issued = data["pnp_enrollment"]["issued"]
    monkeypatch.setattr(network, "async_get_adapters", AsyncMock(return_value=[{"enabled": True,
        "ipv4": [{"address": "192.168.10.1", "network_prefix": 24}]}]))
    manager = lab.runtime.physical_phones = SimpleNamespace(owned=lambda: True, schedule=Mock(),
        async_provisioning_url=AsyncMock(return_value="https://ha.example.test/config.xml"))

    async def deliver(source, *, extra=(), model="snom360", contact=None):
        uri = f"sip:MAC%3a{MAC}@localhost"
        request = sip.SipMessage(method="SUBSCRIBE", uri=uri, headers=(("From", f"<{uri}>;tag=one"),
            ("Event", f'ua-profile;vendor="snom";model="{model}"'),
            ("Contact", f"<sip:{contact or source}:5060>"), *extra))
        return (await lab.collector.handle(request, (source, 5060), "UDP")).follow_up

    assert not lab.collector.candidates and await deliver(HOST)
    changed = "192.168.10.68"
    assert not await deliver(changed)
    watcher.async_process_client(changed, "snom360", MAC)
    assert not await deliver(HOST) and await deliver(changed)
    assert not await deliver(changed, contact=HOST)
    assert not await deliver(changed, extra=(("Record-Route", "<sip:router.example.test;lr>"),))
    assert not await deliver(changed, model="D785")
    watcher.async_process_client(changed, "computer", "001122000099")
    assert not await deliver(changed)
    watcher.async_process_client("192.168.10.99", "computer", "001122000099")
    off_link = "192.168.11.68"
    watcher.async_process_client(off_link, "snom360", MAC)
    assert not await deliver(off_link)
    watcher.async_process_client(changed, "snom360", MAC)
    lab.hass.config_entries.async_update_subentry(lab.entry, record, data={**record.data,
        "pnp_enrollment": {**data["pnp_enrollment"], "issued": issued - 3601}})
    assert not await deliver(changed)
    lab.hass.config_entries.async_update_subentry(lab.entry, record, data=data)

    async def rotate_during_render(*_args, **_kwargs):
        lab.hass.config_entries.async_update_subentry(lab.entry, record,
            data={**record.data, "provisioning_token": "new-test-credential"})
        return "https://ha.example.test/config.xml"
    manager.async_provisioning_url.side_effect = rotate_during_render
    assert not await deliver(changed) and not await deliver(changed)


async def test_pnp_socket_failure_preserves_dhcp_collector_and_its_owned_flow(discovery_lab, monkeypatch, caplog):
    lab = discovery_lab
    watcher = native_cache(lab)
    watcher.async_process_client(HOST, "snom360-000001", MAC)
    partial = lab.collector._transport = Mock()
    monkeypatch.setattr(lab.collector, "_async_start_owned", AsyncMock(side_effect=OSError("bind occupied")))
    from custom_components.voip_stack.physical_phone_runtime import async_reconcile_physical_phone_runtime
    await async_reconcile_physical_phone_runtime(lab.hass, lab.entry)
    await lab.hass.async_block_till_done()
    assert lab.runtime.phone_discovery is lab.collector and lab.collector.owned()
    assert lab.collector._transport is None
    partial.close.assert_called_once()
    assert len(proposals(lab)) == 1 and "native DHCP remains active" in caplog.text
    assert lab.runtime.physical_phones is None
    lab.catalog_guard.assert_not_called()


async def test_without_loaded_dhcp_sip_only_start_never_reads_or_requires_dhcp(discovery_lab, monkeypatch):
    lab = discovery_lab
    assert "dhcp" not in lab.hass.config.components
    read = Mock(side_effect=AssertionError("Optional DHCP was read while unloaded"))
    monkeypatch.setattr(dhcp, "async_discovered_service_info", read)
    await lab.collector.async_start()
    assert lab.collector.owned() and not lab.collector.candidates
    read.assert_not_called()
    manifest = json.loads((Path(__file__).parents[1] / "custom_components/voip_stack/manifest.json").read_text())
    assert "dhcp" not in manifest["dependencies"]


async def test_ignored_and_disabled_dhcp_discovery_create_no_inventory_or_pending_card(discovery_lab):
    lab = discovery_lab
    ignored = await lab.hass.config_entries.flow.async_init("voip_stack", context={"source": SOURCE_IGNORE},
        data={"unique_id": "physical_phone:" + MAC, "title": "Ignored Snom"})
    result = await lab.hass.config_entries.flow.async_init("voip_stack", context={"source": SOURCE_DHCP}, data=info())
    assert result["type"] == "abort" and result["reason"] == "already_configured"
    assert not proposals(lab) and not physical_phone_records(lab.entry)
    assert ignored["result"] in lab.hass.config_entries.async_entries("voip_stack")
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        "phone_provisioning": {"discovery_enabled": False}})
    result = await lab.hass.config_entries.flow.async_init("voip_stack", context={"source": SOURCE_DHCP},
        data=info(macaddress="000413000002", hostname="snom360-000002"))
    assert result["type"] == "abort" and result["reason"] == "phone_discovery_expired"
    assert not proposals(lab) and not physical_phone_records(lab.entry)
