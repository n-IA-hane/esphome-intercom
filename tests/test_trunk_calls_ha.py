"""Named line selection sends real SIP and announcement RTP to the chosen peer."""

import asyncio
import re
import socket
import secrets
import struct

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError

from custom_components.voip_stack.config import normalized_trunk_config
from custom_components.voip_stack.core import rtp, sip
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.runtime_data import sip_trunk, sip_trunks
from custom_components.voip_stack.trunk_runtime import async_start_sip_trunk, async_stop_sip_trunk

from .test_automation_originate_ha import lab as lab, dial, tts_fixture

pytestmark = pytest.mark.ha


@pytest.fixture
async def trunks(lab, monkeypatch, request):
    full_runtime = bool(getattr(request, "param", False))
    if full_runtime:
        from unittest.mock import AsyncMock
        from custom_components.voip_stack import endpoint_runtime, invite_router, peer_snapshot, sensor
        from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime

        lab.runtime.sip = lab.calls = SipEndpointRuntime(allow_dark_sessions=True)
        # The listener needs the same port on UDP and TCP. Keep it outside
        # peer media and OS ephemeral ranges to avoid cross-transport collisions.
        for _ in range(128):
            selected_port = 10000 + secrets.randbelow(2000)
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_probe, socket.socket() as tcp_probe:
                    udp_probe.bind(("0.0.0.0", selected_port))
                    tcp_probe.bind(("0.0.0.0", selected_port))
            except OSError:
                continue
            lab.runtime.transport_config["sip_port"] = selected_port
            break
        else:
            raise RuntimeError("No free UDP/TCP port in the SIP laboratory range")
        # Import before patching, then restore both lookup namespaces on teardown.
        snapshot = AsyncMock(return_value=[])
        monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", snapshot)
        monkeypatch.setattr(sensor, "async_build_peer_snapshot", snapshot)
        monkeypatch.setattr(endpoint_runtime, "_roster_from_peers", lambda *_args: lab.roster)
        monkeypatch.setattr(invite_router, "_roster_from_peers", lambda *_args: lab.roster)
        assert await endpoint_runtime.async_start_sip_endpoint(lab.hass)
    configs = {}
    for index, identity in enumerate(("home", "work")):
        phone = lab.phones[index]
        original = phone.datagram_received

        def receive(raw, address, phone=phone, original=original):
            request = sip.parse_message(raw)
            if request.method != "REGISTER":
                original(raw, address)
                return
            headers = [(key, request.header(key)) for key in ("Via", "From", "To", "Call-ID", "CSeq", "Contact", "Expires")]
            phone.transport.sendto(sip.build_response(200, "OK", headers), address)

        monkeypatch.setattr(phone, "datagram_received", receive)
        configs[identity] = normalized_trunk_config({
            "trunk_enabled": True, "trunk_register": True, "trunk_server": "127.0.0.1",
            "trunk_port": phone.transport.get_extra_info("sockname")[1],
            "trunk_username": identity, "trunk_password": "test-only",
        })
    lab.runtime.trunk_configs = configs
    lab.runtime.default_trunk_id = "home"
    lab.runtime.trunk_config = configs["home"]
    lab.roster.append(RosterEntry("External", number="12345"))
    await async_start_sip_trunk(lab.hass, local_ip="127.0.0.1")
    try:
        assert all(await asyncio.gather(*(client.wait_for_registration(timeout=2) for client in sip_trunks(lab.hass).values())))
        yield lab
    finally:
        await async_stop_sip_trunk(lab.hass)
        if full_runtime:
            await endpoint_runtime.async_stop_sip_endpoint(lab.hass)


@pytest.mark.parametrize("identity,index", [("home", 0), ("work", 1)])
async def test_automation_announcement_uses_selected_trunk_and_preserves_registration(trunks, monkeypatch, identity, index):
    lab = trunks
    stream = tts_fixture(monkeypatch, lab)
    context = Context()
    result = await dial(lab, context, destination="External", trunk_id=identity)
    session = lab.calls.get_session(result["call_id"])
    assert session.metadata["outbound_trunk_id"] == identity
    assert len(lab.phones[index].invites) == 1
    assert not lab.phones[1 - index].invites
    assert lab.phones[index].invites[0].uri.startswith("sip:12345@")
    await lab.hass.services.async_call("voip_stack", "tts_say", {
        "tts_entity_id": "tts.test", "message": "Wake up",
    }, blocking=True, context=context)
    await lab.hass.services.async_call("voip_stack", "hangup", {}, blocking=True, context=context)
    assert len(stream.calls) == 1
    voiced = []
    while not lab.phones[index].audio.empty():
        raw, _ = lab.phones[index].audio.get_nowait()
        packet = rtp.parse_packet(raw)
        if any(packet.payload):
            voiced.append(packet.payload)
    assert b"".join(voiced) == b"\x01\x80" * 960
    assert lab.phones[index].bye.is_set()
    assert not lab.calls.sessions
    assert all(client.ready and not client._outbound_dialogs for client in sip_trunks(lab.hass).values())


