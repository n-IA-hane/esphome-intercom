"""Named trunk persistence, source classification and independent lifecycle."""

from dataclasses import replace
import asyncio
import hashlib
import re
from pathlib import Path
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigSubentry
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.voip_stack.config import normalized_trunk_config, trunk_config
from custom_components.voip_stack.endpoint_registry import EndpointRegistry
from custom_components.voip_stack.endpoint_runtime import _classify_trunk_invite
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.phone_control import PhoneAdapterRegistry
from custom_components.voip_stack.runtime_data import VoipStackRuntime, sip_trunk, sip_trunks
from custom_components.voip_stack.trunk_config import entry_trunk_configs, entry_default_trunk
from custom_components.voip_stack.trunk_runtime import async_start_sip_trunk, async_stop_sip_trunk

from .test_invite_router_behavior import _invite

from .support.ha_frontend import prepare_frontend_dependency


pytestmark = pytest.mark.ha


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    import custom_components

    previous = custom_components.__path__
    custom_components.__path__ = [*previous, str(Path(__file__).parents[1] / "custom_components")]
    yield
    custom_components.__path__ = previous


@pytest.fixture
def catalog(hass):
    records = [ConfigSubentry(subentry_type="trunk", title=name, unique_id=f"trunk:{name}", data={
        "trunk_id": name, "trunk_enabled": True, "trunk_register": False,
        "trunk_server": "192.0.2.1", "trunk_transport": "udp",
        "trunk_allowed_ips": [source], "trunk_inbound_default_target": target,
    }) for name, source, target in (("home", "192.0.2.1", "Kitchen"), ("work", "192.0.2.2", "Office"))]
    entry = MockConfigEntry(domain="voip_stack", data={"default_trunk_id": "home"})
    entry.add_to_hass(hass)
    for record in records:
        hass.config_entries.async_add_subentry(entry, record)
    endpoints = EndpointRegistry()
    sip = SipEndpointRuntime(allow_dark_sessions=True)
    sip.activate()
    entry.runtime_data = VoipStackRuntime(
        transport_config={"sip_port": 5060}, assist_config={}, trunk_config=normalized_trunk_config({}),
        endpoints=endpoints, phones=PhoneAdapterRegistry(hass, endpoints), sip=sip,
        trunk_configs=entry_trunk_configs(entry), default_trunk_id=entry_default_trunk(entry, entry_trunk_configs(entry)),
    )
    return entry


async def test_each_trunk_keeps_config_and_client_when_another_stops(hass, catalog):
    assert trunk_config(hass)["trunk_inbound_default_target"] == "Kitchen"
    assert trunk_config(hass, "work")["trunk_inbound_default_target"] == "Office"
    assert await async_start_sip_trunk(hass, local_ip="127.0.0.1")
    try:
        clients = sip_trunks(hass)
        assert set(clients) == {"home", "work"}
        assert clients["home"] is sip_trunk(hass)
        assert clients["work"] is sip_trunk(hass, "work")
        await clients["home"].stop()
        assert not clients["home"].ready
        assert clients["work"].ready
        assert sip_trunk(hass) is clients["home"]
        assert sip_trunk(hass, "missing") is None
    finally:
        await async_stop_sip_trunk(hass)
    assert not sip_trunks(hass)


async def test_inbound_static_trunk_carries_its_own_identity(hass, catalog):
    await async_start_sip_trunk(hass, local_ip="127.0.0.1")
    try:
        invite = replace(_invite(), source_host="192.0.2.2", signaling_transport="UDP")
        selected = _classify_trunk_invite(invite, enabled=True, trunk=None, trunks=sip_trunks(hass))
        assert selected.received_via_trunk and selected.trunk_id == "work"
        assert trunk_config(hass, selected.trunk_id)["trunk_inbound_default_target"] == "Office"
        stranger = _classify_trunk_invite(replace(invite, source_host="198.51.100.7"),
                                        enabled=True, trunk=None, trunks=sip_trunks(hass))
        assert not stranger.received_via_trunk and not stranger.trunk_id
    finally:
        await async_stop_sip_trunk(hass)


def test_trunk_rename_does_not_change_identity_or_selection(hass, catalog):
    work = next(item for item in catalog.subentries.values() if item.data["trunk_id"] == "work")
    hass.config_entries.async_update_subentry(catalog, work, title="Office provider")
    assert set(entry_trunk_configs(catalog)) == {"home", "work"}
    assert entry_default_trunk(catalog, entry_trunk_configs(catalog)) == "home"


