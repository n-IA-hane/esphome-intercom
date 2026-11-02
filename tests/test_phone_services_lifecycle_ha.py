"""Public LDAP lifecycle and private-account revocation, independent of SIP reachability."""

import asyncio
from dataclasses import replace
import ssl
from unittest.mock import AsyncMock

import ldap3
import pytest
from homeassistant.helpers import issue_registry as ir

from .test_phone_services_ha import (
    custom_integration_path as custom_integration_path,
    phone_lab as phone_lab,
    managed as managed,
    directory_actions, phone_record,
)
from .test_ldap_directory_ha import bind, receive, response, search, collect, BASE
from .test_ldap_standard_extensions_ha import certificate_paths

pytestmark = [pytest.mark.ha, pytest.mark.usefixtures("socket_enabled")]


async def start(lab, **config):
    from custom_components.voip_stack.phone_services import async_setup_phone_services
    directory_actions(lab.native.runtime_data)
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        "phone_services": {"listen_host": "127.0.0.1", "ldap_port": 0, **config}})
    await async_setup_phone_services(lab.hass, lab.entry)
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    return lab.runtime.phone_services


async def connect(lab):
    phone = phone_record(lab)
    reader, writer = await asyncio.open_connection("127.0.0.1", lab.runtime.ldap_server.port)
    writer.write(bind(username=phone.username, password=phone.password.encode()))
    await writer.drain()
    assert response(await receive(reader)) == (1, 0x61, 0)
    return reader, writer


async def close(writer):
    writer.close()
    await writer.wait_closed()


async def test_offline_configured_phone_authenticates_without_registrar_or_contact(managed, caplog):
    lab = managed
    owner = await start(lab)
    try:
        lab.registrar.enabled = False
        assert not lab.registrar.registrations
        reader, writer = await connect(lab)
        writer.write(search())
        await writer.drain()
        frames = await collect(reader, 2)
        assert any(response(frame)[1] == 0x64 for frame in frames)
        assert response(frames[-1]) == (2, 0x65, 0)
        assert phone_record(lab).password not in caplog.text
        await close(writer)
    finally:
        await owner.async_close()


@pytest.mark.parametrize("change", ["password", "disable", "remove", "remove-readd", "suspend", "runtime", "device", "origin"])
async def test_account_revocation_closes_authenticated_socket_before_next_search(managed, change):
    from custom_components.voip_stack.runtime_data import VoipStackRuntime
    lab = managed
    owner = await start(lab)
    try:
        reader, writer = await connect(lab)
        phone = phone_record(lab)
        principal = owner.auth.principal(phone.username)
        account = lab.registrar.accounts[phone.username]
        if change == "device":
            from homeassistant.helpers import device_registry as dr
            dr.async_get(lab.hass).async_remove_device(lab.device.id)
            await lab.hass.async_block_till_done(wait_background_tasks=True)
        elif change == "origin":
            assert await lab.hass.config_entries.async_remove(lab.native.entry_id)
            await lab.hass.async_block_till_done(wait_background_tasks=True)
        elif change == "runtime":
            lab.entry.runtime_data = replace(lab.runtime, ldap_server=None, phone_services=None)
        elif change == "suspend":
            lab.native.runtime_data.services = {}
            await lab.controller.async_sync()
        else:
            accounts = [] if change in {"remove", "remove-readd"} else [replace(account,
                password="rotated-fixture-only" if change == "password" else account.password,
                enabled=change != "disable")]
            lab.registrar.update_accounts(accounts)
            if change == "remove-readd":
                lab.registrar.update_accounts([account])
        assert not owner.auth.valid(principal)
        if change == "runtime":
            writer.write(search())
            await writer.drain()
        assert await asyncio.wait_for(reader.read(), 2) == b""
        await close(writer)
    finally:
        lab.entry.runtime_data = lab.runtime
        await owner.async_close()


async def test_auth_epoch_survives_display_update_and_register_expiry(managed):
    from .test_managed_phones_ha import bind_account
    lab = managed
    owner = await start(lab)
    try:
        phone = phone_record(lab)
        principal = owner.auth.principal(phone.username)
        account = lab.registrar.accounts[phone.username]
        lab.registrar.update_accounts([replace(account, display_name="New name")])
        assert owner.auth.valid(principal)
        await bind_account(lab, phone)
        for registration in lab.registrar.registrations.values():
            registration.expires_at = 0
        lab.registrar.registrations.clear()
        assert owner.auth.valid(principal)
    finally:
        await owner.async_close()


