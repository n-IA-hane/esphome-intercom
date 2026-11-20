"""Physical discovery starts and stops around the real SIP listener owner."""

import asyncio
import socket
from dataclasses import replace
from unittest.mock import AsyncMock, Mock
from xml.etree import ElementTree

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from homeassistant.config_entries import SOURCE_IGNORE
from homeassistant.data_entry_flow import UnknownFlow

from custom_components.voip_stack.core import sip

from .test_queue_agent_wire_ha import Peer
from .test_physical_phone_delivery_url_ha import snom_profile
from .test_physical_phone_runtime_ha import (
    disable_http_server as disable_http_server, hass_config_dir as hass_config_dir, hass_storage as hass_storage,
)
from .test_managed_phones_ha import phone_lab as phone_lab, custom_integration_path as custom_integration_path

pytestmark = pytest.mark.ha


@pytest.fixture
async def pnp_listener(phone_lab, monkeypatch, socket_enabled, unused_tcp_port_factory):
    """Use the real PBX listener without unrelated registered trunk clients."""
    from custom_components.voip_stack import endpoint_runtime, peer_snapshot
    from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
    from custom_components.voip_stack.phone_profile_catalog import PhoneProfileCatalog

    async def local_profile(_self, identity, *, commit=""):
        if identity == "snom/360":
            return snom_profile()
        filenames = {"grandstream/grp2614": "cfg{mac}.xml", "fanvil/x3u-pro": "{mac}.cfg"}
        filename = filenames[identity]
        caps = {"max_lines": 1, "languages": {}, "functions": [], "key_slots": []}
        if identity == "grandstream/grp2614":
            caps["delivery"] = {"pnp": filename}
        return ({"id": identity, "vendor": identity.split("/")[0], "capabilities": caps, "outputs": [{"path": filename,
                 "format": "xml", "template": "templates/device.jinja2"}]},
                {"templates/device.jinja2": "<gs_provision/>" if identity == "grandstream/grp2614" else "<sysConf/>"})

    monkeypatch.setattr(PhoneProfileCatalog, "async_profile", local_profile)

    lab = phone_lab
    await lab.runtime.sip.shutdown()
    lab.runtime.sip = lab.calls = SipEndpointRuntime(allow_dark_sessions=True)
    for _ in range(16):
        port = unused_tcp_port_factory()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.bind(("0.0.0.0", port))
        except OSError:
            continue
        lab.runtime.transport_config["sip_port"] = port
        break
    else:
        raise RuntimeError("No free UDP/TCP port for the PnP test listener")
    monkeypatch.setattr(peer_snapshot, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
    snapshot = AsyncMock(return_value=[])
    monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", snapshot)
    assert await endpoint_runtime.async_start_sip_endpoint(lab.hass)
    try:
        yield lab
    finally:
        if lab.runtime.phone_discovery is not None:
            await lab.runtime.phone_discovery.async_close()
            lab.runtime.phone_discovery = None
        if lab.runtime.physical_phones is not None:
            await lab.runtime.physical_phones.async_close()
            lab.runtime.physical_phones = None
        await endpoint_runtime.async_stop_sip_endpoint(lab.hass)


def pnp_request(peer, call_id, vendor="Fanvil"):
    """The documented wire shapes, with synthetic MACs and loopback sources."""
    models = {"snom": "snom360", "Grandstream": "GRP2614", "Fanvil": "X3U Pro"}
    event = f'ua-profile;profile-type="device";vendor="{vendor}";model="{models[vendor]}"'
    if vendor == "snom":
        event += ';version="8.7.3.25.9"'
    raw = peer.request("SUBSCRIBE", "provision", call_id, extra=(("Event", event), ("Expires", "0")))
    if vendor == "Fanvil":
        return raw
    message = sip.parse_message(raw)
    uri = f"sip:MAC%3a{peer.username.upper()}@localhost"
    host, port = peer.address
    headers = [(key, value) for key, value in message.headers if key not in {"From", "To", "Contact"}]
    headers.extend((("From", f"<{uri}>;tag={call_id}"), ("To", f"<{uri}>"),
                    ("Contact", f"<sip:{host}:{port}>"),
                    ("User-Agent", "snom360/8.7.3.25.9" if vendor == "snom" else "Grandstream GRP2614")))
    return sip.build_request("SUBSCRIBE", uri, headers)


async def assert_no_notify(peer):
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(peer.notifies.get(), .05)


def adopt_candidate(hass, entry, candidate, vendor):
    from custom_components.voip_stack.physical_phone import draft_physical_subentry, normalize_physical_phone

    seed = candidate.setup_data()
    data = normalize_physical_phone({**seed, "name": "Adopted phone",
        "profile_id": {"snom": "snom/360", "Fanvil": "fanvil/x3u-pro", "Grandstream": "grandstream/grp2614"}[vendor],
        "lines": [], "keys": []},
        previous=seed)
    record = draft_physical_subentry(data, previous=candidate.native_subentry, title=data["name"])
    hass.config_entries.async_add_subentry(entry, record)
    return record


@pytest.mark.parametrize("user", ["MAC%3a00135E000001", "mac%3A00135e000001", "00135E000001"])
def test_pnp_identity_decodes_only_the_complete_uri_user(user):
    from custom_components.voip_stack.physical_phone_discovery import pnp_identity

    uri = f"sip:{user}@localhost"
    request = sip.SipMessage(method="SUBSCRIBE", uri=uri, headers=(
        ("From", f"<{uri}>;tag=unrelated"), ("Contact", "<sip:192.168.10.67:5060>"),
        ("Event", 'ua-profile;vendor="snom";model="snom360"')))
    assert pnp_identity(request, ("192.168.10.67", 5060)) == ("00135e000001", "snom", "snom360")


@pytest.mark.parametrize("user", ["ordinary", "phone-00135e000001", "00135e000001-tail",
                                  "MAC%253a00135e000001", "00135e000001:password"])
def test_pnp_does_not_mistake_hosts_tags_display_names_or_partial_users_for_mac(user):
    from custom_components.voip_stack.physical_phone_discovery import pnp_identity

    request = sip.SipMessage(method="SUBSCRIBE", uri=f"sip:{user}@00135e000001", headers=(
        ("From", '"00135e000001" <sip:ordinary@localhost>;tag=00135e000001'),
        ("Contact", "<sip:ordinary@127.0.0.1;random=00135e000001>"),
        ("Event", 'ua-profile;vendor="snom";model="00135e000001"')))
    with pytest.raises(ValueError, match="phone_discovery_mac_missing"):
        pnp_identity(request, ("192.168.10.67", 5060))


def test_pnp_explicit_mac_parameter_and_conflicting_structured_identities():
    from custom_components.voip_stack.physical_phone_discovery import pnp_identity

    request = sip.SipMessage(method="SUBSCRIBE", uri="sip:provision@localhost", headers=(
        ("From", "<sip:ordinary@localhost>;tag=any"), ("Contact", "<sip:192.168.10.67:5060>"),
        ("Event", 'ua-profile;MACADDRESS="00:13:5E:00:00:01";vendor="snom";model="snom360"')))
    assert pnp_identity(request, ("192.168.10.67", 5060))[0] == "00135e000001"
    conflicting = replace(request, uri="sip:MAC%3a00135E000002@localhost")
    with pytest.raises(ValueError, match="phone_discovery_mac_conflict"):
        pnp_identity(conflicting, ("192.168.10.67", 5060))


async def test_missing_ipv4_discovery_is_nonfatal_and_releases_partial_resources(pnp_listener, monkeypatch):
    from custom_components.voip_stack.physical_phone_runtime import async_reconcile_physical_phone_runtime
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery
    lab = pnp_listener
    entry = lab.hass.config_entries.async_entries("voip_stack")[0]
    original = lab.runtime.sip
    assert await async_setup_component(lab.hass, "http", {})
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "phone_provisioning": {"enabled": True}})
    monkeypatch.setattr(PhoneDiscovery, "async_start", AsyncMock(side_effect=HomeAssistantError("No source IPv4")))
    try:
        await async_reconcile_physical_phone_runtime(lab.hass, entry)
        manager = lab.runtime.physical_phones
        assert manager is not None and lab.runtime.phone_discovery is None
        assert lab.runtime.sip is original
        lab.hass.config_entries.async_update_entry(entry, data={**entry.data,
            "phone_provisioning": {"enabled": False, "discovery_enabled": False}})
        await async_reconcile_physical_phone_runtime(lab.hass, entry)
        assert lab.runtime.physical_phones is None and not manager._unsubscribers
        assert not lab.hass.services.has_service("voip_stack", "provision_phone")
    finally:
        if lab.runtime.physical_phones is not None:
            await lab.runtime.physical_phones.async_close()
            lab.runtime.physical_phones = None


