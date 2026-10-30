"""Production ESP LDAP client against the real HA directory adapter."""

import asyncio
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from custom_components.voip_stack.ldap_server import LdapDirectoryServer
from custom_components.voip_stack.core.sip import SipUri
from custom_components.voip_stack.roster import RosterEntry
from tests.test_ldap_directory_ha import BASE, directory

pytestmark = pytest.mark.ha
ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get("VOIP_STACK_REPO", ROOT.parent / "esphome-voip-stack"))
PASSWORD = "fixture-only-password"
OWNER = "esphome:kitchen"


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
               str(SOURCE / "esphome/components/phone_client/net_stream.cpp"), "-o", str(binary)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    return binary


@pytest.fixture
async def phone_server(socket_enabled):
    async def ldap_auth(username, password):
        return OWNER if username == "kitchen" and password == PASSWORD.encode() else None

    book = directory()
    # Use the canonical SIP serializer, as directory_runtime.directory_uri
    # does, while the receiving C++ mapper remains an independent oracle.
    for target in ("Living room", "Night group"):
        book.upsert(RosterEntry(target, name=target, sip_uri=str(SipUri(user=target, host="pbx.example.org")),
                               metadata={"endpoint_kind": "group"}))
    ldap = LdapDirectoryServer(book, ldap_auth)
    await ldap.async_start("127.0.0.1", 0)
    try:
        yield ldap
    finally:
        await ldap.async_close()


async def start_client(binary, port, *, password=PASSWORD, maximum=64):
    process = await asyncio.create_subprocess_exec(
        str(binary), "directory", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "ASAN_OPTIONS": "detect_leaks=1:abort_on_error=1"})
    fields = ("127.0.0.1", str(port), f"uid=kitchen,ou=accounts,{BASE}", password, BASE, str(maximum))
    process.stdin.write(("\n".join(fields) + "\n").encode())
    await process.stdin.drain()
    process.stdin.close()
    return process


async def run_client(binary, port, **kwargs):
    process = await start_client(binary, port, **kwargs)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, f"Client exit {process.returncode}: {stdout!r} {stderr!r}"
    assert not stderr
    return stdout.decode()


async def test_native_ldap_canonical_ids_uris_and_categories(native_phone_clients, phone_server):
    output = await run_client(native_phone_clients, phone_server.port)
    rows = {fields[0]: fields[1:] for fields in (line.split("|") for line in output.splitlines()[1:])}
    assert output.startswith("DIRECTORY 6\n")
    assert rows["esphome:kitchen"] == ["Kitchen", "sip:kitchen@pbx.example.org", "esphome"]
    assert rows["browser:test"] == ["Test", "sip:test@pbx.example.org", "browser"]
    assert rows["reception"] == ["Réception", "sip:reception@pbx.example.org", "contact"]
    assert rows["open_door"][2] == "automation"
    assert rows["Living room"] == ["Living room", "sip:Living%20room@pbx.example.org", "group"]
    assert rows["Night group"] == ["Night group", "sip:Night%20group@pbx.example.org", "group"]
    assert "must-never-be-published" not in output


async def test_native_ldap_atomic_limit_failure_and_authentication(native_phone_clients, phone_server):
    truncated = await run_client(native_phone_clients, phone_server.port, maximum=1)
    assert truncated == "SEARCH_FAILED 4\n"
    assert "must-never-be-published" not in truncated
    denied = await run_client(native_phone_clients, phone_server.port, password="incorrect")
    assert denied == "AUTH_FAILED 49\n"