async def test_pending_announcement_forward_uses_explicit_trunk(trunks, monkeypatch):
    from types import SimpleNamespace
    from custom_components.voip_stack.call_forwarder import async_forward_existing_call

    lab = trunks
    first, second = lab.phones
    first.auto_answer = False
    context = Context()
    stream = tts_fixture(monkeypatch, lab)
    task = asyncio.create_task(dial(lab, context, destination="External", trunk_id="home", answer_timeout=5))
    try:
        await asyncio.wait_for(first.invited.wait(), 2)
        call_id = first.invites[0].header("Call-ID")
        await async_forward_existing_call(SimpleNamespace(
            hass=lab.hass, config=lab.runtime.transport_config, local_ip="127.0.0.1",
        ), call_id=call_id, destination="External", trunk_id="work", on_failure="terminate")
        result = await asyncio.wait_for(task, 5)
        assert result["call_id"] == call_id
        assert len(first.invites) == len(second.invites) == 1
        assert lab.calls.get_session(call_id).metadata["outbound_trunk_id"] == "work"
        await lab.hass.services.async_call("voip_stack", "tts_say", {
            "tts_entity_id": "tts.test", "message": "Wake up",
        }, blocking=True, context=context)
        await lab.hass.services.async_call("voip_stack", "hangup", {}, blocking=True, context=context)
        voiced = []
        while not second.audio.empty():
            packet = rtp.parse_packet(second.audio.get_nowait()[0])
            if any(packet.payload):
                voiced.append(packet.payload)
        assert b"".join(voiced) == b"\x01\x80" * 960
        assert len(stream.calls) == 1 and first.audio.empty()
        assert not lab.calls.sessions
        assert all(client.ready and not client._outbound_dialogs for client in sip_trunks(lab.hass).values())

    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_unavailable_selected_trunk_never_dials_the_healthy_default(trunks):
    lab = trunks
    await sip_trunk(lab.hass, "work").stop()
    assert sip_trunk(lab.hass, "home").ready
    with pytest.raises(ServiceValidationError, match="unavailable"):
        await dial(lab, Context(), destination="External", trunk_id="work")
    assert not any(phone.invites for phone in lab.phones)
    assert not lab.calls.sessions


