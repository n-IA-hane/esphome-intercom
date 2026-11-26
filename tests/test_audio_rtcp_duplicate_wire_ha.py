"""Real UDP receive counts include duplicates while existing PCM stays unique."""
import asyncio
from contextlib import suppress
import socket
import struct
from unittest.mock import AsyncMock

from aiohttp import WSMsgType, web
import pytest
from homeassistant.core import Context

from custom_components.voip_stack.core import rtp, sdp
from custom_components.voip_stack.core.audio_format import AudioFormat
from .test_automation_originate_ha import lab as lab, assert_call_resources_released

pytestmark = pytest.mark.ha


def datagram():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    return sock


def packet(sequence, value, payload_type=96):
    return rtp.build_packet(rtp.RtpPacket(payload_type, sequence, sequence * 320, 0xAABBCCDD,
        struct.pack("!320h", *([value] * 320))))


def loss_block(raw):
    _first, kind, words = struct.unpack_from("!BBH", raw)
    assert kind in {200, 201} and (words + 1) * 4 <= len(raw)
    offset = 28 if kind == 200 else 8
    _ssrc, loss, *_rest = struct.unpack_from("!IIIIII", raw, offset)
    signed = loss & 0xFFFFFF
    return signed - 2**24 if signed & 0x800000 else signed


