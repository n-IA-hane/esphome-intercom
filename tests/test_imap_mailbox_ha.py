"""Real IMAP clients against the canonical voicemail archive and UID ledger."""

import asyncio
import base64
from contextlib import suppress
import email
from email.policy import default
import imaplib
import re
from unittest.mock import AsyncMock

import pytest

from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.core.media_archive import ArchiveError, MediaArchive
from custom_components.voip_stack.imap_message import LoadedVoicemail, MimeCache
from custom_components.voip_stack.imap_server import ImapMailboxServer, _Message
from pymap.parsing.specials import FetchRequirement

pytestmark = pytest.mark.ha
OWNER = "esphome:kitchen"


@pytest.fixture
async def archive(hass, tmp_path):
    value = MediaArchive(tmp_path / "archive", hass.async_add_executor_job)
    await value.load()
    yield value
    await value.close()


async def deposit(archive, *, owner=OWNER, frames=18000, caller="Delivery caller"):
    writer = await archive.begin(owner, caller=caller)
    for start in range(0, frames, 8192):
        count = min(8192, frames - start)
        assert writer.offer(b"\x18\0" * count, AudioFormat(16000, frame_ms=20), offset=start / 16000)
    record = await writer.finish()
    assert record is not None
    return record


@pytest.fixture
async def server(archive, socket_enabled):
    async def authenticate(username, verifier):
        return OWNER if username == "kitchen" and verifier("fixture-only-password") else None
    value = ImapMailboxServer(AsyncMock(return_value=archive), authenticate)
    await value.async_start("127.0.0.1", 0)
    yield value
    await value.async_close()
    await asyncio.sleep(0)
    assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("voip-imap-")]


def literal(values):
    return b"".join(item[1] for item in values if isinstance(item, tuple))


async def lines_until(reader, tag):
    result = []
    while True:
        line = await asyncio.wait_for(reader.readline(), 3)
        assert line
        result.append(line)
        if line.startswith(tag + b" "):
            return result


async def raw_login(value, *, host="127.0.0.1"):
    reader, writer = await asyncio.open_connection(host, value.port)
    assert b"OK" in await reader.readline()
    writer.write(b"A1 LOGIN kitchen fixture-only-password\r\n")
    await writer.drain()
    assert (await lines_until(reader, b"A1"))[-1].startswith(b"A1 OK")
    return reader, writer


async def test_uid_ledger_restart_seen_and_new_incarnation(archive, hass):
    empty = await archive.imap_snapshot(OWNER)
    assert empty.uid_validity > 0 and empty.next_uid == 1 and not empty.messages
    first = await deposit(archive)
    state = await archive.imap_snapshot(OWNER)
    uid, record = state.messages[0]
    assert uid == 1 and record["id"] == first["id"] and state.next_uid == 2
    await archive.set_imap_flags(first["id"], OWNER, seen=True, flags={"\\Flagged"})
    state = await archive.imap_snapshot(OWNER)
    assert state.messages[0][1]["read"] is True
    assert "\\Seen" not in state.messages[0][1]["imap_flags"]
    reloaded = MediaArchive(archive.root, hass.async_add_executor_job)
    await reloaded.load()
    preserved = await reloaded.imap_snapshot(OWNER)
    assert preserved.uid_validity == state.uid_validity
    assert preserved.messages[0][0] == uid
    assert preserved.messages[0][1]["imap_flags"] == ["\\Flagged"]
    await reloaded.delete(first["id"], OWNER)
    second = await deposit(reloaded)
    later = await reloaded.imap_snapshot(OWNER)
    assert later.messages[0][0] == 2 and later.messages[0][1]["id"] == second["id"]
    await reloaded.imap_forget(OWNER)
    raw = (archive.root / ".protocol" / "imap-index.json").read_text()
    assert OWNER not in raw
    reincarnated = await reloaded.imap_snapshot(OWNER)
    assert reincarnated.uid_validity > preserved.uid_validity
    await reloaded.close()


async def test_corrupt_uid_ledger_fails_only_imap_metadata(archive):
    record = await deposit(archive)
    (archive.root / ".protocol").mkdir()
    (archive.root / ".protocol" / "imap-index.json").write_text('{"version":1,"next_validity":0,"mailboxes":{}}')
    with pytest.raises(ArchiveError):
        await archive.imap_snapshot(OWNER)
    assert archive.message(record["id"], OWNER)["read"] is False
    assert archive.segment_path(record["id"], OWNER).exists()


