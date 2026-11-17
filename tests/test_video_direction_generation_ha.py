"""Real UDP and FFmpeg retain one active direction across an SDP direction change."""

import asyncio
from dataclasses import replace
import socket

import pytest

from custom_components.voip_stack.core import rtp, sdp, video_rtp
from custom_components.voip_stack.sip_video_relay import SipVideoRtpRelay, VideoRtpPeer
from tests.test_automation_originate_ha import lab as lab

pytestmark = [pytest.mark.ha, pytest.mark.slow]


@pytest.mark.parametrize("outcome", ["commit", "rollback"])
async def test_direction_change_retains_real_decoder_pid_fd_and_rtp(lab, outcome):
    peers = []
    for _ in range(2):
        peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        peer.bind(("127.0.0.1", 0))
        peer.setblocking(False)
        peers.append(peer)
    left_socket, right_socket = peers
    left = VideoRtpPeer("127.0.0.1", left_socket.getsockname()[1], left_socket.getsockname()[1],
        sdp.RtpVideoFormat(payload_type=104, encoding="VP8", max_framerate=10))
    right = VideoRtpPeer("127.0.0.1", right_socket.getsockname()[1], right_socket.getsockname()[1],
        sdp.RtpVideoFormat(payload_type=26, encoding="JPEG", max_framerate=10))
    left_pair, right_pair = (SipVideoRtpRelay._loopback_socket_pair() for _ in range(2))
    relay = SipVideoRtpRelay(left=left, right=right, left_port=left_pair[0].getsockname()[1],
        right_port=right_pair[0].getsockname()[1], left_socket=left_pair[0],
        right_socket=right_pair[0], left_rtcp_socket=left_pair[1], right_rtcp_socket=right_pair[1])
    relay.configure_transcoding(lab.hass, "direction-real")
    sender = None
    process = await asyncio.create_subprocess_exec("ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=blue:size=320x240:rate=10", "-frames:v", "1", "-an",
        "-c:v", "mjpeg", "-pix_fmt", "yuvj420p", "-threads", "1", "-huffman", "default",
        "-force_duplicated_matrix", "1", "-f", "image2pipe", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    jpeg, error = await asyncio.wait_for(process.communicate(), 5)
    assert process.returncode == 0, error.decode()

    async def feed():
        sequence = 0
        for frame in range(150):
            packets = video_rtp.packetize_jpeg(jpeg, payload_type=26, sequence=sequence,
                timestamp=frame * 9000, ssrc=0x11223344)
            for packet in packets:
                await lab.hass.loop.sock_sendto(right_socket, rtp.build_packet(packet), ("127.0.0.1", relay.right_port))
            sequence += len(packets)
            await asyncio.sleep(.1)

    decoder = video_rtp.Vp8Depacketizer()

    async def receive_frames():
        frames = []
        async with asyncio.timeout(5):
            while len(frames) < 3:
                raw = await lab.hass.loop.sock_recv(left_socket, 65535)
                if len(raw) < 12 or raw[0] >> 6 != 2 or raw[1] & 0x7f != 104:
                    continue
                frame = decoder.push(rtp.parse_packet(raw))
                if frame is not None:
                    frames.append(frame)
        return frames

    try:
        await relay.start()
        original = relay._transcode
        retained = original.transcoders["right"]
        retired = original.transcoders["left"]
        pid = retained.process.pid
        output = original.transports[("right", False)]
        fd = output.get_extra_info("socket").fileno()
        relay_sockets = {key: transport.get_extra_info("socket").fileno()
            for key, transport in relay._transports.items()}
        sender = asyncio.create_task(feed())
        before = await receive_frames()
        receive_only = replace(left, video_format=replace(left.video_format, direction="recvonly"))
        relay.stage_peer_reconfiguration("left", receive_only)
        relay.stage_peer_reconfiguration("right", right)
        commit, rollback = await relay.async_prepare_peer_generation(left=receive_only, right=right)
        await (commit() if outcome == "commit" else rollback())
        assert relay._transcode is original
        assert original.transcoders["right"] is retained
        assert retained.process.pid == pid and retained.process.returncode is None
        assert original.transports[("right", False)] is output
        assert output.get_extra_info("socket").fileno() == fd
        assert {key: transport.get_extra_info("socket").fileno()
            for key, transport in relay._transports.items()} == relay_sockets
        assert original.directions == ({"right"} if outcome == "commit" else {"left", "right"})
        after = await receive_frames()
        assert after[0].timestamp > before[-1].timestamp
        if outcome == "commit":
            assert retired.process is None
            relay.stage_peer_reconfiguration("left", left)
            relay.stage_peer_reconfiguration("right", right)
            commit, _rollback = await relay.async_prepare_peer_generation(left=left, right=right)
            await commit()
            assert original.transcoders["right"] is retained
            assert retained.process.pid == pid
            assert original.directions == {"left", "right"}
            assert (await receive_frames())[0].timestamp > after[-1].timestamp
    finally:
        if sender is not None:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        workers = list(relay._transcode.transcoders.values())
        await relay.stop()
        assert all(worker.process is None for worker in workers)
        assert not relay._transports and not relay._transcode.transcoders
        assert lab.runtime.media.transcoder is None
        for sock in peers:
            sock.close()
