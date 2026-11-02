"""LDAP settings through public HA flow and real ESPHome API serialization."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from .test_phone_services_ha import (
    custom_integration_path as custom_integration_path,
    phone_lab as phone_lab, managed as managed,
    directory_actions, phone_record,
)
from .test_managed_phones_ha import varint, read_varint
from .test_phone_services_lifecycle_ha import start

pytestmark = pytest.mark.ha


async def test_public_reconfigure_ldap_panel_validates_and_persists_without_imap(phone_lab, monkeypatch):
    from custom_components.voip_stack.config_entry_runtime import entry_runtime_signature
    lab = phone_lab
    lab.hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    reload = AsyncMock()
    monkeypatch.setattr(lab.hass.config_entries, "async_reload", reload)
    previous = entry_runtime_signature(lab.entry)
    menu = await lab.hass.config_entries.flow.async_init("voip_stack", context={
        "source": "reconfigure", "entry_id": lab.entry.entry_id})
    assert "phone_services" in menu["menu_options"]
    form = await lab.hass.config_entries.flow.async_configure(menu["flow_id"], {"next_step_id": "phone_services"})
    values = form["data_schema"]({})
    assert values["enabled"] and values["ldap_port"] == 1389
    assert not any("imap" in key for key in values)
    invalid = await lab.hass.config_entries.flow.async_configure(form["flow_id"], {**values, "base_dn": "invalid-dn"})
    assert invalid["errors"] == {"base": "phone_service_base_dn_invalid"}
    invalid = await lab.hass.config_entries.flow.async_configure(form["flow_id"], {**values, "tls": True})
    assert invalid["errors"] == {"base": "phone_service_certificate_required"}
    result = await lab.hass.config_entries.flow.async_configure(form["flow_id"], {**values,
        "listen_host": "127.0.0.1", "ldap_port": 1636, "tls": True,
        "certificate": "/ssl/ldap.pem", "private_key": "/ssl/ldap-key.pem"})
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    assert lab.entry.data["phone_services"]["ldap_port"] == 1636
    assert entry_runtime_signature(lab.entry) == previous


async def test_directory_configure_and_refresh_use_actual_private_api_client_protobuf(managed, socket_enabled):
    from aioesphomeapi import APIClient, api_pb2
    from aioesphomeapi.core import MESSAGE_TYPE_TO_PROTO
    lab = managed
    messages, completed, actors = [], asyncio.Event(), set()
    async def serve(reader, writer):
        actors.add(asyncio.current_task())
        try:
            while True:
                assert await reader.readexactly(1) == b"\0"
                size, kind = await read_varint(reader), await read_varint(reader)
                request = MESSAGE_TYPE_TO_PROTO[kind].FromString(await reader.readexactly(size))
                reply = None
                if kind == 1:
                    reply = 2, api_pb2.HelloResponse(api_version_major=1, api_version_minor=10, name="phone_node")
                elif kind == 3:
                    reply = 4, api_pb2.AuthenticationResponse()
                elif kind == 7:
                    reply = 8, api_pb2.PingResponse()
                elif kind == 5:
                    reply = 6, api_pb2.DisconnectResponse()
                elif isinstance(request, api_pb2.ExecuteServiceRequest):
                    messages.append(request)
                    if len(messages) == 5:
                        completed.set()
                if reply is not None:
                    raw = reply[1].SerializeToString()
                    writer.write(b"\0" + varint(len(raw)) + varint(reply[0]) + raw)
                    await writer.drain()
                if kind == 5:
                    return
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            actors.discard(asyncio.current_task())
    listener = await asyncio.start_server(serve, "127.0.0.1", 0)
    client = APIClient("127.0.0.1", listener.sockets[0].getsockname()[1], expected_name="phone_node")
    owner = None
    try:
        await client.connect(login=True)
        lab.native.runtime_data.client = client
        owner = await start(lab)
        await asyncio.wait_for(completed.wait(), 2)
        assert [message.key for message in messages] == [1, 2, 3, 10, 11]
        configure = messages[3]
        assert [item.string_ for item in configure.args] == ["127.0.0.1", "", "ou=phonebook,dc=voip",
            "uid={username},ou=accounts,ou=phonebook,dc=voip", "ha", "", ""]
        assert configure.args[1].int_ == lab.runtime.ldap_server.port
        assert not configure.args[5].bool_ and configure.args[6].bool_
        assert not messages[4].args
        assert phone_record(lab).password.encode() not in configure.SerializeToString()
        assert not lab.events
    finally:
        if owner is not None:
            await owner.async_close()
        await client.disconnect()
        listener.close()
        await listener.wait_closed()
        await asyncio.gather(*tuple(actors), return_exceptions=True)


async def test_directory_refresh_await_does_not_mark_replaced_api_owner_delivered(managed, socket_enabled):
    from types import SimpleNamespace
    lab = managed
    owner = await start(lab)
    entered, release = asyncio.Event(), asyncio.Event()
    original = lab.native.runtime_data.client.execute_service
    async def blocked(service, data):
        if service.name == "refresh_ldap_directory":
            entered.set()
            await release.wait()
        await original(service, data)
    native = lab.native.runtime_data
    native.client.execute_service = blocked
    owner.controller.configure_directory_service(dict(owner._config), epoch="next-test-epoch")
    try:
        await asyncio.wait_for(entered.wait(), 2)
        native.client = SimpleNamespace(is_connected=True, execute_service=original)
        release.set()
        await lab.hass.async_block_till_done(wait_background_tasks=True)
        # A new API owner must receive configuration and refresh itself.
        before = len(lab.sent)
        await lab.controller.async_sync()
        assert [action for action, _ in lab.sent[before:]] == [
            "set_ha_display_name", "set_account", "register_account", "configure_directory", "refresh_ldap_directory"]
    finally:
        release.set()
        await owner.async_close()


@pytest.mark.parametrize("mode", ["missing-configure", "configure-rpc-failed"])
async def test_refresh_requires_same_owner_current_directory_configuration_delivered(managed, socket_enabled, mode):
    lab = managed
    owner = await start(lab)
    native = lab.native.runtime_data
    try:
        before = len(lab.sent)
        config = dict(owner._config)
        if mode == "missing-configure":
            native.services = {key: service for key, service in native.services.items()
                               if service.name != "configure_directory"}
        else:
            execute = native.client.execute_service
            async def failed_configure(service, data):
                if service.name == "configure_directory":
                    raise ConnectionError("synthetic RPC failure")
                await execute(service, data)
            native.client.execute_service = failed_configure
            config["ldap_port"] = lab.runtime.ldap_server.port + 1
        owner.controller.configure_directory_service(config, epoch="test-new-configuration")
        await lab.hass.async_block_till_done(wait_background_tasks=True)
        assert not any(action == "refresh_ldap_directory" for action, _ in lab.sent[before:])
    finally:
        await owner.async_close()
