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
        sock.bind((host, 0)); sock.setblocking(False); sockets.append(sock)
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
                        packet = rtp.parse_packet(raw); received.append(packet.payload)
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