@pytest.mark.parametrize("path", ["pbx", "discovery"])
@pytest.mark.parametrize("vendor", ["Fanvil", "snom", "Grandstream"])
@pytest.mark.parametrize("disable_http_server", [True], indirect=True)
async def test_real_pnp_wire_follows_feature_toggle_without_reloading_sip_transport(
        pnp_listener, monkeypatch, path, vendor, unused_tcp_port, disable_http_server):
    from aiohttp import ClientSession
    from custom_components.voip_stack.physical_phone_runtime import async_reconcile_physical_phone_runtime
    from custom_components.voip_stack import physical_phone_discovery as module
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery
    from homeassistant.components import network
    lab = pnp_listener
    entry = lab.hass.config_entries.async_entries("voip_stack")[0]
    original = lab.runtime.sip
    endpoint = original.component("udp_listener")
    udp = endpoint.udp_server.transport
    listener = ("127.0.0.1", lab.runtime.transport_config["sip_port"])
    assert await async_setup_component(lab.hass, "http", {
        "http": {"server_host": ["127.0.0.1"], "server_port": unused_tcp_port}})
    await lab.hass.http.start()
    provision = {"allow_insecure_http": True, "base_url": f"http://127.0.0.1:{unused_tcp_port}"}
    monkeypatch.setattr(network, "async_get_source_ip", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(PhoneDiscovery, "_start_flow", AsyncMock())
    if path == "pbx":
        # Exercise the shared-socket branch on the actual existing test port.
        monkeypatch.setattr(module, "PNP_PORT", listener[1])
    mac = "00135e000001" if vendor == "snom" else "001122334455"
    transport, peer = await lab.hass.loop.create_datagram_endpoint(lambda: Peer(mac), local_addr=("127.0.0.4", 0))
    try:
        lab.hass.config_entries.async_update_entry(entry, data={**entry.data,
            "phone_provisioning": {"enabled": False, "discovery_enabled": False}})
        await async_reconcile_physical_phone_runtime(lab.hass, entry)
        transport.sendto(pnp_request(peer, "off-before", vendor), listener)
        assert (await peer.response("off-before", method="SUBSCRIBE")).status_code == 403
        assert peer.notifies.empty()
        lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "phone_provisioning": {
            "enabled": False, "discovery_enabled": True, **provision}})
        await async_reconcile_physical_phone_runtime(lab.hass, entry)
        discovery = lab.runtime.phone_discovery
        assert discovery is not None
        assert lab.runtime.physical_phones is None
        target = listener if path == "pbx" else ("127.0.0.1", discovery._transport.get_extra_info("sockname")[1])
        transport.sendto(pnp_request(peer, "on", vendor), target)
        assert (await peer.response("on", method="SUBSCRIBE")).status_code == 200
        await assert_no_notify(peer)
        assert len(discovery.candidates) == 1
        candidate = discovery.candidates[peer.username]
        assert candidate.token == "" and candidate.native_subentry is None
        assert not lab.calls.sessions
        lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "phone_provisioning": {
            "enabled": True, "discovery_enabled": True, **provision}})
        await async_reconcile_physical_phone_runtime(lab.hass, entry)
        assert lab.runtime.phone_discovery is discovery
        manager = lab.runtime.physical_phones
        manager.schedule = Mock()
        transport.sendto(pnp_request(peer, "enabled-but-unknown", vendor), target)
        assert (await peer.response("enabled-but-unknown", method="SUBSCRIBE")).status_code == 200
        await assert_no_notify(peer)
        assert candidate.token == "" and candidate.native_subentry is None
        record = adopt_candidate(lab.hass, entry, candidate, vendor)
        transport.sendto(pnp_request(peer, "adopted-next-request", vendor), target)
        accepted = await peer.response("adopted-next-request", method="SUBSCRIBE")
        assert accepted.status_code == 200
        notify = await asyncio.wait_for(peer.notifies.get(), 3)
        assert notify.header("Event") == sip.parse_message(pnp_request(peer, "adopted-next-request", vendor)).header("Event")
        assert notify.header("Call-ID") == "adopted-next-request" and notify.header("Content-Type") == "application/url"
        assert sip.parse_sip_uri(sip.name_addr_uri(notify.header("From"))) == sip.parse_sip_uri(
            sip.name_addr_uri(accepted.header("To")))
        assert sip.extract_tag(notify.header("From")) == sip.extract_tag(accepted.header("To"))
        assert candidate.token.encode() in notify.body and candidate.phone_id.encode() in notify.body
        suffix = {"snom": mac + ".xml", "Grandstream": "cfg" + mac + ".xml", "Fanvil": ""}[vendor].encode()
        assert notify.body.endswith(candidate.token.encode() + b"/" + suffix)
        fetch = notify.body.decode() if suffix else notify.body.decode() + mac + ".cfg"
        async with ClientSession() as client, client.get(fetch) as response:
            assert response.status == 200 and response.content_type == "application/xml"
            assert ElementTree.fromstring(await response.read()).tag == {
                "snom": "settings", "Grandstream": "gs_provision", "Fanvil": "sysConf"}[vendor]
        manager.schedule.assert_called_once_with({candidate.phone_id})
        assert record.subentry_id == candidate.native_subentry.subentry_id
        lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "phone_provisioning": {
            "enabled": False, "discovery_enabled": True, "base_url": "https://ha.example.test"}})
        await async_reconcile_physical_phone_runtime(lab.hass, entry)
        assert lab.runtime.phone_discovery is discovery and lab.runtime.physical_phones is None
        transport.sendto(pnp_request(peer, "managed-but-discovery-only", vendor), target)
        assert (await peer.response("managed-but-discovery-only", method="SUBSCRIBE")).status_code == 200
        await assert_no_notify(peer)
        assert not lab.calls.sessions
        lab.hass.config_entries.async_update_entry(entry, data={**entry.data,
            "phone_provisioning": {"enabled": False, "discovery_enabled": False}})
        await async_reconcile_physical_phone_runtime(lab.hass, entry)
        assert lab.runtime.physical_phones is None
        assert discovery._transport is None and discovery._membership is None and not discovery._tasks
        assert not discovery.candidates
        assert lab.runtime.sip is original and endpoint.udp_server.transport is udp
        transport.sendto(pnp_request(peer, "off-after", vendor), listener)
        assert (await peer.response("off-after", method="SUBSCRIBE")).status_code == 403
        assert peer.notifies.empty() and not lab.calls.sessions
    finally:
        transport.close()
        if lab.runtime.phone_discovery is not None:
            await lab.runtime.phone_discovery.async_close()
            lab.runtime.phone_discovery = None
        if lab.runtime.physical_phones is not None:
            await lab.runtime.physical_phones.async_close()
            lab.runtime.physical_phones = None
        await lab.hass.http.stop()


