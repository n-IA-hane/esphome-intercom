"""Negotiated RFC 3556 silence preserves audio and the existing socket owner."""
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

from .test_trunk_calls_ha import trunks as trunks
from .test_queue_call_ha import queue_lab as queue_lab
from .test_media_transfer_audit_ha import card_accounts as card_accounts

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
async def test_negotiated_disabled_initially_keeps_pcm_and_no_control_packets(lab, owner_kind):
    from custom_components.voip_stack.local_call_media import LocalAudioContract, LocalCallMedia
    from custom_components.voip_stack.media_ports import RtpPortReservation
    from custom_components.voip_stack.sip_rtp_bridge import RtpPeer, SipRtpRelay
    wire = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
    incoming, control, destination, destination_control = (datagram() for _ in range(4))
    reservation = RtpPortReservation.allocate(lab.hass)
    pcm = []
    if owner_kind == "local":
        contract = LocalAudioContract("duplicate-local", "127.0.0.1", incoming.getsockname()[1], wire, wire,
            local_audio_direction="sendrecv", source_host="127.0.0.1", audio_rtcp_min_interval=5, audio_rtcp_allowed=False,
            remote_audio_rtcp_host="127.0.0.1", remote_audio_rtcp_port=control.getsockname()[1])
        media = LocalCallMedia(lab.hass, invite=contract, local_rtp_port=reservation.ports[0],
                              reservation=reservation, on_complete=AsyncMock())
        media.decoder.add_pcm_observer(lambda frame, _format: pcm.append(frame))
    else:
        def peer(sock, rtcp):
            return RtpPeer("127.0.0.1", sock.getsockname()[1], 96, AudioFormat(16000),
            rtp_format=wire, send_rtp_format=wire, rtcp_host="127.0.0.1", rtcp_port=rtcp.getsockname()[1],
            rtcp_minimum_interval=5, audio_rtcp_allowed=False)
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
        assert media.rtcp is None if owner_kind == "local" else not media.audio_rtcp
        if owner_kind == "local":
            media.tx_queue.put_nowait(struct.pack("<320h", *([3000] * 320)))
        else:
            await lab.hass.loop.sock_sendto(destination, packet(20, 3000), ("127.0.0.1", media.right_port))
        async with asyncio.timeout(2):
            while True:
                outbound, _address = await lab.hass.loop.sock_recvfrom(incoming, 4096)
                if any(rtp.parse_packet(outbound).payload):
                    break
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(lab.hass.loop.sock_recvfrom(control, 4096), .05)
        # Commit the established owner's real negotiated update, then pause and
        # restore that same reporter without releasing its descriptor.
        from dataclasses import replace
        if owner_kind == "local":
            media.prepare_media_update(replace(contract, audio_rtcp_allowed=True))()
            await wait_for(lambda: media.rtcp is not None and media.rtcp.transport is not None)
            owner = media.rtcp
        else:
            media.prepare_peer_reconfiguration("left", replace(media.left, audio_rtcp_allowed=True))()
            await wait_for(lambda: "left" in media.audio_rtcp and media.audio_rtcp["left"].transport is not None)
            owner = media.audio_rtcp["left"]
        transport = owner.transport
        fd = transport.get_extra_info("socket").fileno()
        if owner_kind == "local":
            media.prepare_media_update(replace(media.invite, audio_rtcp_allowed=False))()
        else:
            media.prepare_peer_reconfiguration("left", replace(media.left, audio_rtcp_allowed=False))()
        owner.send_report(bye=True)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(lab.hass.loop.sock_recvfrom(control, 4096), .05)
        if owner_kind == "local":
            media.prepare_media_update(replace(media.invite, audio_rtcp_allowed=True))()
        else:
            media.prepare_peer_reconfiguration("left", replace(media.left, audio_rtcp_allowed=True))()
        assert owner.transport is transport and transport.get_extra_info("socket").fileno() == fd
        owner.send_report()
        raw, _address = await asyncio.wait_for(lab.hass.loop.sock_recvfrom(control, 4096), 2)
        assert raw[1] in {200, 201}
        owner.set_reporting_allowed(False)
    finally:
        await media.stop()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(lab.hass.loop.sock_recvfrom(control, 4096), .05)
        reservation.release()
        for sock in (incoming, control, destination, destination_control):
            sock.close()


