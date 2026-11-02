"""Independent ldap3 clients for paging and both standard TLS transports."""

import asyncio
import datetime
import ipaddress
import ssl

import ldap3
import pytest

from .test_ldap_directory_ha import server as server, directory, BASE
from .test_phone_services_ha import (
    custom_integration_path as custom_integration_path,
    phone_lab as phone_lab,
    managed as managed,
    directory_actions,
    phone_record,
)

pytestmark = pytest.mark.ha


def certificate_paths(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256()))
    certificate, private_key = tmp_path / "certificate.pem", tmp_path / "key.pem"
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private_key.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
    certificate.chmod(0o600)
    private_key.chmod(0o600)
    return certificate, private_key


async def test_ldap3_paging_round_trip_has_bounded_pages_and_opaque_cookie(server):
    def client():
        connection = ldap3.Connection(ldap3.Server("127.0.0.1", port=server.port, get_info=ldap3.NONE),
            user="kitchen", password="fixture-only-password", auto_bind=True)
        try:
            assert connection.search(BASE, "(uid=*)", attributes=["uid"], paged_size=2)
            control = connection.result.get("controls", {}).get("1.2.840.113556.1.4.319")
            assert control is not None
            assert len(connection.entries) == 2
            cookie = control["value"]["cookie"]
            assert cookie and b"kitchen" not in cookie
            identities = [entry.uid.value for entry in connection.entries]
            assert connection.search(BASE, "(uid=*)", attributes=["uid"], paged_size=2, paged_cookie=cookie)
            assert connection.result["controls"]["1.2.840.113556.1.4.319"]["value"]["cookie"] == b""
            identities.extend(entry.uid.value for entry in connection.entries)
            assert len(set(identities)) == 4
        finally:
            connection.unbind()
    await asyncio.to_thread(client)


async def test_ldap3_starttls_on_configured_plain_listener_verifies_certificate(managed, tmp_path, socket_enabled):
    from custom_components.voip_stack import phone_services
    lab = managed
    directory_actions(lab.native.runtime_data)
    certificate, key = certificate_paths(tmp_path)
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data, "phone_services": {
        "listen_host": "127.0.0.1", "ldap_port": 0, "certificate": str(certificate), "private_key": str(key)}})
    await phone_services.async_setup_phone_services(lab.hass, lab.entry)
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    phone = phone_record(lab)
    try:
        def client():
            tls = ldap3.Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=str(certificate))
            connection = ldap3.Connection(ldap3.Server("localhost", port=lab.runtime.ldap_server.port,
                tls=tls, mode=ldap3.IP_V4_ONLY, get_info=ldap3.NONE), user=phone.username, password=phone.password)
            try:
                assert connection.open() is None
                assert connection.start_tls()
                assert connection.bind()
                assert connection.search(BASE, "(uid=*)", attributes=["uid", "cn"])
                assert any(entry.cn.value == "Ingresso" for entry in connection.entries)
            finally:
                connection.unbind()
        await asyncio.to_thread(client)
    finally:
        await phone_services.async_close_phone_services(lab.hass)


async def test_unknown_extended_operation_returns_protocol_error(server):
    def client():
        connection = ldap3.Connection(ldap3.Server("127.0.0.1", port=server.port, get_info=ldap3.NONE),
            user="kitchen", password="fixture-only-password", auto_bind=True)
        try:
            connection.extend.standard.who_am_i()
            assert connection.result["result"] == 2
        finally:
            connection.unbind()
    await asyncio.to_thread(client)


@pytest.mark.parametrize("invalid", ["unknown", "replay", "different-query", "directory-change", "rebind", "expired"])
async def test_paging_cookie_is_bound_to_one_query_directory_and_authorization(server, invalid):
    from custom_components.voip_stack.roster import RosterEntry
    from dataclasses import replace
    def first_page():
        connection = ldap3.Connection(ldap3.Server("127.0.0.1", port=server.port, get_info=ldap3.NONE),
            user="kitchen", password="fixture-only-password", auto_bind=True)
        assert connection.search(BASE, "(uid=*)", attributes=["uid"], paged_size=1, paged_criticality=True)
        return connection, connection.result["controls"]["1.2.840.113556.1.4.319"]["value"]["cookie"]
    connection, cookie = await asyncio.to_thread(first_page)
    try:
        if invalid == "directory-change":
            server.directory.upsert(RosterEntry("new", name="New"))
        if invalid == "expired":
            for client in server._connections:
                client._cursors = {key: (*value[:3], 0, value[4]) for key, value in client._cursors.items()}
        def continuation():
            chosen = cookie
            if invalid == "unknown":
                chosen = b"unknown-cookie"
            if invalid == "replay":
                assert connection.search(BASE, "(uid=*)", attributes=["uid"], paged_size=1, paged_cookie=cookie)
            if invalid == "rebind":
                assert connection.rebind()
            assert not connection.search(BASE, "(cn=*)" if invalid == "different-query" else "(uid=*)",
                attributes=["uid"], paged_size=1, paged_cookie=chosen)
            assert connection.result["result"] == 53
            assert not connection.entries
        await asyncio.to_thread(continuation)
    finally:
        await asyncio.to_thread(connection.unbind)


