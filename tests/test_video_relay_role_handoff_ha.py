"""An active relay reverses roles without replacing its sockets or conversion worker."""

from types import SimpleNamespace

import pytest

from custom_components.voip_stack.core import rtp, sdp
from custom_components.voip_stack.sip_video_relay import (
    SipVideoRtpRelay, VideoRtpPeer, _VideoRelayProtocol, _TranscodedOutputProtocol,
)

pytestmark = pytest.mark.ha


class OwnedTransport:
    def __init__(self, protocol):
        self.protocol = protocol
        self.packets = []

    def get_protocol(self):
        return self.protocol

    def is_closing(self):
        return False

    def sendto(self, data, address):
        self.packets.append((data, address))


def test_active_video_role_handoff_preserves_worker_and_routes_both_directions():
    h264 = sdp.RtpVideoFormat(payload_type=103, encoding="H264")
    jpeg = sdp.RtpVideoFormat(payload_type=26, encoding="JPEG")
    left = VideoRtpPeer("192.0.2.1", 40000, 40001, h264)
    right = VideoRtpPeer("192.0.2.2", 41000, 41001, jpeg,
                         local_video_format=sdp.RtpVideoFormat(payload_type=108, encoding="H264"))
    relay = SipVideoRtpRelay(left=left, right=right, left_port=42000, right_port=42002)
    public = {(side, control): OwnedTransport(_VideoRelayProtocol(relay, side, rtcp=control))
              for side in ("left", "right") for control in (False, True)}
    relay._transports = dict(public)
    relay._sockets = dict.fromkeys(public)
    inputs = []
    worker = SimpleNamespace(ready=True, stopping=False, send_rtp=inputs.append)
    generation = relay._transcode
    generation.directions = {"left"}
    generation.transcoders = {"left": worker}
    generation.transports = {("left", control): OwnedTransport(_TranscodedOutputProtocol(relay, "left", rtcp=control))
                             for control in (False, True)}
    generation.sockets = dict.fromkeys(generation.transports)
    relay._transcoded_ssrc = {"left": 1234}
    relay._transcode_slot_claimed = relay.started = True
    relay.left_rx_packets, relay.right_rx_packets = 10, 20
    relay.reverse_held_sides()
    assert relay._transcode is generation and generation.transcoders == {"right": worker}
    assert generation.directions == {"right"} and relay._transcode_slot_claimed
    assert relay.left is right and relay.right is left
    assert relay.left_rx_packets == 20 and relay.right_rx_packets == 10
    assert generation.transports[("right", False)].get_protocol().source_side == "right"
    assert relay._transports[("left", False)] is public[("right", False)]
    packet = rtp.build_packet(rtp.RtpPacket(103, 1, 3000, 11, b"\x65\x88", marker=True))
    relay.handle_rtp("right", packet, (left.host, left.port))
    assert inputs == [packet]
    output = rtp.build_packet(rtp.RtpPacket(26, 2, 3000, 1234, b"jpeg", marker=True))
    generation.transports[("right", False)].get_protocol().datagram_received(output, ("127.0.0.1", 43000))
    assert public[("right", False)].packets[-1] == (output, (right.host, right.port))
    passthrough = rtp.build_packet(rtp.RtpPacket(108, 3, 3000, 22, b"\x65\x99", marker=True))
    relay.handle_rtp("left", passthrough, (right.host, right.port))
    actual, address = public[("left", False)].packets[-1]
    assert address == (left.host, left.port) and rtp.parse_packet(actual).payload_type == 103
    assert actual[2:] == passthrough[2:]
    relay.reverse_held_sides()
    assert relay.left is left and relay.right is right
    assert generation.transcoders == {"left": worker} and generation.directions == {"left"}
    assert all(transport.get_protocol().side == side for (side, _control), transport in public.items())
    assert generation.transports[("left", False)].get_protocol().source_side == "left"