async def test_mime_immutable_singleflight_size_duration_and_partial_bytes(archive):
    record = await deposit(archive)
    cache = MimeCache()
    first, second = await asyncio.gather(cache.get(archive, OWNER, record), cache.get(archive, OWNER, record))
    assert first is second and cache.build_count == 1
    message = _Message(type("Mailbox", (), {"archive": archive, "endpoint_id": OWNER, "server": type("Server", (), {"content": cache})()})(), 1, record)
    loaded = LoadedVoicemail(message, FetchRequirement.CONTENT, first.layout, first)
    wire = bytes(loaded.get_body())
    assert len(wire) == first.layout.size
    parsed = email.message_from_bytes(wire, policy=default)
    assert parsed["Content-Duration"] == "2"
    assert parsed.get_payload(1)["Content-Duration"] == "2"
    raw = archive.segment_path(record["id"], OWNER).read_bytes()
    assert parsed.get_payload(1).get_payload(decode=True) == raw
    body = bytes(loaded.get_body((2,)))
    assert body == base64.encodebytes(raw).replace(b"\n", b"\r\n")
    assert bytes(loaded.get_body((2,))) is body
    assert str(len(body)).encode() in bytes(loaded.get_body_structure())
    assert first.layout.encoded_bytes == len(body)
    assert cache.build_count == 1
    assert not list(archive.root.glob("*.eml"))


async def test_imaplib_login_list_uid_fetch_and_partial_wav(server, archive):
    record = await deposit(archive)
    raw = archive.segment_path(record["id"], OWNER).read_bytes()
    def client():
        connection = imaplib.IMAP4("127.0.0.1", server.port, timeout=3)
        try:
            assert connection.login("kitchen", "fixture-only-password")[0] == "OK"
            assert connection.list()[0] == "OK"
            assert connection.select("INBOX")[1] == [b"1"]
            _, values = connection.uid("search", None, "ALL")
            uid = values[0].split()[0]
            status, metadata = connection.uid("fetch", uid, "(UID FLAGS BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (CONTENT-DURATION SUBJECT)])")
            assert status == "OK" and b"Content-Duration: 2" in literal(metadata)
            metadata_bytes = b"".join(item[0] if isinstance(item, tuple) else item for item in metadata if item)
            size = re.search(br'"AUDIO" "WAV" .*?"BASE64" (\d+)', metadata_bytes, re.IGNORECASE)
            assert size, metadata_bytes
            encoded_size = int(size[1])
            chunks, offset = [], 0
            while True:
                status, values = connection.uid("fetch", uid, f"(BODY.PEEK[2]<{offset}.4096>)")
                assert status == "OK"
                part = literal(values)
                if not part:
                    break
                chunks.append(part)
                offset += len(part)
            encoded = b"".join(chunks)
            assert len(encoded) == encoded_size
            assert base64.b64decode(encoded) == raw
            status, values = connection.uid("fetch", uid, "(RFC822.SIZE BODY.PEEK[])")
            complete = literal(values)
            declared = re.search(br'RFC822.SIZE (\d+)', b"".join(item[0] if isinstance(item, tuple) else item for item in values if item))
            assert status == "OK" and int(declared[1]) == len(complete)
            assert email.message_from_bytes(complete, policy=default).get_payload(1).get_payload(decode=True) == raw
        finally:
            with suppress(OSError, imaplib.IMAP4.error):
                connection.logout()
    await asyncio.to_thread(client)
    assert server.content.build_count == 1
    assert archive.counts(OWNER) == (1, 0)


async def test_imaplib_seen_flags_and_expunge_share_archive(server, archive):
    record = await deposit(archive)
    def client():
        connection = imaplib.IMAP4("127.0.0.1", server.port, timeout=3)
        try:
            connection.login("kitchen", "fixture-only-password")
            connection.select("INBOX")
            uid = connection.uid("search", None, "ALL")[1][0].split()[0]
            assert connection.uid("fetch", uid, "(BODY[2]<0.8>)")[0] == "OK"
            assert archive.counts(OWNER) == (0, 1)
            assert connection.uid("store", uid, "-FLAGS", r"(\Seen)")[0] == "OK"
            assert archive.counts(OWNER) == (1, 0)
            assert connection.uid("store", uid, "+FLAGS", r"(\Deleted)")[0] == "OK"
            assert connection.expunge()[0] == "OK"
        finally:
            with suppress(OSError, imaplib.IMAP4.error):
                connection.logout()
    await asyncio.to_thread(client)
    assert archive.counts(OWNER) == (0, 0)
    assert not list(archive.root.glob("*.wav"))
    with pytest.raises(ArchiveError):
        archive.message(record["id"], OWNER)


