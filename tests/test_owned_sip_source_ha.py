"""Real RTP codec continuity and the card's existing SIP source acquisition."""

import asyncio
from dataclasses import replace
import math
import socket
import struct

import pytest

from custom_components.voip_stack import audio_ws_view, media_ports
from custom_components.voip_stack.core import rtp, sdp
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.core.audio_pcm import PcmFrameConverter
from custom_components.voip_stack.core.rtp_payload import RtpPayloadEncoder
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.local_call_media import LocalCallMedia
from custom_components.voip_stack.local_media_bridge import LocalMediaBridge
from custom_components.voip_stack.owned_sip_source import OwnedSipSource
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.rtp_audio_handoff import RtpAudioHandoff
from custom_components.voip_stack.rtp_audio_receiver import RtpAudioReceiver
from .test_automation_call import application as application
from .test_browser_pcm_edge_ha import WebSocketBoundary
from .test_queue_call_ha import queue_lab as queue_lab, wait_until
from .test_trunk_calls_ha import trunks as trunks, lab as lab

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("control", ["", "source", "ambiguous"], ids=["no-card", "sip-control", "ambiguous"])
async def test_source_capture_requires_an_actual_card_claim(queue_lab, control):
    runtime = queue_lab
    session = runtime.calls.upsert("capture-admission", owner="ha_softphone", state="in_call")
    try:
        if control == "ambiguous":
            for identity in ("capture-card-a", "capture-card-b"):
                runtime.runtime.endpoints.register(PhoneEndpoint(endpoint_id=identity,
                    name=identity, kind=EndpointKind.BROWSER, availability="available"))
                runtime.calls.claim_endpoint(session.call_id, identity, role="destination")
            with pytest.raises(RuntimeError, match="no unique card owner"):
                OwnedSipSource.capture(runtime.hass, session)
        else:
            if control:
                runtime.calls.claim_endpoint(session.call_id, control, role="caller")
            assert OwnedSipSource.capture(runtime.hass, session, control) is None
        assert runtime.calls.resource_for(session.call_id, "owned_sip_source") is None
    finally:
        await runtime.calls.request_termination(session.call_id, TerminationIntent("test_cleanup"))


def tone(fmt, *, offset=0):
    samples = [round(10000 * math.sin(2 * math.pi * 650 * (offset + i) / fmt.sample_rate))
               for i in range(fmt.nominal_frame_samples)]
    return b"".join(struct.pack("<h", sample) * fmt.channels for sample in samples)


@pytest.mark.parametrize("fmt", [sdp.RtpPcmFormat(110, "L16", 16000, 1, 20),
    sdp.RtpPcmFormat(9, "G722", 8000, 1, 20), sdp.RtpPcmFormat(111, "OPUS", 48000, 2, 20)],
    ids=["L16", "G722", "Opus"])
