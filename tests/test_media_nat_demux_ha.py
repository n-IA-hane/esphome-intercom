"""Late STUN responses cannot evict media or masquerade as RTP loss."""

import asyncio
import struct

import pytest

from custom_components.voip_stack.audio_ws_view import _RtpAudioProtocol, _rx_queue_frame_limit
from custom_components.voip_stack.video_ws_view import _RtpVideoProtocol, _ByteBudgetQueue
from custom_components.voip_stack.sip_rtp_bridge import SipRtpRelay, RtpPeer
from custom_components.voip_stack.sip_video_relay import SipVideoRtpRelay, VideoRtpPeer
from custom_components.voip_stack.core.audio_format import AudioFormat, PcmFormat
from custom_components.voip_stack.core.sdp import RtpVideoFormat

pytestmark = pytest.mark.ha
STUN = struct.pack("!HHI", 0x0101, 0, 0x2112A442) + bytes(range(12))


@pytest.mark.parametrize("frame_ms,expected", [(2.5, 16), (13.5, 12), (15, 11), (32, 5), (40, 4)])
def test_audio_receive_queue_preserves_fractional_packet_duration(frame_ms, expected):
    queue = asyncio.Queue(maxsize=16)
    protocol = _RtpAudioProtocol(queue, frame_ms=frame_ms)

    assert _rx_queue_frame_limit(frame_ms) == expected
    assert type(protocol.frame_limit) is int
    assert protocol.frame_limit == expected
    protocol.reconfigure_frame_ms(13.5)
    assert protocol.frame_limit == 12


def test_late_stun_does_not_evict_audio_or_video_queue():
    queue = asyncio.Queue(maxsize=1)
    original = (b"existing media", ("192.168.1.1", 1234))
    queue.put_nowait(original)
    audio = _RtpAudioProtocol(queue, frame_ms=20)
    audio.datagram_received(STUN, ("198.51.100.1", 3478))
    assert queue.get_nowait() is original and audio.dropped_packets == 0
    video_queue = _ByteBudgetQueue(maxsize=1, max_bytes=100, item_size=lambda item: len(item[0]))
    video_queue.put_nowait(original)
    video = _RtpVideoProtocol(video_queue, source_allowed=lambda _host: False, min_datagram_bytes=12)
    video.datagram_received(STUN, ("198.51.100.1", 3478))
    assert video_queue.get_nowait() is original and video.dropped_packets == 0


def test_late_stun_does_not_change_relay_source_or_drop_count():
    fmt = AudioFormat(16000, PcmFormat.S16LE, 1, 20)
    audio = SipRtpRelay(left=RtpPeer("192.168.1.1", 1234, 96, fmt),
                        right=RtpPeer("192.168.1.2", 1236, 96, fmt), left_port=41000, right_port=41002)
    audio.handle_packet("right", STUN, ("198.51.100.1", 3478))
    assert audio.dropped == 0 and audio.right.rx_ssrc is None and audio.right.host == "192.168.1.2"
    video = SipVideoRtpRelay(left=VideoRtpPeer("192.168.1.1", 1234, 1235, RtpVideoFormat(26, "JPEG")),
                            right=VideoRtpPeer("192.168.1.2", 1236, 1237, RtpVideoFormat(26, "JPEG")),
                            left_port=41004, right_port=41006)
    video.handle_rtp("right", STUN, ("198.51.100.1", 3478))
    video.handle_rtcp("right", STUN, ("198.51.100.1", 3478))
    assert video.dropped == 0 and video.right.rx_ssrc is None and video.right.host == "192.168.1.2"
    # Invalid ordinary UDP remains a rejection; this is not a blanket counter reset.
    audio.handle_packet("right", b"not media", ("198.51.100.1", 3478))
    video.handle_rtp("right", b"not media", ("198.51.100.1", 3478))
    assert audio.dropped == 1 and video.dropped == 1