async def test_snom_wire_adoption_keeps_source_token_and_native_identity_binding(pnp_listener, monkeypatch):
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery
    from custom_components.voip_stack.physical_phone_runtime import async_reconcile_physical_phone_runtime
    from homeassistant.components import network

    lab = pnp_listener
    entry = lab.hass.config_entries.async_entries("voip_stack")[0]
    assert await async_setup_component(lab.hass, "http", {})
    monkeypatch.setattr(network, "async_get_source_ip", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(PhoneDiscovery, "_start_flow", AsyncMock())
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "phone_provisioning": {
        "enabled": True, "discovery_enabled": True, "base_url": "https://ha.example.test"}})
    await async_reconcile_physical_phone_runtime(lab.hass, entry)
    collector, manager = lab.runtime.phone_discovery, lab.runtime.physical_phones
    manager.schedule = Mock()
    target = ("127.0.0.1", lab.runtime.transport_config["sip_port"])
    sockets = []
    try:
        for host in ("127.0.0.4", "127.0.0.5"):
            sockets.append(await lab.hass.loop.create_datagram_endpoint(lambda: Peer("00135e000001"), local_addr=(host, 0)))
        transport, peer = sockets[0]
        other_transport, other = sockets[1]
        transport.sendto(pnp_request(peer, "unknown", "snom"), target)
        assert (await peer.response("unknown", method="SUBSCRIBE")).status_code == 200
        await assert_no_notify(peer)
        candidate = collector.candidates[peer.username]
        assert candidate.token == "" and candidate.native_subentry is None
        other_transport.sendto(pnp_request(other, "unknown-other-source", "snom"), target)
        assert (await other.response("unknown-other-source", method="SUBSCRIBE")).status_code == 403
        record = adopt_candidate(lab.hass, entry, candidate, "snom")
        transport.sendto(pnp_request(peer, "managed", "snom"), target)
        assert (await peer.response("managed", method="SUBSCRIBE")).status_code == 200
        notify = await asyncio.wait_for(peer.notifies.get(), 3)
        assert candidate.token.encode() in notify.body
        manager.schedule.reset_mock()
        other_transport.sendto(pnp_request(other, "managed-other-source", "snom"), target)
        assert (await other.response("managed-other-source", method="SUBSCRIBE")).status_code == 200
        await assert_no_notify(other)
        assert candidate.host == peer.address[0]
        lab.hass.config_entries.async_update_subentry(entry, record,
            data={**record.data, "provisioning_token": "r" * 48})
        transport.sendto(pnp_request(peer, "rotated-token", "snom"), target)
        assert (await peer.response("rotated-token", method="SUBSCRIBE")).status_code == 200
        await assert_no_notify(peer)
        manager.schedule.assert_not_called()
        assert not lab.calls.sessions
    finally:
        for transport, _peer in sockets:
            transport.close()
        await collector.async_close()
        lab.runtime.phone_discovery = None
        await manager.async_close()
        lab.runtime.physical_phones = None