async def test_browser_b0_preserves_prebound_fd_and_duplex_pcm_without_rtcp(
        lab, monkeypatch, aiohttp_client, hass_admin_user):
    from homeassistant.helpers import entity_registry as er
    from custom_components.voip_stack import audio_ws_view, softphone_originate
    from custom_components.voip_stack.audio_ws import decode_audio_frame, encode_audio_frame
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
            f"m=audio {phone.rtp_socket.getsockname()[1]} RTP/AVP 96\r\nb=RS:0\r\nb=RR:0\r\na=rtpmap:96 L16/16000\r\n"
            f"a=rtcp:{control.getsockname()[1]} IN IP4 127.0.0.1\r\na=ptime:20\r\na=sendrecv\r\n").encode()
        phone.transport.sendto(sip.build_response(200, "OK", headers, body), phone.remote)
        await wait_for(lambda: _ha_softphone_state(lab.hass, endpoint.endpoint_id).get("state") == "in_call")
        assert client.dialog.audio_rtcp_allowed is False
        prebound = client.audio_rtcp_socket
        assert prebound is not None and prebound.fileno() >= 0
        prebound_fd = prebound.fileno()
        ws = await http.ws_connect("/api/voip_stack/ws?endpoint_id=" + endpoint.endpoint_id
            + "&call_id=" + call_id + "&client_id=duplicate-browser-owner")
        await ws.send_json({"type": "audio_ready"})
        await wait_for(lambda: (call_id, endpoint.endpoint_id) in lab.runtime.media.sessions_for("audio"))
        media = lab.runtime.media.sessions_for("audio")[(call_id, endpoint.endpoint_id)]
        await wait_for(lambda: media.audio_state is not None)
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
        assert media.audio_rtcp is None
        assert client.audio_rtcp_socket is prebound and prebound.fileno() == prebound_fd
        # Browser microphone still reaches the SIP peer while reports are off.
        await ws.send_bytes(encode_audio_frame(struct.pack("<960h", *([3000] * 960))))
        outbound, _address = await asyncio.wait_for(lab.hass.loop.sock_recvfrom(phone.rtp_socket, 4096), 2)
        assert rtp.parse_packet(outbound).payload
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(lab.hass.loop.sock_recvfrom(control, 4096), .05)
        # Exercise the actual SIP remote-offer transaction and ACK, including
        # parsed dialog policy, browser generation and unchanged descriptor.
        responses = asyncio.Queue()
        original_receive = phone.datagram_received
        def capture(raw, address):
            reply = sip.parse_message(raw)
            if reply.is_response and reply.header("CSeq").endswith(" INVITE"):
                responses.put_nowait(reply)
            else:
                original_receive(raw, address)
        monkeypatch.setattr(phone, "datagram_received", capture)
        sequence = 1
        async def commit(allowed):
            nonlocal sequence
            sequence += 1
            local_port = phone.transport.get_extra_info("sockname")[1]
            headers = [("Via", f"SIP/2.0/UDP 127.0.0.1:{local_port};branch=z9hG4bK-rtcp-{sequence};rport"),
                ("From", request.header("To") + ";tag=duplicate"), ("To", request.header("From")),
                ("Call-ID", call_id), ("CSeq", f"{sequence} INVITE"),
                ("Contact", f"<sip:phone@127.0.0.1:{local_port}>"), ("Content-Type", "application/sdp")]
            offer = body.replace(b"b=RS:0\r\nb=RR:0\r\n", b"") if allowed else body
            target = sip.name_addr_uri(request.header("Contact"))
            phone.transport.sendto(sip.build_request("INVITE", target, headers, offer), phone.remote)
            async with asyncio.timeout(2):
                while True:
                    reply = await responses.get()
                    if reply.status_code >= 200:
                        break
            assert reply.status_code == 200
            ack_headers = [(key, value) for key, value in headers if key not in {"Content-Type", "CSeq", "To"}]
            ack_headers += [("To", reply.header("To")), ("CSeq", f"{sequence} ACK")]
            phone.transport.sendto(sip.build_request("ACK", target, ack_headers), phone.remote)
            await wait_for(lambda: client.dialog.audio_rtcp_allowed is allowed and media.audio_rtcp_allowed is allowed)
        await commit(True)
        await wait_for(lambda: media.audio_rtcp is not None and media.audio_rtcp.transport is not None)
        owner = media.audio_rtcp
        assert owner.bound_socket is prebound and client.audio_rtcp_socket is None
        transport = owner.transport
        await commit(False)
        await wait_for(lambda: not owner.reporting_allowed)
        owner.send_report(bye=True)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(lab.hass.loop.sock_recvfrom(control, 4096), .05)
        await commit(True)
        await wait_for(lambda: owner.reporting_allowed)
        assert media.audio_rtcp is owner and owner.transport is transport
        assert prebound.fileno() == prebound_fd
        owner.send_report()
        raw, _address = await asyncio.wait_for(lab.hass.loop.sock_recvfrom(control, 4096), 2)
        assert raw[1] in {200, 201}
        await commit(False)
        await wait_for(lambda: not owner.reporting_allowed)
    finally:
        if call_id:
            await lab.hass.services.async_call("voip_stack", "hangup", {"device_id": endpoint.device_id,
                "call_id": call_id}, blocking=True, context=Context(user_id=hass_admin_user.id))
        if ws is not None:
            await ws.close()
        await http.close()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(lab.hass.loop.sock_recvfrom(control, 4096), .05)
        assert prebound.fileno() == -1
        control.close()
        await lab.hass.async_block_till_done()
    assert_call_resources_released(lab)