@pytest.mark.parametrize("override,index", [("", 1), ("home", 0)])
async def test_browser_phone_preference_and_explicit_override_dial_the_selected_line(trunks, monkeypatch, override, index):
    from unittest.mock import AsyncMock
    from homeassistant.core import ServiceCall, callback
    from custom_components.voip_stack import softphone_originate
    from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
    from custom_components.voip_stack.phone_policy import PhonePolicy
    from custom_components.voip_stack.phone_control import CallControlRequest, PhoneOperation
    from custom_components.voip_stack.roster import dump_roster_json
    from custom_components.voip_stack.websocket_api import CALL_EVENT

    lab = trunks
    phone = PhoneEndpoint(endpoint_id="browser", name="Browser", kind=EndpointKind.BROWSER,
                          device_id="browser-device", availability="available",
                          policy=PhonePolicy(outbound_trunk_id="work"))
    lab.runtime.endpoints.register(phone)
    lab.hass.states.async_set("sensor.voip_phonebook", "3", {"roster_json": dump_roster_json(lab.roster)})
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    connected = asyncio.Event()

    @callback
    def observe(event):
        if event.data.get("state") == "in_call" and event.data.get("endpoint_id") == "browser":
            connected.set()

    unsubscribe = lab.hass.bus.async_listen(CALL_EVENT, observe)
    try:
        call = ServiceCall(lab.hass, "voip_stack", "call", {
            "device_id": "browser-device", "destination": "External", "trunk_id": override,
        })
        await softphone_originate.async_originate_browser_call(call, endpoint_id="browser", browser_endpoint=phone)
        await asyncio.wait_for(connected.wait(), 2)
        assert len(lab.phones[index].invites) == 1
        assert not lab.phones[1 - index].invites
        call_id = lab.phones[index].invites[0].header("Call-ID")
        session = lab.calls.get_session(call_id)
        assert session.metadata["outbound_trunk_id"] == ("home" if index == 0 else "work")
        await lab.runtime.phones.control(
            ServiceCall(lab.hass, "voip_stack", "hangup", {"device_id": "browser-device", "call_id": call_id}),
            PhoneOperation.HANGUP, CallControlRequest(call_id=call_id),
        )
        assert not lab.calls.sessions
        assert all(client.ready for client in sip_trunks(lab.hass).values())
    finally:
        unsubscribe()


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("rename", [False, True])
async def test_sip_phone_uses_its_line_preference_through_real_ha_listener_and_duplex_rtp(trunks, rename):
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
    from custom_components.voip_stack.phone_policy import PhonePolicy
    from custom_components.voip_stack.sip_client import SipCallClient
    from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol

    lab = trunks
    lab.runtime.endpoints.register(PhoneEndpoint(
        endpoint_id="source", name="Source", username="source", kind=EndpointKind.SIP_ACCOUNT,
        availability="available", policy=PhonePolicy(outbound_trunk_id="work"),
    ))
    lab.roster.append(RosterEntry("source", address="127.0.0.2", metadata={"endpoint_id": "source"}))
    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.bind(("127.0.0.2", 0))
    audio.setblocking(False)
    client = SipCallClient(local_ip="127.0.0.2", local_name="Source", local_uri_user="source",
                           local_sip_port=5060, local_rtp_port=audio.getsockname()[1],
                           supported_formats=[AudioFormat(sample_rate=16000, frame_ms=20)])
    # Bind the independent phone to another loopback IP, outside the providers'
    # trusted source address. Signaling and all call state use the real client.
    client.transport, client.protocol = await lab.hass.loop.create_datagram_endpoint(
        lambda: SipDatagramQueueProtocol(client.queue), local_addr=("127.0.0.2", 0),
    )
    client.local_sip_port = client.transport.get_extra_info("sockname")[1]
    platform = None
    try:
        result = await client.invite(target="External", remote_host="127.0.0.1",
                                     remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
        if result == "ringing":
            result = await client.wait_for_final(timeout=3)
        assert result == "in_call"
        assert not lab.phones[0].invites and len(lab.phones[1].invites) == 1
        session = lab.calls.get_session(client.dialog_ids.call_id)
        assert session.metadata["outbound_trunk_id"] == "work"
        platform = None
        if rename:
            from datetime import timedelta
            import logging
            from homeassistant.helpers import entity_registry as er
            from homeassistant.helpers.entity_platform import EntityPlatform
            from custom_components.voip_stack.sensor import PhoneEndpointCallStateSensor

            entity = PhoneEndpointCallStateSensor(lab.hass, lab.runtime.endpoints.require("source"), lab.runtime.endpoints)
            platform = EntityPlatform(hass=lab.hass, logger=logging.getLogger(__name__), domain="sensor",
                platform_name="voip_stack", platform=None, scan_interval=timedelta(seconds=30), entity_namespace=None)
            platform.config_entry = lab.hass.config_entries.async_entries("voip_stack")[0]
            await platform.async_add_entities([entity])
            old_id = entity.entity_id
            renamed = asyncio.Event()
            unsubscribe_rename = lab.runtime.endpoints.subscribe(
                lambda event: renamed.set() if "sensor.source_renamed_during_call" in event.endpoint.entity_ids else None
            )
            try:
                er.async_get(lab.hass).async_update_entity(old_id, new_entity_id="sensor.source_renamed_during_call")
                await asyncio.wait_for(renamed.wait(), 3)
            finally:
                unsubscribe_rename()
            assert lab.runtime.endpoints.by_entity_id(old_id) is None
            assert lab.runtime.endpoints.by_entity_id(entity.entity_id).active_call_id == session.call_id
            assert lab.calls.get_session(session.call_id) is session
        dialog = client.dialog
        async with asyncio.timeout(3):
            for index in range(3):
                packet = rtp.build_packet(rtp.RtpPacket(dialog.send_format.payload_type, index, index * 320,
                    901, struct.pack("!320h", *([2000] * 320))))
                await lab.hass.loop.sock_sendto(audio, packet, (dialog.remote_rtp_host, dialog.remote_rtp_port))
            received, _ = await lab.phones[1].audio.get()
            assert set(struct.unpack("!320h", rtp.parse_packet(received).payload)) == {2000}
            peer_port = int(re.search(rb"m=audio (\d+)", lab.phones[1].invites[0].body)[1])
            response = rtp.build_packet(rtp.RtpPacket(lab.phones[1].payload_type, 0, 0, 902,
                                                      struct.pack("!320h", *([3000] * 320))))
            await lab.hass.loop.sock_sendto(lab.phones[1].rtp_socket, response, ("127.0.0.1", peer_port))
            returned = rtp.parse_packet(await lab.hass.loop.sock_recv(audio, 4096))
            assert set(struct.unpack("!320h", returned.payload)) == {3000}
        assert client.bye()
        await asyncio.wait_for(session.termination_started.wait(), 3)
        barrier = lab.calls.request_termination(session.call_id, session.termination_intent)
        if barrier is not None:
            await barrier
        assert not lab.calls.sessions
        assert all(trunk.ready for trunk in sip_trunks(lab.hass).values())
        if rename:
            await client.close()
            client = SipCallClient(local_ip="127.0.0.2", local_name="Source", local_uri_user="source",
                local_sip_port=5060, local_rtp_port=audio.getsockname()[1],
                supported_formats=[AudioFormat(sample_rate=16000, frame_ms=20)])
            client.transport, client.protocol = await lab.hass.loop.create_datagram_endpoint(
                lambda: SipDatagramQueueProtocol(client.queue), local_addr=("127.0.0.2", 0),
            )
            client.local_sip_port = client.transport.get_extra_info("sockname")[1]
            result = await client.invite(target="External", remote_host="127.0.0.1",
                remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
            if result == "ringing":
                result = await client.wait_for_final(timeout=3)
            assert result == "in_call"
            second = lab.calls.get_session(client.dialog_ids.call_id)
            assert second is not session
            assert client.bye()
            await asyncio.wait_for(second.termination_started.wait(), 3)
            barrier = lab.calls.request_termination(second.call_id, second.termination_intent)
            if barrier is not None:
                await barrier
            assert not lab.calls.sessions
    finally:
        if platform is not None:
            await platform.async_reset()
        await client.close()
        audio.close()


@pytest.mark.parametrize('source', ['browser', 'automation'])
@pytest.mark.parametrize('missing', [True, False])
async def test_external_call_distinguishes_no_line_selection_from_unavailable_line(trunks,monkeypatch,source,missing):
    from unittest.mock import AsyncMock
    from homeassistant.core import ServiceCall
    from custom_components.voip_stack import softphone_originate
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint,EndpointKind
    from custom_components.voip_stack.roster import dump_roster_json
    lab=trunks
    if missing:
        lab.runtime.default_trunk_id=''
    else:
        await sip_trunk(lab.hass,'home').stop()
    expected='outgoing_trunk_not_selected' if missing else 'outgoing_trunk_unavailable'
    with pytest.raises(ServiceValidationError) as caught:
        if source=='automation':
            await dial(lab,Context(),destination='External')
        else:
            phone=PhoneEndpoint(endpoint_id='browser',name='Browser',kind=EndpointKind.BROWSER,
                device_id='browser-device',availability='available')
            lab.runtime.endpoints.register(phone)
            lab.hass.states.async_set('sensor.voip_phonebook','ready',{'roster_json':dump_roster_json(lab.roster)})
            monkeypatch.setattr(softphone_originate,'_resolve_target_device',AsyncMock(return_value=None))
            call=ServiceCall(lab.hass,'voip_stack','call',{'device_id':'browser-device','destination':'External'})
            await softphone_originate.async_originate_browser_call(call,endpoint_id='browser',browser_endpoint=phone)
    assert caught.value.translation_key==expected
    assert not any(phone.invites for phone in lab.phones)
    assert not lab.calls.sessions


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("with_sdp", [False, True], ids=["bare-183", "183-with-early-pcma"])
async def test_registered_sip_caller_receives_default_trunk_early_media(trunks, monkeypatch, with_sdp):
    from types import SimpleNamespace
    from custom_components.voip_stack.endpoint_session import TerminationIntent
    from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
    from custom_components.voip_stack.runtime_data import sip_registrar
    from custom_components.voip_stack.sip_registrar import SipAccount
    from .test_udp_outbound_flow_ha import PASSWORD, WirePhone, register_phone

    lab = trunks
    registrar = sip_registrar(lab.hass)
    registrar.enabled = True
    registrar.update_accounts([*registrar.accounts.values(), SipAccount("early-source", "Early source", PASSWORD)])
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="sip:early-source", name="Early source",
        username="early-source", kind=EndpointKind.SIP_ACCOUNT, availability="available"))
    listener = ("127.0.0.1", lab.runtime.transport_config["sip_port"])
    transport, caller = await lab.hass.loop.create_datagram_endpoint(WirePhone, local_addr=("127.0.0.2", 0))
    caller.contact = caller
    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.bind(("127.0.0.2", 0))
    audio.setblocking(False)
    provider = lab.phones[0]
    original = provider.datagram_received
    emitted = asyncio.Event()
    media_tasks = []
    ids = sip.SipDialogIds(call_id=sip.make_call_id("early-test"), local_tag=sip.make_tag())
    payload = b"\xaa" * 160

    async def send_early_pcm(port):
        for index in range(8):
            raw = rtp.build_packet(rtp.RtpPacket(8, index, index * 160, 0x12345678, payload))
            await lab.hass.loop.sock_sendto(provider.rtp_socket, raw, ("127.0.0.1", port))
            emitted.set()
            await asyncio.sleep(.02)

    def receive(raw, address):
        request = sip.parse_message(raw)
        if request.method != "INVITE":
            return original(raw, address)
        provider.requests.append(request)
        provider.invites.append(request)
        provider.last_request, provider.remote = request, address
        headers = [(name, request.header(name)) for name in ("Via", "From", "Call-ID", "CSeq")]
        headers.append(("To", request.header("To") + ";tag=bedroom"))
        body = b""
        if with_sdp:
            headers.extend((("Content-Type", "application/sdp"),
                ("Contact", f"<sip:provider@127.0.0.1:{provider.transport.get_extra_info('sockname')[1]}>")))
            body = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Early announcement\r\n"
                "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                f"m=audio {provider.rtp_socket.getsockname()[1]} RTP/AVP 8\r\n"
                "a=rtpmap:8 PCMA/8000\r\na=ptime:20\r\na=sendrecv\r\n").encode()
        provider.transport.sendto(sip.build_response(183, "Session Progress", headers, body), address)
        provider.invited.set()
        if with_sdp:
            media_tasks.append(asyncio.create_task(send_early_pcm(int(re.search(rb"m=audio (\d+)", request.body)[1]))))

    monkeypatch.setattr(provider, "datagram_received", receive)
    try:
        await register_phone(SimpleNamespace(lab=lab, registrar=registrar, listener=listener, a=caller),
            username="early-source", outbound=False)
        target = str(sip.SipUri("External", *listener))
        local = str(sip.SipUri("early-source", *caller.address))
        offer = ("v=0\r\no=- 2 1 IN IP4 127.0.0.2\r\ns=Caller\r\n"
            "c=IN IP4 127.0.0.2\r\nt=0 0\r\n"
            f"m=audio {audio.getsockname()[1]} RTP/AVP 8\r\n"
            "a=rtpmap:8 PCMA/8000\r\na=ptime:20\r\na=sendrecv\r\n").encode()
        headers = sip.dialog_headers(request_uri=target, local_uri=local, remote_uri=target,
            contact_uri=local, dialog=ids, method="INVITE", content_type="application/sdp")
        transport.sendto(sip.build_request("INVITE", target, headers, offer), listener)
        async with asyncio.timeout(2):
            while True:
                progress, _ = await caller.receive()
                if progress.header("Call-ID") == ids.call_id and progress.status_code != 100:
                    break
        assert len(provider.invites) == 1 and not lab.phones[1].invites
        session = lab.calls.get_session(ids.call_id)
        assert session.metadata["outbound_trunk_id"] == "home"
        assert not provider.answered.is_set() and not session.answer_committed
        if with_sdp:
            await asyncio.wait_for(emitted.wait(), 1)
            assert progress.status_code == 183 and progress.body, "Trunk early SDP was replaced by local ringback"
            packet = rtp.parse_packet(await asyncio.wait_for(lab.hass.loop.sock_recv(audio, 2048), 1))
            assert packet.payload_type == 8 and packet.payload == payload
        else:
            assert progress.status_code == 180 and not progress.body
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(lab.hass.loop.sock_recv(audio, 2048), .1)
    finally:
        barrier = lab.calls.request_termination(ids.call_id, TerminationIntent("test_done"))
        if barrier is not None:
            await barrier
        for task in media_tasks:
            task.cancel()
        await asyncio.gather(*media_tasks, return_exceptions=True)
        transport.close()
        audio.close()
        assert not lab.calls.sessions
        assert all(client.ready and not client._outbound_dialogs for client in sip_trunks(lab.hass).values())


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("mode", ["cancel", "final", "bodyless", "changed", "late-reliable", "late-queued-final"])
async def test_registered_trunk_early_media_primes_nat_and_retains_one_relay(trunks, monkeypatch, mode):
    """A provider behind symmetric RTP withholds its announcement until HA sends."""
    from dataclasses import replace
    from types import SimpleNamespace
    from custom_components.voip_stack.endpoint_session import TerminationIntent
    from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
    from custom_components.voip_stack.runtime_data import sip_registrar
    from custom_components.voip_stack.sip_registrar import SipAccount
    from .test_udp_outbound_flow_ha import PASSWORD, WirePhone, register_phone

    lab = trunks
    registrar = sip_registrar(lab.hass)
    registrar.enabled = True
    registrar.update_accounts([*registrar.accounts.values(), SipAccount("early-nat", "Early NAT", PASSWORD)])
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="sip:early-nat", name="Early NAT",
        username="early-nat", kind=EndpointKind.SIP_ACCOUNT, availability="available"))
    listener = ("127.0.0.1", lab.runtime.transport_config["sip_port"])
    transport, caller = await lab.hass.loop.create_datagram_endpoint(WirePhone, local_addr=("127.0.0.2", 0))
    caller.contact = caller
    caller_receive = caller.datagram_received

    def receive_caller(raw, address):
        request = sip.parse_message(raw)
        if request.method == "UPDATE":
            caller.respond(request, address)
        else:
            caller_receive(raw, address)

    monkeypatch.setattr(caller, "datagram_received", receive_caller)
    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.bind(("127.0.0.2", 0))
    audio.setblocking(False)
    changed_audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    changed_audio.bind(("127.0.0.1", 0))
    changed_audio.setblocking(False)
    provider = lab.phones[0]
    original = provider.datagram_received
    provider.auto_answer = False
    ids = sip.SipDialogIds(call_id=sip.make_call_id("early-nat"), local_tag=sip.make_tag())
    tone = b"\xaa" * 160
    next_tone = b"\x55" * 160
    target = str(sip.SipUri("External", *listener))
    local = str(sip.SipUri("early-nat", *caller.address))
    current_request = None
    media_task = None
    nat_opened = asyncio.Event()
    final_ack = asyncio.Event()
    priming = []
    baseline_ports = set(lab.runtime.rtp_port_pool.get("used", ()))

    def answer_sdp(port):
        return ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Provider\r\n"
            "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
            f"m=audio {port} RTP/AVP 8\r\n"
            "a=rtpmap:8 PCMA/8000\r\na=ptime:20\r\na=sendrecv\r\n").encode()

    def respond(status, *, body=b""):
        request = current_request
        headers = [(key, request.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
        headers += [("To", request.header("To") + ";tag=bedroom"),
            ("Contact", f"<sip:provider@127.0.0.1:{provider.transport.get_extra_info('sockname')[1]}>")]
        if body:
            headers.append(("Content-Type", "application/sdp"))
        provider.transport.sendto(sip.build_response(status, {180: "Ringing", 183: "Session Progress", 200: "OK"}[status],
            headers, body), provider.remote)

    async def gated_announcement(port):
        # The independent provider deliberately does not send until a valid RTP
        # packet arrives on its negotiated socket. The caller sends no RTP yet.
        raw, _ = await provider.audio.get()
        prime = rtp.parse_packet(raw)
        assert prime.payload_type == 8 and prime.payload == b"\xd5" * 160
        priming.append(prime)
        nat_opened.set()
        await lab.hass.loop.sock_sendto(provider.rtp_socket,
            rtp.build_packet(rtp.RtpPacket(8, 10, 1600, 0x12345678, tone)), ("127.0.0.1", port))

    def receive(raw, address):
        nonlocal current_request, media_task
        request = sip.parse_message(raw)
        if request.method != "INVITE":
            if request.method == "ACK":
                final_ack.set()
            return original(raw, address)
        provider.requests.append(request)
        provider.invites.append(request)
        provider.last_request, provider.remote = request, address
        current_request = request
        if mode.startswith("late-"):
            respond(180)
        else:
            respond(183, body=answer_sdp(provider.rtp_socket.getsockname()[1]))
        port = int(re.search(rb"m=audio (\d+)", request.body)[1])
        media_task = asyncio.create_task(gated_announcement(port))
        provider.invited.set()

    monkeypatch.setattr(provider, "datagram_received", receive)
    invite_headers = None
    cseq = 1

    def acknowledge_progress(response):
        nonlocal cseq
        cseq += 1
        remote_tag = sip.extract_tag(response.header("To"))
        request_uri = str(sip.parse_sip_uri(response.header("Contact"))) if response.header("Contact") else target
        prack = sip.dialog_headers(request_uri=request_uri, local_uri=local, remote_uri=target,
            contact_uri=local, dialog=replace(ids, remote_tag=remote_tag, cseq=cseq, branch="z9hG4bK-prack-" + str(cseq)),
            method="PRACK")
        prack.append(("RAck", f"{response.header('RSeq')} 1 INVITE"))
        transport.sendto(sip.build_request("PRACK", request_uri, prack), listener)

    async def source_response(status, *, acknowledge=True):
        async with asyncio.timeout(3):
            while True:
                response, _ = await caller.receive()
                if response.header("Call-ID") != ids.call_id or not response.header("CSeq").endswith("INVITE"):
                    continue
                if response.header("RSeq") and acknowledge:
                    acknowledge_progress(response)
                if response.status_code == status:
                    return response

    try:
        await register_phone(SimpleNamespace(lab=lab, registrar=registrar, listener=listener, a=caller),
            username="early-nat", outbound=False)
        offer = ("v=0\r\no=- 2 1 IN IP4 127.0.0.2\r\ns=Caller\r\n"
            "c=IN IP4 127.0.0.2\r\nt=0 0\r\n"
            f"m=audio {audio.getsockname()[1]} RTP/AVP 8\r\n"
            "a=rtpmap:8 PCMA/8000\r\na=ptime:20\r\na=sendrecv\r\n").encode()
        invite_headers = sip.dialog_headers(request_uri=target, local_uri=local, remote_uri=target,
            contact_uri=local, dialog=ids, method="INVITE", content_type="application/sdp")
        transport.sendto(sip.build_request("INVITE", target, invite_headers, offer), listener)
        if mode.startswith("late-"):
            first = await source_response(180, acknowledge=mode != "late-queued-final")
            assert not first.body
            respond(183, body=answer_sdp(provider.rtp_socket.getsockname()[1]))
            if mode == "late-queued-final":
                await asyncio.wait_for(nat_opened.wait(), 2)
                session = lab.calls.get_session(ids.call_id)
                assert not session.answer_committed
                respond(200, body=answer_sdp(provider.rtp_socket.getsockname()[1]))
                await asyncio.wait_for(final_ack.wait(), 2)
                # Do not deliver a final answer ahead of the queued reliable
                # SDP answer. The outstanding 180 has deliberately no PRACK.
                with pytest.raises(TimeoutError):
                    async with asyncio.timeout(.05):
                        while True:
                            queued, _ = await caller.receive()
                            assert not (queued.status_code == 200 and queued.header("CSeq") == "1 INVITE")
                acknowledge_progress(first)
        early = await source_response(183)
        if mode.startswith("late-"):
            assert int(early.header("RSeq")) == int(first.header("RSeq")) + 1
        assert early.body and early.header("RSeq")
        await asyncio.wait_for(nat_opened.wait(), 2)
        await media_task
        packet = rtp.parse_packet(await asyncio.wait_for(lab.hass.loop.sock_recv(audio, 2048), 2))
        assert packet.payload_type == 8 and packet.payload == tone
        session = lab.calls.get_session(ids.call_id)
        client = lab.calls.sip_client_for(current_request.header("Call-ID"))
        relay = lab.calls.resource_for(ids.call_id, "relay")
        assert relay is not None
        if mode != "late-queued-final":
            assert client.dialog is None and not session.answer_committed
        assert len(provider.invites) == 1 and not lab.phones[1].invites
        ports = (relay.left_port, relay.right_port)
        generation = session.generation
        if mode == "cancel":
            cancel = [(name, value if name != "CSeq" else "1 CANCEL") for name, value in invite_headers
                if name in {"Via", "From", "To", "Call-ID", "CSeq", "Max-Forwards"}]
            transport.sendto(sip.build_request("CANCEL", target, cancel), listener)
            cancelled = await source_response(487)
            ack_headers = [(name, value if name != "CSeq" else "1 ACK") for name, value in cancel]
            ack_headers = [(name, cancelled.header("To") if name == "To" else value) for name, value in ack_headers]
            transport.sendto(sip.build_request("ACK", target, ack_headers), listener)
            await asyncio.wait_for(session.terminated.wait(), 3)
            assert any(request.method == "CANCEL" for request in provider.requests)
            assert any(request.method == "ACK" for request in provider.requests)
        else:
            final_port = changed_audio.getsockname()[1] if mode == "changed" else provider.rtp_socket.getsockname()[1]
            if mode != "late-queued-final":
                respond(200, body=b"" if mode == "bodyless" else answer_sdp(final_port))
            final = await source_response(200)
            assert final.body == early.body
            ack = sip.dialog_headers(request_uri=target, local_uri=local, remote_uri=target, contact_uri=local,
                dialog=replace(ids, remote_tag=sip.extract_tag(final.header("To"))), method="ACK")
            transport.sendto(sip.build_request("ACK", target, ack), listener)
            assert session.answer_committed and session.generation == generation
            assert lab.calls.resource_for(ids.call_id, "relay") is relay
            assert ports == (relay.left_port, relay.right_port)
            assert relay.right.port == final_port
            outbound = rtp.build_packet(rtp.RtpPacket(8, 1, 0, 0x55556666, tone))
            await lab.hass.loop.sock_sendto(audio, outbound, ("127.0.0.1", relay.left_port))
            if mode == "changed":
                forwarded = rtp.parse_packet(await asyncio.wait_for(lab.hass.loop.sock_recv(changed_audio, 2048), 2))
                source_socket = changed_audio
            else:
                forwarded = rtp.parse_packet((await asyncio.wait_for(provider.audio.get(), 2))[0])
                source_socket = provider.rtp_socket
            assert forwarded.payload == tone and forwarded.payload_type == 8
            assert forwarded.ssrc == priming[0].ssrc
            assert forwarded.sequence == rtp.next_sequence(priming[0].sequence)
            assert forwarded.timestamp == rtp.next_timestamp(priming[0].timestamp, 160)
            await lab.hass.loop.sock_sendto(source_socket,
                rtp.build_packet(rtp.RtpPacket(8, 20, 3200, 0xABCDEF12, next_tone)), ("127.0.0.1", relay.right_port))
            after = rtp.parse_packet(await asyncio.wait_for(lab.hass.loop.sock_recv(audio, 2048), 2))
            assert after.payload == next_tone and after.payload_type == 8
            assert after.ssrc == packet.ssrc and after.sequence == rtp.next_sequence(packet.sequence)
            assert any(request.method == "ACK" for request in provider.requests)
    finally:
        barrier = lab.calls.request_termination(ids.call_id, TerminationIntent("test_done"))
        if barrier is not None:
            await barrier
        if media_task is not None:
            media_task.cancel()
            await asyncio.gather(media_task, return_exceptions=True)
        transport.close()
        audio.close()
        changed_audio.close()
        assert not lab.calls.sessions
        assert set(lab.runtime.rtp_port_pool.get("used", ())) == baseline_ports
        assert all(client.ready and not client._outbound_dialogs for client in sip_trunks(lab.hass).values())


@pytest.mark.parametrize("encoding,rate,channels,pt", [("PCMA", 8000, 1, 8), ("G722", 8000, 1, 9), ("OPUS", 48000, 2, 111)])
def test_early_media_prime_uses_negotiated_codec_and_owned_rtp_clock(encoding, rate, channels, pt):
    from custom_components.voip_stack.core.sdp import RtpPcmFormat
    from custom_components.voip_stack.sip_client import RtpPayloadDecoder
    from custom_components.voip_stack.sip_rtp_bridge import RtpPeer, SipRtpRelay

    fmt = RtpPcmFormat(pt, encoding, rate, channels, 20)
    left = RtpPeer("127.0.0.1", 30000, pt, fmt.audio_format, rtp_format=fmt, send_rtp_format=fmt)
    right = RtpPeer("127.0.0.1", 30002, pt, fmt.audio_format, rtp_format=fmt, send_rtp_format=fmt,
        sequence=100, timestamp=1000, ssrc=0x12345678)
    relay = SipRtpRelay(left=left, right=right, left_port=30004, right_port=30006)
    sent = []

    class Transport:
        def sendto(self, raw, address):
            sent.append((rtp.parse_packet(raw), address))

    relay.right_transport = Transport()
    assert relay.prime_silence("right")
    packet, address = sent.pop()
    assert address == (right.host, right.port)
    assert (packet.payload_type, packet.sequence, packet.timestamp, packet.ssrc) == (pt, 100, 1000, 0x12345678)
    decoded = RtpPayloadDecoder(fmt).decode(packet.payload)
    samples = struct.unpack("<" + "h" * (len(decoded) // 2), decoded)
    assert len(decoded) == fmt.audio_format.nominal_frame_bytes
    assert max(abs(value) for value in samples) <= 32
    assert right.sequence == 101 and right.timestamp == 1000 + fmt.rtp_timestamp_step
    right.can_receive = False
    assert not relay.prime_silence("right") and not sent
    assert right.sequence == 101 and right.timestamp == 1000 + fmt.rtp_timestamp_step