async def test_paging_zero_abandons_cookie_and_active_cursors_are_bounded(server):
    from dataclasses import replace
    server.limits = replace(server.limits, cursors_per_connection=1)
    def client():
        connection = ldap3.Connection(ldap3.Server("127.0.0.1", port=server.port, get_info=ldap3.NONE),
            user="kitchen", password="fixture-only-password", auto_bind=True)
        try:
            assert connection.search(BASE, "(uid=*)", attributes=["uid"], paged_size=1)
            cookie = connection.result["controls"]["1.2.840.113556.1.4.319"]["value"]["cookie"]
            assert not connection.search(BASE, "(uid=*)", attributes=["uid"], paged_size=1)
            assert connection.result["result"] == 51
            connection.search(BASE, "(uid=*)", attributes=["uid"], paged_size=0, paged_cookie=cookie)
            assert connection.result["result"] == 0
            assert connection.result["controls"]["1.2.840.113556.1.4.319"]["value"]["cookie"] == b""
            assert not connection.entries
            assert connection.search(BASE, "(uid=*)", attributes=["uid"], paged_size=1)
        finally:
            connection.unbind()
    await asyncio.to_thread(client)


async def test_starttls_resets_authorization_and_disallows_repeated_upgrade(socket_enabled, tmp_path):
    from unittest.mock import AsyncMock
    from custom_components.voip_stack import ldap_codec as codec
    from custom_components.voip_stack.ldap_server import LdapDirectoryServer
    from .test_ldap_directory_ha import connected, message, tlv, receive, response, search, collect, bind
    certificate, key = certificate_paths(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    value = LdapDirectoryServer(directory(), AsyncMock(return_value="esphome:kitchen"))
    await value.async_start("127.0.0.1", 0, starttls_context=context)
    reader, writer = await connected(value)
    request = lambda identity: message(identity, tlv(0x77, tlv(0x80, codec.START_TLS.encode())))
    try:
        writer.write(request(2))
        await writer.drain()
        assert response(await receive(reader)) == (2, 0x78, 0)
        await writer.start_tls(ssl.create_default_context(cafile=str(certificate)), server_hostname="localhost")
        writer.write(search(3))
        await writer.drain()
        assert response((await collect(reader, 3))[-1])[2] == 50
        writer.write(bind(4))
        await writer.drain()
        assert response(await receive(reader)) == (4, 0x61, 0)
        writer.write(request(5))
        await writer.drain()
        assert response(await receive(reader)) == (5, 0x78, 1)
    finally:
        writer.close()
        await writer.wait_closed()
        await value.async_close()


@pytest.mark.parametrize("change", ["deref", "root-dse"])
async def test_paging_rejects_changed_alias_policy_and_unsolicited_root_cookie(server, change):
    def client():
        connection = ldap3.Connection(ldap3.Server("127.0.0.1", port=server.port, get_info=ldap3.NONE),
            user="kitchen", password="fixture-only-password", auto_bind=True)
        try:
            if change == "root-dse":
                connection.search("", "(objectClass=*)", search_scope=ldap3.BASE,
                    attributes=["supportedLDAPVersion"], paged_size=1, paged_cookie=b"not-issued")
            else:
                assert connection.search(BASE, "(uid=*)", attributes=["uid"], paged_size=1,
                    dereference_aliases=ldap3.DEREF_ALWAYS)
                cookie = connection.result["controls"]["1.2.840.113556.1.4.319"]["value"]["cookie"]
                connection.search(BASE, "(uid=*)", attributes=["uid"], paged_size=1,
                    dereference_aliases=ldap3.DEREF_NEVER, paged_cookie=cookie)
            assert connection.result["result"] == 53
            assert not connection.entries
        finally:
            connection.unbind()
    await asyncio.to_thread(client)
