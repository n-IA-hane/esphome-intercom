"""A bridged re-offer retains the current receiver's frame-rate budget."""

import asyncio
from dataclasses import replace
import inspect
import json
import os
from pathlib import Path
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
from tests.test_trunk_calls_ha import sip_test_account


pytestmark = pytest.mark.ha


async def _jpeg_frame(size, *, quality=None):
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        f"color=c=red:size={size[0]}x{size[1]}:rate=10", "-frames:v", "1", "-an",
        "-c:v", "mjpeg", "-pix_fmt", "yuvj420p", "-threads", "1", "-huffman", "default",
        "-force_duplicated_matrix", "1", *(["-q:v", str(quality)] if quality is not None else []),
        "-f", "image2pipe", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        frame, error = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, error.decode()
        assert video_rtp.jpeg_dimensions(frame) == size
        return frame
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.parametrize("caller_fps", [2, 15])
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
    lab, monkeypatch, receiver_hint, transcoding, peer_attributes, source_calls, expected_resize, caller_fps,
    *, frame_size=(800, 800), source_claim=(800, 800), runtime_resize=None, expected_output=None,
    wire_phases=None, reuse_quantization=False, evidence_path=None,
):
    """A receiver-originated real SIP call must resize the opposite RTP path."""
    await endpoint_runtime.async_stop_sip_endpoint(lab.hass)
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
    monkeypatch.setattr("custom_components.voip_stack.endpoint_group_calls.roster_from_peers", lambda *_args: lab.roster)
    monkeypatch.setattr(invite_router, "_roster_from_peers", lambda *_args: lab.roster)
    assert await endpoint_runtime.async_start_sip_endpoint(lab.hass)
    lab.runtime.endpoints.update("bedroom0", availability="available")
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
            imageattr = (f"a=imageattr:26 send [x={source_claim[0]},y={source_claim[1]},sar=1.0] "
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
    receiver = sdp.RtpVideoFormat(payload_type=26, encoding="JPEG", max_framerate=caller_fps,
        image_attributes=sdp.parse_imageattr(caller_hint)[1] if caller_hint else None)
    account = sip_test_account(lab, "bedroom1")
    caller = SipCallClient(local_ip="127.0.0.2", local_name="Bounded JPEG receiver",
        username=account.username, password=account.password,
        local_sip_port=0, local_rtp_port=35000, supported_formats=[AudioFormat(16000, frame_ms=10)],
        local_video_rtp_port=caller_video.getsockname()[1], video_formats=(receiver,),
        video_direction="sendrecv" if source_calls else "recvonly")
    sender = video_receiver = None
    try:
        result = await caller.invite(target="bedroom0", remote_host="127.0.0.1",
            remote_sip_port=listener_port, timeout=3)
        if result == "ringing":
            result = await caller.wait_for_final(timeout=3)
        assert result == "in_call"
        call_id = caller.dialog_ids.call_id
        audio_relay = lab.calls.resource_for(call_id, "relay")
        relay = audio_relay.video_relay
        expected_source = os.environ.get("VOIP_EXPECT_PRODUCTION_ROOT")
        if expected_source:
            assert Path(endpoint_runtime.__file__).resolve().parent == Path(expected_source).resolve()
            assert Path(inspect.getfile(type(relay))).resolve().parent == Path(expected_source).resolve()
        hints = {
            side: {
                "send_image_attributes": repr(peer.send_format.image_attributes)
                    if peer.send_format.image_attributes is not None else None,
                "recv_image_attributes": repr(peer.recv_format.image_attributes)
                    if peer.recv_format.image_attributes is not None else None,
            }
            for side, peer in (("left", relay.left), ("right", relay.right))
        }
        evidence = {
            "call_id": call_id, "receiver_hint": receiver_hint,
            "source_declares_imageattr": peer_attributes,
            "transcoding_opt_in": transcoding,
            "transcoder_hass_configured": relay._transcode_hass is not None,
            "leg_contracts": hints,
            "loaded_relay_owner": inspect.getfile(type(relay)),
        }
        if evidence_path is not None:
            evidence_path.write_text(json.dumps(evidence, indent=2) + "\n")
        if receiver_hint == "26 recv [x=[8:8:320],y=[8:8:240]]":
            receiving_peer = relay.right if source_calls else relay.left
            receiving = receiving_peer.send_format.image_attributes
            assert receiving is not None and receiving.recv is not None, evidence
            assert not receiving.recv.wildcard, evidence
            assert all(item.x.maximum <= 320 and item.y.maximum <= 240
                       for item in receiving.recv.sets), evidence
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
        assert relay.left.send_format.max_framerate == relay.right.recv_format.max_framerate == caller_fps
        assert relay.right.send_format.max_framerate == 10
        if source_calls and not expected_resize:
            assert relay.left.recv_format.max_framerate == 10
        jpeg = await _jpeg_frame(frame_size)
        bounded_receiver = receiver_hint == "26 recv [x=[8:8:320],y=[8:8:240]]"
        if runtime_resize is None:
            runtime_resize = (transcoding and bounded_receiver
                              and (frame_size[0] > 320 or frame_size[1] > 240))
        actual_resize = expected_resize or runtime_resize
        drop_oversize = (not transcoding and bounded_receiver
                         and (frame_size[0] > 320 or frame_size[1] > 240))
        if wire_phases is not None:
            input_socket = remote_video
            output_socket = caller_video
            relay_port = relay.right_port
            initial_dialog = caller.dialog_ids.call_id
            initial_ports = (relay.left_port, relay.right_port)
            source_side = "right"
            sequence, picture = 0, 0
            reference_tables = None
            expected_payloads = {}
            completed = asyncio.Queue()
            bootstrap_reinjections = 0
            receiver_sources = []

            async def receive_continuously():
                decoder = video_rtp.JpegDepacketizer()
                source_ssrc = None
                by_timestamp = {}
                try:
                    while True:
                        raw = await lab.hass.loop.sock_recv(output_socket, 65535)
                        if len(raw) == 20 and raw[:8] == b"\x00\x11\x00\x00\x21\x12\xa4\x42":
                            continue
                        packet = rtp.parse_packet(raw)
                        if packet.ssrc != source_ssrc:
                            # Real ESP reset_session discards quantizers when a
                            # converted SSRC replaces rawA, then rawA returns.
                            source_ssrc = packet.ssrc
                            receiver_sources.append(source_ssrc)
                            decoder = video_rtp.JpegDepacketizer()
                            by_timestamp.clear()
                        by_timestamp.setdefault(packet.timestamp, []).append(packet.payload)
                        frame = decoder.push(packet)
                        if frame is not None:
                            width, height = video_rtp.jpeg_dimensions(frame.data)
                            assert width <= 320 and height <= 240
                            await completed.put((frame, packet, by_timestamp.pop(packet.timestamp)))
                except Exception as error:
                    await completed.put(error)
                    raise

            video_receiver = asyncio.create_task(receive_continuously())
            for phase, (size, output_size, converted) in enumerate(wire_phases):
                image = await _jpeg_frame(size, quality=5 if reuse_quantization else None)
                phase_first_timestamp = picture * 9000
                # The receiver already consumed every packet and updated its
                # source-aware cache; only old completed pictures are discarded.
                while not completed.empty():
                    old = completed.get_nowait()
                    if isinstance(old, Exception):
                        raise old

                async def feed_phase():
                    nonlocal sequence, picture, reference_tables
                    for _ in range(50):
                        packets = video_rtp.packetize_jpeg(image, payload_type=26, sequence=sequence,
                            timestamp=picture * 9000, ssrc=0x11223344)
                        if reuse_quantization:
                            rewritten = []
                            for packet in packets:
                                payload = bytearray(packet.payload)
                                assert payload[5] == 255 and payload[4] in (0, 1)
                                payload[5] = 128
                                if int.from_bytes(payload[1:4], "big") == 0:
                                    length = int.from_bytes(payload[10:12], "big")
                                    tables = bytes(payload[12:12 + length])
                                    if reference_tables is None:
                                        reference_tables = tables
                                    assert tables == reference_tables
                                    if picture != 0:
                                        payload[10:12] = b"\x00\x00"
                                        del payload[12:12 + length]
                                rewritten.append(replace(packet, payload=bytes(payload)))
                            packets = rewritten
                        expected_payloads[picture * 9000] = [packet.payload for packet in packets]
                        picture += 1
                        sequence = (sequence + len(packets)) & 0xffff
                        for packet in packets:
                            await lab.hass.loop.sock_sendto(input_socket, rtp.build_packet(packet),
                                ("127.0.0.1", relay_port))
                        await asyncio.sleep(0.1)

                sender = asyncio.create_task(feed_phase())
                async with asyncio.timeout(8):
                    while not (relay.processing_known_from(source_side)
                               and relay.transcodes_from(source_side) is converted):
                        await asyncio.sleep(0.01)
                frames = []
                async with asyncio.timeout(8):
                    while len(frames) < 3:
                        item = await completed.get()
                        if isinstance(item, Exception):
                            raise item
                        frame, packet, payloads = item
                        if video_rtp.jpeg_dimensions(frame.data) != output_size:
                            continue  # An already delivered preceding-mode picture.
                        if not converted:
                            assert packet.ssrc == 0x11223344
                            if packet.timestamp < phase_first_timestamp:
                                continue
                            wanted = expected_payloads[packet.timestamp]
                            if payloads != wanted:
                                # Exactly one standard DQT bootstrap may repair
                                # rawA/Length0 after the converted source reset.
                                assert reuse_quantization and phase == 2
                                assert len(payloads) == len(wanted)
                                changed = 0
                                for actual, original in zip(payloads, wanted):
                                    if actual == original:
                                        continue
                                    changed += 1
                                    assert int.from_bytes(original[1:4], "big") == 0
                                    assert original[5] == actual[5] == 128
                                    assert original[10:12] == b"\x00\x00"
                                    length = int.from_bytes(actual[10:12], "big")
                                    assert length == len(reference_tables)
                                    assert actual[:10] == original[:10]
                                    assert actual[12:12 + length] == reference_tables
                                    assert actual[12 + length:] == original[12:]
                                assert changed == 1
                                bootstrap_reinjections += 1
                                assert bootstrap_reinjections == 1
                        frames.append(frame)
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
                sender = None
                assert caller.dialog_ids.call_id == initial_dialog
                assert (relay.left_port, relay.right_port) == initial_ports
                assert lab.calls.resource_for(call_id, "relay") is audio_relay
                assert (caller.dialog.send_format, caller.dialog.recv_format) == original_audio
            assert receiver_sources[0] == receiver_sources[-1] == 0x11223344
            assert any(value != 0x11223344 for value in receiver_sources)
            if reuse_quantization:
                assert bootstrap_reinjections == 1
            video_receiver.cancel()
            await asyncio.gather(video_receiver, return_exceptions=True)
            video_receiver = None
            assert not relay.transcoding and not lab.runtime.media.transcoders
        elif drop_oversize:
            input_socket = caller_video if source_calls else remote_video
            output_socket = remote_video if source_calls else caller_video
            relay_port = relay.left_port if source_calls else relay.right_port
            before = relay.dropped
            for packet in video_rtp.packetize_jpeg(jpeg, payload_type=26, sequence=13,
                    timestamp=90000, ssrc=0x11223344):
                await lab.hass.loop.sock_sendto(input_socket, rtp.build_packet(packet), ("127.0.0.1", relay_port))
            async with asyncio.timeout(2):
                while relay.dropped == before:
                    await asyncio.sleep(0.01)
            # STUN is not video. Every actual RTP/JPEG output would violate bounds.
            while True:
                try:
                    raw = output_socket.recvfrom(65535)[0]
                except BlockingIOError:
                    break
                assert len(raw) == 20 and raw[:8] == b"\x00\x11\x00\x00\x21\x12\xa4\x42"
            assert not relay.transcoding and not lab.runtime.media.transcoders
            assert caller.dialog_ids.call_id == call_id
            assert (caller.dialog.send_format, caller.dialog.recv_format) == original_audio
        elif actual_resize:
            destination = relay.right if source_calls else relay.left
            plan = sdp.jpeg_image_plan(destination.send_format, *frame_size)
            wanted = expected_output or (plan.width, plan.height)
            if bounded_receiver:
                assert wanted[0] <= 320 and wanted[1] <= 240
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
            actual_sizes = [video_rtp.jpeg_dimensions(frame.data) for frame in frames]
            evidence["actual_output_sizes"] = actual_sizes
            evidence["expected_output_size"] = wanted
            evidence["source_JPEG_size"] = frame_size
            evidence["transcoded_input_packets"] = relay.transcoded_input_packets
            evidence["transcoded_output_packets"] = relay.transcoded_output_packets
            if evidence_path is not None:
                evidence_path.write_text(json.dumps(evidence, indent=2) + "\n")
                for index, frame in enumerate(frames):
                    evidence_path.with_name(f"jpeg-output-{index}.jpg").write_bytes(frame.data)
            assert all(size == wanted for size in actual_sizes), evidence
            assert relay.transcoded_output_packets > 0
            assert lab.calls.resource_for(call_id, "relay") is audio_relay
            assert (caller.dialog.send_format, caller.dialog.recv_format) == original_audio
        else:
            assert not relay.transcoding and not lab.runtime.media.transcoders
            input_socket = caller_video if source_calls else remote_video
            output_socket = remote_video if source_calls else caller_video
            relay_port = relay.left_port if source_calls else relay.right_port
            sent = video_rtp.packetize_jpeg(jpeg, payload_type=26, sequence=13,
                timestamp=90000, ssrc=0x11223344)
            for packet in sent:
                await lab.hass.loop.sock_sendto(input_socket, rtp.build_packet(packet), ("127.0.0.1", relay_port))
            received = []
            decoder = video_rtp.JpegDepacketizer()
            frame = None
            async with asyncio.timeout(2):
                while len(received) < len(sent):
                    raw = await lab.hass.loop.sock_recv(output_socket, 65535)
                    if len(raw) == 20 and raw[:8] == b"\x00\x11\x00\x00\x21\x12\xa4\x42":
                        continue
                    packet = rtp.parse_packet(raw)
                    received.append(packet.payload)
                    decoded = decoder.push(packet)
                    if decoded is not None:
                        frame = decoded
            assert received == [packet.payload for packet in sent]
            assert frame is not None and video_rtp.jpeg_dimensions(frame.data) == frame_size
            assert relay.transcoded_output_packets == 0
        assert caller.bye()
        session = lab.calls.get_session(call_id)
        await asyncio.wait_for(session.termination_started.wait(), 3)
        barrier = lab.calls.request_termination(call_id, session.termination_intent)
        if barrier is not None:
            await barrier
        assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")
        assert not lab.runtime.media.transcoders
    finally:
        if sender is not None:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        if video_receiver is not None:
            video_receiver.cancel()
            await asyncio.gather(video_receiver, return_exceptions=True)
        await caller.close()
        for session in tuple(lab.calls.sessions.values()):
            await lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
        await endpoint_runtime.async_stop_sip_endpoint(lab.hass)
        remote_video.close()
        caller_video.close()


@pytest.mark.parametrize("claim,size,output,receiver,runtime_resize", [
    pytest.param(None, (400, 704), (136, 240), "26 recv [x=[8:8:320],y=[8:8:240]]", True,
                 id="unknown-source-portrait"),
    pytest.param((320, 240), (400, 704), (136, 240), "26 recv [x=[8:8:320],y=[8:8:240]]", True,
                 id="inaccurate-source-portrait"),
    pytest.param(None, (320, 240), (320, 240), "26 recv [x=[8:8:320],y=[8:8:240]]", False,
                 id="compatible-unknown-source"),
    pytest.param(None, (400, 704), (400, 704), None, False, id="unconstrained-receiver"),
])
async def test_actual_jpeg_header_obeys_receiver_when_source_sdp_is_not_authoritative(
    lab, monkeypatch, tmp_path, claim, size, output, receiver, runtime_resize,
):
    # CC: negotiated receiver bounds constrain actual media, not just source SDP.
    # Wire400x704 is distinct from the original JPEG400x700 probe. Independent
    # scale=-2:240 plus8-pixelpadding and Pillow both prove136x240 for this wire.
    # Receiver bounds are also asserted independently of the production planner.
    await test_receive_only_bridge_negotiates_camera_geometry_before_resizing(
        lab, monkeypatch, receiver, True, claim is not None, False, False, 10,
        frame_size=size, source_claim=claim, runtime_resize=runtime_resize,
        expected_output=output,
        evidence_path=tmp_path / "jpeg-leg-contracts.json",
    )


@pytest.mark.parametrize("reuse_quantization", [False, True], ids=["Q255", "cached-Q128"])
async def test_unknown_jpeg_size_changes_keep_one_dialog_and_restore_passthrough(
    lab, monkeypatch, tmp_path, reuse_quantization,
):
    await test_receive_only_bridge_negotiates_camera_geometry_before_resizing(
        lab, monkeypatch, "26 recv [x=[8:8:320],y=[8:8:240]]", True, False, False, False, 10,
        frame_size=(320, 240), source_claim=None,
        wire_phases=[((320, 240), (320, 240), False), ((800, 800), (240, 240), True),
                     ((320, 240), (320, 240), False)],
        reuse_quantization=reuse_quantization,
        evidence_path=tmp_path / "jpeg-leg-contracts.json",
    )


@pytest.mark.parametrize("caller_fps", [10, 2])
@pytest.mark.parametrize("direction,receiver_limit,expected_invites", [
    pytest.param("sendrecv", 2, 2, id="direction-only-unchanged-receiver"),
    pytest.param("sendonly", 2, 1, id="unchanged-direction"),
    pytest.param("sendrecv", 5, 2, id="fresh-receiver-budget"),
])
async def test_bridge_reoffer_answers_current_receiver_budget(lab, monkeypatch, direction, receiver_limit, expected_invites, caller_fps):
    await endpoint_runtime.async_stop_sip_endpoint(lab.hass)
    lab.runtime.sip = lab.calls = SipEndpointRuntime(allow_dark_sessions=True)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp, socket.socket() as tcp:
        udp.bind(("127.0.0.1", 0))
        listener_port = udp.getsockname()[1]
        tcp.bind(("127.0.0.1", listener_port))
    lab.runtime.transport_config.update(sip_port=listener_port)
    lab.runtime.transport_config[CONF_SIP_VIDEO] = True
    monkeypatch.setattr(peer_snapshot, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", AsyncMock(return_value=[]))
    monkeypatch.setattr("custom_components.voip_stack.endpoint_group_calls.roster_from_peers", lambda *_args: lab.roster)
    monkeypatch.setattr(invite_router, "_roster_from_peers", lambda *_args: lab.roster)
    assert await endpoint_runtime.async_start_sip_endpoint(lab.hass)
    lab.runtime.endpoints.update("bedroom0", availability="available")
    receiver = lab.phones[0]

    def answer():
        request = receiver.last_request
        audio = next(fmt for fmt in sdp.offered_pcm_formats(request.body)
            if fmt.encoding == "L16" and fmt.sample_rate == 16000)
        video = sdp.offered_video_formats(request.body)[0]
        assert video.max_framerate == caller_fps
        limit = 2 if len(receiver.invites) == 1 else receiver_limit
        body = sdp.build_answer_directional("127.0.0.1", "127.0.0.1",
            receiver.rtp_socket.getsockname()[1], audio, audio, remote_sdp=request.body,
            video_port=35002, video_format=replace(video, max_framerate=limit),
            video_direction="recvonly").encode()
        receiver.respond(request, receiver.remote, 200, body)
        receiver.answered.set()

    monkeypatch.setattr(receiver, "answer", answer)
    account = sip_test_account(lab, "bedroom1")
    caller = SipCallClient(local_ip="127.0.0.2", local_name="Independent camera caller",
        username=account.username, password=account.password,
        local_sip_port=0, local_rtp_port=35000, supported_formats=[AudioFormat(16000, frame_ms=10)],
        local_video_rtp_port=35004,
        video_formats=(sdp.RtpVideoFormat(payload_type=26, encoding="JPEG", max_framerate=caller_fps),))
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
        assert initial.local_video_direction == "sendonly"
        assert sdp.offered_video_formats(answers[-1].body)[0].max_framerate == 2
        call_id = caller.dialog_ids.call_id
        relay = lab.calls.resource_for(call_id, "relay")
        destination = lab.calls.sip_client_for(lab.calls.bridge_for(call_id)[1])
        # This is the actual asymmetric contract: the decoder answer is 2,
        # while the distinct local receive contract still comes from our offer.
        assert destination.dialog.send_video_format.max_framerate == 2
        assert destination.dialog.recv_video_format.max_framerate == caller_fps

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
        assert destination.dialog.recv_video_format.max_framerate == caller_fps
        assert len(receiver.invites) == expected_invites
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
