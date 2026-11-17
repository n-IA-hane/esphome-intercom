from __future__ import annotations

import asyncio
from dataclasses import replace
import socket
import types
import unittest
from unittest import mock

import pytest

from custom_components.voip_stack.core import rtp, sdp, video_rtcp
from custom_components.voip_stack import sip_video_relay

pytestmark = [pytest.mark.ha, pytest.mark.usefixtures("socket_enabled")]

RtpVideoFormat = sdp.RtpVideoFormat
video_formats_passthrough_compatible = sdp.video_formats_passthrough_compatible
SipVideoRtpRelay = sip_video_relay.SipVideoRtpRelay
VideoRtpPeer = sip_video_relay.VideoRtpPeer
build_pli = video_rtcp.build_pli


class _Transport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False

    def sendto(self, data: bytes, addr) -> None:
        self.sent.append((data, addr))

    def close(self) -> None:
        self.closed = True


def _format(
    payload_type: int, *, direction: str = "sendrecv", profile: str = "42e01f"
) -> RtpVideoFormat:
    return RtpVideoFormat(
        payload_type=payload_type,
        encoding="H264",
        profile_level_id=profile,
        packetization_mode=1,
        direction=direction,
    )


@pytest.mark.parametrize("outcome", ["commit", "rollback"])
def test_direction_update_keeps_unchanged_worker_and_never_duplicates_a_direction(outcome):
    async def scenario():
        left = VideoRtpPeer("127.0.0.1", 41000, 41001, RtpVideoFormat(payload_type=104, encoding="VP8"))
        right = VideoRtpPeer("127.0.0.1", 42000, 42001, RtpVideoFormat(payload_type=26, encoding="JPEG"))
        relay = SipVideoRtpRelay(left=left, right=right, left_port=43000, right_port=43002)
        relay.configure_transcoding(types.SimpleNamespace(data={}), "direction-owner")
        relay.started, relay._transcode_slot_claimed = True, True
        relay._maintain_nat = mock.Mock()
        alive = {side: set() for side in ("left", "right")}
        peak = {side: 0 for side in alive}
        starts, closes = [], []

        class Worker:
            ready = True
            def __init__(self, side):
                self.side = side
                alive[side].add(self)
                peak[side] = max(peak[side], len(alive[side]))
                starts.append(self)
            async def async_close(self):
                if self in alive[self.side]:
                    alive[self.side].remove(self)
                    closes.append(self)

        async def build(_loop, candidate_left, candidate_right, *, directions=None, **_kwargs):
            selected = relay.transcode_directions_for(candidate_left, candidate_right) if directions is None else set(directions)
            return sip_video_relay._TranscodeGeneration(selected, {side: Worker(side) for side in selected}, {}, {})

        relay._transcode = await build(None, left, right)
        original_generation = relay._transcode
        original_left = relay._transcode.transcoders["left"]
        original_right = relay._transcode.transcoders["right"]
        held_right = replace(right, video_format=replace(right.video_format, direction="recvonly"))
        with mock.patch.object(relay, "_build_transcode_generation", side_effect=build), \
             mock.patch.object(sip_video_relay, "_claim_transcoder_slot", new=mock.AsyncMock()), \
             mock.patch.object(sip_video_relay, "_release_transcoder_slot", new=mock.AsyncMock()):
            try:
                relay.stage_peer_reconfiguration("right", held_right)
                commit, rollback = await relay.async_prepare_peer_generation(left=left, right=held_right)
                await (commit() if outcome == "commit" else rollback())
                assert peak == {"left": 1, "right": 1}
                assert relay._transcode is original_generation
                assert relay._transcode.transcoders["left"] is original_left
                if outcome == "rollback":
                    assert relay._transcode.transcoders["right"] is original_right
                    assert len(starts) == 2 and not closes
                    return
                assert relay._transcode.directions == {"left"}
                assert closes == [original_right] and len(starts) == 2
                relay.stage_peer_reconfiguration("right", right)
                commit, _rollback = await relay.async_prepare_peer_generation(left=left, right=right)
                await commit()
                assert relay._transcode is original_generation
                assert relay._transcode.transcoders["left"] is original_left
                assert relay._transcode.transcoders["right"] is not original_right
                assert peak == {"left": 1, "right": 1} and len(starts) == 3
            finally:
                await relay.stop()
        assert all(not workers for workers in alive.values())
    asyncio.run(scenario())


class SipVideoRelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.left = VideoRtpPeer("10.0.0.1", 10000, 10001, _format(102))
        self.right = VideoRtpPeer("10.0.0.2", 20000, 20001, _format(110))
        self.relay = SipVideoRtpRelay(
            left=self.left,
            right=self.right,
            left_port=30000,
            right_port=30002,
        )
        self.right_rtp = _Transport()
        self.left_rtp = _Transport()
        self.right_rtcp = _Transport()
        self.left_rtcp = _Transport()
        self.relay._transports = {  # noqa: SLF001 - deterministic unit boundary.
            ("left", False): self.left_rtp,
            ("right", False): self.right_rtp,
            ("left", True): self.left_rtcp,
            ("right", True): self.right_rtcp,
        }

    def test_sdp_direction_controls_remote_media_capabilities(self) -> None:
        expected = {
            "sendonly": (True, False),
            "recvonly": (False, True),
            "sendrecv": (True, True),
            "inactive": (False, False),
        }

        for direction, (can_send, can_receive) in expected.items():
            with self.subTest(direction=direction):
                video_format = _format(102, direction=direction)
                self.assertIs(
                    sip_video_relay.remote_can_send(video_format),
                    can_send,
                )
                self.assertIs(
                    sip_video_relay.remote_can_receive(video_format),
                    can_receive,
                )

        self.assertFalse(sip_video_relay.remote_can_send(None))
        self.assertFalse(sip_video_relay.remote_can_receive(None))
        self.assertFalse(
            sip_video_relay.remote_can_receive(
                _format(102, direction="sendrecv"),
                connection_held=True,
            )
        )

    def test_rewrites_only_leg_local_payload_type(self) -> None:
        packet = rtp.build_packet(
            rtp.RtpPacket(
                payload_type=102,
                sequence=7,
                timestamp=12345,
                ssrc=0x11223344,
                marker=True,
                payload=b"encoded-frame",
            )
        )

        self.relay.handle_rtp("left", packet, ("10.0.0.1", 10008))

        self.assertEqual(len(self.right_rtp.sent), 1)
        outgoing, destination = self.right_rtp.sent[0]
        self.assertEqual(destination, ("10.0.0.2", 20000))
        self.assertEqual(outgoing[:1], packet[:1])
        self.assertEqual(outgoing[2:], packet[2:])
        parsed = rtp.parse_packet(outgoing)
        self.assertEqual(parsed.payload_type, 110)
        self.assertTrue(parsed.marker)
        self.assertEqual(parsed.sequence, 7)
        self.assertEqual(parsed.timestamp, 12345)
        self.assertEqual(parsed.ssrc, 0x11223344)
        self.assertEqual(self.left.port, 10008)

    def test_accepts_signaling_host_for_nat_media_but_rejects_other_hosts(self) -> None:
        self.left.host = "10.0.0.20"
        self.left.signaling_host = "198.51.100.20"
        packet = rtp.build_packet(
            rtp.RtpPacket(
                payload_type=102,
                sequence=7,
                timestamp=12345,
                ssrc=0x11223344,
                marker=True,
                payload=b"encoded-frame",
            )
        )

        self.relay.handle_rtp("left", packet, ("198.51.100.20", 10008))
        self.relay.handle_rtp("left", packet, ("203.0.113.20", 10008))

        self.assertEqual(len(self.right_rtp.sent), 1)
        self.assertEqual(self.left.host, "198.51.100.20")
        self.assertEqual(self.left.port, 10008)
        self.assertEqual(self.relay.dropped, 1)

    def test_explicit_rtcp_host_cannot_latch_the_rtp_leg(self) -> None:
        self.left.rtcp_host = "198.51.100.21"
        packet = rtp.build_packet(
            rtp.RtpPacket(
                payload_type=102,
                sequence=7,
                timestamp=12345,
                ssrc=0x11223344,
                marker=True,
                payload=b"encoded-frame",
            )
        )

        self.relay.handle_rtp("left", packet, ("198.51.100.21", 10008))

        self.assertFalse(self.right_rtp.sent)
        self.assertIsNone(self.left.rx_ssrc)
        self.assertEqual(self.left.host, "10.0.0.1")
        self.assertEqual(self.relay.dropped, 1)

    def test_rejects_unnegotiated_direction_and_ssrc_change(self) -> None:
        self.left.video_format = _format(102, direction="recvonly")
        packet = rtp.build_packet(rtp.RtpPacket(102, 1, 1, 1, b"x"))
        self.relay.handle_rtp("left", packet, ("10.0.0.1", 10000))
        self.assertFalse(self.right_rtp.sent)

        self.left.video_format = _format(102)
        self.relay.handle_rtp("left", packet, ("10.0.0.1", 10000))
        changed = rtp.build_packet(rtp.RtpPacket(102, 2, 2, 2, b"x"))
        self.relay.handle_rtp("left", changed, ("10.0.0.1", 10000))
        self.assertEqual(len(self.right_rtp.sent), 1)
        self.assertEqual(self.relay.dropped, 2)

    def test_forwards_valid_rtcp_feedback(self) -> None:
        pli = build_pli(1, 2)
        self.relay.handle_rtcp("right", pli, ("10.0.0.2", 20009))
        self.assertEqual(self.left_rtcp.sent, [(pli, ("10.0.0.1", 10001))])
        self.assertEqual(self.right.rtcp_port, 20009)

        self.relay.handle_rtcp("right", b"bad", ("10.0.0.2", 20009))
        self.assertEqual(self.relay.dropped, 1)

        # An unexpected source is an ordinary media drop, not an exception
        # escaping asyncio.DatagramProtocol.datagram_received().
        self.relay.handle_rtcp("right", pli, ("10.0.0.99", 20009))
        self.assertEqual(self.relay.dropped, 2)

    def test_rtcp_source_port_is_latched_after_first_valid_packet(self) -> None:
        pli = build_pli(1, 2)

        self.relay.handle_rtcp("right", pli, ("10.0.0.2", 20009))
        self.relay.handle_rtcp("right", pli, ("10.0.0.2", 20010))

        self.assertEqual(self.right.rtcp_source_port, 20009)
        self.assertEqual(self.left_rtcp.sent, [(pli, ("10.0.0.1", 10001))])
        self.assertEqual(self.relay.dropped, 1)

    def test_rtcp_feedback_must_target_observed_destination_ssrc(self) -> None:
        packet = rtp.build_packet(rtp.RtpPacket(102, 1, 1, 0x11223344, b"encoded"))
        self.relay.handle_rtp("left", packet, ("10.0.0.1", 10000))

        wrong = build_pli(1, 0x55667788)
        correct = build_pli(1, 0x11223344)
        self.relay.handle_rtcp("right", wrong, ("10.0.0.2", 20009))
        self.relay.handle_rtcp("right", correct, ("10.0.0.2", 20009))

        self.assertEqual(self.left_rtcp.sent, [(correct, ("10.0.0.1", 10001))])
        self.assertEqual(self.relay.dropped, 1)

    def test_transcoder_requests_negotiated_rtcp_fir_on_first_source_packet(
        self,
    ) -> None:
        self.right.local_video_format = RtpVideoFormat(
            payload_type=110,
            encoding="H264",
            transport_profile="RTP/AVP",
            rtcp_feedback=("ccm fir",),
        )
        self.relay._transcode.directions = {"right"}  # noqa: SLF001

        self.assertTrue(self.relay.arm_keyframe_request("right"))
        packet = rtp.build_packet(rtp.RtpPacket(110, 1, 1, 0x11223344, b"encoded"))
        self.relay.handle_rtp("right", packet, (self.right.host, self.right.port))

        self.assertEqual(len(self.right_rtcp.sent), 1)
        raw, destination = self.right_rtcp.sent[0]
        self.assertEqual(destination, (self.right.rtcp_host, self.right.rtcp_port))
        feedback = video_rtcp.parse_compound(raw)[-1]
        self.assertEqual((feedback.packet_type, feedback.fmt), (206, 4))
        self.assertTrue(self.relay.arm_keyframe_request("right"))
        self.relay.handle_rtp("right", packet, (self.right.host, self.right.port))
        self.assertEqual(len(self.right_rtcp.sent), 1)

    def test_avp_transcoder_does_not_emit_unnegotiated_rtcp_feedback(self) -> None:
        self.relay._transcode.directions = {"right"}  # noqa: SLF001

        self.assertFalse(self.relay.arm_keyframe_request("right"))

    def test_connection_hold_blocks_only_traffic_toward_held_leg(self) -> None:
        self.left.connection_held = True
        packet = rtp.build_packet(rtp.RtpPacket(110, 1, 1, 1, b"x"))
        pli = build_pli(1, 2)

        self.relay.handle_rtp("right", packet, (self.right.host, self.right.port))
        self.relay.handle_rtcp(
            "right",
            pli,
            (self.right.rtcp_host, self.right.rtcp_port),
        )
        self.assertFalse(self.left_rtp.sent)
        self.assertFalse(self.left_rtcp.sent)
        self.assertEqual(self.relay.drop_connection_hold, 2)

        from_left = rtp.build_packet(rtp.RtpPacket(102, 2, 2, 2, b"x"))
        self.relay.handle_rtp("left", from_left, (self.left.host, self.left.port))
        self.assertEqual(len(self.right_rtp.sent), 1)

    def test_rtcp_uses_separately_advertised_destination_address(self) -> None:
        self.left.rtcp_host = "198.51.100.10"
        pli = build_pli(1, 2)

        self.relay.handle_rtcp("right", pli, ("10.0.0.2", 20009))

        self.assertEqual(
            self.left_rtcp.sent,
            [(pli, ("198.51.100.10", 10001))],
        )
        self.assertEqual(self.right.rtcp_host, "10.0.0.2")

    def test_passthrough_compatibility_ignores_pt_and_direction(self) -> None:
        self.assertTrue(
            video_formats_passthrough_compatible(
                _format(102, direction="sendonly"),
                _format(110, direction="recvonly"),
            )
        )
        self.assertFalse(
            video_formats_passthrough_compatible(
                _format(102),
                _format(110, profile="64001f"),
            )
        )

    def test_peer_reconfiguration_is_staged_until_commit(self) -> None:
        replacement = VideoRtpPeer("10.0.0.9", 19000, 19001, _format(120))

        commit = self.relay.prepare_peer_reconfiguration("left", replacement)

        self.assertIs(self.relay.left, self.left)
        commit()
        self.assertIs(self.relay.left, replacement)
        self.assertIsNone(replacement.rx_ssrc)
        self.assertIsNone(replacement.rtcp_source_port)

    def test_stale_same_side_peer_reconfiguration_is_rejected(self) -> None:
        first = VideoRtpPeer("10.0.0.9", 19000, 19001, _format(120))
        second = VideoRtpPeer("10.0.0.10", 20000, 20001, _format(121))
        stale_commit = self.relay.prepare_peer_reconfiguration("left", first)

        self.relay.reconfigure_peer("left", second)

        with self.assertRaisesRegex(RuntimeError, "video relay peer changed"):
            stale_commit()
        self.assertIs(self.relay.left, second)

    def test_direction_reoffer_stages_in_band_headers_until_commit(self) -> None:
        self.right.video_format = _format(110, direction="recvonly")
        replacement = VideoRtpPeer(
            self.right.host,
            self.right.port,
            self.right.rtcp_port,
            _format(110, direction="sendrecv"),
        )
        commit, _rollback = self.relay.stage_peer_reconfiguration("right", replacement)
        packet = rtp.build_packet(
            rtp.RtpPacket(
                payload_type=110,
                sequence=1,
                timestamp=90_000,
                ssrc=123,
                marker=True,
                payload=b"sps-pps-idr",
            )
        )

        self.relay.handle_rtp("right", packet, (self.right.host, self.right.port))

        self.assertEqual(self.left_rtp.sent, [])
        self.assertEqual(self.relay.dropped, 0)
        commit()
        self.assertEqual(len(self.left_rtp.sent), 1)
        forwarded, target = self.left_rtp.sent[0]
        self.assertEqual(target, (self.left.host, self.left.port))
        self.assertEqual(rtp.parse_packet(forwarded).payload, b"sps-pps-idr")
        self.assertEqual(rtp.parse_packet(forwarded).payload_type, 102)

    def test_passthrough_generation_does_not_require_transcoder(self) -> None:
        self.relay.started = True
        replacement_left = VideoRtpPeer(
            self.left.host,
            self.left.port,
            self.left.rtcp_port,
            _format(102, direction="sendrecv"),
        )
        replacement_right = VideoRtpPeer(
            self.right.host,
            self.right.port,
            self.right.rtcp_port,
            _format(110, direction="sendrecv"),
        )
        self.relay.stage_peer_reconfiguration("left", replacement_left)
        self.relay.stage_peer_reconfiguration("right", replacement_right)

        commit, _rollback = asyncio.run(
            self.relay.async_prepare_peer_generation(
                left=replacement_left,
                right=replacement_right,
            )
        )

        self.assertIs(self.relay.left, self.left)
        self.assertIs(self.relay.right, self.right)
        asyncio.run(commit())
        self.assertIs(self.relay.left, replacement_left)
        self.assertIs(self.relay.right, replacement_right)
        self.assertFalse(self.relay.transcoding)
        self.assertIsNone(self.relay._transcode_hass)  # noqa: SLF001

    def test_direction_change_preserves_transcoder_but_codec_change_does_not(self) -> None:
        self.relay.started = True
        self.left.video_format = RtpVideoFormat(payload_type=116, encoding="VP8")
        self.right.video_format = RtpVideoFormat(payload_type=26, encoding="JPEG")
        generation = self.relay._transcode
        generation.directions = {"left", "right"}
        replacement_left = replace(self.left)
        replacement_right = replace(
            self.right, video_format=replace(self.right.video_format, direction="recvonly"),
        )
        self.relay.stage_peer_reconfiguration("left", replacement_left)
        self.relay.stage_peer_reconfiguration("right", replacement_right)
        commit, _rollback = asyncio.run(self.relay.async_prepare_peer_generation(
            left=replacement_left, right=replacement_right,
        ))
        asyncio.run(commit())
        self.assertIs(self.relay._transcode, generation)
        self.assertIs(self.relay.right, replacement_right)
        changed = replace(
            replacement_right,
            video_format=replace(replacement_right.video_format, max_framerate=5),
        )
        self.relay.stage_peer_reconfiguration("right", changed)
        # Changed encoder limits must enter the full generation preparation.
        with self.assertRaisesRegex(RuntimeError, "transcoding is not configured"):
            asyncio.run(self.relay.async_prepare_peer_generation(
                left=replacement_left, right=changed,
            ))

    def test_receive_only_nat_uses_stun_and_timer_stops_with_relay(self) -> None:
        async def exercise():
            self.relay.started = True
            self.left.video_format = replace(self.left.video_format, direction="sendonly")
            self.relay._maintain_nat()
            packet, target = self.left_rtp.sent[-1]
            self.assertEqual(target, (self.left.host, self.left.port))
            self.assertEqual(len(packet), 20)
            self.assertEqual(packet[:8], bytes.fromhex("001100002112a442"))
            timer = self.relay._nat_timer
            self.relay._maintain_nat()
            self.assertTrue(timer.cancelled())
            timer = self.relay._nat_timer
            self.relay._close_resources()
            self.assertTrue(timer.cancelled())
            self.assertIsNone(self.relay._nat_timer)
        asyncio.run(exercise())


    def test_opposite_side_staged_reconfigurations_commit_independently(self) -> None:
        left = VideoRtpPeer("10.0.0.9", 19000, 19001, _format(120))
        right = VideoRtpPeer("10.0.0.10", 20000, 20001, _format(121))
        commit_left = self.relay.prepare_peer_reconfiguration("left", left)
        commit_right = self.relay.prepare_peer_reconfiguration("right", right)

        commit_left()
        commit_right()

        self.assertIs(self.relay.left, left)
        self.assertIs(self.relay.right, right)

    def test_vp8_receiver_limits_differ_while_rtp_relays_both_directions(self) -> None:
        self.left.video_format = RtpVideoFormat(
            payload_type=103,
            encoding="VP8",
            fmtp="max-fr=30;max-fs=3600",
        )
        self.right.video_format = RtpVideoFormat(
            payload_type=110,
            encoding="VP8",
            fmtp="max-fr=15;max-fs=1200",
        )
        self.assertTrue(
            video_formats_passthrough_compatible(
                self.left.video_format,
                self.right.video_format,
            )
        )

        left_packet = rtp.build_packet(rtp.RtpPacket(103, 1, 9000, 0x1111, b"left-vp8"))
        right_packet = rtp.build_packet(
            rtp.RtpPacket(110, 2, 18000, 0x2222, b"right-vp8")
        )
        self.relay.handle_rtp("left", left_packet, (self.left.host, self.left.port))
        self.relay.handle_rtp("right", right_packet, (self.right.host, self.right.port))

        self.assertEqual(rtp.parse_packet(self.right_rtp.sent[0][0]).payload_type, 110)
        self.assertEqual(rtp.parse_packet(self.left_rtp.sent[0][0]).payload_type, 103)

    def test_h264_bilateral_levels_are_enforced_per_relay_direction(self) -> None:
        # Caller offered Level 3.1 but was answered at Level 1.3 after the
        # destination selected Level 1.3.  The reverse stream still uses the
        # original Level 3.1 receive envelope on both SDP legs.
        self.left.video_format = _format(102, profile="42801f")
        self.left.local_video_format = _format(102, profile="42800d")
        self.right.video_format = _format(110, profile="42800d")
        self.right.local_video_format = _format(110, profile="42801f")

        toward_right = rtp.build_packet(
            rtp.RtpPacket(102, 1, 9000, 0x1111, b"low-level")
        )
        toward_left = rtp.build_packet(
            rtp.RtpPacket(110, 2, 18000, 0x2222, b"high-level")
        )

        self.relay.handle_rtp("left", toward_right, (self.left.host, self.left.port))
        self.relay.handle_rtp("right", toward_left, (self.right.host, self.right.port))

        self.assertEqual(len(self.right_rtp.sent), 1)
        self.assertEqual(len(self.left_rtp.sent), 1)
        self.assertEqual(
            rtp.parse_packet(self.right_rtp.sent[0][0]).payload_type,
            110,
        )
        self.assertEqual(
            rtp.parse_packet(self.left_rtp.sent[0][0]).payload_type,
            102,
        )
        snapshot = self.relay.snapshot()
        self.assertIn("profile-level-id=42800d", snapshot["left_recv_format"])
        self.assertIn("profile-level-id=42801f", snapshot["left_send_format"])

    def test_cross_codec_packets_are_delivered_to_directional_transcoder(self) -> None:
        jpeg = RtpVideoFormat(payload_type=26, encoding="JPEG")
        h264 = _format(105, profile="42c00c")
        self.left.video_format = jpeg
        self.left.local_video_format = jpeg
        self.right.video_format = h264
        self.right.local_video_format = h264
        self.relay.configure_transcoding(types.SimpleNamespace(data={}), "cross-codec")

        class Transcoder:
            def __init__(self) -> None:
                self.received: list[bytes] = []

            def send_rtp(self, data: bytes) -> None:
                self.received.append(data)

        left_transcoder = Transcoder()
        right_transcoder = Transcoder()
        self.relay._transcode.transcoders = {  # noqa: SLF001
            "left": left_transcoder,
            "right": right_transcoder,
        }
        left_packet = rtp.build_packet(rtp.RtpPacket(26, 1, 9000, 0x1111, b"jpeg"))
        right_packet = rtp.build_packet(rtp.RtpPacket(105, 2, 18000, 0x2222, b"h264"))

        self.relay.handle_rtp("left", left_packet, (self.left.host, self.left.port))
        self.relay.handle_rtp("right", right_packet, (self.right.host, self.right.port))

        self.assertEqual(left_transcoder.received, [left_packet])
        self.assertEqual(right_transcoder.received, [right_packet])
        self.assertFalse(self.left_rtp.sent)
        self.assertFalse(self.right_rtp.sent)
        self.assertEqual(self.relay.transcoded_input_packets, 2)

    def test_cross_codec_recvonly_retains_later_send_authorization(self) -> None:
        """Only the active direction converts, retaining the same relay owner."""

        vp8 = RtpVideoFormat(
            payload_type=104,
            encoding="VP8",
            direction="recvonly",
        )
        jpeg = RtpVideoFormat(
            payload_type=26,
            encoding="JPEG",
            direction="sendonly",
        )
        self.left.video_format = vp8
        self.left.local_video_format = vp8
        self.right.video_format = jpeg
        self.right.local_video_format = jpeg

        owner = types.SimpleNamespace(data={})
        self.relay.configure_transcoding(owner, "direction-update")
        self.assertEqual(self.relay._transcode.directions, {"right"})
        self.assertIs(self.relay._transcode_hass, owner)
        next_left = replace(self.left, video_format=replace(vp8, direction="sendrecv"))
        next_right = replace(self.right, video_format=replace(jpeg, direction="sendrecv"))
        self.assertEqual(
            self.relay.transcode_directions_for(next_left, next_right),
            {"left", "right"},
        )

    def test_cross_codec_startup_packets_wait_for_ffmpeg_input(self) -> None:
        jpeg = RtpVideoFormat(payload_type=26, encoding="JPEG")
        h264 = _format(105, profile="42c00c")
        self.left.video_format = jpeg
        self.left.local_video_format = jpeg
        self.right.video_format = h264
        self.right.local_video_format = h264
        self.relay.configure_transcoding(types.SimpleNamespace(data={}), "cross-codec")

        class Transcoder:
            def __init__(self) -> None:
                self.ready = False
                self.received: list[bytes] = []

            def send_rtp(self, data: bytes) -> None:
                self.received.append(data)

        transcoder = Transcoder()
        self.relay._transcode.transcoders = {  # noqa: SLF001
            "right": transcoder
        }
        parameter_set = rtp.build_packet(
            rtp.RtpPacket(105, 1, 9000, 0x2222, b"sps-pps")
        )

        self.relay.handle_rtp(
            "right",
            parameter_set,
            (self.right.host, self.right.port),
        )

        self.assertEqual(transcoder.received, [])
        self.assertEqual(self.relay.transcode_startup_buffered, 1)
        self.assertEqual(self.relay.dropped, 0)
        transcoder.ready = True
        self.relay._flush_transcode_startup_rtp()  # noqa: SLF001
        self.assertEqual(transcoder.received, [parameter_set])
        self.assertEqual(self.relay.transcoded_input_packets, 1)
        self.assertEqual(self.relay.transcode_startup_dropped, 0)

    def test_packets_after_stop_do_not_pollute_media_drop_counter(self) -> None:
        self.relay._stop_requested = True  # noqa: SLF001
        packet = rtp.build_packet(rtp.RtpPacket(102, 1, 1, 1, b"late"))

        self.relay.handle_rtp("left", packet, (self.left.host, self.left.port))

        self.assertEqual(self.relay.dropped, 0)
        self.assertEqual(self.relay.ignored_after_stop, 1)

    def test_transcoded_output_uses_advertised_relay_socket(self) -> None:
        jpeg = RtpVideoFormat(payload_type=26, encoding="JPEG")
        h264 = _format(105, profile="42c00c")
        self.left.video_format = jpeg
        self.left.local_video_format = jpeg
        self.right.video_format = h264
        self.right.local_video_format = h264
        self.relay.configure_transcoding(types.SimpleNamespace(data={}), "cross-codec")
        output = rtp.build_packet(rtp.RtpPacket(105, 1, 9000, 0x5566, b"encoded"))

        self.relay.handle_transcoded_rtp("left", output)

        self.assertEqual(
            self.right_rtp.sent, [(output, (self.right.host, self.right.port))]
        )
        self.assertEqual(self.relay.transcoded_output_packets, 1)
        self.assertEqual(self.relay.right_tx_packets, 1)

    def test_directional_contract_mismatch_drops_only_invalid_path(self) -> None:
        self.left.video_format = _format(102, profile="42800d")
        self.left.local_video_format = _format(102, profile="42800d")
        self.right.video_format = _format(110, profile="42800d")
        self.right.local_video_format = _format(110, profile="42801f")

        invalid_toward_left = rtp.build_packet(
            rtp.RtpPacket(110, 1, 9000, 0x1111, b"too-large")
        )
        valid_toward_right = rtp.build_packet(
            rtp.RtpPacket(102, 2, 18000, 0x2222, b"fits")
        )

        self.relay.handle_rtp(
            "right", invalid_toward_left, (self.right.host, self.right.port)
        )
        self.relay.handle_rtp(
            "left", valid_toward_right, (self.left.host, self.left.port)
        )

        self.assertFalse(self.left_rtp.sent)
        self.assertEqual(len(self.right_rtp.sent), 1)
        self.assertEqual(self.relay.dropped, 1)
        self.assertEqual(
            self.relay.snapshot()["rtp_drop_reasons"],
            {"directional RTP codec contracts are incompatible": 1},
        )


class SipVideoRelayLifecycleTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _relay(released: list[tuple[int, int]]) -> SipVideoRtpRelay:
        return SipVideoRtpRelay(
            left=VideoRtpPeer("10.0.0.1", 10000, 10001, _format(102)),
            right=VideoRtpPeer("10.0.0.2", 20000, 20001, _format(110)),
            left_port=30000,
            right_port=30002,
            on_release=released.append,
        )

    async def test_failed_endpoint_creation_closes_internally_allocated_socket(
        self,
    ) -> None:
        sockets: list[socket.socket] = []

        def allocate(_port: int) -> socket.socket:
            item = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sockets.append(item)
            return item

        released: list[tuple[int, int]] = []
        relay = SipVideoRtpRelay(
            left=VideoRtpPeer("10.0.0.1", 10000, 10001, _format(102)),
            right=VideoRtpPeer("10.0.0.2", 20000, 20001, _format(110)),
            left_port=30000,
            right_port=30002,
            on_release=released.append,
        )
        loop = asyncio.get_running_loop()
        with (
            mock.patch.object(relay, "_socket", side_effect=allocate),
            mock.patch.object(
                loop,
                "create_datagram_endpoint",
                new=mock.AsyncMock(side_effect=OSError("bind failed")),
            ),
        ):
            with self.assertRaisesRegex(OSError, "bind failed"):
                await relay.start()

        self.assertEqual(len(sockets), 1)
        self.assertEqual(sockets[0].fileno(), -1)
        self.assertEqual(released, [(30000, 30002)])

    async def test_failed_endpoint_creation_closes_all_prebound_sockets(self) -> None:
        sockets = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(4)]
        released: list[tuple[int, int]] = []
        relay = SipVideoRtpRelay(
            left=VideoRtpPeer("10.0.0.1", 10000, 10001, _format(102)),
            right=VideoRtpPeer("10.0.0.2", 20000, 20001, _format(110)),
            left_port=30000,
            right_port=30002,
            left_socket=sockets[0],
            right_socket=sockets[1],
            left_rtcp_socket=sockets[2],
            right_rtcp_socket=sockets[3],
            on_release=released.append,
        )
        loop = asyncio.get_running_loop()
        with mock.patch.object(
            loop,
            "create_datagram_endpoint",
            new=mock.AsyncMock(side_effect=OSError("bind failed")),
        ):
            with self.assertRaisesRegex(OSError, "bind failed"):
                await relay.start()

        self.assertTrue(all(item.fileno() == -1 for item in sockets))
        self.assertEqual(released, [(30000, 30002)])

    @pytest.mark.fault
    async def test_stop_racing_start_cannot_resurrect_transports(self) -> None:
        entered = asyncio.Event()
        release_endpoint = asyncio.Event()
        transports: list[_Transport] = []
        released: list[tuple[int, int]] = []
        relay = self._relay(released)

        async def create_endpoint(*_args, **_kwargs):
            transport = _Transport()
            transports.append(transport)
            entered.set()
            try:
                await release_endpoint.wait()
            except asyncio.CancelledError:
                # Model an endpoint factory whose internal completion wins the
                # cancellation race and still returns an acquired transport.
                await release_endpoint.wait()
            return transport, object()

        loop = asyncio.get_running_loop()
        with mock.patch.object(
            loop,
            "create_datagram_endpoint",
            new=create_endpoint,
        ):
            start_task = asyncio.create_task(relay.start())
            await asyncio.wait_for(entered.wait(), timeout=1)
            stop_task = asyncio.create_task(relay.stop())
            await asyncio.sleep(0)
            self.assertFalse(stop_task.done())
            release_endpoint.set()
            await asyncio.wait_for(stop_task, timeout=1)
            with self.assertRaises(asyncio.CancelledError):
                await start_task

        self.assertFalse(relay.started)
        self.assertFalse(relay._transports)  # noqa: SLF001
        self.assertTrue(all(item.closed for item in transports))
        self.assertEqual(released, [(30000, 30002)])

    @pytest.mark.fault
    async def test_cancelled_stop_waiters_cannot_interrupt_owned_cleanup(self) -> None:
        entered = asyncio.Event()
        release_endpoint = asyncio.Event()
        transports: list[_Transport] = []
        released: list[tuple[int, int]] = []
        relay = self._relay(released)

        async def create_endpoint(*_args, **_kwargs):
            transport = _Transport()
            transports.append(transport)
            entered.set()
            try:
                await release_endpoint.wait()
            except asyncio.CancelledError:
                # Model a system endpoint operation that finishes acquiring
                # its transport after cancellation has already raced it.
                await release_endpoint.wait()
            return transport, object()

        loop = asyncio.get_running_loop()
        with mock.patch.object(
            loop,
            "create_datagram_endpoint",
            new=create_endpoint,
        ):
            start_task = asyncio.create_task(relay.start())
            await asyncio.wait_for(entered.wait(), timeout=1)
            first_stop = asyncio.create_task(relay.stop())
            second_stop = asyncio.create_task(relay.stop())
            await asyncio.sleep(0)
            first_stop.cancel()
            second_stop.cancel()
            first_stop.cancel()
            second_stop.cancel()
            release_endpoint.set()
            for waiter in (first_stop, second_stop):
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(waiter, timeout=1)
            with self.assertRaises(asyncio.CancelledError):
                await start_task
            self.assertFalse(relay.started)
            self.assertFalse(relay._transports)  # noqa: SLF001
            self.assertTrue(all(item.closed for item in transports))
            self.assertEqual(released, [(30000, 30002)])
            # A later idempotent waiter observes the same completed cleanup.
            await asyncio.wait_for(relay.stop(), timeout=1)

        self.assertFalse(relay.started)
        self.assertFalse(relay._transports)  # noqa: SLF001
        self.assertTrue(all(item.closed for item in transports))
        self.assertEqual(released, [(30000, 30002)])

    @pytest.mark.fault
    async def test_concurrent_start_uses_one_endpoint_sequence(self) -> None:
        entered = asyncio.Event()
        release_endpoint = asyncio.Event()
        calls = 0
        transports: list[_Transport] = []
        released: list[tuple[int, int]] = []
        relay = self._relay(released)

        async def create_endpoint(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release_endpoint.wait()
            transport = _Transport()
            transports.append(transport)
            return transport, object()

        loop = asyncio.get_running_loop()
        with mock.patch.object(
            loop,
            "create_datagram_endpoint",
            new=create_endpoint,
        ):
            first = asyncio.create_task(relay.start())
            await asyncio.wait_for(entered.wait(), timeout=1)
            second = asyncio.create_task(relay.start())
            release_endpoint.set()
            await asyncio.gather(first, second)
            await relay.stop()

        self.assertEqual(calls, 4)
        self.assertTrue(all(item.closed for item in transports))
        self.assertEqual(released, [(30000, 30002)])

    @pytest.mark.fault
    async def test_start_is_rejected_once_stop_has_claimed_lifecycle(self) -> None:
        released: list[tuple[int, int]] = []
        relay = self._relay(released)
        stop_entered = asyncio.Event()
        finish_stop = asyncio.Event()
        original_stop = relay._stop  # noqa: SLF001 - deterministic lifecycle race.

        async def delayed_stop() -> None:
            stop_entered.set()
            await finish_stop.wait()
            await original_stop()

        relay._stop = delayed_stop  # type: ignore[method-assign]
        stopping = asyncio.create_task(relay.stop())
        await asyncio.wait_for(stop_entered.wait(), timeout=1)
        try:
            with self.assertRaisesRegex(RuntimeError, "already been stopped"):
                await relay.start()
        finally:
            finish_stop.set()
            await asyncio.wait_for(stopping, timeout=1)

        self.assertFalse(relay.started)
        self.assertEqual(released, [(30000, 30002)])


if __name__ == "__main__":
    unittest.main()


@pytest.mark.parametrize("failure", ["prepare_stale", "commit_stale", "cancel_rollback"])
def test_direction_addition_fences_owner_and_joins_new_worker_cleanup(failure):
    async def scenario():
        left = VideoRtpPeer("127.0.0.1", 41000, 41001,
            RtpVideoFormat(payload_type=104, encoding="VP8", direction="recvonly"))
        right = VideoRtpPeer("127.0.0.1", 42000, 42001,
            RtpVideoFormat(payload_type=26, encoding="JPEG"))
        relay = SipVideoRtpRelay(left=left, right=right, left_port=43000, right_port=43002)
        relay.configure_transcoding(types.SimpleNamespace(data={}), "direction-fence")
        relay.started, relay._transcode_slot_claimed = True, True
        relay._maintain_nat = mock.Mock()
        alive = {side: set() for side in ("left", "right")}
        entered, release = asyncio.Event(), asyncio.Event()
        newer_left = replace(left, port=41002)

        class Worker:
            ready = True
            def __init__(self, side):
                self.side = side
                alive[side].add(self)
                assert len(alive[side]) == 1
            async def async_close(self):
                if failure == "cancel_rollback" and self.side == "left":
                    entered.set()
                    await release.wait()
                alive[self.side].discard(self)

        original = sip_video_relay._TranscodeGeneration({"right"}, {"right": Worker("right")}, {}, {})
        relay._transcode = original
        retained = original.transcoders["right"]

        async def build(_loop, _left, _right, *, directions, **_kwargs):
            result = sip_video_relay._TranscodeGeneration(set(directions),
                {side: Worker(side) for side in directions}, {}, {})
            if failure == "prepare_stale":
                relay.reconfigure_peer("left", newer_left)
            return result

        async def no_slot(*_args):
            pass

        active_left = replace(left, video_format=replace(left.video_format, direction="sendrecv"))
        with mock.patch.object(relay, "_build_transcode_generation", side_effect=build), \
             mock.patch.object(sip_video_relay, "_release_transcoder_slot", side_effect=no_slot):
            try:
                relay.stage_peer_reconfiguration("left", active_left)
                relay.stage_peer_reconfiguration("right", right)
                if failure == "prepare_stale":
                    with pytest.raises(RuntimeError, match="changed"):
                        await relay.async_prepare_peer_generation(left=active_left, right=right)
                else:
                    commit, rollback = await relay.async_prepare_peer_generation(left=active_left, right=right)
                    if failure == "commit_stale":
                        committing = asyncio.create_task(commit())
                        asyncio.get_running_loop().call_soon(relay.reconfigure_peer, "left", newer_left)
                        with pytest.raises(RuntimeError, match="changed"):
                            await committing
                    else:
                        rolling_back = asyncio.create_task(rollback())
                        await entered.wait()
                        rolling_back.cancel()
                        release.set()
                        with pytest.raises(asyncio.CancelledError):
                            await rolling_back
                assert relay._transcode is original
                assert original.transcoders == {"right": retained}
                assert alive == {"left": set(), "right": {retained}}
                assert not relay._staged_peers
                assert relay.left is (left if failure == "cancel_rollback" else newer_left)
            finally:
                release.set()
                await relay.stop()
        assert not alive["left"] and not alive["right"]
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["commit", "rollback", "cancel_rollback", "concurrent_peer_update", "peer_update_before_commit", "new_failure", "restore_failure", "unsupported"])
def test_changed_video_contract_stops_old_before_start_and_restores_exclusively(outcome):
    async def scenario():
        left = VideoRtpPeer("127.0.0.1", 41000, 41001, RtpVideoFormat(payload_type=104, encoding="VP8"))
        right = VideoRtpPeer("127.0.0.1", 42000, 42001, RtpVideoFormat(payload_type=26, encoding="JPEG"))
        # Only this peer's receiver constraint changes; its sent RTP contract
        # remains the independently negotiated local_video_format.
        right.local_video_format = right.video_format
        relay = SipVideoRtpRelay(left=left, right=right, left_port=43000, right_port=43002)
        relay.configure_transcoding(types.SimpleNamespace(data={}), "exclusive-codec-owner")
        relay.started, relay._transcode_slot_claimed = True, True
        relay._maintain_nat = mock.Mock()
        alive = {side: set() for side in ("left", "right")}
        peak = {side: 0 for side in alive}
        events = []
        close_entered, allow_close = asyncio.Event(), asyncio.Event()

        class Worker:
            ready = True
            def __init__(self, side, target):
                self.side, self.target = side, target
                alive[side].add(self)
                peak[side] = max(peak[side], len(alive[side]))
                events.append(("start", side, self))
            async def async_close(self):
                async def close():
                    if self in alive[self.side]:
                        if outcome == "cancel_rollback" and self.target.max_framerate == 7:
                            close_entered.set()
                            await allow_close.wait()
                        alive[self.side].remove(self)
                        events.append(("stop", self.side, self))
                # The actual FFmpeg driver also finishes owned cleanup before
                # propagating cancellation to the generation caller.
                await sip_video_relay.async_wait_for_cleanup(asyncio.create_task(close()))

        async def build(_loop, new_left, new_right, *, suffix="", directions=None):
            if outcome in {"new_failure", "restore_failure"} and suffix == "-reoffer":
                raise RuntimeError("injected new encoder failure")
            if outcome == "restore_failure" and suffix == "-restore":
                raise RuntimeError("injected previous encoder failure")
            selected = relay.transcode_directions_for(new_left, new_right) if directions is None else set(directions)
            return sip_video_relay._TranscodeGeneration(selected, {
                side: Worker(side, (new_right if side == "left" else new_left).send_format)
                for side in selected}, {}, {})

        relay._transcode = await build(None, left, right)
        original = relay._transcode
        old_left, old_right = original.transcoders["left"], original.transcoders["right"]
        changed = replace(right, video_format=replace(right.video_format, max_framerate=7))
        if outcome == "unsupported":
            changed = replace(right, video_format=replace(right.video_format, encoding="H265"))
        with mock.patch.object(relay, "_build_transcode_generation", side_effect=build), \
             mock.patch.object(sip_video_relay, "_claim_transcoder_slot", new=mock.AsyncMock()), \
             mock.patch.object(sip_video_relay, "_release_transcoder_slot", new=mock.AsyncMock()):
            try:
                relay.stage_peer_reconfiguration("right", changed)
                if outcome == "unsupported":
                    with pytest.raises(ValueError, match="unsupported"):
                        await relay.async_prepare_peer_generation(left=left, right=changed)
                    assert all(kind == "start" for kind, _side, _worker in events)
                elif outcome == "restore_failure":
                    with pytest.raises(sip_video_relay.VideoTranscodeRestoreError):
                        await relay.async_prepare_peer_generation(left=left, right=changed)
                    assert not alive["left"] and alive["right"] == {old_right}
                elif outcome == "new_failure":
                    with pytest.raises(RuntimeError, match="injected new encoder failure"):
                        await relay.async_prepare_peer_generation(left=left, right=changed)
                    assert relay._transcode.transcoders["left"].target == right.send_format
                else:
                    commit, rollback = await relay.async_prepare_peer_generation(left=left, right=changed)
                    if outcome == "concurrent_peer_update":
                        # The existing UAS path commits an independent source
                        # RTP tuple through this same public peer primitive.
                        newer_left = replace(left, port=41002)
                        relay.reconfigure_peer("left", newer_left)
                    if outcome == "peer_update_before_commit":
                        newer_left = replace(left, port=41002)
                        committing = asyncio.create_task(commit())
                        asyncio.get_running_loop().call_soon(relay.reconfigure_peer, "left", newer_left)
                        with pytest.raises(RuntimeError, match="changed"):
                            await committing
                    elif outcome == "cancel_rollback":
                        cancelling = asyncio.create_task(rollback())
                        await close_entered.wait()
                        cancelling.cancel()
                        allow_close.set()
                        with pytest.raises(asyncio.CancelledError):
                            await cancelling
                    else:
                        await (commit() if outcome == "commit" else rollback())
                    expected = changed if outcome == "commit" else right
                    assert relay.right is expected
                    assert relay._transcode.transcoders["left"].target == expected.send_format
                    if outcome in {"concurrent_peer_update", "peer_update_before_commit"}:
                        assert relay.left is newer_left
                assert peak == {"left": 1, "right": 1}
                assert relay._transcode is original and relay._transcode.transcoders["right"] is old_right
                if outcome not in {"unsupported", "restore_failure"}:
                    replacement = next(worker for kind, side, worker in events
                        if kind == "start" and side == "left" and worker is not old_left)
                    assert events.index(("stop", "left", old_left)) < events.index(("start", "left", replacement))
            finally:
                await relay.stop()
        assert all(not workers for workers in alive.values())
    asyncio.run(scenario())