async def test_real_snom_udp_creates_native_ha_proposal_without_provisioning_or_catalog(pnp_listener, monkeypatch):
    """A real datagram reaches Core's actual discovery flow while delivery is off."""
    from homeassistant.components import network
    from custom_components.voip_stack.const import DOMAIN
    from custom_components.voip_stack.phone_profile_catalog import PhoneProfileCatalog
    from custom_components.voip_stack.physical_phone import physical_phone_records
    from custom_components.voip_stack.physical_phone_runtime import async_reconcile_physical_phone_runtime
    from .support.ha_frontend import prepare_frontend_dependency

    lab = pnp_listener
    entry = lab.hass.config_entries.async_entries(DOMAIN)[0]
    assert await async_setup_component(lab.hass, "http", {})
    lab.hass.config.components.update({"assist_pipeline", "ffmpeg", "lovelace", "media_source", "network"})
    prepare_frontend_dependency(lab.hass)
    manifest = AsyncMock(side_effect=AssertionError("Discovery downloaded the catalog"))
    profile = AsyncMock(side_effect=AssertionError("Discovery downloaded a profile"))
    monkeypatch.setattr(PhoneProfileCatalog, "async_manifest", manifest)
    monkeypatch.setattr(PhoneProfileCatalog, "async_profile", profile)
    monkeypatch.setattr(network, "async_get_source_ip", AsyncMock(return_value="127.0.0.1"))
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data,
        "phone_provisioning": {"enabled": False, "discovery_enabled": True}})
    await async_reconcile_physical_phone_runtime(lab.hass, entry)
    discovery = lab.runtime.phone_discovery
    listener = ("127.0.0.1", lab.runtime.transport_config["sip_port"])
    original_udp = lab.calls.component("udp_listener").udp_server.transport
    transport, peer = await lab.hass.loop.create_datagram_endpoint(
        lambda: Peer("00135e000001"), local_addr=("127.0.0.4", 0))
    manager = lab.hass.config_entries.flow
    unrelated = None
    try:
        transport.sendto(pnp_request(peer, "real-snom-proposal", "snom"), listener)
        response = await peer.response("real-snom-proposal", method="SUBSCRIBE")
        assert response.status_code == 200 and response.body == b""
        await assert_no_notify(peer)
        await lab.hass.async_block_till_done()
        flows = manager.async_progress_by_handler(DOMAIN)
        proposal = next(flow for flow in flows if flow["context"].get("unique_id") == "physical_phone:00135e000001")
        assert proposal["step_id"] == "phone_discovered"
        assert proposal["context"]["title_placeholders"]["name"] == "Snom 360"
        candidate = discovery.candidates[peer.username]
        assert candidate.flow_id == proposal["flow_id"]
        assert candidate.public()["vendor"] == "snom" and candidate.public()["model"] == "snom360"
        assert candidate.native_subentry is None and not candidate.phone_id and not candidate.token
        assert not physical_phone_records(entry) and lab.runtime.physical_phones is None
        manifest.assert_not_awaited()
        profile.assert_not_awaited()
        transport.sendto(pnp_request(peer, "same-snom-again", "snom"), listener)
        assert (await peer.response("same-snom-again", method="SUBSCRIBE")).status_code == 200
        await assert_no_notify(peer)
        await lab.hass.async_block_till_done()
        assert len([flow for flow in manager.async_progress_by_handler(DOMAIN)
                    if flow["context"].get("unique_id") == "physical_phone:00135e000001"]) == 1
        ignored = await manager.async_init(DOMAIN, context={"source": SOURCE_IGNORE},
            data={"unique_id": "physical_phone:00135e000002", "title": "Ignored phone"})
        assert ignored["type"] == "create_entry"
        unrelated = await manager.async_init(DOMAIN,
            context={"source": "reconfigure", "entry_id": entry.entry_id})
        assert unrelated["type"] == "menu"
        await discovery.async_close()
        with pytest.raises(UnknownFlow):
            manager.async_get(proposal["flow_id"])
        assert manager.async_get(unrelated["flow_id"])
        assert ignored["result"] in lab.hass.config_entries.async_entries(DOMAIN)
        assert not discovery.candidates and not discovery._tasks
        assert lab.calls.component("udp_listener").udp_server.transport is original_udp
        assert not lab.calls.sessions
    finally:
        transport.close()
        await discovery.async_close()
        lab.runtime.phone_discovery = None
        if unrelated is not None:
            manager.async_abort(unrelated["flow_id"])