@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_incoming_uas_reinvite_bandwidth_updates_existing_browser_owner(
        queue_lab, card_accounts, aiohttp_client, hass_admin_user):
    from custom_components.voip_stack import audio_ws_view
    from custom_components.voip_stack.core import sip
    runtime = queue_lab
    card = await card_accounts("rtcp-uas-card")
    from homeassistant.helpers import entity_registry as er
    entity = er.async_get(runtime.hass).async_get_or_create("event", "voip_stack", "rtcp-uas-card-event",
        device_id=card.device_id, config_entry=runtime.hass.config_entries.async_entries("voip_stack")[0])
    card = runtime.runtime.endpoints.update(card.endpoint_id, entity_ids={entity.entity_id})
    caller, audio = await runtime.caller()
    control = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    control.bind(("127.0.0.2", audio.getsockname()[1] + 1))
    control.setblocking(False)
    caller.audio_rtcp_min_interval = 5
    call_id, ws = "", None
    @web.middleware
    async def authenticate(request, handler):
        request["hass_user"] = hass_admin_user
        return await handler(request)
    app = web.Application(middlewares=[authenticate])
    app["hass"] = runtime.hass
    app.router.add_get("/api/voip_stack/ws", audio_ws_view.VoipAudioWebSocketView().get)
    http = await aiohttp_client(app)
    try:
        assert await caller.invite(target=card.username, remote_host="127.0.0.1",
            remote_sip_port=runtime.runtime.transport_config["sip_port"], timeout=3) == "ringing"
        call_id = caller.dialog_ids.call_id
        await runtime.hass.services.async_call("voip_stack", "answer", {
            "device_id": card.device_id, "call_id": call_id, "media_client_id": "rtcp-uas-browser-owner"}, blocking=True)
        assert await caller.wait_for_final(timeout=3) == "in_call"
        ws = await http.ws_connect("/api/voip_stack/ws?endpoint_id=" + card.endpoint_id
            + "&call_id=" + call_id + "&client_id=rtcp-uas-browser-owner")
        await ws.send_json({"type": "audio_ready"})
        await wait_for(lambda: (call_id, card.endpoint_id) in runtime.runtime.media.sessions_for("audio"))
        media = runtime.runtime.media.sessions_for("audio")[(call_id, card.endpoint_id)]
        await wait_for(lambda: media.audio_rtcp is not None and media.audio_rtcp.transport is not None)
        owner = media.audio_rtcp
        transport = owner.transport
        fd = transport.get_extra_info("socket").fileno()
        dialog = caller.dialog
        sequence = 100
        async def reoffer(allowed):
            nonlocal sequence
            sequence += 1
            headers = [("Via", f"SIP/2.0/UDP 127.0.0.2:{caller.local_sip_port};branch=z9hG4bK-uas-rtcp-{sequence};rport"),
                ("From", f"<{dialog.local_uri}>;tag={caller.dialog_ids.local_tag}"),
                ("To", f"<{dialog.remote_uri}>;tag={dialog.remote_tag}"),
                ("Call-ID", call_id), ("CSeq", f"{sequence} INVITE"),
                ("Contact", f"<sip:source@127.0.0.2:{caller.local_sip_port}>"), ("Content-Type", "application/sdp")]
            body = dialog.local_sdp_body.replace("b=RS:0\r\n", "").replace("b=RR:0\r\n", "")
            if not allowed:
                body = body.replace("m=audio", "b=RS:0\r\nb=RR:0\r\nm=audio", 1)
            caller.transport.sendto(sip.build_request("INVITE", dialog.remote_target_uri, headers, body.encode()),
                ("127.0.0.1", runtime.runtime.transport_config["sip_port"]))
            async with asyncio.timeout(2):
                while True:
                    raw, _address = await caller.queue.get()
                    reply = sip.parse_message(raw)
                    if reply.is_response and reply.header("CSeq") == f"{sequence} INVITE" and reply.status_code >= 200:
                        break
            assert reply.status_code == 200
            from custom_components.voip_stack.core.sdp_parser import audio_rtcp_allowed
            assert audio_rtcp_allowed(reply.body) is allowed
            ack = [(key, value) for key, value in headers if key not in {"Content-Type", "CSeq"}]
            ack.append(("CSeq", f"{sequence} ACK"))
            caller.transport.sendto(sip.build_request("ACK", dialog.remote_target_uri, ack),
                ("127.0.0.1", runtime.runtime.transport_config["sip_port"]))
            await wait_for(lambda: media.audio_rtcp_allowed is allowed and owner.reporting_allowed is allowed)
        await reoffer(False)
        owner.send_report(bye=True)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(runtime.hass.loop.sock_recvfrom(control, 4096), .05)
        await reoffer(True)
        assert media.audio_rtcp is owner and owner.transport is transport
        assert transport.get_extra_info("socket").fileno() == fd
        owner.send_report()
        raw, _address = await asyncio.wait_for(runtime.hass.loop.sock_recvfrom(control, 4096), 2)
        assert raw[1] in {200, 201}
        await reoffer(False)
    finally:
        if call_id and runtime.calls.get_session(call_id) is not None:
            await runtime.hass.services.async_call("voip_stack", "hangup", {
                "device_id": card.device_id, "call_id": call_id}, blocking=True)
        if ws is not None:
            await ws.close()
        await http.close()
        control.close()
        await runtime.hass.async_block_till_done()
    assert_call_resources_released(runtime)