async def test_plain_auth_uses_real_nonloopback_peer_policy(archive, socket_enabled):
    import ipaddress
    import socket
    import pytest_socket
    from proxyprotocol.sock import SocketInfo

    # The HA plugin applies its own localhost restriction after pytest markers.
    pytest_socket.socket_allow_hosts(["127.0.0.1", "192.0.2.1"])
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))  # Kernel route selection only, no packet sent.
        address = probe.getsockname()[0]
    pytest_socket.socket_allow_hosts(["127.0.0.1", address])
    if ipaddress.ip_address(address).is_loopback:
        pytest.skip("A non-loopback interface is required for the LAN policy oracle")
    async def authenticate(username, verify):
        return OWNER if username == "kitchen" and verify("fixture-only-password") else None
    value = ImapMailboxServer(AsyncMock(return_value=archive), authenticate)
    await value.async_start(address, 0)
    reader, writer = await asyncio.open_connection(address, value.port)
    try:
        assert SocketInfo.get(writer, None).from_localhost is False
        assert writer.get_extra_info("ssl_object") is None
        assert b"AUTH=PLAIN" in await reader.readline()
        writer.write(b"A1 LOGIN kitchen wrong-password\r\n")
        await writer.drain()
        assert (await lines_until(reader, b"A1"))[-1].startswith(b"A1 NO")
        writer.write(b"A2 LOGIN kitchen fixture-only-password\r\n")
        await writer.drain()
        assert (await lines_until(reader, b"A2"))[-1].startswith(b"A2 OK")
    finally:
        writer.close()
        await writer.wait_closed()
        await value.async_close()
        pytest_socket.socket_allow_hosts(["127.0.0.1"])


async def test_idle_changes_and_close_release_library_tasks(server, archive):
    await deposit(archive)
    reader, writer = await raw_login(server)
    writer.write(b"A2 SELECT INBOX\r\n")
    await writer.drain()
    assert (await lines_until(reader, b"A2"))[-1].startswith(b"A2 OK")
    writer.write(b"A3 IDLE\r\n")
    await writer.drain()
    assert (await reader.readline()).startswith(b"+")
    await deposit(archive, caller="Second caller")
    server.notify_changed(OWNER)
    updates = []
    while not any(b"2 EXISTS" in line for line in updates):
        updates.append(await asyncio.wait_for(reader.readline(), 2))
    await asyncio.wait_for(server.async_close(), 2)
    # Already queued RECENT/FETCH notifications precede the normal socket EOF.
    await asyncio.wait_for(reader.read(), 1)
    assert reader.at_eof()
    assert not [task for task in asyncio.all_tasks()
                if "pymap/imap" in getattr(getattr(task.get_coro(), "cr_code", None), "co_filename", "")]
    writer.close()
    await writer.wait_closed()


async def test_revoke_live_login_scope(server, archive):
    await deposit(archive)
    reader, writer = await raw_login(server)
    await server.async_revoke_endpoint(OWNER, username="kitchen")
    assert await asyncio.wait_for(reader.read(), 1) == b""
    writer.close()
    await writer.wait_closed()
    assert OWNER not in server._mailboxes
    assert all(key[0] != OWNER for key in server.content._active)


async def test_header_only_fetch_never_reads_wav_or_builds_mime(server, archive):
    await deposit(archive)
    def client():
        connection = imaplib.IMAP4("127.0.0.1", server.port, timeout=3)
        try:
            connection.login("kitchen", "fixture-only-password")
            connection.select("INBOX")
            for _ in range(3):
                status, data = connection.uid("fetch", "1:*", "(UID FLAGS INTERNALDATE BODY.PEEK[HEADER.FIELDS (CONTENT-DURATION SUBJECT FROM)])")
                assert status == "OK" and b"Content-Duration: 2" in literal(data)
        finally:
            connection.logout()
    await asyncio.to_thread(client)
    assert server.content.build_count == 0


async def test_scope_rejects_other_mailboxes_and_unsupported_mutations(server, archive):
    await deposit(archive)
    def client():
        connection = imaplib.IMAP4("127.0.0.1", server.port, timeout=3)
        try:
            with pytest.raises(imaplib.IMAP4.error):
                connection.login("kitchen", "wrong-password")
            connection.login("kitchen", "fixture-only-password")
            assert connection.select("another-phone")[0] == "NO"
            assert connection.create("Other")[0] == "NO"
            assert connection.select("INBOX")[0] == "OK"
            with pytest.raises(imaplib.IMAP4.error, match="APPEND"):
                connection.append("INBOX", None, None, b"Subject: injected\r\n\r\nnot voicemail")
        finally:
            connection.logout()
    await asyncio.to_thread(client)
    assert len(archive.list(OWNER)) == 1