async def test_close_aborts_a_real_flow_during_async_init_before_returned_flow_id(phone_lab, monkeypatch):
    from custom_components.voip_stack.config_flow import VoipStackConfigFlow
    from custom_components.voip_stack.const import DOMAIN
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery
    from .support.ha_frontend import prepare_frontend_dependency

    lab = phone_lab
    lab.hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    prepare_frontend_dependency(lab.hass)
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        "phone_provisioning": {"enabled": False, "discovery_enabled": True}})
    discovery = PhoneDiscovery(lab.hass, lab.entry)
    lab.runtime.phone_discovery = discovery
    entered, release = asyncio.Event(), asyncio.Event()
    original = VoipStackConfigFlow.async_step_integration_discovery

    async def pending(self, data):
        entered.set()
        await release.wait()
        return await original(self, data)

    monkeypatch.setattr(VoipStackConfigFlow, "async_step_integration_discovery", pending)
    request = sip.SipMessage(method="SUBSCRIBE", uri="sip:MAC%3a00135E000001@localhost", headers=(
        ("From", "<sip:MAC%3a00135E000001@localhost>;tag=one"), ("Contact", "<sip:127.0.0.4:5060>"),
        ("Event", 'ua-profile;vendor="snom";model="snom360"')))
    try:
        assert (await discovery.handle(request, ("127.0.0.4", 5060), "UDP")).follow_up is None
        await asyncio.wait_for(entered.wait(), 3)
        candidate = discovery.candidates["00135e000001"]
        assert candidate.flow_id == "" and candidate.native_subentry is None
        assert lab.hass.config_entries.flow.async_progress_by_handler(DOMAIN, include_uninitialized=True)
        await discovery.async_close()
        assert not lab.hass.config_entries.flow.async_progress_by_handler(DOMAIN, include_uninitialized=True)
        assert not discovery._tasks and not discovery.candidates
    finally:
        release.set()
        await discovery.async_close()
        lab.runtime.phone_discovery = None


