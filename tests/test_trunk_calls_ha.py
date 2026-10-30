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


class FixturePeerSource:
    """Synthetic discovery publishes through the same canonical owner."""

    def __init__(self, hass):
        self.hass = hass
        self._value = []

    async def __call__(self, _hass):
        return self._value

    @property
    def return_value(self):
        return self._value

    @return_value.setter
    def return_value(self, value):
        from custom_components.voip_stack.directory_runtime import async_refresh_directory
        from custom_components.voip_stack.endpoint_lifecycle import create_runtime_task

        self._value = value
        create_runtime_task(self.hass, async_refresh_directory(self.hass, peers=value))


@pytest.fixture
async def trunks(lab, monkeypatch, request):
    full_runtime = bool(getattr(request, "param", False))
    if full_runtime:
        from custom_components.voip_stack import endpoint_runtime, invite_router, peer_snapshot
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
        monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", FixturePeerSource(lab.hass))
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