class Registrar(asyncio.DatagramProtocol):
    """Independent digest oracle with a real UDP REGISTER exchange."""
    def __init__(self):
        self.transport = None
        self.contacts = set()
        self.requests = []

    def connection_made(self, transport):
        self.transport = transport

    def binding_key(self, contact):
        return contact

    def datagram_received(self, raw, address):
        from custom_components.voip_stack.core import sip

        request = sip.parse_message(raw)
        self.requests.append(request)
        assert request.method == "REGISTER"
        headers = [(name, request.header(name)) for name in ("Via", "From", "To", "Call-ID", "CSeq")]
        auth = request.header("Authorization")
        if not auth:
            status, reason = 401, "Unauthorized"
            headers.append(("WWW-Authenticate", 'Digest realm="lab", nonce="fixed-nonce", algorithm=MD5, qop="auth"'))
        else:
            fields = {key: value.strip('"') for key, value in re.findall(r'(\w+)=("[^"]*"|[^, ]+)', auth)}
            def md5(value):
                return hashlib.md5(value.encode()).hexdigest()
            expected = md5(f'{md5("ha:lab:test-only")}:{fields["nonce"]}:{fields["nc"]}:{fields["cnonce"]}:auth:{md5("REGISTER:" + fields["uri"])}')
            status, reason = (200, "OK") if fields["response"] == expected else (403, "Forbidden")
            if status == 200:
                contact = request.header("Contact")
                binding = self.binding_key(contact)
                if request.header("Expires") == "0":
                    self.contacts.discard(binding)
                else:
                    self.contacts.add(binding)
                headers.extend([("Contact", contact), ("Expires", request.header("Expires"))])
                if "reg-id=" in contact:
                    headers.append(("Require", "outbound"))
        self.transport.sendto(sip.build_response(status, reason, headers), address)


@pytest.fixture
async def registrar(hass, socket_enabled):
    transport, peer = await hass.loop.create_datagram_endpoint(Registrar, local_addr=("127.0.0.1", 0))
    yield peer, transport.get_extra_info("sockname")[1]
    transport.close()


@pytest.fixture
async def tcp_registrar(hass, socket_enabled):
    class OutboundRegistrar(Registrar):
        def binding_key(self, contact):
            # RFC 5626 compares instance/reg-id, not the changing Contact URI.
            return (re.search(r'\+sip.instance="([^"]+)"', contact)[1],
                    int(re.search(r'reg-id=(\d+)', contact)[1]))

    peer = OutboundRegistrar()
    tasks, writers = set(), set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        writers.add(writer)
        try:
            while True:
                raw = await reader.readuntil(b"\r\n\r\n")
                length = re.search(rb"(?im)^Content-Length:\s*(\d+)", raw)
                raw += await reader.readexactly(int(length[1]) if length else 0)
                peer.transport = SimpleNamespace(sendto=lambda data, _address: writer.write(data))
                peer.datagram_received(raw, writer.get_extra_info("peername"))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            writers.discard(writer)
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    yield peer, server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    for writer in tuple(writers):
        writer.close()
    await asyncio.gather(*tuple(tasks), return_exceptions=True)