async def test_repeated_unknown_announcements_share_one_flow_task_and_candidate_slots_are_bounded(phone_lab, monkeypatch):
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery

    lab = phone_lab
    discovery = PhoneDiscovery(lab.hass, lab.entry)
    lab.runtime.phone_discovery = discovery
    entered, release = asyncio.Event(), asyncio.Event()

    async def pending(_candidate):
        entered.set()
        await release.wait()

    start = AsyncMock(side_effect=pending)
    monkeypatch.setattr(discovery, "_start_flow", start)
    def request(mac):
        uri = f"sip:MAC%3a{mac}@localhost"
        return sip.SipMessage(method="SUBSCRIBE", uri=uri, headers=(
            ("From", f"<{uri}>;tag=one"), ("Contact", "<sip:127.0.0.4:5060>"),
            ("Event", 'ua-profile;vendor="snom";model="snom360"')))
    try:
        first = request("00135e000001")
        assert (await discovery.handle(first, ("127.0.0.4", 5060), "UDP")).status == 200
        await asyncio.wait_for(entered.wait(), 2)
        for _ in range(5):
            assert (await discovery.handle(first, ("127.0.0.4", 5060), "UDP")).follow_up is None
        assert start.await_count == 1 and len(discovery.candidates) == 1
        for index in range(2, 65):
            assert (await discovery.handle(request(f"00135e{index:06x}"), ("127.0.0.4", 5060), "UDP")).status == 200
        assert len(discovery.candidates) == 64
        assert (await discovery.handle(request("00135effffff"), ("127.0.0.4", 5060), "UDP")).status == 503
        assert all(not candidate.token and candidate.native_subentry is None for candidate in discovery.candidates.values())
    finally:
        await discovery.async_close()
        lab.runtime.phone_discovery = None


