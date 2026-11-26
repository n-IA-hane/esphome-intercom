"""Two independent HA PBXs exchange calls through their production SIP owners."""

import asyncio
from contextlib import AsyncExitStack
import hashlib
import re
import socket
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant.common import MockConfigEntry, async_test_home_assistant

from custom_components.voip_stack.core import rtp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol

from .test_automation_originate_ha import Phone


pytestmark = pytest.mark.ha
PASSWORD = "isolated-pbx-qualification"


def free_port():
    """Reserve a candidate usable by both signaling transports on loopback."""
    for _ in range(128):
        with socket.socket() as tcp, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            tcp.bind(("127.0.0.1", 0))
            port = tcp.getsockname()[1]
            try:
                udp.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("No free UDP/TCP laboratory port")


async def register_phone(pbx):
    """An independent phone constructs its Digest response without the SIP client."""
    responses = asyncio.Queue()
    previous = pbx.phone.datagram_received
    call_id = "register-" + pbx.username

    def receive(raw, address):
        message = sip.parse_message(raw)
        if message.is_response and message.header("Call-ID") == call_id:
            responses.put_nowait(message)
        elif message.method == "OPTIONS":
            pbx.phone.respond(message, address, 200)
        else:
            previous(raw, address)

    pbx.phone.datagram_received = receive
    phone_port = pbx.phone.transport.get_extra_info("sockname")[1]
    uri = f"sip:127.0.0.1:{pbx.port}"
    identity = f"<sip:{pbx.username}@127.0.0.1>"

    async def exchange(cseq, authorization=""):
        headers = [
            ("Via", f"SIP/2.0/UDP 127.0.0.1:{phone_port};branch=z9hG4bK-{call_id}-{cseq};rport"),
            ("From", identity + ";tag=phone"), ("To", identity),
            ("Call-ID", call_id), ("CSeq", f"{cseq} REGISTER"),
            ("Contact", f"<sip:{pbx.username}@127.0.0.1:{phone_port}>"), ("Expires", "120"),
        ]
        if authorization:
            headers.append(("Authorization", authorization))
        pbx.phone.transport.sendto(sip.build_request("REGISTER", uri, headers), ("127.0.0.1", pbx.port))
        return await asyncio.wait_for(responses.get(), 3)

    response = await exchange(1)
    assert response.status_code == 401
    challenge = dict(re.findall(r'(\w+)="([^"]*)"', response.header("WWW-Authenticate")))
    md5 = lambda value: hashlib.md5(value.encode()).hexdigest()
    digest = md5(f'{md5(pbx.username + ":" + challenge["realm"] + ":" + PASSWORD)}:'
                 f'{challenge["nonce"]}:00000001:phone:auth:{md5("REGISTER:" + uri)}')
    authorization = (f'Digest username="{pbx.username}", realm="{challenge["realm"]}", '
                     f'nonce="{challenge["nonce"]}", uri="{uri}", response="{digest}", '
                     'algorithm=MD5, qop=auth, nc=00000001, cnonce="phone"')
    assert (await exchange(2, authorization)).status_code == 200
    assert pbx.calls.component("registrar").registered_contacts(pbx.username)