async def test_pending_bind_cannot_publish_after_same_password_account_aba(managed):
    lab = managed
    owner = await start(lab)
    entered, release = asyncio.Event(), asyncio.Event()
    server = lab.runtime.ldap_server
    original = server.authenticate
    async def pending(username, password):
        principal = await original(username, password)
        entered.set()
        await release.wait()
        return principal
    server.authenticate = pending
    try:
        phone = phone_record(lab)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(bind(username=phone.username, password=phone.password.encode()))
        await writer.drain()
        await entered.wait()
        account = lab.registrar.accounts[phone.username]
        lab.registrar.update_accounts([])
        lab.registrar.update_accounts([account])
        release.set()
        frame = await asyncio.wait_for(reader.read(1024), 2)
        assert not frame or response(frame)[2] != 0
        await close(writer)
    finally:
        release.set()
        await owner.async_close()


async def test_bind_failure_repair_keeps_sip_and_never_disables_native_then_recovers(managed):
    lab = managed
    owner = await start(lab)
    sip_owner = lab.runtime.sip
    sent_before = list(lab.sent)
    blocked = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    blocked_port = blocked.sockets[0].getsockname()[1]
    try:
        lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
            "phone_services": {"listen_host": "127.0.0.1", "ldap_port": blocked_port}})
        await owner.async_reconcile()
        await lab.hass.async_block_till_done(wait_background_tasks=True)
        assert lab.runtime.ldap_server is None
        assert lab.runtime.sip is sip_owner and sip_owner.active
        assert ir.async_get(lab.hass).async_get_issue("voip_stack", "phone_service_ldap")
        assert not [data for action, data in lab.sent[len(sent_before):]
                    if action == "configure_directory" and data["enabled"] is False]
        blocked.close()
        await blocked.wait_closed()
        await owner.async_reconcile()  # Retry identical configuration after external failure clears.
        await lab.hass.async_block_till_done(wait_background_tasks=True)
        assert lab.runtime.ldap_server.port == blocked_port
        assert not ir.async_get(lab.hass).async_get_issue("voip_stack", "phone_service_ldap")
        assert len([1 for action, _ in lab.sent if action == "refresh_ldap_directory"]) == 2
        assert lab.runtime.sip is sip_owner
    finally:
        blocked.close()
        await blocked.wait_closed()
        await owner.async_close()


async def test_certificate_failure_closes_old_plain_listener_preserving_provider_until_recovery_or_admin_disable(managed, tmp_path):
    lab = managed
    owner = await start(lab)
    previous = lab.runtime.ldap_server
    previous_refresh = sum(action == "refresh_ldap_directory" for action, _ in lab.sent)
    try:
        certificate, key = tmp_path / "later-cert.pem", tmp_path / "later-key.pem"
        config = {"listen_host": "127.0.0.1", "ldap_port": 0, "tls": True,
                  "certificate": str(certificate), "private_key": str(key)}
        lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data, "phone_services": config})
        await owner.async_reconcile()
        await lab.hass.async_block_till_done(wait_background_tasks=True)
        assert lab.runtime.ldap_server is None
        assert ir.async_get(lab.hass).async_get_issue("voip_stack", "phone_service_tls")
        assert not any(action == "configure_directory" and not data["enabled"] for action, data in lab.sent)
        real_cert, real_key = certificate_paths(tmp_path)
        certificate.write_bytes(real_cert.read_bytes())
        key.write_bytes(real_key.read_bytes())
        key.chmod(0o600)
        await owner.async_reconcile()
        await lab.hass.async_block_till_done(wait_background_tasks=True)
        assert lab.runtime.ldap_server is not previous
        assert sum(action == "refresh_ldap_directory" for action, _ in lab.sent) == previous_refresh + 1
        assert not ir.async_get(lab.hass).async_get_issue("voip_stack", "phone_service_tls")
        lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
            "phone_services": {**config, "enabled": False}})
        await owner.async_reconcile()
        await lab.hass.async_block_till_done(wait_background_tasks=True)
        assert lab.runtime.ldap_server is None
        assert [data for action, data in lab.sent if action == "configure_directory"][-1]["enabled"] is False
    finally:
        await owner.async_close()


async def test_directory_change_immediately_coalesces_refresh_and_unchanged_records_do_not(managed):
    from custom_components.voip_stack.roster import RosterEntry
    lab = managed
    owner = await start(lab)
    try:
        directory = lab.runtime.directory
        calls = lambda: sum(action == "refresh_ldap_directory" for action, _ in lab.sent)
        before = calls()
        await owner.async_reconcile()
        await lab.controller.async_sync()
        assert calls() == before
        # Canonical generation observer invokes the existing coalescing coordinator.
        from homeassistant.helpers import device_registry as dr
        devices = dr.async_get(lab.hass)
        async with lab.controller._lock:
            devices.async_update_device(lab.device.id, name_by_user="One")
            devices.async_update_device(lab.device.id, name_by_user="Two")
        await lab.hass.async_block_till_done(wait_background_tasks=True)
        assert calls() == before + 1
        directory.replace(directory.snapshot())
        await lab.hass.async_block_till_done(wait_background_tasks=True)
        assert calls() == before + 1
    finally:
        await owner.async_close()


