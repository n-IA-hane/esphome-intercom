"""A bridged re-offer retains the current receiver's frame-rate budget."""

import asyncio
from dataclasses import replace
import socket
from unittest.mock import AsyncMock

import pytest

from custom_components.voip_stack import endpoint_runtime, invite_router, peer_snapshot
from custom_components.voip_stack.const import CONF_SIP_VIDEO, CONF_VIDEO_TRANSCODING
from custom_components.voip_stack.core import rtp, sdp, video_rtp
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.sip_client import SipCallClient
from tests.test_automation_originate_ha import lab as lab


pytestmark = pytest.mark.ha


@pytest.mark.parametrize("receiver_hint,transcoding,peer_attributes,source_calls,expected_resize", [
    pytest.param("26 recv [x=[8:8:320],y=[8:8:240]]", True, True, False, True, id="receiver-originates"),
    pytest.param("26 recv [x=800,y=800]", True, True, False, False, id="matching-size"),
    pytest.param("26 recv [x=[8:8:320],y=[8:8:240]]", False, True, False, False, id="transcoder-disabled"),
    pytest.param("26 recv *", True, True, False, False, id="wildcard-receiver"),
    pytest.param(None, True, True, False, False, id="unknown-receiver"),
    pytest.param("26 recv [x=[8:8:320],y=[8:8:240]]", True, False, False, False, id="legacy-source"),
    pytest.param("26 recv [x=[8:8:320],y=[8:8:240]]", True, True, True, True, id="source-originates"),
])
async def test_receive_only_bridge_negotiates_camera_geometry_before_resizing(
    lab, monkeypatch, receiver_hint, transcoding, peer_attributes, source_calls, expected_resize,
):
    """A receiver-originated real SIP call must resize the opposite RTP path."""
    lab.runtime.sip = lab.calls = SipEndpointRuntime(allow_dark_sessions=True)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp, socket.socket() as tcp:
        udp.bind(("127.0.0.1", 0))
        listener_port = udp.getsockname()[1]
        tcp.bind(("127.0.0.1", listener_port))
    lab.runtime.transport_config.update({
        "sip_port": listener_port, CONF_SIP_VIDEO: True, CONF_VIDEO_TRANSCODING: transcoding,
    })
    monkeypatch.setattr(peer_snapshot, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", AsyncMock(return_value=[]))
    monkeypatch.setattr(endpoint_runtime, "_roster_from_peers", lambda *_args: lab.roster)
    monkeypatch.setattr(invite_router, "_roster_from_peers", lambda *_args: lab.roster)
    assert await endpoint_runtime.async_start_sip_endpoint(lab.hass)
    lab.runtime.endpoints.update("sip:bedroom0", availability="available")
    camera = lab.phones[0]
    remote_video = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    remote_video.bind(("127.0.0.1", 0))
    remote_video.setblocking(False)
    caller_video = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    caller_video.bind(("127.0.0.2", 0))
    caller_video.setblocking(False)

    def answer():
        request = camera.last_request
        audio = next(fmt for fmt in sdp.offered_pcm_formats(request.body)
            if fmt.encoding == "L16" and fmt.sample_rate == 16000)
        offered = sdp.offered_video_formats(request.body)[0]
        # Independent fixed-source peer: invalid/absent imageattr is omitted
        # from its answer, as RFC 6236 requires for an unoffered mechanism.
        attrs = offered.image_attributes
        accepted = peer_attributes and attrs is not None
        if source_calls:
            imageattr = f"a=imageattr:{receiver_hint}\r\n" if accepted else ""
        else:
            accepted = accepted and attrs.recv is not None and attrs.recv.wildcard
            imageattr = ("a=imageattr:26 send [x=800,y=800,sar=1.0] "
                         "recv [x=[8:8:800],y=[8:8:800],sar=1.0]\r\n") if accepted else ""
        body = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Fixed JPEG source\r\n"
            "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
            f"m=audio {camera.rtp_socket.getsockname()[1]} RTP/AVP {audio.payload_type}\r\n"
            f"a=rtpmap:{audio.payload_type} L16/16000/1\r\na=ptime:10\r\na=sendrecv\r\n"
            f"m=video {remote_video.getsockname()[1]} RTP/AVP 26\r\n"
            "a=rtpmap:26 JPEG/90000\r\na=framerate:10\r\n"
            f"{imageattr}a={'recvonly' if source_calls else 'sendonly'}\r\n").encode()
        camera.respond(request, camera.remote, 200, body)
        camera.answered.set()

    monkeypatch.setattr(camera, "answer", answer)
    caller_hint = ("26 send [x=800,y=800] recv [x=[8:8:800],y=[8:8:800]]"
        if source_calls else receiver_hint)
    receiver = sdp.RtpVideoFormat(payload_type=26, encoding="JPEG", max_framerate=10,
        image_attributes=sdp.parse_imageattr(caller_hint)[1] if caller_hint else None)
    caller = SipCallClient(local_ip="127.0.0.2", local_name="Bounded JPEG receiver",
        local_sip_port=0, local_rtp_port=35000, supported_formats=[AudioFormat(16000, frame_ms=10)],
        local_video_rtp_port=caller_video.getsockname()[1], video_formats=(receiver,),
        video_direction="sendrecv" if source_calls else "recvonly")
    sender = None
    try:
        result = await caller.invite(target="bedroom0", remote_host="127.0.0.1",
            remote_sip_port=listener_port, timeout=3)
        if result == "ringing":
            result = await caller.wait_for_final(timeout=3)
        assert result == "in_call"
        call_id = caller.dialog_ids.call_id
        audio_relay = lab.calls.resource_for(call_id, "relay")
        relay = audio_relay.video_relay
        original_audio = (caller.dialog.send_format, caller.dialog.recv_format)
        # The actual bridge must negotiate the camera's send envelope before
        # comparing it with the caller's separately retained receive bounds.
        assert b"a=imageattr:26\r\n" not in camera.invites[0].body
        if not source_calls:
            if transcoding and receiver_hint is not None:
                assert b"a=imageattr:26 send * recv *\r\n" in camera.invites[0].body
            else:
                assert b"a=imageattr:" not in camera.invites[0].body
        assert relay.transcodes_from("left") == (expected_resize and source_calls)
        assert relay.transcodes_from("right") == (expected_resize and not source_calls)
        assert relay.left.send_format.max_framerate == relay.right.recv_format.max_framerate == 10
        if expected_resize:
            destination = relay.right if source_calls else relay.left
            plan = sdp.jpeg_image_plan(destination.send_format, 800, 800)
            assert (plan.width, plan.height) == (240, 240)
            encoder = await asyncio.create_subprocess_exec("ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=red:size=800x800:rate=10", "-frames:v", "1", "-an",
                "-c:v", "mjpeg", "-pix_fmt", "yuvj420p", "-threads", "1", "-huffman", "default",
                "-force_duplicated_matrix", "1", "-f", "image2pipe", "pipe:1",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            jpeg, error = await asyncio.wait_for(encoder.communicate(), 5)
            assert encoder.returncode == 0, error.decode()
            assert video_rtp.jpeg_dimensions(jpeg) == (800, 800)
            input_socket = caller_video if source_calls else remote_video
            output_socket = remote_video if source_calls else caller_video
            relay_port = relay.left_port if source_calls else relay.right_port

            async def send_frames():
                sequence = 0
                for frame in range(40):
                    packets = video_rtp.packetize_jpeg(jpeg, payload_type=26, sequence=sequence,
                        timestamp=frame * 9000, ssrc=0x11223344)
                    for packet in packets:
                        await lab.hass.loop.sock_sendto(input_socket, rtp.build_packet(packet),
                            ("127.0.0.1", relay_port))
                    sequence += len(packets)
                    await asyncio.sleep(0.1)

            sender = asyncio.create_task(send_frames())
            decoder = video_rtp.JpegDepacketizer()
            frames = []
            async with asyncio.timeout(8):
                while len(frames) < 3:
                    raw = await lab.hass.loop.sock_recv(output_socket, 65535)
                    # The real relay sends this RFC 5389 binding indication
                    # on the same media socket before RTP (RFC 7983 demux).
                    if len(raw) == 20 and raw[:8] == b"\x00\x11\x00\x00\x21\x12\xa4\x42":
                        continue
                    frame = decoder.push(rtp.parse_packet(raw))
                    if frame is not None:
                        frames.append(frame)
            assert all(video_rtp.jpeg_dimensions(frame.data) == (240, 240) for frame in frames)
            assert relay.transcoded_output_packets > 0
            assert lab.calls.resource_for(call_id, "relay") is audio_relay
            assert (caller.dialog.send_format, caller.dialog.recv_format) == original_audio
        assert caller.bye()
        session = lab.calls.get_session(call_id)
        await asyncio.wait_for(session.termination_started.wait(), 3)
        barrier = lab.calls.request_termination(call_id, session.termination_intent)
        if barrier is not None:
            await barrier
        assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")
        assert lab.runtime.media.transcoder is None
    finally:
        if sender is not None:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        await caller.close()
        for session in tuple(lab.calls.sessions.values()):
            await lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
        await endpoint_runtime.async_stop_sip_endpoint(lab.hass)
        remote_video.close()
        caller_video.close()


@pytest.mark.parametrize("direction,receiver_limit", [
    pytest.param("sendonly", 2, id="direction-only-unchanged-receiver"),
    pytest.param("sendrecv", 2, id="unchanged-direction"),
    pytest.param("sendonly", 5, id="fresh-receiver-budget"),
])
async def test_bridge_reoffer_answers_current_receiver_budget(lab, monkeypatch, direction, receiver_limit):
    lab.runtime.sip = lab.calls = SipEndpointRuntime(allow_dark_sessions=True)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp, socket.socket() as tcp:
        udp.bind(("127.0.0.1", 0))
        listener_port = udp.getsockname()[1]
        tcp.bind(("127.0.0.1", listener_port))
    lab.runtime.transport_config.update(sip_port=listener_port)
    lab.runtime.transport_config[CONF_SIP_VIDEO] = True
    monkeypatch.setattr(peer_snapshot, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", AsyncMock(return_value=[]))
    monkeypatch.setattr(endpoint_runtime, "_roster_from_peers", lambda *_args: lab.roster)
    monkeypatch.setattr(invite_router, "_roster_from_peers", lambda *_args: lab.roster)
    assert await endpoint_runtime.async_start_sip_endpoint(lab.hass)
    lab.runtime.endpoints.update("sip:bedroom0", availability="available")
    receiver = lab.phones[0]

    def answer():
        request = receiver.last_request
        audio = next(fmt for fmt in sdp.offered_pcm_formats(request.body)
            if fmt.encoding == "L16" and fmt.sample_rate == 16000)
        video = sdp.offered_video_formats(request.body)[0]
        assert video.max_framerate == 10
        limit = 2 if len(receiver.invites) == 1 else receiver_limit
        body = sdp.build_answer_directional("127.0.0.1", "127.0.0.1",
            receiver.rtp_socket.getsockname()[1], audio, audio, remote_sdp=request.body,
            video_port=35002, video_format=replace(video, max_framerate=limit),
            video_direction="recvonly").encode()
        receiver.respond(request, receiver.remote, 200, body)
        receiver.answered.set()

    monkeypatch.setattr(receiver, "answer", answer)
    caller = SipCallClient(local_ip="127.0.0.2", local_name="Independent camera caller",
        local_sip_port=0, local_rtp_port=35000, supported_formats=[AudioFormat(16000, frame_ms=10)],
        local_video_rtp_port=35004,
        video_formats=(sdp.RtpVideoFormat(payload_type=26, encoding="JPEG", max_framerate=10),))
    answers = []
    read_response = caller._read_response

    async def observe_response(*args, **kwargs):
        response = await read_response(*args, **kwargs)
        if response is not None:
            message, _address = response
            if message.status_code == 200 and message.header("CSeq").endswith(" INVITE"):
                answers.append(message)
        return response

    monkeypatch.setattr(caller, "_read_response", observe_response)
    try:
        result = await caller.invite(target="bedroom0", remote_host="127.0.0.1",
            remote_sip_port=listener_port, timeout=3)
        if result == "ringing":
            result = await caller.wait_for_final(timeout=3)
        assert result == "in_call"
        initial = caller.dialog
        assert sdp.offered_video_formats(answers[-1].body)[0].max_framerate == 2
        call_id = caller.dialog_ids.call_id
        relay = lab.calls.resource_for(call_id, "relay")
        destination = lab.calls.sip_client_for(lab.calls.bridge_for(call_id)[1])
        # This is the actual asymmetric contract: the decoder answer is 2,
        # while the distinct local receive contract still comes from offer 10.
        assert destination.dialog.send_video_format.max_framerate == 2
        assert destination.dialog.recv_video_format.max_framerate == 10

        candidate = await caller.async_prepare_video_reinvite(local_video_rtp_port=35004,
            video_formats=caller.video_formats, video_direction=direction)
        assert candidate is not None
        assert len(answers) == 2
        answer_format = sdp.offered_video_formats(answers[-1].body)[0]
        assert answer_format.max_framerate == receiver_limit
        assert answer_format.direction == "recvonly"
        assert answer_format.encoding == "JPEG" and answer_format.payload_type == 26
        assert caller.commit_prepared_reinvite(initial, candidate)
        async with asyncio.timeout(3):
            while relay.video_relay.left.recv_format.max_framerate != receiver_limit:
                await asyncio.sleep(0)
        assert destination.dialog.send_video_format.max_framerate == receiver_limit
        assert destination.dialog.recv_video_format.max_framerate == 10
        assert len(receiver.invites) == (1 if direction == "sendrecv" else 2)
        assert caller.bye()
        session = lab.calls.get_session(call_id)
        await asyncio.wait_for(session.termination_started.wait(), 3)
        barrier = lab.calls.request_termination(call_id, session.termination_intent)
        if barrier is not None:
            await barrier
        assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")
    finally:
        await caller.close()
        for session in tuple(lab.calls.sessions.values()):
            await lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
        await endpoint_runtime.async_stop_sip_endpoint(lab.hass)
