"""Production ESP protocol clients against the real HA LDAP and IMAP adapters."""

import asyncio
import os
from pathlib import Path
import shutil
import subprocess
from unittest.mock import AsyncMock

import pytest

from custom_components.voip_stack.imap_server import ImapMailboxServer
from custom_components.voip_stack.ldap_server import LdapDirectoryServer
from custom_components.voip_stack.core.sip import SipUri
from custom_components.voip_stack.roster import RosterEntry
from tests import test_imap_mailbox_ha as mailbox_fixtures
from tests.test_ldap_directory_ha import BASE, directory

pytestmark = pytest.mark.ha
ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get("VOIP_STACK_REPO", ROOT.parent / "esphome-voip-stack"))
PASSWORD = "fixture-only-password"
OWNER = mailbox_fixtures.OWNER
archive = mailbox_fixtures.archive
deposit = mailbox_fixtures.deposit


@pytest.fixture(scope="module")
def native_phone_clients(tmp_path_factory):
    if not (SOURCE / "tests/phone_clients_ha_interop.cpp").is_file():
        if os.environ.get("VOIP_REQUIRE_PHONE_CLIENTS") == "1":
            pytest.fail("The required coordinated ESP VoIP source checkout is missing")
        pytest.skip("The coordinated ESP VoIP source checkout is required")
    compiler = shutil.which("g++")
    if compiler is None:
        if os.environ.get("VOIP_REQUIRE_PHONE_CLIENTS") == "1":
            pytest.fail("g++ is required for the production ESP protocol client")
        pytest.skip("g++ is required for the production ESP protocol client")
    binary = tmp_path_factory.mktemp("phone-native") / "clients"
    command = [compiler, "-std=c++17", "-O1", "-g", "-Wall", "-Wextra", "-Werror",
               "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-no-pie", "-I", str(SOURCE),
               str(SOURCE / "tests/phone_clients_ha_interop.cpp"),
               str(SOURCE / "esphome/components/ldap_directory/ldap_protocol.cpp"),
               str(SOURCE / "esphome/components/ldap_directory/directory_contact.cpp"),
               str(SOURCE / "esphome/components/imap_voicemail/imap_protocol.cpp"),
               str(SOURCE / "esphome/components/phone_client/net_stream.cpp"), "-o", str(binary)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    return binary


@pytest.fixture
async def phone_servers(archive, socket_enabled):
    async def ldap_auth(username, password):
        return OWNER if username == "kitchen" and password == PASSWORD.encode() else None

    async def imap_auth(username, verifier):
        return OWNER if username == "kitchen" and verifier(PASSWORD) else None

    book = directory()
    # Use the canonical SIP serializer, as directory_runtime.directory_uri
    # does, while the receiving C++ mapper remains an independent oracle.
    for target in ("Living room", "Night group"):
        book.upsert(RosterEntry(target, name=target, sip_uri=str(SipUri(user=target, host="pbx.example.org")),
                               metadata={"endpoint_kind": "group"}))
    ldap = LdapDirectoryServer(book, ldap_auth)
    imap = ImapMailboxServer(AsyncMock(return_value=archive), imap_auth)
    await ldap.async_start("127.0.0.1", 0)
    await imap.async_start("127.0.0.1", 0)
    try:
        yield ldap, imap
    finally:
        await ldap.async_close()
        await imap.async_close()


async def start_client(binary, mode, port, output, *, password=PASSWORD, maximum=64):
    process = await asyncio.create_subprocess_exec(
        str(binary), mode, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "ASAN_OPTIONS": "detect_leaks=1:abort_on_error=1"})
    fields = ("127.0.0.1", str(port), "kitchen", password, BASE,
              f"uid=kitchen,ou=accounts,{BASE}", "INBOX", str(output), str(maximum))
    process.stdin.write(("\n".join(fields) + "\n").encode())
    await process.stdin.drain()
    process.stdin.close()
    return process