async def test_handoff_preserves_actual_stateful_codecs_and_retargets_pcm(application, fmt):
    """The native codec continues the same samples; RTP clock is not PCM rate."""
    app, _registry, peer, _released = application
    browser_output = AudioFormat(48000, "s16le", 2, 10)
    browser_input = AudioFormat(48000, "s16le", 1, 20)
    capture = replace(browser_input, frame_ms=10)
    receiver = RtpAudioReceiver(fmt, output_pcm=browser_output)
    reference = RtpAudioReceiver(fmt)
    remote = RtpPayloadEncoder(fmt)
    encoder = RtpPayloadEncoder(fmt, input_format=browser_input)
    tx_converter = PcmFrameConverter(capture, browser_input)
    sender = rtp.AudioRtpSenderState(501, 123456, 0x102030, fmt.rtp_clock_rate)
    media = LocalCallMedia(app.hass, invite=replace(app.invite,
        send_format=fmt, recv_format=fmt, recv_formats=(fmt,), send_formats=(fmt,)),
        local_rtp_port=app.invite.remote_rtp_port, reservation=media_ports.RtpPortReservation.allocate(app.hass),
        on_complete=lambda _reason: None)
    try:
        encoder.encode(tone(browser_input))
        assert tx_converter.convert(tone(capture)) == []
        for index in range(4):
            packet = rtp.RtpPacket(fmt.payload_type, 100 + index, index * fmt.rtp_timestamp_step,
                0x998877, remote.encode(tone(fmt.audio_format, offset=index * fmt.audio_format.nominal_frame_samples)))
            receiver.decode_rtp(packet, normalize_frames=True)
            reference.decode_rtp(packet, normalize_frames=True)
        native_decoder = receiver._decoders[fmt.payload_type]
        native_encoder = encoder._codec
        admission = receiver.receive_state
        state = RtpAudioHandoff(receiver, encoder, None, sender, tx_converter,
            "127.0.0.1", peer.getsockname()[1])
        media.adopt_audio_state(state)
        assert media.decoder is receiver and media.encoder is encoder
        assert receiver._decoders[fmt.payload_type] is native_decoder
        assert encoder._codec is native_encoder
        assert receiver.receive_state is admission and receiver.source_ssrc == 0x998877
        assert receiver.output_pcm == fmt.audio_format
        assert encoder.input_format == fmt.audio_format
        assert media.rx_converter.src == fmt.audio_format
        assert media.tx_converter.dst == fmt.audio_format
        assert media.rtp_source is sender and sender.clock_rate == fmt.rtp_clock_rate
        assert (sender.sequence, sender.timestamp, sender.ssrc) == (501, 123456, 0x102030)
        # The retired 10 ms capture remains a typed half-quantum before the
        # application's next 20 ms frame, including stereo Opus and G.722.
        frames = media.tx_converter.convert(tone(media.tx_converter.src))
        assert len(frames) == 1 and len(frames[0]) == fmt.audio_format.nominal_frame_bytes
        assert len(media.tx_converter.drain_pending_pcm()) == fmt.audio_format.nominal_frame_bytes // 2
        for index in range(4, 8):
            packet = rtp.RtpPacket(fmt.payload_type, 100 + index, index * fmt.rtp_timestamp_step,
                0x998877, remote.encode(tone(fmt.audio_format, offset=index * fmt.audio_format.nominal_frame_samples)))
            actual = receiver.decode_rtp(packet, normalize_frames=True)
            expected = reference.decode_rtp(packet, normalize_frames=True)
            assert actual == expected and any(any(frame) for frame in actual)
        assert receiver._decoders[fmt.payload_type] is native_decoder
        assert encoder._codec is native_encoder
    finally:
        media.release_reservation_on_stop = False
        await media.stop()
        remote.close()
        reference.close()


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("incoming", [False, True], ids=["UAC", "UAS"])
@pytest.mark.parametrize("attached", [False, True], ids=["noWS", "activeWS"])
async def test_owned_source_stages_then_moves_same_udp_port_and_closes_once(queue_lab, incoming, attached):
    """Actual SIP dialogs and UDP transport survive staged rollback and WS exit."""
    runtime = queue_lab
    card = PhoneEndpoint(endpoint_id="owned-source-card", username="owned-source-card", name="Source card",
        device_id="owned-source-device", kind=EndpointKind.BROWSER, availability="available",
        capabilities=frozenset({"audio", "dtmf"}))
    runtime.runtime.endpoints.register(card)
    runtime.roster.append(RosterEntry(card.username, name=card.name,
        metadata={"local_ha": True, "endpoint_id": card.endpoint_id}))
    runtime.runtime.directory.replace(runtime.roster)
    worker = prepared = None
    destination = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    destination.bind(("127.0.0.1", 0))
    destination.setblocking(False)
    source_client = None
    try:
        if incoming:
            source_client, audio = await runtime.caller()
            assert await source_client.invite(target=card.username, remote_host="127.0.0.1",
                remote_sip_port=runtime.runtime.transport_config["sip_port"], timeout=3) == "ringing"
            call_id = source_client.dialog_ids.call_id
            await runtime.hass.services.async_call("voip_stack", "answer", {"device_id": card.device_id,
                "call_id": call_id, "media_client_id": "owned-source-browser"}, blocking=True)
            assert await source_client.wait_for_final(timeout=3) == "in_call"
        else:
            await runtime.hass.services.async_call("voip_stack", "call", {"device_id": card.device_id,
                "destination": "bedroom0", "media_client_id": "owned-source-browser"}, blocking=True)
            call_id = runtime.runtime.endpoints.require(card.endpoint_id).active_call_id
            audio = runtime.phones[0].rtp_socket
        await wait_until(lambda: runtime.calls.get_session(call_id).state == "in_call")
        session = runtime.calls.get_session(call_id)
        if attached:
            connection = audio_ws_view._active_softphone_media_session(runtime.hass, card.endpoint_id)
            client = runtime.calls.sip_client_for(connection.call_id)
            if client is not None and client.audio_rtp_socket is connection.rtp_socket:
                client.audio_rtp_socket = None  # Same boundary as the real HTTP handler.
            browser = WebSocketBoundary()
            worker = asyncio.create_task(audio_ws_view._run_audio_session(runtime.hass, browser,
                connection, endpoint_id=card.endpoint_id))
            await wait_until(lambda: connection.handoff_transport is not None)
        source = OwnedSipSource.capture(runtime.hass, session, card.endpoint_id)
        assert source is not None
        descriptor = runtime.calls.resource_for(call_id, "softphone_media")
        fmt = source.invite.recv_format
        def packet(index):
            return rtp.build_packet(rtp.RtpPacket(fmt.payload_type, 100 + index,
                index * fmt.rtp_timestamp_step, 0x552299,
                struct.pack(f"!{fmt.audio_format.nominal_frame_samples}h", *([1700] * fmt.audio_format.nominal_frame_samples))))
        if attached:
            await runtime.hass.loop.sock_sendto(audio, packet(0), ("127.0.0.1", source.local_port))
            await asyncio.wait_for(browser.audio.get(), 2)
            old_receiver, old_encoder = connection.audio_state.receiver, connection.audio_state.encoder
        target = replace(source.invite, call_id="independent-media-target", remote_rtp_host="127.0.0.1",
            remote_rtp_port=destination.getsockname()[1], source_host="127.0.0.1")
        abandoned = media_ports.RtpPortReservation.allocate(runtime.hass)
        prepared = await LocalMediaBridge.prepare_owned_source(source, target, abandoned)
        assert not source.adopted and card.endpoint_id in session.endpoint_claims
        assert runtime.calls.resource_for(call_id, "softphone_media") is descriptor
        if not attached:
            from custom_components.voip_stack.websocket_owner import MediaWebSocketOwner

            contested_ports = media_ports.RtpPortReservation.allocate(runtime.hass)
            contested = await LocalMediaBridge.prepare_owned_source(source, target, contested_ports)
            owner_key = f"{card.endpoint_id}|{call_id}"
            late = MediaWebSocketOwner()
            runtime.runtime.media.owners_for("audio")[owner_key] = late
            try:
                with pytest.raises(RuntimeError, match="connection started"):
                    await contested.acquire_source()
                assert not source.adopted and card.endpoint_id in session.endpoint_claims
                assert runtime.calls.resource_for(call_id, "softphone_media") is descriptor
            finally:
                runtime.runtime.media.owners_for("audio").pop(owner_key, None)
                late.released.set()
                await contested.rollback()
                contested_ports.release()
        await prepared.rollback()
        abandoned.release()
        assert not source.adopted and card.endpoint_id in session.endpoint_claims
        assert runtime.calls.resource_for(call_id, "softphone_media") is descriptor
        staged = media_ports.RtpPortReservation.allocate(runtime.hass)
        prepared = await LocalMediaBridge.prepare_owned_source(source, target, staged)
        await prepared.acquire_source()
        relay = prepared.commit()
        runtime.calls.attach_relay(call_id, relay)
        source.release_to_session(relay)
        assert runtime.calls.resource_for(call_id, "owned_sip_source") is None
        await source.close("retired_capability")
        assert not relay.left_transport.is_closing()
        assert source.adopted and card.endpoint_id not in session.endpoint_claims
        assert runtime.calls.resource_for(call_id, "softphone_media") is None
        assert relay.left_port == source.local_port
        if attached:
            await asyncio.wait_for(worker, 3)
            assert relay.left_decoder is old_receiver and relay.left_encoder is old_encoder
            assert relay.left_decoder.source_ssrc == 0x552299
        await runtime.hass.loop.sock_sendto(audio, packet(1), ("127.0.0.1", source.local_port))
        async with asyncio.timeout(3):
            while True:
                received = rtp.parse_packet(await runtime.hass.loop.sock_recv(destination, 4096))
                if any(received.payload):
                    break
        assert received.payload_type == target.send_format.payload_type
        assert session.live and len([item for item in session.resources if item.name.startswith("relay:")]) == 1
        await runtime.calls.request_termination(call_id, TerminationIntent("test_complete"))
        await wait_until(lambda: not runtime.calls.sessions)
        assert not runtime.runtime.rtp_port_pool.get("used")
    finally:
        if prepared is not None:
            await prepared.rollback()
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        for session in tuple(runtime.calls.sessions.values()):
            if session.live:
                await runtime.calls.request_termination(session.call_id, TerminationIntent("test_cleanup"))
        destination.close()