async def test_disable_during_discovery_start_does_not_attach_late_callbacks(pnp_listener, monkeypatch):
    from custom_components.voip_stack.physical_phone_runtime import async_reconcile_physical_phone_runtime
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery
    lab = pnp_listener
    entry = lab.hass.config_entries.async_entries("voip_stack")[0]
    assert await async_setup_component(lab.hass, "http", {})
    entered, release = asyncio.Event(), asyncio.Event()

    async def start(_discovery):
        entered.set()
        await release.wait()

    monkeypatch.setattr(PhoneDiscovery, "async_start", start)
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data,
        "phone_provisioning": {"enabled": False, "discovery_enabled": True}})
    enabling = asyncio.create_task(async_reconcile_physical_phone_runtime(lab.hass, entry))
    discovery = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        discovery = lab.runtime.phone_discovery
        lab.hass.config_entries.async_update_entry(entry, data={**entry.data,
            "phone_provisioning": {"enabled": False, "discovery_enabled": False}})
        await async_reconcile_physical_phone_runtime(lab.hass, entry)
        assert lab.runtime.phone_discovery is None and discovery._closed
        release.set()
        await asyncio.wait_for(enabling, 2)
        assert discovery._closed and not discovery._tasks
    finally:
        release.set()
        await asyncio.gather(enabling, return_exceptions=True)
        if discovery is not None:
            await discovery.async_close()
        lab.runtime.physical_phones = None


@pytest.mark.parametrize("stage", ["network", "transport"])
async def test_retired_discovery_never_keeps_a_late_udp_transport(pnp_listener, monkeypatch, stage):
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery
    from homeassistant.components import network
    lab = pnp_listener
    entry = lab.hass.config_entries.async_entries("voip_stack")[0]
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "phone_provisioning": {"enabled": True}})
    discovery = PhoneDiscovery(lab.hass, entry)
    lab.runtime.phone_discovery = discovery
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