async def make_pbx(hass, *, username, inbound_account, port, remote_port, transport, register=False):
    from custom_components.voip_stack import endpoint_runtime
    from custom_components.voip_stack.config import normalized_trunk_config
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
    from custom_components.voip_stack.phone_config import sync_registry_from_entry
    from custom_components.voip_stack.phone_control import PhoneAdapterRegistry
    from custom_components.voip_stack.phone_directory import PhoneDirectory
    from custom_components.voip_stack.runtime_data import VoipStackRuntime
    from custom_components.voip_stack.trunk_config import entry_trunk_configs

    entry = MockConfigEntry(domain="voip_stack", data={
        "initial_phone_created": True, "sip_registrar_enabled": True, "default_trunk_id": "remote",
    })
    entry.add_to_hass(hass)
    second_username = str(int(username) + 1)
    for name in (username, second_username, inbound_account):
        hass.config_entries.async_add_subentry(entry, ConfigSubentry(
            subentry_type="phone", title=name, unique_id="phone:" + name,
            data={"kind": "sip_account", "name": name, "username": name,
                  "password": PASSWORD, "username_source": "user"},
        ))
    remote_account = "from_" + username
    trunk = normalized_trunk_config({
        "trunk_enabled": True, "trunk_register": register, "trunk_server": "127.0.0.1",
        "trunk_port": remote_port, "trunk_transport": transport.lower(),
        "trunk_username": remote_account, "trunk_auth_username": remote_account,
        "trunk_password": PASSWORD, "trunk_caller_id_policy": "account",
        "trunk_inbound_account": inbound_account, "trunk_inbound_unknown_target": "reject",
        "trunk_audio_codecs": ["PCMA"],
    })
    hass.config_entries.async_add_subentry(entry, ConfigSubentry(
        subentry_type="trunk", title="Remote PBX", unique_id="trunk:remote",
        data={**trunk, "trunk_id": "remote"},
    ))
    endpoints = EndpointRegistry()
    calls = SipEndpointRuntime(allow_dark_sessions=True)
    runtime = VoipStackRuntime(
        transport_config={"sip_port": port, "rtp_port": free_port(), "advertise_host": "127.0.0.1",
                          "sip_registrar_enabled": True},
        assist_config={}, trunk_config=trunk, trunk_configs=entry_trunk_configs(entry),
        default_trunk_id="remote", endpoints=endpoints, phones=PhoneAdapterRegistry(hass, endpoints),
        sip=calls, directory=PhoneDirectory([]),
    )
    entry.runtime_data = runtime
    for domain, unique in (("sensor", "voip_stack_phonebook"), ("event", "voip_stack_call_event")):
        er.async_get(hass).async_get_or_create(domain, "voip_stack", unique, config_entry=entry)
    sync_registry_from_entry(hass, entry)
    resources, phones = [], []

    async def close():
        from custom_components.voip_stack.endpoint_session import TerminationIntent
        for session in list(calls.sessions.values()):
            await calls.request_termination(session.call_id, TerminationIntent("test_done"))
        await endpoint_runtime.async_stop_sip_endpoint(hass)
        for audio, phone_transport, collector, phone in resources:
            phone_transport.close()
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)
            audio.close()
            assert not phone.invalid_datagrams

    try:
        assert await endpoint_runtime.async_start_sip_endpoint(hass)
        for name in (username, second_username):
            audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            audio.bind(("127.0.0.1", 0))
            audio.setblocking(False)
            phone_transport, phone = await hass.loop.create_datagram_endpoint(
                lambda: Phone(audio), local_addr=("127.0.0.1", 0),
            )

            async def collect(receiver=phone):
                while True:
                    raw = await hass.loop.sock_recv(receiver.rtp_socket, 4096)
                    await receiver.audio.put(raw)

            collector = asyncio.create_task(collect())
            resources.append((audio, phone_transport, collector, phone))
            endpoint = SimpleNamespace(hass=hass, entry=entry, runtime=runtime, calls=calls,
                phone=phone, username=name, inbound_account=inbound_account, port=port)
            await register_phone(endpoint)
            phones.append(endpoint)
    except BaseException:
        await close()
        raise

    pbx = phones[0]
    pbx.extra = phones[1]
    pbx.close = close
    return pbx


async def attach_range_route(pbx, first, last):
    """The existing native trigger chooses a named trunk without a default line."""
    from homeassistant.helpers.trigger import TriggerConfig
    from custom_components import voip_stack
    from custom_components.voip_stack import services
    from custom_components.voip_stack.trigger import VoipCallTrigger

    pbx.runtime.default_trunk_id = ""
    pbx.hass.config_entries.async_update_entry(pbx.entry, data={**pbx.entry.data, "default_trunk_id": ""})
    await services.async_register_services(pbx.hass, {"route": voip_stack._handle_sip_route_service})
    pbx.routed = []
    pbx.extra.routed = pbx.routed

    async def choose(payload, context):
        destination = payload["call"]["dialed_target"]
        pbx.routed.append(destination)
        await pbx.hass.services.async_call("voip_stack", "route", {
            "action": "forward", "destination": destination, "trunk_id": "remote",
        }, blocking=True, context=context)

    def run(payload, _description, context):
        return pbx.hass.async_create_task(choose(payload, context))

    native = VoipCallTrigger(pbx.hass, TriggerConfig("voip_stack.route_requested", options={
        "ingress": "extension", "destination": first, "destination_match": "range",
        "destination_range_end": last,
    }))
    return await native.async_attach_runner(run)