def test_receiver_retarget_carries_partial_native_pcm_without_reinterpreting_48k():
    fmt = sdp.RtpPcmFormat(110, "L16", 16000, 1, 20)
    receiver = RtpAudioReceiver(fmt, output_pcm=AudioFormat(48000, "s16le", 2, 10))
    output = []
    try:
        def packet(sequence, start, samples):
            values = [round(10000 * math.sin(2 * math.pi * 650 * (start + i) / 16000)) for i in range(samples)]
            return rtp.RtpPacket(110, sequence, start, 0x123456, struct.pack(f"!{samples}h", *values))
        assert receiver.decode_rtp(packet(1, 0, 160), normalize_frames=True) == []
        native = receiver._decoders[110]
        admission = receiver.receive_state
        output.extend(receiver.retarget_output(fmt.audio_format))
        output.extend(receiver.decode_rtp(packet(2, 160, 320), normalize_frames=True))
        output.extend(receiver.decode_rtp(packet(3, 480, 160), normalize_frames=True))
        assert [len(frame) for frame in output] == [640, 640]
        assert receiver._decoders[110] is native and receiver.receive_state is admission
        assert receiver.output_pcm == fmt.audio_format and receiver.source_ssrc == 0x123456
        assert receiver._next_timestamp == 640
        samples = struct.unpack("<640h", b"".join(output))
        stable = samples[160:]
        def power(frequency):
            return abs(sum(value * complex(math.cos(2 * math.pi * frequency * i / 16000),
                math.sin(2 * math.pi * frequency * i / 16000)) for i, value in enumerate(stable)))
        assert power(650) > 8 * max(power(450), power(900))
    finally:
        receiver.close()


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_public_uac_transfer_to_browser_keeps_real_sip_peer_and_duplex_audio(queue_lab, monkeypatch):
    """Call/transfer/answer exercise the actual UAC source DTO and browser handoff."""
    from aiohttp import WSMsgType
    from types import SimpleNamespace
    from custom_components.voip_stack.audio_ws import encode_audio_frame
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.core.audio_format import parse_audio_format_token
    from custom_components.voip_stack.websocket_owner import MediaWebSocketOwner, async_release_media_owner
    from .test_browser_paging_ha import Browser

    runtime = queue_lab
    operator = PhoneEndpoint(endpoint_id="uac-transfer-operator", username="uac-transfer-operator",
        name="UAC operator", device_id="uac-operator-device", kind=EndpointKind.BROWSER,
        availability="available", capabilities=frozenset({"audio", "dtmf"}))
    recipient = PhoneEndpoint(endpoint_id="uac-transfer-recipient", username="uac-transfer-recipient",
        name="UAC recipient", device_id="uac-recipient-device", kind=EndpointKind.BROWSER,
        availability="available", capabilities=frozenset({"audio", "dtmf"}))
    for endpoint in (operator, recipient):
        runtime.runtime.endpoints.register(endpoint)
        runtime.runtime.softphone_presence[endpoint.endpoint_id] = 1
        runtime.roster.append(RosterEntry(endpoint.username, name=endpoint.name,
            metadata={"local_ha": True, "endpoint_id": endpoint.endpoint_id}))
    runtime.runtime.directory.replace(runtime.roster)
    phone = runtime.phones[0]
    received_refers = []
    original_receive = phone.datagram_received
    def receive(raw, address):
        message = sip.parse_message(raw)
        if message.method == "REFER":
            received_refers.append(message)
            phone.transport.sendto(sip.build_uas_response(message, 405, "Method Not Allowed"), address)
        else:
            original_receive(raw, address)
    monkeypatch.setattr(phone, "datagram_received", receive)
    transfer = worker = None
    try:
        await runtime.hass.services.async_call("voip_stack", "call", {"device_id": operator.device_id,
            "destination": "bedroom0", "media_client_id": "uac-operator-client"}, blocking=True)
        call_id = runtime.runtime.endpoints.require(operator.endpoint_id).active_call_id
        await wait_until(lambda: runtime.calls.get_session(call_id).state == "in_call")
        session = runtime.calls.get_session(call_id)
        client = runtime.calls.sip_client_for(call_id)
        original_dialog, original_port = client.dialog, client.dialog.local_rtp_port
        transfer = asyncio.create_task(runtime.hass.services.async_call("voip_stack", "transfer", {
            "device_id": operator.device_id, "call_id": call_id, "destination": recipient.username},
            blocking=True, return_response=True))
        await wait_until(lambda: runtime.runtime.softphones.get(recipient.endpoint_id, {}).get("state") == "ringing")
        await runtime.hass.services.async_call("voip_stack", "answer", {"device_id": recipient.device_id,
            "call_id": call_id, "media_client_id": "uac-recipient-client"}, blocking=True)
        await wait_until(lambda: runtime.calls.resource_for(call_id,
            f"softphone_media_endpoint:{recipient.endpoint_id}") is not None)
        connection = audio_ws_view._active_softphone_media_session(runtime.hass, recipient.endpoint_id)
        assert connection is not None
        browser = Browser()
        owner_key = f"{recipient.endpoint_id}|{call_id}"
        owner = MediaWebSocketOwner(websocket=browser, client_id="uac-recipient-client")
        runtime.runtime.media.owners_for("audio")[owner_key] = owner
        playback = runtime.calls.resource_for(call_id, "browser_playback")
        playback.attach(browser)
        async def run_browser():
            try:
                await audio_ws_view._run_audio_session(runtime.hass, browser, connection,
                    endpoint_id=recipient.endpoint_id, playback=playback,
                    handoff_requested=owner.handoff_requested)
            finally:
                playback.detach(browser)
                await async_release_media_owner(runtime.runtime.media.owners_for("audio"),
                    runtime.runtime.media.owner_lock_for("audio"), owner_key, owner)
        worker = asyncio.create_task(run_browser())
        await wait_until(lambda: bool(browser.contracts))
        browser.ready()
        response = await asyncio.wait_for(transfer, 8)
        assert response["success"], response
        assert not received_refers and len(phone.invites) == 1
        assert client.dialog is original_dialog and runtime.calls.source_sip_client_for(call_id) is client
        assert operator.endpoint_id not in session.endpoint_claims and recipient.endpoint_id in session.endpoint_claims
        assert runtime.calls.resource_for(call_id, "owned_sip_source") is None
        relay = runtime.calls.resource_for(call_id, "relay")
        assert relay.left_port == original_port and not relay.left_transport.is_closing()
        wire = client.dialog.recv_format
        for index in range(12):
            count = wire.audio_format.nominal_frame_samples
            payload = struct.pack(f"!{count}h", *([2310] * count))
            await runtime.hass.loop.sock_sendto(phone.rtp_socket, rtp.build_packet(rtp.RtpPacket(
                wire.payload_type, 200 + index, index * wire.rtp_timestamp_step, 0x810055, payload)),
                ("127.0.0.1", relay.left_port))
            await asyncio.sleep(wire.frame_ms / 1000)
        await wait_until(lambda: any(any(frame) for frame in browser.frames))
        received = next(frame for frame in reversed(browser.frames) if any(frame))
        assert all(abs(value - 2310) <= 1 for value in struct.unpack(f"<{len(received)//2}h", received)[-64:])
        capture = parse_audio_format_token(browser.contracts[-1]["tx_format"])
        for _ in range(round(160 / capture.frame_ms)):
            browser.incoming.put_nowait(SimpleNamespace(type=WSMsgType.BINARY,
                data=encode_audio_frame(struct.pack(f"<{capture.nominal_frame_samples}h",
                    *([-1230] * capture.nominal_frame_samples)))))
            await asyncio.sleep(capture.frame_ms / 1000)
        async with asyncio.timeout(3):
            while True:
                packet = rtp.parse_packet((await phone.audio.get())[0])
                if any(packet.payload):
                    assert packet.payload_type == client.dialog.send_format.payload_type
                    assert all(abs(value + 1230) <= 1 for value in
                        struct.unpack(f"!{len(packet.payload)//2}h", packet.payload)[-64:])
                    break
        await runtime.hass.services.async_call("voip_stack", "hangup", {
            "device_id": recipient.device_id, "call_id": call_id}, blocking=True)
        await wait_until(lambda: not runtime.calls.sessions)
        await asyncio.wait_for(worker, 3)
        assert owner.released.is_set() and not runtime.runtime.rtp_port_pool.get("used")
    finally:
        for task in (transfer, worker):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (transfer, worker) if task is not None), return_exceptions=True)
        for session in tuple(runtime.calls.sessions.values()):
            if session.live:
                await runtime.calls.request_termination(session.call_id, TerminationIntent("test_cleanup"))