async def test_implicit_tls_ldap3_validates_ca_and_hostname_and_reads_canonical_directory(managed, tmp_path):
    lab = managed
    certificate, key = certificate_paths(tmp_path)
    owner = await start(lab, tls=True, certificate=str(certificate), private_key=str(key))
    phone = phone_record(lab)
    def client():
        connection = ldap3.Connection(ldap3.Server("localhost", port=lab.runtime.ldap_server.port,
            use_ssl=True, mode=ldap3.IP_V4_ONLY, tls=ldap3.Tls(validate=ssl.CERT_REQUIRED,
            ca_certs_file=str(certificate)), get_info=ldap3.NONE), user=phone.username, password=phone.password,
            auto_bind=True)
        try:
            assert connection.search(BASE, "(&(uid=*)(ou=esphome))", attributes=["uid", "cn", "labeledURI"])
            assert len(connection.entries) == 1
            assert connection.entries[0].uid.value == phone.endpoint_id
            assert connection.entries[0].cn.value == "Ingresso"
        finally:
            connection.unbind()
    try:
        await asyncio.to_thread(client)
    finally:
        await owner.async_close()


async def test_partial_implicit_tls_client_is_closed_with_listener_without_waiting_handshake(managed, tmp_path):
    lab = managed
    certificate, key = certificate_paths(tmp_path)
    owner = await start(lab, tls=True, certificate=str(certificate), private_key=str(key))
    reader, writer = await asyncio.open_connection("127.0.0.1", lab.runtime.ldap_server.port)
    await asyncio.sleep(0)
    await asyncio.wait_for(owner.async_close(), 1)
    assert await asyncio.wait_for(reader.read(), 1) == b""
    await close(writer)


async def test_anonymous_old_runtime_cannot_publish_directory_after_owner_replaced(managed):
    lab = managed
    owner = await start(lab, allow_anonymous=True)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", lab.runtime.ldap_server.port)
        lab.entry.runtime_data = replace(lab.runtime, ldap_server=None, phone_services=None)
        writer.write(search())
        await writer.drain()
        assert await asyncio.wait_for(reader.read(1024), 2) == b""
        await close(writer)
    finally:
        lab.entry.runtime_data = lab.runtime
        await owner.async_close()


@pytest.mark.parametrize("operation", ["listener", "revoke"])
async def test_established_tls_client_ignoring_close_notify_cannot_hold_shutdown(managed, tmp_path, operation):
    import socket
    lab = managed
    certificate, key = certificate_paths(tmp_path)
    owner = await start(lab, tls=True, certificate=str(certificate), private_key=str(key))
    server = lab.runtime.ldap_server
    server.limits = replace(server.limits, write_seconds=0.05)
    phone = phone_record(lab)
    def stalled_client():
        context = ssl.create_default_context(cafile=str(certificate))
        client = context.wrap_socket(socket.socket(), server_hostname="localhost")
        client.connect(("127.0.0.1", server.port))
        client.sendall(bind(username=phone.username, password=phone.password.encode()))
        assert response(client.recv(1024)) == (1, 0x61, 0)
        return client
    client = await asyncio.to_thread(stalled_client)
    await asyncio.sleep(0)
    try:
        if operation == "revoke":
            await asyncio.wait_for(server.async_revoke_endpoint(phone.endpoint_id), 0.5)
            assert not server._listener._clients
        else:
            await asyncio.wait_for(owner.async_close(), 0.5)
        assert not server._connections
    finally:
        client.close()
        await owner.async_close()


async def test_parallel_search_failures_close_stalled_tls_transport_via_connection_owner(managed, tmp_path):
    import socket
    lab = managed
    certificate, key = certificate_paths(tmp_path)
    owner = await start(lab, tls=True, certificate=str(certificate), private_key=str(key))
    server = lab.runtime.ldap_server
    server.limits = replace(server.limits, write_seconds=0.05)
    phone = phone_record(lab)
    def bound_client():
        client = ssl.create_default_context(cafile=str(certificate)).wrap_socket(socket.socket(), server_hostname="localhost")
        client.connect(("127.0.0.1", server.port))
        client.sendall(bind(username=phone.username, password=phone.password.encode()))
        assert response(client.recv(1024)) == (1, 0x61, 0)
        return client
    client = await asyncio.to_thread(bound_client)
    async def failed_search(_request, _generation):
        raise ConnectionError("synthetic write failure")
    for connection in server._connections:
        connection._search = failed_search
    try:
        await asyncio.to_thread(client.sendall, search(2) + search(3))
        async with asyncio.timeout(0.5):
            while server._connections or server._listener._clients:
                await asyncio.sleep(0.01)
    finally:
        client.close()
        await owner.async_close()