@pytest.fixture
async def two_pbx(hass, tmp_path, monkeypatch, socket_enabled, request):
    from custom_components.voip_stack import peer_snapshot
    from custom_components.voip_stack.sip_ingress import SipIngressPolicy
    from custom_components.voip_stack.trunk_runtime import async_start_sip_trunk

    monkeypatch.setattr(peer_snapshot, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", AsyncMock(return_value=[]))
    received = []
    original = SipIngressPolicy.authorize_initial_invite

    def record(policy, message, invite, addr, transport):
        result = original(policy, message, invite, addr, transport)
        received.append((policy.hass, message, result, transport))
        return result

    monkeypatch.setattr(SipIngressPolicy, "authorize_initial_invite", record)
    settings = getattr(request, "param", "UDP")
    if isinstance(settings, str):
        settings = {"transport": settings}
    transport = settings["transport"]
    async with AsyncExitStack() as cleanup:
        second = await cleanup.enter_async_context(async_test_home_assistant(config_dir=str(tmp_path / "second")))
        cleanup.push_async_callback(second.async_stop, force=True)
        port_a, port_b = free_port(), free_port()
        middle = None
        if settings.get("asterisk"):
            from .asterisk_peer_fixture import asterisk_middle
            middle = await cleanup.enter_async_context(asterisk_middle(tmp_path / "asterisk",
                port=free_port(), port_a=port_a, port_b=port_b, transport=transport,
                password=PASSWORD, force_caller=settings.get("force_caller", False)))
        first_pbx = await make_pbx(hass, username="101", inbound_account="from_202",
            port=port_a, remote_port=middle.port if middle else port_b, transport=transport,
            register=settings.get("register", False))
        cleanup.push_async_callback(first_pbx.close)
        second_pbx = await make_pbx(second, username="202", inbound_account="from_101",
            port=port_b, remote_port=middle.port if middle else port_a, transport=transport,
            register=settings.get("register", False))
        cleanup.push_async_callback(second_pbx.close)
        for pbx in (first_pbx, second_pbx):
            assert await async_start_sip_trunk(pbx.hass, local_ip="127.0.0.1")
            assert await pbx.calls.component("trunk:remote").wait_for_registration(timeout=5)
        if settings.get("register"):
            for pbx in (first_pbx, second_pbx):
                assert pbx.calls.component("registrar").registered_contacts(pbx.inbound_account)
        cleanup.callback(await attach_range_route(first_pbx, "200", "299"))
        cleanup.callback(await attach_range_route(second_pbx, "100", "199"))
        yield SimpleNamespace(a=first_pbx, b=second_pbx, received=received, transport=transport,
                              middle=middle, force_caller=settings.get("force_caller", False))


async def call_between(pair, source, destination, *, connected=None):
    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.bind(("127.0.0.2", 0))
    audio.setblocking(False)
    caller = SipCallClient(
        local_ip="127.0.0.2", local_name=source.username, local_uri_user=source.username,
        local_sip_port=0, local_rtp_port=audio.getsockname()[1],
        username=source.username, password=PASSWORD,
        supported_formats=[AudioFormat(16000, frame_ms=20)],
    )
    caller.transport, caller.protocol = await source.hass.loop.create_datagram_endpoint(
        lambda: SipDatagramQueueProtocol(caller.queue), local_addr=("127.0.0.2", 0),
    )
    caller.local_sip_port = caller.transport.get_extra_info("sockname")[1]
    try:
        state = await caller.invite(target=destination.username, remote_host="127.0.0.1",
                                    remote_sip_port=source.port, timeout=5)
        if state == "ringing":
            state = await caller.wait_for_final(timeout=5)
        assert state == "in_call", (state, caller.last_sip_status_code)
        assert destination.username in source.routed
        if connected is not None:
            await asyncio.wait_for(connected.wait(), 5)
            assert len(source.calls.sessions) >= 2 and len(destination.calls.sessions) >= 2
        request = destination.phone.invites[-1]
        expected_caller = "central-pbx" if pair.force_caller else source.username
        assert sip.parse_sip_uri(sip.name_addr_uri(request.header("From"))).user == expected_caller
        hop = next((message, result, transport) for hass, message, result, transport in reversed(pair.received)
                   if hass is destination.hass and getattr(result, "authenticated_username", "")
                   and sip.parse_sip_uri(message.uri).user == destination.username)
        assert hop[1].authenticated_username == destination.inbound_account
        assert hop[1].received_via_trunk and hop[1].trunk_id == "remote"
        assert sip.parse_sip_uri(sip.name_addr_uri(hop[0].header("From"))).user == expected_caller
        expected_contact = "middle-pbx" if pair.middle else destination.inbound_account
        assert sip.parse_sip_uri(sip.name_addr_uri(hop[0].header("Contact"))).user == expected_contact
        assert sip.parse_sip_uri(hop[0].uri).user == destination.username
        assert hop[2] == pair.transport
        assert int(request.header("Max-Forwards")) < int(hop[0].header("Max-Forwards"))
        dialog = caller.dialog
        # Distinct signals also detect swapped RTP ownership during concurrency.
        outbound_sample = 1008 if source.username in {"101", "202"} else 2016
        returned_sample = 2016 if destination.username in {"101", "202"} else 4032
        async with asyncio.timeout(5):
            for index in range(3):
                packet = rtp.build_packet(rtp.RtpPacket(dialog.send_format.payload_type,
                    index, index * 320, 901, struct.pack("!320h", *([outbound_sample] * 320))))
                await source.hass.loop.sock_sendto(audio, packet, (dialog.remote_rtp_host, dialog.remote_rtp_port))
            for _ in range(3):
                received = rtp.parse_packet(await destination.phone.audio.get())
            # G.711 and the 16/8 kHz resamplers have startup transients. The
            # settled frame must carry the originating signal in each direction.
            assert max(abs(value - outbound_sample) for value in struct.unpack("!320h", received.payload)) <= outbound_sample / 15
            peer_port = int(re.search(rb"m=audio (\d+)", request.body)[1])
            for index in range(3):
                packet = rtp.build_packet(rtp.RtpPacket(destination.phone.payload_type,
                    index, index * 320, 902, struct.pack("!320h", *([returned_sample] * 320))))
                await source.hass.loop.sock_sendto(destination.phone.rtp_socket, packet, ("127.0.0.1", peer_port))
            for _ in range(3):
                returned = rtp.parse_packet(await source.hass.loop.sock_recv(audio, 4096))
            assert max(abs(value - returned_sample) for value in struct.unpack("!320h", returned.payload)) <= returned_sample / 15
        await caller.terminate()
        async with asyncio.timeout(5):
            while source.calls.sessions or destination.calls.sessions:
                await asyncio.sleep(.01)
        for pbx in (source, destination):
            assert not pbx.runtime.rtp_port_pool.get("used")
            assert pbx.calls.component("trunk:remote").ready
            assert not pbx.calls.component("trunk:remote")._outbound_dialogs
    finally:
        await caller.close()
        audio.close()


@pytest.mark.parametrize("two_pbx", [
    {"transport": transport, "register": register}
    for transport in ("UDP", "TCP") for register in (False, True)
], ids=["UDP-static", "UDP-registered", "TCP-static", "TCP-registered"], indirect=True)
async def test_two_independent_ha_pbxs_call_both_directions_with_audio_and_cleanup(two_pbx):
    await call_between(two_pbx, two_pbx.a, two_pbx.b)
    await call_between(two_pbx, two_pbx.b, two_pbx.a)


@pytest.mark.parametrize("two_pbx", [
    {"transport": transport, "asterisk": True, "force_caller": force}
    for transport in ("UDP", "TCP") for force in (False, True)
], ids=["UDP-preserve", "UDP-force", "TCP-preserve", "TCP-force"], indirect=True)
async def test_two_ha_pbxs_via_real_asterisk_preserve_or_force_identity(two_pbx):
    await call_between(two_pbx, two_pbx.a, two_pbx.b)
    await call_between(two_pbx, two_pbx.b, two_pbx.a)
    _status, channels = await two_pbx.middle.command("core show channels count")
    assert "0 active channels" in channels


@pytest.mark.parametrize("two_pbx", [
    {"transport": transport, "register": True} for transport in ("UDP", "TCP")
], ids=["UDP-registered", "TCP-registered"], indirect=True)
async def test_registered_peer_principal_carries_two_simultaneous_calls(two_pbx):
    connected = asyncio.Barrier(2)
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(call_between(two_pbx, two_pbx.a, two_pbx.b, connected=connected))
        tasks.create_task(call_between(two_pbx, two_pbx.a.extra, two_pbx.b.extra, connected=connected))
