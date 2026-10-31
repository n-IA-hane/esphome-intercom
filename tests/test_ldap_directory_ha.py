"""Independent BER, real LDAP clients and lifecycle checks for the directory."""

import asyncio
from dataclasses import replace
import os
import shutil
from unittest.mock import AsyncMock

import pytest

from custom_components.voip_stack import ldap_codec as codec
from custom_components.voip_stack.ldap_server import LdapDirectoryServer, Limits, matches
from custom_components.voip_stack.phone_directory import PhoneDirectory
from custom_components.voip_stack.roster import RosterEntry, RosterError

pytestmark = pytest.mark.ha
BASE = "ou=phonebook,dc=voip"


def tlv(tag, value):
    length = len(value)
    size = bytes([length]) if length < 128 else bytes([0x80 + (length.bit_length() + 7) // 8]) + length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([tag]) + size + value


def integer(value):
    raw = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    return tlv(2, b"\0" + raw if raw[0] & 128 else raw)


def message(identity, operation):
    return tlv(0x30, integer(identity) + operation)


def bind(identity=1, username="kitchen", password=b"fixture-only-password", *, version=3):
    name = f"uid={username},ou=accounts,{BASE}" if username else ""
    return message(identity, tlv(0x60, integer(version) + tlv(4, name.encode()) + tlv(0x80, password)))


def search(identity=2, *, base=BASE, scope=2, filter=None, attributes=("cn", "uid", "telephoneNumber", "labeledURI", "ou"), size=64, types=False, critical=False):
    filter = filter or tlv(0x87, b"cn")
    body = (tlv(4, base.encode()) + tlv(10, bytes([scope])) + tlv(10, b"\0") + integer(size) + integer(3)
            + tlv(1, b"\xff" if types else b"\0") + filter + tlv(0x30, b"".join(tlv(4, item.encode()) for item in attributes)))
    control = tlv(0xA0, tlv(0x30, tlv(4, b"1.2.3.4") + tlv(1, b"\xff"))) if critical else b""
    return message(identity, tlv(0x63, body) + control)


def equality(attribute, value):
    return tlv(0xA3, tlv(4, attribute.encode()) + tlv(4, value))


def parts(data):
    """Small independent BER oracle, not the production schema or framing."""
    output = []
    while data:
        tag, length = data[:2]
        offset = 2
        if length & 128:
            count = length & 127
            length = int.from_bytes(data[offset:offset + count], "big")
            offset += count
        output.append((tag, data[offset:offset + length]))
        data = data[offset + length:]
    return output


def response(data):
    root, = parts(data)
    fields = parts(root[1])
    identity = int.from_bytes(fields[0][1], "big")
    operation = fields[1]
    code = int.from_bytes(parts(operation[1])[0][1], "big") if operation[0] != 0x64 else None
    return identity, operation[0], code


async def receive(reader):
    header = await reader.readexactly(2)
    length = header[1]
    if length & 128:
        extension = await reader.readexactly(length & 127)
        length = int.from_bytes(extension, "big")
        header += extension
    assert length < 1024 * 1024
    return header + await reader.readexactly(length)


async def collect(reader, identity):
    frames = []
    while True:
        frame = await asyncio.wait_for(receive(reader), 2)
        assert response(frame)[0] == identity
        frames.append(frame)
        if response(frame)[1] == 0x65:
            return frames


def directory():
    return PhoneDirectory([
        RosterEntry("kitchen", name="Kitchen", extension="101", sip_uri="sip:kitchen@pbx.example.org",
                    metadata={"endpoint_id": "esphome:kitchen", "endpoint_kind": "esphome", "registered": False,
                              "password": "must-never-be-published"}),
        RosterEntry("reception", name="Réception", number="+39 010-1234", sip_uri="sip:reception@pbx.example.org"),
        RosterEntry("test", name="Test", extension="667", sip_uri="sip:test@pbx.example.org",
                    metadata={"endpoint_id": "browser:test", "endpoint_kind": "browser"}),
        RosterEntry("open_door", name="Open door", extension="202", sip_uri="sip:open_door@pbx.example.org",
                    metadata={"virtual_endpoint": "automation"}),
        RosterEntry("hidden", name="Hidden", enabled=False),
    ])


@pytest.fixture
async def server(socket_enabled):
    async def authenticate(username, password):
        return "esphome:kitchen" if username == "kitchen" and password == b"fixture-only-password" else None
    value = LdapDirectoryServer(directory(), authenticate)
    await value.async_start("127.0.0.1", 0)
    yield value
    await value.async_close()
    await asyncio.sleep(0)
    assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("voip-ldap-")]


async def connected(server):
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write(bind())
    await writer.drain()
    assert response(await receive(reader)) == (1, 0x61, 0)
    return reader, writer


async def test_ldap_assist_uses_robot_category_and_other_services_keep_their_kind(server):
    for identity, virtual in (("assistant", "assist_pipeline"), ("other_service", "other_service")):
        server.directory.upsert(RosterEntry(identity, name=identity, metadata={"virtual_endpoint": virtual}))
    reader, writer = await connected(server)
    try:
        for identity, expected in (("assistant", b"automation"), ("other_service", b"service")):
            writer.write(search(filter=equality("uid", identity.encode()), attributes=("uid", "ou")))
            await writer.drain()
            frames = await collect(reader, 2)
            entry, = [frame for frame in frames if response(frame)[1] == 0x64]
            fields = parts(parts(parts(entry)[0][1])[1][1])
            attributes = {
                parts(attribute[1])[0][1]: tuple(value[1] for value in parts(parts(attribute[1])[1][1]))
                for attribute in parts(fields[1][1])
            }
            assert attributes[b"uid"] == (identity.encode(),)
            assert attributes[b"ou"] == (expected,)
    finally:
        writer.close()
        await writer.wait_closed()


async def test_canonical_group_type_exports_group_ou_over_ldap(server):
    for name, kind in (("Reception ring", "ring"), ("Family call", "conference")):
        server.directory.upsert(RosterEntry(name, name=name,
            sip_uri=f"sip:{name.replace(' ', '%20')}@pbx.example.org",
            metadata={"group_type": kind, "members": ["kitchen"]}))
    reader, writer = await connected(server)
    try:
        # Exclude the organizational-unit container, as the real directory
        # client does by requiring a callable contact's attributes.
        group_contacts = tlv(0xA0, equality("ou", b"group") + tlv(0x87, b"uid"))
        writer.write(search(filter=group_contacts, attributes=("uid", "ou")))
        await writer.drain()
        frames = await collect(reader, 2)
        assert len([frame for frame in frames if response(frame)[1] == 0x64]) == 2
        for frame in frames:
            if response(frame)[1] != 0x64:
                continue
            fields = parts(parts(parts(frame)[0][1])[1][1])
            assert b",ou=group," in fields[0][1]
            attributes = {
                parts(attribute[1])[0][1]: tuple(value[1] for value in parts(parts(attribute[1])[1][1]))
                for attribute in parts(fields[1][1])
            }
            assert attributes[b"ou"] == (b"group",)
    finally:
        writer.close()
        await writer.wait_closed()


def test_directory_single_record_identity_atomic_update_and_offline():
    value = directory()
    events = []
    unsubscribe = value.subscribe(lambda: events.append(value.generation))
    original = value.get("esphome:kitchen")
    assert original and original.metadata["registered"] is False
    assert not value.upsert(original)
    renamed = replace(original, name="New name")
    assert value.upsert(renamed)
    assert len(value.snapshot()) == 5 and value.get("esphome:kitchen").name == "New name"
    previous = value.snapshot()
    with pytest.raises(RosterError):
        value.replace([renamed, renamed])
    assert value.snapshot() == previous
    assert value.remove("esphome:kitchen") and not value.remove("esphome:kitchen")
    unsubscribe()
    assert len(events) == 2


def test_codec_independent_bind_abandon_and_response_goldens():
    request = codec.decode_request(bytes.fromhex("300c020101600702010304008000"))
    assert request.bind_dn == "" and request.password == b"" and request.version == 3
    assert codec.encode_result(1, "bindResponse") == bytes.fromhex("300c02010161070a010004000400")
    request = codec.decode_request(bytes.fromhex("3006020103500102"))
    assert request.message_id == 3 and request.abandon_id == 2
    assert "fixture-only-password" not in repr(codec.decode_request(bind()))


def test_codec_nested_filters_utf8_and_substring_bounds():
    subtree = tlv(0xA0, tlv(0x87, b"objectClass") + tlv(0xA1, equality("cn", "Réception".encode()) +
        tlv(0xA4, tlv(4, b"cn") + tlv(0x30, tlv(0x80, b"Kitchen")))))
    request = codec.decode_request(search(filter=subtree))
    assert request.filter.operation == "and"
    assert request.filter.children[1].children[0].value == "Réception".encode()
    deep = tlv(0x87, b"objectClass")
    for _ in range(17):
        deep = tlv(0xA0, deep)
    with pytest.raises(codec.LdapProtocolError):
        codec.decode_request(search(filter=deep))
    wide = tlv(0xA1, b"".join(tlv(0x87, b"cn") for _ in range(65)))
    with pytest.raises(codec.LdapProtocolError):
        codec.decode_request(search(filter=wide))


@pytest.mark.parametrize("wire", [b"\x30\x80\0\0", b"\x30\x84\x7f\xff\xff\xff",
    bytes.fromhex("300e0201016009020103240204008000"), bytes.fromhex("300c020100600702010304008000"),
    bytes.fromhex("300c02010161070a010004000400")])
def test_codec_rejects_non_ldap_ber_and_response_operations(wire):
    with pytest.raises(codec.LdapProtocolError):
        codec.decode_request(wire)


def test_codec_fragment_header_and_types_only_entries():
    wire = search()
    assert codec.frame_size(wire[:1]) is None
    assert codec.frame_size(wire[:2]) == len(wire)
    encoded = codec.encode_entry(2, "uid=phone," + BASE, {"cn": (b"Phone",)}, types_only=True)
    attributes = parts(parts(parts(encoded)[0][1])[1][1])[1]
    assert attributes[0] == 0x30
    attribute, = parts(attributes[1])
    assert parts(attribute[1])[-1] == (0x31, b"")


def test_filter_undefined_not_uri_case_and_linear_substrings():
    fields = {"cn": (b"Kitchen",), "telephonenumber": (b"+39 010-1234",), "labeleduri": (b"sip:Case@host",)}
    assert matches(codec.Filter("equalityMatch", "telephonenumber", b"+390101234"), fields)
    assert not matches(codec.Filter("equalityMatch", "labeleduri", b"sip:case@host"), fields)
    assert matches(codec.Filter("not", children=(codec.Filter("present", "unknownAttribute"),)), fields) is None
    assert matches(codec.Filter("substringFilter", "cn", substrings=(("initial", b"kit"), ("final", b"chen"))), fields)


async def test_raw_fragmented_bind_root_dse_and_authorization(server):
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write(search(1, base="", scope=0, filter=tlv(0x87, b"objectClass"), attributes=("supportedLDAPVersion", "namingContexts")))
    await writer.drain()
    frames = await collect(reader, 1)
    assert response(frames[-1])[2] == 0 and b"supportedldapversion" in frames[0]
    writer.write(search(2))
    await writer.drain()
    assert response((await collect(reader, 2))[-1])[2] == 50
    request = bind(3)
    for byte in request:
        writer.write(bytes([byte]))
        await writer.drain()
    assert response(await receive(reader)) == (3, 0x61, 0)
    writer.write(search(4, filter=equality("telephoneNumber", b"101")))
    await writer.drain()
    frames = await collect(reader, 4)
    assert len(frames) == 2 and b"Kitchen" in frames[0]
    assert b"must-never-be-published" not in b"".join(frames)
    writer.close()
    await writer.wait_closed()


async def test_invalid_bind_removes_previous_authorization(server):
    reader, writer = await connected(server)
    writer.write(bind(2, password=b"wrong"))
    await writer.drain()
    assert response(await receive(reader)) == (2, 0x61, 49)
    writer.write(search(3))
    await writer.drain()
    assert response((await collect(reader, 3))[-1])[2] == 50
    writer.close()
    await writer.wait_closed()


async def test_abandon_and_rebind_cancel_search_without_terminal_response(server):
    reader, writer = await connected(server)
    writer.write(search(2) + message(3, tlv(0x50, b"\x02")) +
                 search(4, base="", scope=0, filter=tlv(0x87, b"objectClass")))
    await writer.drain()
    assert response((await collect(reader, 4))[-1])[2] == 0
    writer.write(search(5) + bind(6))
    await writer.drain()
    assert response(await receive(reader)) == (6, 0x61, 0)
    writer.close()
    await writer.wait_closed()


async def test_search_limits_unknown_base_and_critical_controls(server):
    reader, writer = await connected(server)
    for identity, options, expected in [(2, {"size": 1}, 4), (3, {"base": "ou=missing," + BASE}, 32),
                                        (4, {"critical": True}, 12), (5, {"base": "not-a-dn"}, 34)]:
        writer.write(search(identity, **options))
        await writer.drain()
        assert response((await collect(reader, identity))[-1])[2] == expected
    writer.close()
    await writer.wait_closed()


async def test_ldap3_real_client_filter_offline_scope_and_types(server):
    def client():
        from ldap3 import BASE as BASE_SCOPE, Connection, NONE, SUBTREE, Server
        connection = Connection(Server("127.0.0.1", port=server.port, get_info=NONE), user="kitchen",
                                password="fixture-only-password", raise_exceptions=True, receive_timeout=2)
        try:
            assert connection.bind()
            assert connection.search(BASE, "(&(objectClass=organizationalRole)(|(cn=Kitchen*)(telephoneNumber=101)))",
                                     SUBTREE, attributes=["cn", "uid", "telephoneNumber", "labeledURI", "ou"])
            assert len(connection.entries) == 1
            entry = connection.entries[0]
            assert entry.cn.value == "Kitchen" and entry.uid.value == "esphome:kitchen"
            dn = entry.entry_dn
            assert connection.search(dn, "(objectClass=*)", BASE_SCOPE, attributes=["cn"], types_only=True)
            assert connection.entries[0].cn.values == []
            connection.search(BASE, "(!(unsupportedAttribute=value))", SUBTREE, attributes=["cn"])
            assert not connection.entries
        finally:
            connection.unbind()
    await asyncio.to_thread(client)


async def test_revoke_endpoint_cancels_pending_auth_without_storing_password(socket_enabled):
    entered, release = asyncio.Event(), asyncio.Event()
    async def authenticate(username, password):
        entered.set()
        await release.wait()
        return "esphome:kitchen"
    value = LdapDirectoryServer(directory(), authenticate)
    await value.async_start("127.0.0.1", 0)
    reader, writer = await asyncio.open_connection("127.0.0.1", value.port)
    try:
        writer.write(bind())
        await writer.drain()
        await asyncio.wait_for(entered.wait(), 1)
        await value.async_revoke_endpoint("esphome:kitchen", username="kitchen")
        assert await asyncio.wait_for(reader.read(), 1) == b""
    finally:
        writer.close()
        await writer.wait_closed()
        await value.async_close()


async def test_partial_frame_timeout_connection_limit_and_shutdown(socket_enabled):
    auth = AsyncMock(return_value="esphome:kitchen")
    value = LdapDirectoryServer(directory(), auth, limits=Limits(connections=1, frame_seconds=0.05))
    await value.async_start("127.0.0.1", 0)
    reader, writer = await connected(value)
    other_reader, other_writer = await asyncio.open_connection("127.0.0.1", value.port)
    assert await asyncio.wait_for(other_reader.read(), 1) == b""
    other_writer.close()
    await other_writer.wait_closed()
    writer.write(b"\x30")
    await writer.drain()
    assert await asyncio.wait_for(reader.read(), 1) == b""
    writer.close()
    await writer.wait_closed()
    reader, writer = await connected(value)
    await value.async_close()
    assert await asyncio.wait_for(reader.read(), 1) == b""
    writer.close()
    await writer.wait_closed()
    await value.async_close()
    assert value.port is None


async def test_anonymous_opt_in_dn_escaping_and_directory_updates(socket_enabled):
    value = LdapDirectoryServer(directory(), AsyncMock(return_value=None), allow_anonymous=True)
    value.directory.upsert(RosterEntry("contact:desk, east", name="East", extension="303"))
    await value.async_start("127.0.0.1", 0)
    reader, writer = await asyncio.open_connection("127.0.0.1", value.port)
    try:
        writer.write(search(1, base=r"uid=contact:desk\2c east,ou=contact," + BASE, scope=0))
        await writer.drain()
        assert len(await collect(reader, 1)) == 2
        value.directory.remove("contact:desk, east")
        writer.write(search(2, base=r"uid=contact:desk\, east,ou=contact," + BASE, scope=0))
        await writer.drain()
        assert response((await collect(reader, 2))[-1])[2] == 32
    finally:
        writer.close()
        await writer.wait_closed()
        await value.async_close()


async def test_openldap_ldapsearch_independent_client(server, tmp_path):
    executable = os.environ.get("LDAPSEARCH_BINARY") or shutil.which("ldapsearch")
    if not executable:
        pytest.skip("Set LDAPSEARCH_BINARY for the optional OpenLDAP wire oracle")
    password = tmp_path / "bind-password"
    password.write_text("fixture-only-password")
    password.chmod(0o600)
    process = await asyncio.create_subprocess_exec(executable, "-LLL", "-x", "-H", f"ldap://127.0.0.1:{server.port}",
        "-D", "uid=kitchen,ou=accounts," + BASE, "-y", str(password), "-b", BASE,
        "(|(cn=Kitchen*)(telephoneNumber=101))", "cn", "uid", "telephoneNumber", "labeledURI", "ou",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    output, errors = await asyncio.wait_for(process.communicate(), 5)
    assert process.returncode == 0, errors.decode()
    assert b"cn: Kitchen" in output and b"uid: esphome:kitchen" in output
    assert b"labeleduri: sip:kitchen@pbx.example.org" in output


@pytest.mark.parametrize("wire,operation,code", [
    (bind(version=2), 0x61, 2),
    (message(1, tlv(0x60, integer(3) + tlv(4, b"kitchen") + tlv(0xA3, tlv(4, b"PLAIN")))), 0x61, 7),
    (message(1, tlv(0x68, tlv(4, ("cn=new," + BASE).encode()) + tlv(0x30, b""))), 0x69, 53),
])
async def test_protocol_version_sasl_and_write_operations_are_explicit(server, wire, operation, code):
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write(wire)
    await writer.drain()
    assert response(await receive(reader)) == (1, operation, code)
    writer.close()
    await writer.wait_closed()


async def test_one_level_scope_exact_dn_and_reused_message_id(server):
    reader, writer = await connected(server)
    for base, expected in [(BASE, 0), ("ou=esphome," + BASE, 1)]:
        writer.write(search(2, base=base, scope=1))
        await writer.drain()
        frames = await collect(reader, 2)
        assert len(frames) == expected + 1
        assert response(frames[-1])[2] == 0
    writer.close()
    await writer.wait_closed()


async def test_close_is_idempotent_and_listener_can_restart(socket_enabled):
    value = LdapDirectoryServer(directory(), AsyncMock(return_value="esphome:kitchen"))
    await value.async_start("127.0.0.1", 0)
    reader, writer = await connected(value)
    await asyncio.gather(value.async_close(), value.async_close())
    assert await asyncio.wait_for(reader.read(), 1) == b""
    writer.close()
    await writer.wait_closed()
    await value.async_start("127.0.0.1", 0)
    reader, writer = await connected(value)
    writer.close()
    await writer.wait_closed()
    await value.async_close()


async def test_optional_tls_listener_uses_standard_ssl_and_same_auth(socket_enabled, tmp_path):
    from datetime import datetime, timezone
    import ipaddress
    import ssl
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    # Synthetic local test certificate only, never a project/runtime credential.
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(datetime(2020, 1, 1, tzinfo=timezone.utc))
        .not_valid_after(datetime(2040, 1, 1, tzinfo=timezone.utc))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256()))
    cert_file, key_file = tmp_path / "test-cert.pem", tmp_path / "test-key.pem"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_file.chmod(0o600)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_file, key_file)
    client_context = ssl.create_default_context(cafile=str(cert_file))
    value = LdapDirectoryServer(directory(), AsyncMock(return_value="esphome:kitchen"))
    await value.async_start("127.0.0.1", 0, ssl_context=server_context)
    reader, writer = await asyncio.open_connection("127.0.0.1", value.port, ssl=client_context, server_hostname="localhost")
    try:
        writer.write(bind())
        await writer.drain()
        assert response(await receive(reader)) == (1, 0x61, 0)
        await value.async_close()
        assert await asyncio.wait_for(reader.read(), 1) == b""
    finally:
        writer.close()
        await writer.wait_closed()
        await value.async_close()