async def test_native_flow_adopts_validated_registration_without_second_register(hass, catalog, registrar, monkeypatch):
    from custom_components.voip_stack import trunk_flow
    from custom_components.voip_stack.trunk_runtime import async_reconcile_trunks

    peer, port = registrar
    monkeypatch.setattr(trunk_flow, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
    hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    prepare_frontend_dependency(hass)
    form = await hass.config_entries.subentries.async_init((catalog.entry_id, "trunk"), context={"source": "user"})
    form = await hass.config_entries.subentries.async_configure(form["flow_id"], {
        "name": "Carrier", "trunk_enabled": True, "trunk_register": True, "make_default": False,
    })
    result = await hass.config_entries.subentries.async_configure(form["flow_id"], {
        "trunk_server": "127.0.0.1", "trunk_port": port, "trunk_username": "ha", "trunk_password": "test-only", "trunk_inbound_default_target": "Kitchen",
    })
    assert result["type"] == "create_entry", result
    identity = result["data"]["trunk_id"]
    candidate = catalog.runtime_data.sip.component(f"pending_trunk:{identity}")
    assert candidate is not None and candidate.ready
    assert len(peer.contacts) == 1
    count = len(peer.requests)
    try:
        await async_reconcile_trunks(hass, catalog)
        assert sip_trunk(hass, identity) is candidate
        assert len(peer.requests) == count
        assert ";line=" in candidate.contact_uri
        assert catalog.runtime_data.default_trunk_id == "home"
    finally:
        await async_stop_sip_trunk(hass)
    assert not peer.contacts


async def test_failed_reconfiguration_preserves_live_registration_and_saved_credentials(hass, catalog, registrar, monkeypatch):
    from custom_components.voip_stack import trunk_flow
    from custom_components.voip_stack.trunk_runtime import async_reconcile_trunks

    peer, port = registrar
    monkeypatch.setattr(trunk_flow, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
    hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    prepare_frontend_dependency(hass)
    form = await hass.config_entries.subentries.async_init((catalog.entry_id, "trunk"), context={"source": "user"})
    form = await hass.config_entries.subentries.async_configure(form["flow_id"], {
        "name": "Carrier", "trunk_enabled": True, "trunk_register": True, "make_default": False,
    })
    created = await hass.config_entries.subentries.async_configure(form["flow_id"], {
        "trunk_server": "127.0.0.1", "trunk_port": port, "trunk_username": "ha", "trunk_password": "test-only", "trunk_inbound_default_target": "Kitchen",
    })
    assert created["type"] == "create_entry"
    identity = created["data"]["trunk_id"]
    await async_reconcile_trunks(hass, catalog)
    original = sip_trunk(hass, identity)
    contacts = set(peer.contacts)
    record = next(item for item in catalog.subentries.values() if item.data["trunk_id"] == identity)
    try:
        form = await hass.config_entries.subentries.async_init((catalog.entry_id, "trunk"),
            context={"source": "reconfigure", "subentry_id": record.subentry_id})
        form = await hass.config_entries.subentries.async_configure(form["flow_id"], form["data_schema"]({}))
        values = form["data_schema"]({})
        values["trunk_password"] = "wrong-test-value"
        result = await hass.config_entries.subentries.async_configure(form["flow_id"], values)
        assert result["type"] == "form"
        assert result["errors"] == {"base": "trunk_registration_failed"}
        assert original.ready and sip_trunk(hass, identity) is original
        assert peer.contacts == contacts
        assert catalog.subentries[record.subentry_id].data["trunk_password"] == "test-only"
        hass.config_entries.subentries.async_abort(form["flow_id"])
    finally:
        await async_stop_sip_trunk(hass)
    assert not peer.contacts


async def test_tcp_reconfiguration_unregisters_only_the_old_outbound_flow(hass, catalog, tcp_registrar, monkeypatch):
    from custom_components.voip_stack import trunk_flow
    from custom_components.voip_stack.trunk_runtime import async_reconcile_trunks

    peer, port = tcp_registrar
    monkeypatch.setattr(trunk_flow, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
    hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    prepare_frontend_dependency(hass)
    form = await hass.config_entries.subentries.async_init((catalog.entry_id, "trunk"), context={"source": "user"})
    form = await hass.config_entries.subentries.async_configure(form["flow_id"], {
        "name": "TCP carrier", "trunk_enabled": True, "trunk_register": True, "make_default": False,
    })
    created = await hass.config_entries.subentries.async_configure(form["flow_id"], {
        "trunk_server": "127.0.0.1", "trunk_port": port, "trunk_transport": "tcp",
        "trunk_username": "ha", "trunk_password": "test-only", "trunk_inbound_default_target": "Kitchen",
    })
    assert created["type"] == "create_entry", created
    identity = created["data"]["trunk_id"]
    await async_reconcile_trunks(hass, catalog)
    original = sip_trunk(hass, identity)
    record = next(item for item in catalog.subentries.values() if item.data["trunk_id"] == identity)
    try:
        form = await hass.config_entries.subentries.async_init((catalog.entry_id, "trunk"),
            context={"source": "reconfigure", "subentry_id": record.subentry_id})
        form = await hass.config_entries.subentries.async_configure(form["flow_id"], form["data_schema"]({}))
        values = form["data_schema"]({})
        values["trunk_register_expires"] = 600
        result = await hass.config_entries.subentries.async_configure(form["flow_id"], values)
        assert result["type"] == "abort", result
        assert len(peer.contacts) == 2
        await async_reconcile_trunks(hass, catalog)
        replacement = sip_trunk(hass, identity)
        assert replacement is not original and replacement.ready and not original.ready
        assert replacement._instance_id == original._instance_id
        assert replacement.config.registration_id != original.config.registration_id
        assert peer.contacts == {(f"<{replacement._instance_id}>", replacement.config.registration_id)}
        assert catalog.subentries[record.subentry_id].data["trunk_registration_id"] == replacement.config.registration_id
    finally:
        await async_stop_sip_trunk(hass)
    assert not peer.contacts


async def test_failed_multi_trunk_update_keeps_every_previous_client(hass, catalog, monkeypatch):
    from custom_components.voip_stack import trunk_runtime

    await async_start_sip_trunk(hass, local_ip="127.0.0.1")
    previous = sip_trunks(hass)
    for record in tuple(catalog.subentries.values()):
        hass.config_entries.async_update_subentry(catalog, record, data={**record.data, "trunk_port": 5090})
    created = []
    factory = trunk_runtime.create_trunk_client

    def fail_second(hass, cfg, *, local_ip, trunk_id=""):
        if trunk_id == "work":
            raise OSError("synthetic trunk construction failure")
        client = factory(hass, cfg, local_ip=local_ip, trunk_id=trunk_id)
        created.append(client)
        return client

    monkeypatch.setattr(trunk_runtime, "create_trunk_client", fail_second)
    try:
        with pytest.raises(OSError, match="synthetic"):
            await trunk_runtime.async_reconcile_trunks(hass, catalog)
        assert sip_trunks(hass) == previous
        assert all(client.ready for client in previous.values())
        assert created and all(not client.ready for client in created)
    finally:
        await async_stop_sip_trunk(hass)


def test_legacy_trunk_migrates_once_without_losing_phone_settings(hass):
    from custom_components.voip_stack.trunk_config import migrate_legacy_trunk

    entry = MockConfigEntry(domain="voip_stack", data={
        "trunk_enabled": True, "trunk_server": "provider.example", "trunk_username": "ha",
        "trunk_password": " test-only ", "esphome_phone_policies": {"esp": {"outbound_trunk_id": ""}},
        "external_call_blocked_endpoints": ["kitchen"],
    })
    entry.add_to_hass(hass)
    migrate_legacy_trunk(hass, entry)
    identity = entry.data["default_trunk_id"]
    assert entry_trunk_configs(entry)[identity]["trunk_password"] == " test-only "
    assert "trunk_password" not in entry.data
    assert entry.data["external_call_blocked_endpoints"] == ["kitchen"]
    assert "esp" in entry.data["esphome_phone_policies"]
    migrate_legacy_trunk(hass, entry)
    assert len(entry.subentries) == 1
    assert entry.data["default_trunk_id"] == identity


async def test_global_reconfiguration_preserves_named_trunks_and_phone_policies(hass, catalog):
    hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    prepare_frontend_dependency(hass)
    hass.config_entries.async_update_entry(catalog, data={**catalog.data,
        "esphome_phone_policies": {"esp": {"outbound_trunk_id": "work"}},
    })
    original = dict(catalog.subentries)
    form = await hass.config_entries.flow.async_init("voip_stack",
        context={"source": "reconfigure", "entry_id": catalog.entry_id})
    form = await hass.config_entries.flow.async_configure(form["flow_id"], {"next_step_id": "user"})
    values = form["data_schema"]({})
    assert "trunk_enabled" not in values
    result = await hass.config_entries.flow.async_configure(form["flow_id"], values)
    assert result["type"] == "abort", result
    assert catalog.data["default_trunk_id"] == "home"
    assert catalog.data["esphome_phone_policies"]["esp"]["outbound_trunk_id"] == "work"
    assert dict(catalog.subentries) == original


async def test_native_trunk_readiness_updates_from_lifecycle_events_without_polling(hass, catalog):
    from custom_components.voip_stack.binary_sensor import async_setup_entry

    await async_start_sip_trunk(hass, local_ip="127.0.0.1")
    added = []
    await async_setup_entry(hass, catalog,
                            lambda entities, **_kwargs: added.extend(entities))
    sensors = {entity.identity: entity for entity in added}
    assert set(sensors) == {"home", "work"}
    assert all(entity.is_on and not entity.should_poll for entity in sensors.values())
    try:
        await sip_trunk(hass, "work").stop()
        await hass.async_block_till_done()
        assert sensors["home"].is_on
        assert not sensors["work"].is_on
        assert sensors["work"].extra_state_attributes["registration_enabled"] is False
    finally:
        await async_stop_sip_trunk(hass)


@pytest.mark.parametrize('with_listener', [True, False])
@pytest.mark.parametrize('make_default', [True, False])
async def test_native_flow_live_listener_adopts_new_trunk_and_creates_sensor(hass, catalog, registrar, monkeypatch, with_listener, make_default):
    from custom_components.voip_stack import trunk_flow

    from custom_components.voip_stack.config_entry_runtime import async_config_entry_updated
    from custom_components.voip_stack.binary_sensor import async_setup_entry
    from custom_components.voip_stack import mailbox
    monkeypatch.setattr(mailbox, 'async_refresh_mailbox_state', AsyncMock())
    unsubscribe = catalog.add_update_listener(async_config_entry_updated) if with_listener else lambda: None
    added = []
    await async_setup_entry(hass, catalog, lambda entities, **kwargs: added.extend(entities))
    peer, port = registrar
    monkeypatch.setattr(trunk_flow, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
    hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    prepare_frontend_dependency(hass)
    form = await hass.config_entries.subentries.async_init((catalog.entry_id, "trunk"), context={"source": "user"})
    form = await hass.config_entries.subentries.async_configure(form["flow_id"], {
        "name": "Carrier", "trunk_enabled": True, "trunk_register": True, "make_default": make_default,
    })
    result = await hass.config_entries.subentries.async_configure(form["flow_id"], {
        "trunk_server": "127.0.0.1", "trunk_port": port, "trunk_username": "ha", "trunk_password": "test-only", "trunk_inbound_default_target": "Kitchen",
    })
    assert result["type"] == "create_entry", result
    identity = result["data"]["trunk_id"]
    await hass.async_block_till_done()
    candidate = sip_trunk(hass, identity)
    assert candidate is not None and candidate.ready
    assert len(peer.contacts) == 1
    count = len(peer.requests)
    try:
        assert identity in {entity.identity for entity in added}
        assert sip_trunk(hass, identity) is candidate
        assert len(peer.requests) == count
        assert ";line=" in candidate.contact_uri
        assert catalog.runtime_data.default_trunk_id == (identity if make_default else "home")
    finally:
        unsubscribe()
        await async_stop_sip_trunk(hass)
    assert not peer.contacts


async def test_sensor_exists_while_saved_trunk_waits_for_runtime_adoption(hass, catalog):
    from custom_components.voip_stack.binary_sensor import async_setup_entry
    from homeassistant.helpers.dispatcher import async_dispatcher_send
    from custom_components.voip_stack.trunk_config import TRUNK_UPDATE_SIGNAL
    record = ConfigSubentry(subentry_type='trunk',title='Pending carrier',unique_id='trunk:pending',
        data={'trunk_id':'pending','trunk_enabled':True,'trunk_register':True,'trunk_server':'example.invalid','trunk_transport':'udp'})
    hass.config_entries.async_add_subentry(catalog,record)
    added=[]
    await async_setup_entry(hass,catalog,lambda entities,**kwargs:added.extend(entities))
    sensor=next(entity for entity in added if entity.identity=='pending')
    assert sensor.available is False
    assert sensor.is_on is False
    catalog.runtime_data.trunk_configs=entry_trunk_configs(catalog)
    async_dispatcher_send(hass,TRUNK_UPDATE_SIGNAL,'pending')
    await hass.async_block_till_done()
    assert sensor.available is True
    assert sensor.is_on is False
    assert len([entity for entity in added if entity.identity=='pending'])==1


@pytest.mark.parametrize('remaining', [0, 1, 2])
async def test_removing_default_clears_saved_and_runtime_selection_without_failover(hass,catalog,monkeypatch,remaining):
    from custom_components.voip_stack.trunk_runtime import async_reconcile_trunks
    from custom_components.voip_stack import peer_snapshot
    monkeypatch.setattr(peer_snapshot,'async_advertise_host',AsyncMock(return_value='127.0.0.1'))
    records=list(catalog.subentries.values())
    if remaining==0:
        hass.config_entries.async_remove_subentry(catalog,records[1].subentry_id)
    if remaining==2:
        hass.config_entries.async_add_subentry(catalog,ConfigSubentry(subentry_type='trunk',title='Other',
            unique_id='trunk:other',data={'trunk_id':'other','trunk_enabled':False}))
    hass.config_entries.async_remove_subentry(catalog,records[0].subentry_id)
    try:
        await async_reconcile_trunks(hass,catalog)
        assert catalog.data['default_trunk_id']==''
        assert catalog.runtime_data.default_trunk_id==''
        assert entry_default_trunk(catalog,entry_trunk_configs(catalog))==''
        assert not trunk_config(hass)['trunk_enabled']
    finally:
        await async_stop_sip_trunk(hass)


async def test_native_subentry_delete_notifies_runtime_and_repairs_selection(hass,catalog,monkeypatch):
    from custom_components.voip_stack.config_entry_runtime import async_config_entry_updated
    from custom_components.voip_stack import peer_snapshot,mailbox
    from homeassistant.helpers import issue_registry as ir
    monkeypatch.setattr(peer_snapshot,'async_advertise_host',AsyncMock(return_value='127.0.0.1'))
    monkeypatch.setattr(mailbox,'async_refresh_mailbox_state',AsyncMock())
    unsubscribe=catalog.add_update_listener(async_config_entry_updated)
    record=next(s for s in catalog.subentries.values() if s.data['trunk_id']=='home')
    try:
        hass.config_entries.async_remove_subentry(catalog,record.subentry_id)
        await hass.async_block_till_done()
        assert catalog.data['default_trunk_id']==''
        assert catalog.runtime_data.default_trunk_id==''
        assert ir.async_get(hass).async_get_issue('voip_stack','default_trunk_missing') is not None
        hass.config_entries.async_update_entry(catalog,data={**catalog.data,'default_trunk_id':'work'})
        await hass.async_block_till_done()
        assert catalog.runtime_data.default_trunk_id=='work'
        assert ir.async_get(hass).async_get_issue('voip_stack','default_trunk_missing') is None
    finally:
        unsubscribe()
        await async_stop_sip_trunk(hass)


async def test_startup_heals_saved_deleted_default_without_selecting_survivor(hass,catalog):
    record=next(s for s in catalog.subentries.values() if s.data['trunk_id']=='home')
    hass.config_entries.async_remove_subentry(catalog,record.subentry_id)
    catalog.runtime_data.trunk_configs=entry_trunk_configs(catalog)
    try:
        await async_start_sip_trunk(hass,local_ip='127.0.0.1')
        assert catalog.data['default_trunk_id']==''
        assert catalog.runtime_data.default_trunk_id==''
        assert sip_trunk(hass) is None
        assert sip_trunk(hass,'work').ready
    finally:
        await async_stop_sip_trunk(hass)


async def test_first_trunk_is_default_without_toggle_second_trunk_offers_choice(hass,catalog,registrar,monkeypatch):
    from custom_components.voip_stack import trunk_flow
    peer,port=registrar
    for record in tuple(catalog.subentries.values()):
        hass.config_entries.async_remove_subentry(catalog,record.subentry_id)
    hass.config_entries.async_update_entry(catalog,data={'default_trunk_id':''})
    catalog.runtime_data.trunk_configs={}
    catalog.runtime_data.default_trunk_id=''
    monkeypatch.setattr(trunk_flow,'async_advertise_host',AsyncMock(return_value='127.0.0.1'))
    hass.config.components.update({'assist_pipeline','ffmpeg','http','lovelace','media_source','network'})
    prepare_frontend_dependency(hass)
    first=await hass.config_entries.subentries.async_init((catalog.entry_id,'trunk'),context={'source':'user'})
    assert 'make_default' not in first['data_schema'].schema
    first=await hass.config_entries.subentries.async_configure(first['flow_id'],{
        'name':'First','trunk_enabled':True,'trunk_register':True})
    result=await hass.config_entries.subentries.async_configure(first['flow_id'],{
        'trunk_server':'127.0.0.1','trunk_port':port,'trunk_username':'ha','trunk_inbound_default_target':'Kitchen','trunk_password':'test-only'})
    identity=result['data']['trunk_id']
    try:
        await hass.async_block_till_done()
        assert catalog.data['default_trunk_id']==identity
        assert catalog.runtime_data.default_trunk_id==identity
        assert sip_trunk(hass,identity).ready
        record=next(s for s in catalog.subentries.values() if s.data['trunk_id']==identity)
        edit=await hass.config_entries.subentries.async_init((catalog.entry_id,'trunk'),
            context={'source':'reconfigure','subentry_id':record.subentry_id})
        assert 'make_default' not in edit['data_schema'].schema
        hass.config_entries.subentries.async_abort(edit['flow_id'])
        second=await hass.config_entries.subentries.async_init((catalog.entry_id,'trunk'),context={'source':'user'})
        assert 'make_default' in second['data_schema'].schema
        assert second['data_schema']({'name':'Second'})['make_default'] is False
        hass.config_entries.subentries.async_abort(second['flow_id'])
        assert catalog.data['default_trunk_id']==identity
    finally:
        await async_stop_sip_trunk(hass)


async def test_trunk_flow_persists_codec_choices_and_preserves_them_when_disabled(hass,catalog,registrar,monkeypatch):
    from custom_components.voip_stack import trunk_flow
    peer,port=registrar
    monkeypatch.setattr(trunk_flow,'async_advertise_host',AsyncMock(return_value='127.0.0.1'))
    hass.config.components.update({'assist_pipeline','ffmpeg','http','lovelace','media_source','network'})
    prepare_frontend_dependency(hass)
    form=await hass.config_entries.subentries.async_init((catalog.entry_id,'trunk'),context={'source':'user'})
    form=await hass.config_entries.subentries.async_configure(form['flow_id'],{
        'name':'Limited codecs','trunk_enabled':True,'trunk_register':True,'make_default':False})
    assert 'trunk_audio_codecs' in form['data_schema'].schema
    assert 'trunk_video_codecs' in form['data_schema'].schema
    result=await hass.config_entries.subentries.async_configure(form['flow_id'],{
        'trunk_server':'127.0.0.1','trunk_port':port,'trunk_username':'ha','trunk_inbound_default_target':'Kitchen','trunk_password':'test-only',
        'trunk_audio_codecs':['PCMU'],'trunk_video_codecs':[],
        'trunk_outbound_media_address':'198.51.100.22'})
    assert result['type']=='create_entry'
    identity=result['data']['trunk_id']
    try:
        await hass.async_block_till_done()
        assert sip_trunk(hass,identity).config.audio_codecs==('PCMU',)
        assert sip_trunk(hass,identity).config.video_codecs==()
        assert sip_trunk(hass,identity).config.outbound_media_address=='198.51.100.22'
        record=next(s for s in catalog.subentries.values() if s.data['trunk_id']==identity)
        form=await hass.config_entries.subentries.async_init((catalog.entry_id,'trunk'),context={'source':'reconfigure','subentry_id':record.subentry_id})
        result=await hass.config_entries.subentries.async_configure(form['flow_id'],{
            'name':'Limited codecs','trunk_enabled':False,'trunk_register':True,'make_default':False})
        assert result['type']=='abort'
        await hass.async_block_till_done()
        assert catalog.subentries[record.subentry_id].data['trunk_audio_codecs']==['PCMU']
        assert catalog.subentries[record.subentry_id].data['trunk_video_codecs']==[]
        assert catalog.subentries[record.subentry_id].data['trunk_outbound_media_address']=='198.51.100.22'
    finally:
        await async_stop_sip_trunk(hass)


def test_trunk_destination_picker_uses_real_phone_names(hass, catalog):
    from custom_components.voip_stack.config_flow import _trunk_target_choices
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
    runtime = catalog.runtime_data
    runtime.endpoints.register(PhoneEndpoint(endpoint_id='browser:initial', name='Casa', kind='browser', device_id='home-device'))
    runtime.endpoints.register(PhoneEndpoint(endpoint_id='browser:test', name='Test', kind='browser', device_id='test-device'))
    runtime.preferred_phone_device_id = 'home-device'
    choices, selected = _trunk_target_choices(hass, catalog, {'trunk_inbound_default_target': 'HA'})
    assert choices == ['Casa', 'Test']
    assert selected == 'Casa'
    assert _trunk_target_choices(hass, catalog, {'trunk_inbound_default_target': 'Test'})[1] == 'Test'
    assert _trunk_target_choices(hass, catalog, {'trunk_inbound_default_target': 'custom-route'})[1] == 'custom-route'
    runtime.endpoints.update('browser:initial', name='Renamed home')
    assert _trunk_target_choices(hass, catalog, {})[1] == 'Renamed home'
    runtime.preferred_phone_device_id = ''
    assert _trunk_target_choices(hass, catalog, {})[1] == ''


async def test_home_assistant_stop_unregisters_owned_trunk_binding(hass, catalog, registrar):
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP
    from custom_components.voip_stack import _register_runtime_shutdown
    peer, port = registrar
    runtime = catalog.runtime_data
    runtime.trunk_configs = {'home': normalized_trunk_config({
        'trunk_enabled': True, 'trunk_register': True,
        'trunk_server': '127.0.0.1', 'trunk_port': port,
        'trunk_username': 'ha', 'trunk_password': 'test-only',
    })}
    assert await async_start_sip_trunk(hass, local_ip='127.0.0.1')
    client = sip_trunk(hass, 'home')
    try:
        assert await client.wait_for_registration(timeout=2)
        assert len(peer.contacts) == 1
        _register_runtime_shutdown(hass, catalog)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
        assert not peer.contacts
        assert peer.requests[-1].header('Expires') == '0'
        assert not client.registered
        assert client.transport is None
        assert client._receive_task is None
        count = len(peer.requests)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
        assert len(peer.requests) == count
    finally:
        await client.stop()


@pytest.mark.parametrize("address", ["0.0.0.0", "127.0.0.1", "224.0.0.1", "::1", "https://example.org", "192.0.2.1\r\na=sendrecv"])
async def test_trunk_rejects_invalid_outbound_media_address(hass, catalog, address):
    hass.config.components.update({'assist_pipeline', 'ffmpeg', 'http', 'lovelace', 'media_source', 'network'})
    prepare_frontend_dependency(hass)
    form = await hass.config_entries.subentries.async_init((catalog.entry_id, 'trunk'), context={'source': 'user'})
    form = await hass.config_entries.subentries.async_configure(form['flow_id'], {
        'name': 'Media address validation', 'trunk_enabled': True, 'trunk_register': True, 'make_default': False,
    })
    try:
        result = await hass.config_entries.subentries.async_configure(form['flow_id'], {
            'trunk_server': '127.0.0.1', 'trunk_username': 'ha', 'trunk_password': 'test-only',
            'trunk_inbound_default_target': 'Kitchen', 'trunk_outbound_media_address': address,
        })
        assert result['type'] == 'form'
        assert result['errors']['trunk_outbound_media_address'] == 'trunk_media_address_invalid'
    finally:
        hass.config_entries.subentries.async_abort(form['flow_id'])


@pytest.mark.parametrize("register", [False, True])
async def test_native_first_trunk_keeps_client_ready_with_eager_entry_listener(hass, catalog, registrar, monkeypatch, register):
    from custom_components.voip_stack import trunk_flow
    from custom_components.voip_stack.trunk_runtime import async_reconcile_trunks

    monkeypatch.setattr(trunk_flow, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
    hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    prepare_frontend_dependency(hass)
    for subentry_id in tuple(catalog.subentries):
        hass.config_entries.async_remove_subentry(catalog, subentry_id)
    hass.config_entries.async_update_entry(catalog, data={})
    catalog.runtime_data.trunk_configs = {}
    catalog.runtime_data.default_trunk_id = ""
    unsubscribe = catalog.add_update_listener(async_reconcile_trunks)
    try:
        form = await hass.config_entries.subentries.async_init(
            (catalog.entry_id, "trunk"), context={"source": "user"})
        form = await hass.config_entries.subentries.async_configure(form["flow_id"], {
            "name": "First live trunk", "trunk_enabled": True, "trunk_register": register,
        })
        result = await hass.config_entries.subentries.async_configure(form["flow_id"], {
            "trunk_server": "127.0.0.1",
            **({"trunk_username": "ha", "trunk_password": "test-only", "trunk_port": registrar[1]}
               if register else {"trunk_allowed_ips": ["127.0.0.1"]}),
            "trunk_inbound_default_target": "Kitchen",
        })
        assert result["type"] == "create_entry", result
        await hass.async_block_till_done()
        identity = result["data"]["trunk_id"]
        client = sip_trunk(hass, identity)
        assert client is not None and client.ready
        assert catalog.runtime_data.sip.component(f"pending_trunk:{identity}") is None
    finally:
        unsubscribe()
        await async_stop_sip_trunk(hass)
