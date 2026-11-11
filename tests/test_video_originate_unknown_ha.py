"""A compliant independent UDP peer proves unknown-video offer policy publicly."""

import asyncio
import socket
import struct
from dataclasses import replace

import pytest

from custom_components.voip_stack.const import CONF_SIP_VIDEO, CONF_VIDEO_CAMERA_SEND
from custom_components.voip_stack.core import sdp
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
from tests.test_automation_originate_ha import lab as lab

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("policy", ["unknown_compliant", "known_audio_only", "unknown_bad_profile"])
async def test_public_unknown_video_negotiates_without_relaxing_profile_validation(lab, monkeypatch, policy):
    lab.runtime.transport_config.update({CONF_SIP_VIDEO: True, CONF_VIDEO_CAMERA_SEND: False})
    source = PhoneEndpoint(endpoint_id="browser:unknown-video", device_id="unknown-video-browser",
        name="Unknown video browser", kind=EndpointKind.BROWSER,
        capabilities=frozenset({"audio", "dtmf", "video"}))
    lab.runtime.endpoints.register(source)
    capabilities = ["audio", "dtmf"] if policy == "known_audio_only" else []
    lab.roster[1] = replace(lab.roster[1], metadata={**lab.roster[1].metadata, "capabilities": capabilities})
    from custom_components.voip_stack.roster import dump_roster_json
    lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
    lab.runtime.endpoints.update("sip:bedroom0", availability="available")
    phone = lab.phones[0]
    answers, offered = [], []

    def answer():
        request = phone.last_request
        audio = next(fmt for fmt in sdp.offered_pcm_formats(request.body)
            if fmt.encoding == "L16" and fmt.sample_rate == 16000)
        formats = sdp.offered_video_formats(request.body)
        offered.append(formats)
        video = formats[0] if formats else None
        if video is not None and policy == "unknown_bad_profile":
            assert video.profile_level_id == "42801f"
            video = replace(video, profile_level_id="42e01f")
        body = sdp.build_answer_directional("127.0.0.1", "127.0.0.1",
            phone.rtp_socket.getsockname()[1], audio, audio,
            remote_sdp=request.body, video_port=35002 if video is not None else 0,
            video_format=video, video_direction="sendonly")
        answers.append(body)
        phone.respond(request, phone.remote, 200, body.encode())
        phone.answered.set()

    monkeypatch.setattr(phone, "answer", answer)
    call_id = ""
    try:
        await lab.hass.services.async_call("voip_stack", "call", {
            "device_id": source.device_id, "destination": "bedroom0", "send_video": False,
        }, blocking=True)
        async with asyncio.timeout(3):
            while not (call_id := lab.runtime.endpoints.get(source.endpoint_id).active_call_id):
                await asyncio.sleep(0)
            while (client := lab.calls.sip_client_for(call_id)).dialog is None:
                await asyncio.sleep(0)
        dialog = client.dialog
        assert bool(offered[0]) is (policy != "known_audio_only")
        assert dialog.send_format.sample_rate == dialog.recv_format.sample_rate == 16000
        assert dialog.send_format.frame_ms == dialog.recv_format.frame_ms
        if policy == "unknown_compliant":
            assert dialog.video_format is not None
            assert dialog.video_format.profile_level_id == "42801f"
            assert dialog.local_video_direction == "recvonly"
        else:
            assert dialog.video_format is None
            if policy == "unknown_bad_profile":
                assert sdp.offered_video_formats(answers[0])[0].profile_level_id == "42e01f"
    finally:
        if call_id:
            await lab.hass.services.async_call("voip_stack", "hangup", {
                "device_id": source.device_id, "call_id": call_id,
            }, blocking=True)
    assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")