async def run_client(binary, mode, port, output, **kwargs):
    process = await start_client(binary, mode, port, output, **kwargs)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, f"Client exit {process.returncode}: {stdout!r} {stderr!r}"
    assert not stderr
    return stdout.decode()


async def test_native_ldap_canonical_ids_uris_categories_and_atomic_failure(native_phone_clients, phone_servers, tmp_path):
    ldap, _ = phone_servers
    output = await run_client(native_phone_clients, "directory", ldap.port, tmp_path / "unused")
    rows = {fields[0]: fields[1:] for fields in (line.split("|") for line in output.splitlines()[1:])}
    assert output.startswith("DIRECTORY 6\n")
    assert rows["esphome:kitchen"] == ["Kitchen", "sip:kitchen@pbx.example.org", "esphome"]
    assert rows["browser:test"] == ["Test", "sip:test@pbx.example.org", "browser"]
    assert rows["reception"] == ["Réception", "sip:reception@pbx.example.org", "contact"]
    assert rows["open_door"][2] == "automation"
    assert rows["Living room"] == ["Living room", "sip:Living%20room@pbx.example.org", "group"]
    assert rows["Night group"] == ["Night group", "sip:Night%20group@pbx.example.org", "group"]
    assert "must-never-be-published" not in output
    truncated = await run_client(native_phone_clients, "directory", ldap.port, tmp_path / "unused", maximum=1)
    assert truncated == "SEARCH_FAILED 4\n"
    denied = await run_client(native_phone_clients, "directory", ldap.port, tmp_path / "unused", password="incorrect")
    assert denied == "AUTH_FAILED 49\n"


async def test_native_imap_catalogue_duration_peek_exact_wav_and_owner_isolation(native_phone_clients, phone_servers, archive, tmp_path):
    _, imap = phone_servers
    record = await deposit(archive)
    await deposit(archive, owner="esphome:other", caller="Private other owner")
    output = await run_client(native_phone_clients, "catalogue", imap.port, tmp_path / "unused")
    lines = output.splitlines()
    assert lines[0].split()[2:] == ["1", "1"]
    values = lines[1].split("|")
    assert values[1] == "0" and "Delivery caller" in values[2]
    assert values[4:8] == ["1", "2", "2", "audio/wav"]
    assert "Private other owner" not in output
    wav = tmp_path / "voicemail.wav"
    assert (await run_client(native_phone_clients, "fetch", imap.port, wav)).startswith("FETCHED ")
    assert wav.read_bytes() == archive.segment_path(record["id"], OWNER).read_bytes()
    assert archive.counts(OWNER) == (1, 0)
    assert archive.counts("esphome:other") == (1, 0)


async def test_native_imap_seen_unseen_uid_delete_and_empty_snapshot(native_phone_clients, phone_servers, archive, tmp_path):
    _, imap = phone_servers
    await deposit(archive)
    await deposit(archive, owner="esphome:other")
    for mode, counts in (("seen", (0, 1)), ("unseen", (1, 0)), ("delete", (0, 0))):
        assert (await run_client(native_phone_clients, mode, imap.port, tmp_path / "unused")).startswith("CHANGED ")
        assert archive.counts(OWNER) == counts
        assert archive.counts("esphome:other") == (1, 0)
    output = await run_client(native_phone_clients, "catalogue", imap.port, tmp_path / "unused")
    assert len(output.splitlines()) == 1
    assert output.split()[2:] == ["0", "0"]
    denied = await run_client(native_phone_clients, "catalogue", imap.port, tmp_path / "unused", password="incorrect")
    assert denied == "AUTH_FAILED\n"


async def test_native_imap_idle_observes_canonical_new_message(native_phone_clients, phone_servers, archive, tmp_path):
    _, imap = phone_servers
    process = await start_client(native_phone_clients, "idle", imap.port, tmp_path / "unused")
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 5) == b"IDLE_READY\n"
        await deposit(archive)
        imap.notify_changed(OWNER)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, (stdout, stderr)
    assert stdout == b"UPDATED 1\n" and not stderr