async def wait_for(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.parametrize("owner_kind", ["local", "relay"])
async def test_udp_duplicate_rr_and_unique_pcm_for_local_and_relay(lab, owner_kind):
    from custom_components.voip_stack.local_call_media import LocalAudioContract, LocalCallMedia
    from custom_components.voip_stack.media_ports import RtpPortReservation
    from custom_components.voip_stack.sip_rtp_bridge import RtpPeer, SipRtpRelay
    wire = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
    incoming, control, destination, destination_control = (datagram() for _ in range(4))
    reservation = RtpPortReservation.allocate(lab.hass)
    pcm = []
    if owner_kind == "local":
        contract = LocalAudioContract("duplicate-local", "127.0.0.1", incoming.getsockname()[1], wire, wire,
            local_audio_direction="recvonly", source_host="127.0.0.1", audio_rtcp_min_interval=5,
            remote_audio_rtcp_host="127.0.0.1", remote_audio_rtcp_port=control.getsockname()[1])
        media = LocalCallMedia(lab.hass, invite=contract, local_rtp_port=reservation.ports[0],
                              reservation=reservation, on_complete=AsyncMock())
        media.decoder.add_pcm_observer(lambda frame, _format: pcm.append(frame))
    else:
        def peer(sock, rtcp):
            return RtpPeer("127.0.0.1", sock.getsockname()[1], 96, AudioFormat(16000),
            rtp_format=wire, send_rtp_format=wire, rtcp_host="127.0.0.1", rtcp_port=rtcp.getsockname()[1],
            rtcp_minimum_interval=5)
        media = SipRtpRelay(left=peer(incoming, control), right=peer(destination, destination_control),
            left_port=reservation.ports[0], right_port=reservation.ports[1], on_release=lambda _ports: reservation.release())
        media.observe_audio(lambda side, frame, _format: pcm.append(frame) if side == "left" else None)
    try:
        await media.start()
        port = media.local_rtp_port if owner_kind == "local" else media.left_port
        for sequence, value in [(10, 1000), (11, 2000), (11, 2000)]:
            await lab.hass.loop.sock_sendto(incoming, packet(sequence, value), ("127.0.0.1", port))
        await wait_for(lambda: len(pcm) == 2 and (media.counters["drop_sequence"] if owner_kind == "local" else media.dropped))
        assert b"".join(pcm) == struct.pack("<640h", *([1000] * 320 + [2000] * 320))
        owner = media.rtcp if owner_kind == "local" else media.audio_rtcp["left"]
        owner.send_report()
        raw, _address = await asyncio.wait_for(lab.hass.loop.sock_recvfrom(control, 4096), 2)
        assert loss_block(raw) == -1  # expected2 minus actually received3 (RFC3550 A.3).
        assert owner.reception.received == 3
    finally:
        await media.stop()
        reservation.release()
        for sock in (incoming, control, destination, destination_control):
            sock.close()


async def test_browser_udp_duplicate_rr_preserves_the_same_two_pcm_frames(
        lab, monkeypatch, aiohttp_client, hass_admin_user):
    from homeassistant.helpers import entity_registry as er
    from custom_components.voip_stack import audio_ws_view, softphone_originate
    from custom_components.voip_stack.audio_ws import decode_audio_frame
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
    from custom_components.voip_stack.websocket_api import _ha_softphone_state
    entity = er.async_get(lab.hass).async_get_or_create("event", "voip_stack", "duplicate-browser")
    endpoint = PhoneEndpoint(endpoint_id="duplicate-browser", name="Duplicate browser", username="duplicate-browser",
        device_id="duplicate-browser-device", kind="browser", availability="available", entity_ids={entity.entity_id})
    lab.runtime.endpoints.register(endpoint)
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    phone = lab.phones[0]
    phone.auto_answer = False
    control = datagram()
    @web.middleware
    async def authenticate(request, handler):
        request["hass_user"] = hass_admin_user
        return await handler(request)
    app = web.Application(middlewares=[authenticate])
    app["hass"] = lab.hass
    app.router.add_get("/api/voip_stack/ws", audio_ws_view.VoipAudioWebSocketView().get)
    http = await aiohttp_client(app)
    call_id, ws = "", None
    try:
        await lab.hass.services.async_call("voip_stack", "call", {"device_id": endpoint.device_id,
            "destination": "bedroom0", "media_client_id": "duplicate-browser-owner"}, blocking=True,
            context=Context(user_id=hass_admin_user.id))
        call_id = lab.runtime.endpoints.require(endpoint.endpoint_id).active_call_id
        client = lab.calls.sip_client_for(call_id)
        await asyncio.wait_for(phone.invited.wait(), 2)
        request = phone.last_request
        headers = [(name, request.header(name)) for name in ("Via", "From", "Call-ID", "CSeq")]
        headers += [("To", request.header("To") + ";tag=duplicate"), ("Content-Type", "application/sdp"),
            ("Contact", f"<sip:phone@127.0.0.1:{phone.transport.get_extra_info('sockname')[1]}>")]
        body = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Duplicate\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
            f"m=audio {phone.rtp_socket.getsockname()[1]} RTP/AVP 96\r\na=rtpmap:96 L16/16000\r\n"
            f"a=rtcp:{control.getsockname()[1]} IN IP4 127.0.0.1\r\na=ptime:20\r\na=sendrecv\r\n").encode()
        phone.transport.sendto(sip.build_response(200, "OK", headers, body), phone.remote)
        await wait_for(lambda: _ha_softphone_state(lab.hass, endpoint.endpoint_id).get("state") == "in_call")
        ws = await http.ws_connect("/api/voip_stack/ws?endpoint_id=" + endpoint.endpoint_id
            + "&call_id=" + call_id + "&client_id=duplicate-browser-owner")
        await ws.send_json({"type": "audio_ready"})
        await wait_for(lambda: (call_id, endpoint.endpoint_id) in lab.runtime.media.sessions_for("audio"))
        media = lab.runtime.media.sessions_for("audio")[(call_id, endpoint.endpoint_id)]
        await wait_for(lambda: media.audio_state is not None and media.audio_rtcp is not None)
        pcm = []
        media.audio_state.receiver.add_pcm_observer(lambda frame, _format: pcm.append(frame))
        for sequence, value in [(10, 1000), (11, 2000)]:
            await lab.hass.loop.sock_sendto(phone.rtp_socket, packet(sequence, value, client.dialog.recv_format.payload_type), ("127.0.0.1", client.local_rtp_port))
        delivered = []
        while len(delivered) < 2:
            message = await ws.receive(timeout=2)
            if message.type == WSMsgType.BINARY:
                delivered.append(decode_audio_frame(message.data))
        await lab.hass.loop.sock_sendto(phone.rtp_socket, packet(11, 2000, client.dialog.recv_format.payload_type), ("127.0.0.1", client.local_rtp_port))
        await wait_for(lambda: not media.audio_state.receiver.last_packet_accepted)
        assert b"".join(pcm) == struct.pack("<640h", *([1000] * 320 + [2000] * 320))
        with suppress(asyncio.TimeoutError):
            message = await ws.receive(timeout=.05)
            assert message.type != WSMsgType.BINARY
        owner = media.audio_rtcp
        owner.send_report()
        raw, _address = await asyncio.wait_for(lab.hass.loop.sock_recvfrom(control, 4096), 2)
        assert loss_block(raw) == -1 and owner.reception.received == 3
    finally:
        if call_id:
            await lab.hass.services.async_call("voip_stack", "hangup", {"device_id": endpoint.device_id,
                "call_id": call_id}, blocking=True, context=Context(user_id=hass_admin_user.id))
        if ws is not None:
            await ws.close()
        await http.close()
        control.close()
        await lab.hass.async_block_till_done()
    assert_call_resources_released(lab)