@pytest.mark.parametrize("source_codec,transcoding,video_enabled,known_audio_only,expected", [
    pytest.param("JPEG", False, True, False, "direct", id="direct-no-transcoder"),
    pytest.param("JPEG", True, True, False, "direct", id="direct-with-transcoding-enabled"),
    pytest.param("H264", True, True, False, "convert", id="unknown-h264-to-jpeg"),
    pytest.param("H264", False, True, False, "audio", id="no-transcoding-rejects-only-video"),
    pytest.param("JPEG", True, False, False, "audio", id="video-policy-disabled"),
    pytest.param("JPEG", True, True, True, "audio", id="known-audio-only-destination"),
])
async def test_forwarded_standard_sip_uses_sdp_and_preserves_direct_video(
    lab, monkeypatch, source_codec, transcoding, video_enabled, known_audio_only, expected,
):
    """Real forward O/A and RTP replace legacy source-text capability prerequisites."""
    from unittest.mock import AsyncMock
    from custom_components.voip_stack import call_forwarder, endpoint_runtime, invite_router, peer_snapshot
    from custom_components.voip_stack.const import CONF_VIDEO_TRANSCODING
    from custom_components.voip_stack.core import rtp, video_rtp
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.endpoint_session import TerminationIntent
    from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
    from custom_components.voip_stack.roster import RosterEntry
    from custom_components.voip_stack.sip_client import SipCallClient

    lab.runtime.sip = lab.calls = SipEndpointRuntime(allow_dark_sessions=True)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp, socket.socket() as tcp:
        udp.bind(("127.0.0.1", 0))
        listener = udp.getsockname()[1]
        tcp.bind(("127.0.0.1", listener))
    lab.runtime.transport_config.update({
        "sip_port": listener, CONF_SIP_VIDEO: video_enabled,
        CONF_VIDEO_TRANSCODING: transcoding,
    })
    monkeypatch.setattr(peer_snapshot, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", AsyncMock(return_value=[]))
    for module in (endpoint_runtime, invite_router, call_forwarder):
        monkeypatch.setattr(module, "_roster_from_peers", lambda *_args: lab.roster)
    monkeypatch.setattr(call_forwarder, "_async_build_peer_snapshot", AsyncMock(return_value=[]))
    source = PhoneEndpoint(endpoint_id="sip:plain-source", username="plain-source",
        name="Standard source", kind=EndpointKind.SIP_ACCOUNT,
        capabilities=frozenset({"audio", "dtmf"}), availability="available")
    lab.runtime.endpoints.register(source)
    lab.roster.append(RosterEntry("plain-source", address="127.0.0.2",
        metadata={"endpoint_id": source.endpoint_id, "endpoint_kind": "sip_account"}))
    assert not source.supports("video")
    for name in ("sip:bedroom0", "sip:bedroom1"):
        lab.runtime.endpoints.update(name, availability="available")
    if known_audio_only:
        lab.roster[2] = replace(lab.roster[2], metadata={
            **lab.roster[2].metadata, "capabilities": ["audio", "dtmf"],
        })
    assert await endpoint_runtime.async_start_sip_endpoint(lab.hass)
    first, destination = lab.phones
    first.auto_answer = False
    sockets = []
    def media_socket(host):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((host, 0))
        sock.setblocking(False)
        sockets.append(sock)
        return sock
    source_audio = media_socket("127.0.0.2")
    source_video = media_socket("127.0.0.2")
    destination_video = media_socket("127.0.0.1")
    offered = []
    def answer():
        request = destination.last_request
        audio = next(fmt for fmt in sdp.offered_pcm_formats(request.body)
            if fmt.encoding == "L16" and fmt.sample_rate == 16000)
        videos = sdp.offered_video_formats(request.body)
        offered.extend(videos)
        jpeg = next((fmt for fmt in videos if fmt.encoding == "JPEG"), None)
        body = sdp.build_answer_directional("127.0.0.1", "127.0.0.1",
            destination.rtp_socket.getsockname()[1], audio, audio,
            remote_sdp=request.body, video_port=destination_video.getsockname()[1] if jpeg else 0,
            video_format=jpeg, video_direction="recvonly")
        destination.respond(request, destination.remote, 200, body.encode())
        destination.answered.set()
    monkeypatch.setattr(destination, "answer", answer)
    source_format = sdp.RtpVideoFormat(payload_type=26, encoding="JPEG") if source_codec == "JPEG" else sdp.CONSTRAINED_BASELINE_H264_FORMAT
    caller = SipCallClient(local_ip="127.0.0.2", local_name="plain-source", local_sip_port=0,
        local_rtp_port=source_audio.getsockname()[1], supported_formats=(AudioFormat(16000, frame_ms=20),),
        local_video_rtp_port=source_video.getsockname()[1], video_formats=(source_format,), video_direction="sendonly")
    call_id = ""
    try:
        assert await caller.invite(target="bedroom0", remote_host="127.0.0.1",
            remote_sip_port=listener, timeout=2) == "ringing"
        call_id = caller.dialog_ids.call_id
        await lab.calls.forward_call(call_id=call_id, destination="bedroom1", on_failure="terminate")
        assert await caller.wait_for_final(timeout=3) == "in_call"
        await asyncio.wait_for(destination.answered.wait(), 2)
        relay = lab.calls.resource_for(call_id, "relay")
        video = relay.video_relay
        assert caller.dialog.send_format.sample_rate == caller.dialog.recv_format.sample_rate == 16000
        assert bool(offered) is (video_enabled and not known_audio_only)
        if offered:
            assert offered[0].encoding == source_codec
        assert (caller.dialog.video_format is not None) is (expected != "audio")
        if expected == "audio":
            assert video is None
            assert lab.runtime.media.transcoder is None
        else:
            assert video is not None
            assert video.transcodes_from("left") is (expected == "convert")
            assert video.transcodes_from("right") is False
            if expected == "direct":
                assert not video.transcoding and lab.runtime.media.transcoder is None
        samples = caller.dialog.send_format.sample_rate * caller.dialog.send_format.frame_ms // 1000
        pcm = struct.pack(f"!{samples}h", *([1200] * samples))
        await lab.hass.loop.sock_sendto(source_audio, rtp.build_packet(rtp.RtpPacket(
            caller.dialog.send_format.payload_type, 5, 0, 0x1212, pcm)),
            (caller.dialog.remote_rtp_host, caller.dialog.remote_rtp_port))
        async with asyncio.timeout(2):
            while True:
                raw, _ = await destination.audio.get()
                if len(raw) >= 12 and raw[0] >> 6 == 2:
                    packet = rtp.parse_packet(raw)
                    if packet.payload:
                        assert packet.payload == pcm[:len(packet.payload)]
                        break
        if expected != "audio":
            process = await asyncio.create_subprocess_exec("ffmpeg", "-nostdin", "-v", "error",
                "-f", "lavfi", "-i", "color=c=red:size=160x120:rate=5", "-frames:v", "1", "-an",
                *(["-c:v", "mjpeg", "-pix_fmt", "yuvj420p", "-threads", "1", "-huffman", "default", "-force_duplicated_matrix", "1", "-f", "image2pipe"]
                  if source_codec == "JPEG" else ["-c:v", "libx264", "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-threads", "1", "-f", "h264"]),
                "pipe:1", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            frame, error = await asyncio.wait_for(process.communicate(), 5)
            assert process.returncode == 0, error.decode()
            packets = (video_rtp.packetize_jpeg(frame, payload_type=caller.dialog.video_format.payload_type,
                sequence=10, timestamp=90000, ssrc=0x3434) if source_codec == "JPEG" else
                video_rtp.packetize_annex_b(frame, payload_type=caller.dialog.video_format.payload_type,
                sequence=10, timestamp=90000, ssrc=0x3434))
            packets = list(packets)
            async def send_video():
                sequence, timestamp = 10, 90000
                for _ in range(20 if source_codec == "H264" else 1):
                    batch = (video_rtp.packetize_annex_b(frame,
                        payload_type=caller.dialog.video_format.payload_type,
                        sequence=sequence, timestamp=timestamp, ssrc=0x3434)
                        if source_codec == "H264" else packets)
                    for packet in batch:
                        await lab.hass.loop.sock_sendto(source_video, rtp.build_packet(packet),
                            ("127.0.0.1", video.left_port))
                    sequence = rtp.next_sequence(batch[-1].sequence)
                    timestamp = rtp.next_timestamp(timestamp, 18000)
                    if source_codec == "H264":
                        await asyncio.sleep(1 / 5)
            sender = asyncio.create_task(send_video())
            decoder = video_rtp.JpegDepacketizer()
            received, decoded = [], None
            try:
                async with asyncio.timeout(6):
                    while decoded is None:
                        raw = await lab.hass.loop.sock_recv(destination_video, 65535)
                        if len(raw) < 12 or raw[0] >> 6 != 2:
                            continue
                        packet = rtp.parse_packet(raw)
                        received.append(packet.payload)
                        decoded = decoder.push(packet)
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            assert video_rtp.jpeg_dimensions(decoded.data) == (160, 120)
            from io import BytesIO
            from PIL import Image
            pixel = Image.open(BytesIO(decoded.data)).convert("RGB").getpixel((80, 60))
            assert pixel[0] > 180 and pixel[1] < 60 and pixel[2] < 60
            if expected == "direct":
                assert received == [packet.payload for packet in packets]
                assert video.transcoded_output_packets == 0
            else:
                assert video.transcoded_output_packets > 0
            assert relay is lab.calls.resource_for(call_id, "relay")
        assert caller.bye()
        session = lab.calls.get_session(call_id)
        if session is not None:
            await asyncio.wait_for(session.termination_started.wait(), 3)
            await lab.calls.request_termination(call_id, session.termination_intent)
        assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")
        assert lab.runtime.media.transcoder is None
    finally:
        await caller.close()
        for session in tuple(lab.calls.sessions.values()):
            await lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
        await endpoint_runtime.async_stop_sip_endpoint(lab.hass)
        for sock in sockets:
            sock.close()


@pytest.mark.parametrize('policy,direction', [
    ('unknown', 'sendrecv'), ('unknown', 'sendonly'), ('unknown', 'recvonly'),
    ('observed_audio_only', 'sendrecv'), ('observed_jpeg', 'sendrecv'),
    ('options_native_jpeg', 'sendrecv'), ('options_native_audio_only', 'sendrecv'),
    ('answer_audio_only', 'sendrecv'),
])
async def test_registered_managed_target_negotiates_video_after_audio_only_discovery(lab, monkeypatch, policy, direction):
    """REGISTER, partial OPTIONS and the real resolver must reach second-leg SDP."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.voip_stack import endpoint_runtime
    from custom_components.voip_stack.core import rtp, sip, video_rtp
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.endpoint_session import TerminationIntent
    from custom_components.voip_stack.managed_phones import ManagedPhone, managed_records
    from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
    from custom_components.voip_stack.peer_snapshot import async_build_peer_snapshot
    from custom_components.voip_stack.endpoint_routing import peer_video_codec, roster_from_peers
    from custom_components.voip_stack.runtime_data import sip_registrar
    from custom_components.voip_stack.sip_client import SipCallClient
    from custom_components.voip_stack.store import config_entry
    from tests.test_sip_capabilities_ha import RegisteredPeer, register_peer, wait_until
    import pytest_socket

    # HA's autouse fixture overwrites the marker allowlist during fixture setup.
    # The larger video INVITE may try TCP before falling back to this UDP peer.
    pytest_socket.socket_allow_hosts(['127.0.0.1', '127.0.0.2', '127.0.0.3'])

    lab.runtime.sip = lab.calls = SipEndpointRuntime(allow_dark_sessions=True)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp, socket.socket() as tcp:
        udp.bind(('127.0.0.1', 0))
        listener = udp.getsockname()[1]
        tcp.bind(('127.0.0.1', listener))
    lab.runtime.transport_config.update({"sip_port": listener, CONF_SIP_VIDEO: True})
    assert await endpoint_runtime.async_start_sip_endpoint(lab.hass)
    sockets, transports = [], []
    def media_socket(host):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((host, 0))
        sock.setblocking(False)
        sockets.append(sock)
        return sock
    source_audio = media_socket('127.0.0.2')
    source_video = media_socket('127.0.0.2')
    sink_audio = media_socket('127.0.0.3')
    sink_video = media_socket('127.0.0.3')
    jpeg = sdp.RtpVideoFormat(payload_type=26, encoding='JPEG')
    caller = SipCallClient(local_ip='127.0.0.2', local_name='caller', local_sip_port=0,
        local_rtp_port=source_audio.getsockname()[1],
        supported_send_formats=[AudioFormat(16000, frame_ms=10)],
        supported_recv_formats=[AudioFormat(48000, frame_ms=10)],
        allow_directional_audio_payloads=True,
        local_video_rtp_port=source_video.getsockname()[1], video_formats=(jpeg,), video_direction=direction)
    observed = {}

    class CallerWire(RegisteredPeer):
        def datagram_received(self, raw, address):
            if sip.parse_message(raw).is_response:
                caller.queue.put_nowait((raw, address))
            super().datagram_received(raw, address)

    class SinkWire(RegisteredPeer):
        def datagram_received(self, raw, address):
            request = sip.parse_message(raw)
            if request.method == 'OPTIONS' and policy.startswith('options_native_'):
                from pathlib import Path
                body = (Path(__file__).parent / 'fixtures/sdp/native_options_capabilities.sdp').read_bytes()
                body = body.replace(b'192.0.2.192', b'127.0.0.3')
                if policy == 'options_native_audio_only':
                    body = body.split(b'm=video', 1)[0]
                self.options.put_nowait((request, address))
                self.answer(request, address, body=body)
                return
            if request.method != 'INVITE':
                super().datagram_received(raw, address)
                return
            videos = sdp.offered_video_formats(request.body)
            observed.update(request=request, videos=videos)
            audio = next(f for f in sdp.offered_pcm_formats(request.body)
                         if f.encoding == 'L16' and f.sample_rate == 16000)
            selected = next((f for f in videos if f.encoding == 'JPEG'), None)
            if policy == 'answer_audio_only':
                selected = None
            answer_direction = {'sendonly': 'recvonly', 'recvonly': 'sendonly', 'sendrecv': 'sendrecv'}[direction]
            body = sdp.build_answer_directional('127.0.0.3', '127.0.0.3',
                sink_audio.getsockname()[1], audio, audio, remote_sdp=request.body,
                video_port=sink_video.getsockname()[1] if selected else 0,
                video_format=selected, video_direction=answer_direction)
            self.answer(request, address, body=body.encode())

    transport, source = await lab.hass.loop.create_datagram_endpoint(
        lambda: CallerWire('no-ptime'), local_addr=('127.0.0.2', 0))
    transports.append(transport)
    caller.transport, caller.protocol = transport, source
    caller.local_sip_port = transport.get_extra_info('sockname')[1]
    transport, sink = await lab.hass.loop.create_datagram_endpoint(
        lambda: SinkWire('no-ptime'), local_addr=('127.0.0.3', 0))
    transports.append(transport)
    origin = MockConfigEntry(domain='esphome', data={'host': '127.0.0.3', 'device_name': 'managed_sink'})
    origin.add_to_hass(lab.hass)
    device = dr.async_get(lab.hass).async_get_or_create(config_entry_id=origin.entry_id,
        identifiers={('esphome', '84c7bb000003')},
        connections={(dr.CONNECTION_NETWORK_MAC, '84:c7:bb:00:00:03')}, name='Managed sink')
    record = ManagedPhone(device.id, origin.entry_id, '84c7bb000003', 'fixture-only',
        username='managed_sink', display_name='Managed sink', api_host='127.0.0.3', extension='101')
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data,
        'managed_esphome_phones': {device.id: record.record()}})
    restored = managed_records(entry)[device.id]
    state_entity = er.async_get(lab.hass).async_get_or_create('sensor', 'esphome', 'managed_sink_voip_state',
        device_id=device.id, config_entry=origin)
    lab.hass.states.async_set(state_entity.entity_id, 'idle')
    entity = er.async_get(lab.hass).async_get_or_create('sensor', 'esphome', 'managed_sink_voip_endpoint',
        device_id=device.id, config_entry=origin)
    state = ('Managed sink | 127.0.0.3 | ' + str(transport.get_extra_info('sockname')[1]) +
        ' | 40000 | full_duplex | 16000:s16le:1:10 | 48000:s16le:1:10 | sip_udp | 101'
        if policy.startswith('observed_') else 'unknown')
    if policy == 'observed_jpeg':
        state += ' | video=jpeg'
    lab.hass.states.async_set(entity.entity_id, state)
    registrar = sip_registrar(lab.hass)
    registrar.enabled = True
    from custom_components.voip_stack.sip_registrar import SipAccount
    registrar.update_accounts([restored.account(), SipAccount('caller', 'Caller', 'fixture-only')])
    call_id = ''
    try:
        await register_peer(lab, sink, restored.username,
            contact=f"sip:opaque-sink@127.0.0.3:{transport.get_extra_info('sockname')[1]};transport=udp")
        await register_peer(lab, source, 'caller', contact=f'sip:opaque-caller@127.0.0.2:{caller.local_sip_port}')
        await wait_until(lambda: all(registrar.registered_contacts(name)[0].capabilities is not None
                                    for name in ['managed_sink', 'caller']))
        assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get('used')
        peers = await async_build_peer_snapshot(lab.hass)
        rows = roster_from_peers(lab.hass, peers, registrar.registered_roster_entries())
        row = next(row for row in rows if row.id == restored.username)
        assert row.metadata['sip_capabilities_known'] is True
        known_jpeg = policy in {'observed_jpeg', 'options_native_jpeg'}
        assert row.metadata['capabilities'] == (['audio', 'dtmf', 'video']
                                              if known_jpeg else ['audio', 'dtmf'])
        if policy.startswith('options_native_'):
            assert row.metadata['sip_audio_tx_formats'] == [
                'L16/16000/1/10', 'L16/16000/1/20', 'G722/8000/1/20', 'G722/8000/1/10']
            assert row.metadata['sip_audio_rx_formats'] == [
                'L16/48000/1/10', 'L16/16000/1/20', 'L16/16000/1/10',
                'G722/8000/1/20', 'G722/8000/1/10']
        else:
            assert row.metadata['sip_audio_tx_formats'] == row.metadata['sip_audio_rx_formats'] == []
        expected_contract = '' if policy == 'observed_audio_only' else ('jpeg' if known_jpeg else None)
        assert row.metadata['sip_video_codec'] == expected_contract
        assert peer_video_codec(None, row) == expected_contract
        while not caller.queue.empty():
            caller.queue.get_nowait()
        result = await caller.invite(target='101', remote_host='127.0.0.1', remote_sip_port=listener, timeout=3)
        if result == 'ringing':
            result = await caller.wait_for_final(timeout=3)
        call_id = caller.dialog_ids.call_id
        assert result == 'in_call'
        assert bool(observed['videos']) is (policy != 'observed_audio_only')
        has_video = policy not in {'observed_audio_only', 'answer_audio_only'}
        assert (caller.dialog.video_format is not None) is has_video
        assert caller.dialog.send_format.sample_rate == 16000
        assert caller.dialog.recv_format.sample_rate == 48000
        assert caller.dialog.send_format.frame_ms == 20
        assert caller.dialog.recv_format.frame_ms == 10
        relay = lab.calls.resource_for(call_id, 'relay')
        if has_video:
            assert caller.dialog.local_video_direction == direction
            video = relay.video_relay
            assert video is not None and not video.transcoding
            assert lab.runtime.media.transcoder is None
            from io import BytesIO
            from PIL import Image
            encoded = BytesIO()
            Image.new('RGB', (160, 120), (230, 15, 10)).save(encoded, format='JPEG', subsampling=2)
            frame = encoded.getvalue()
            for can_send, sender, receiver, port, ssrc in (
                (direction != 'recvonly', source_video, sink_video, video.left_port, 0x1234),
                (direction != 'sendonly', sink_video, source_video, video.right_port, 0x5678),
            ):
                if not can_send:
                    continue
                packets = list(video_rtp.packetize_jpeg(frame, payload_type=26,
                    sequence=19, timestamp=90000, ssrc=ssrc))
                for packet in packets:
                    await lab.hass.loop.sock_sendto(sender, rtp.build_packet(packet), ('127.0.0.1', port))
                decoder = video_rtp.JpegDepacketizer()
                received, decoded = [], None
                async with asyncio.timeout(2):
                    while decoded is None:
                        raw = await lab.hass.loop.sock_recv(receiver, 65535)
                        if len(raw) < 12 or raw[0] >> 6 != 2:
                            continue
                        packet = rtp.parse_packet(raw)
                        received.append(packet.payload)
                        decoded = decoder.push(packet)
                assert received == [packet.payload for packet in packets]
                assert video_rtp.jpeg_dimensions(decoded.data) == (160, 120)
                pixel = Image.open(BytesIO(decoded.data)).convert('RGB').getpixel((80, 60))
                assert pixel[0] > 180 and pixel[1] < 60 and pixel[2] < 60
            assert video.transcoded_output_packets == 0
        else:
            assert relay.video_relay is None
    finally:
        if caller.dialog is not None:
            caller.bye()
        await caller.close()
        for session in tuple(lab.calls.sessions.values()):
            await lab.calls.request_termination(session.call_id, TerminationIntent('test_done'))
        await endpoint_runtime.async_stop_sip_endpoint(lab.hass)
        for transport in transports:
            transport.close()
        for sock in sockets:
            sock.close()
    assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get('used')


@pytest.mark.parametrize('video_enabled', [False, True])
async def test_registered_sip_account_preserves_configured_video_policy_without_discovery(lab, video_enabled):
    """An absent peer must not erase the real account's explicit media policy."""
    from custom_components.voip_stack.endpoint_routing import assemble_directory_entries, peer_video_codec
    from custom_components.voip_stack.phone_config import endpoint_from_data, sip_account_phone_data
    from custom_components.voip_stack.roster import RosterEntry
    from custom_components.voip_stack.store import config_entry

    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_SIP_VIDEO: True})
    data = sip_account_phone_data({'username': 'manual-policy', 'password': 'fixture-only'})
    data['video_enabled'] = video_enabled
    endpoint = endpoint_from_data(entry, data)
    lab.runtime.endpoints.register(endpoint)
    rows = assemble_directory_entries(lab.hass, [], [RosterEntry(
        id=endpoint.username, sip_uri='sip:opaque-manual@127.0.0.1:41000',
        metadata={'registered': True, 'sip_transport': 'udp'},
    )])
    row = next(row for row in rows if row.id == endpoint.username)
    assert endpoint.supports('video') is video_enabled
    assert 'sip_video_codec' not in row.metadata
    assert peer_video_codec(None, row) == (None if video_enabled else '')