async def test_protocol_debug_logging_never_exposes_login_credentials(server, caplog):
    import logging
    caplog.set_level(logging.DEBUG, logger="pymap.imap")
    reader, writer = await raw_login(server)
    writer.write(b"A2 LOGOUT\r\n")
    await writer.drain()
    await lines_until(reader, b"A2")
    writer.close()
    await writer.wait_closed()
    assert "fixture-only-password" not in caplog.text
    logging.getLogger("pymap.imap").debug("Unrelated IMAP diagnostics remain visible")
    assert "Unrelated IMAP diagnostics remain visible" in caplog.text


async def test_revoke_pending_auth_prevents_late_mailbox_creation(archive, socket_enabled):
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def authenticate(username, verify):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    value = ImapMailboxServer(AsyncMock(return_value=archive), authenticate)
    await value.async_start("127.0.0.1", 0)
    reader, writer = await asyncio.open_connection("127.0.0.1", value.port)
    try:
        await reader.readline()
        writer.write(b"A1 LOGIN kitchen fixture-only-password\r\n")
        await writer.drain()
        await asyncio.wait_for(entered.wait(), 1)
        await value.async_revoke_endpoint(OWNER, username="kitchen")
        await asyncio.wait_for(cancelled.wait(), 1)
        await asyncio.wait_for(reader.read(), 1)
        assert reader.at_eof() and not value._mailboxes
        assert not (archive.root / ".protocol" / "imap-index.json").exists()
    finally:
        writer.close()
        await writer.wait_closed()
        await value.async_close()


async def test_provider_failure_is_safe_and_listener_remains_usable(archive, socket_enabled, caplog):
    calls = 0
    async def authenticate(username, verify):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("fixture-provider-secret")
        return OWNER if verify("fixture-only-password") else None
    value = ImapMailboxServer(AsyncMock(return_value=archive), authenticate)
    await value.async_start("127.0.0.1", 0)
    reader, writer = await asyncio.open_connection("127.0.0.1", value.port)
    try:
        await reader.readline()
        writer.write(b"A1 LOGIN kitchen fixture-only-password\r\n")
        await writer.drain()
        lines = await lines_until(reader, b"A1")
        assert lines[-1].startswith(b"A1 NO") and b"fixture-provider-secret" not in b"".join(lines)
        writer.write(b"A2 LOGIN kitchen fixture-only-password\r\n")
        await writer.drain()
        assert (await lines_until(reader, b"A2"))[-1].startswith(b"A2 OK")
        assert "fixture-provider-secret" not in caplog.text
    finally:
        writer.close()
        await writer.wait_closed()
        await value.async_close()


async def test_purge_clears_metadata_only_layouts_and_content_leases(server, archive):
    record = await deposit(archive)
    server.archive = archive  # Normally assigned when the authenticated session opens.
    server.content.layout(record)
    value = await server.content.get(archive, OWNER, record)
    await archive.delete(record["id"], OWNER)
    server.notify_changed(OWNER)
    assert not server.content._layouts and not server.content._lru
    await server.async_revoke_endpoint(OWNER, username="kitchen")
    assert not server.content._active and not server.content._epochs
    assert value.key == (OWNER, record["id"])  # Caller ownership does not reauthorize it.


async def test_uid_commit_finishes_before_cancellation_propagates(archive):
    from custom_components.voip_stack.core.imap_index import ImapIndex
    entered, release = asyncio.Event(), asyncio.Event()
    async def run_io(function):
        entered.set()
        await release.wait()
        return await archive.run_io(function)
    index = ImapIndex(archive.root, run_io)
    # Preload only empty state; hold the actual transaction in its executor seam.
    index.state = {"version": 1, "next_validity": 123, "mailboxes": {}}
    operation = asyncio.create_task(index.snapshot(OWNER, []))
    await entered.wait()
    operation.cancel()
    await asyncio.sleep(0)
    assert not operation.done()
    operation.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert index.state["mailboxes"][OWNER]["uid_validity"] == 123
    state = await archive.imap_snapshot(OWNER)
    assert state.uid_validity == 123


async def test_imaplib_tls_uses_same_canonical_account(archive, socket_enabled, tmp_path):
    from datetime import datetime, timezone
    import ipaddress
    import ssl
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

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
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    client_context = ssl.create_default_context(cafile=str(cert_file))
    async def authenticate(username, verify):
        return OWNER if username == "kitchen" and verify("fixture-only-password") else None
    value = ImapMailboxServer(AsyncMock(return_value=archive), authenticate)
    await value.async_start("127.0.0.1", 0, ssl_context=context)
    def client():
        with imaplib.IMAP4_SSL("127.0.0.1", value.port, ssl_context=client_context, timeout=3) as connection:
            assert connection.login("kitchen", "fixture-only-password")[0] == "OK"
            assert connection.select("INBOX")[0] == "OK"
    try:
        await asyncio.to_thread(client)
    finally:
        await value.async_close()
