from __future__ import annotations

import asyncio
import importlib.util
import socket
import sys
import types
import unittest
from unittest import mock
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _load(name: str):
    if "custom_components" not in sys.modules:
        package = types.ModuleType("custom_components")
        package.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = package
    if PKG_NAME not in sys.modules:
        package = types.ModuleType(PKG_NAME)
        package.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = package
    full_name = f"{PKG_NAME}.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


rtp = _load("rtp")
sdp = _load("sdp")
_load("video_rtcp")
sip_video_relay = _load("sip_video_relay")
RtpVideoFormat = sdp.RtpVideoFormat
video_formats_passthrough_compatible = sdp.video_formats_passthrough_compatible
SipVideoRtpRelay = sip_video_relay.SipVideoRtpRelay
VideoRtpPeer = sip_video_relay.VideoRtpPeer
build_pli = sys.modules[f"{PKG_NAME}.video_rtcp"].build_pli


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
        packet = rtp.build_packet(
            rtp.RtpPacket(102, 1, 1, 0x11223344, b"encoded")
        )
        self.relay.handle_rtp("left", packet, ("10.0.0.1", 10000))

        wrong = build_pli(1, 0x55667788)
        correct = build_pli(1, 0x11223344)
        self.relay.handle_rtcp("right", wrong, ("10.0.0.2", 20009))
        self.relay.handle_rtcp("right", correct, ("10.0.0.2", 20009))

        self.assertEqual(self.left_rtcp.sent, [(correct, ("10.0.0.1", 10001))])
        self.assertEqual(self.relay.dropped, 1)

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
        replacement = VideoRtpPeer(
            "10.0.0.9", 19000, 19001, _format(120)
        )

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
        self.relay._transcoders = {  # noqa: SLF001
            "left": left_transcoder,
            "right": right_transcoder,
        }
        left_packet = rtp.build_packet(
            rtp.RtpPacket(26, 1, 9000, 0x1111, b"jpeg")
        )
        right_packet = rtp.build_packet(
            rtp.RtpPacket(105, 2, 18000, 0x2222, b"h264")
        )

        self.relay.handle_rtp("left", left_packet, (self.left.host, self.left.port))
        self.relay.handle_rtp(
            "right", right_packet, (self.right.host, self.right.port)
        )

        self.assertEqual(left_transcoder.received, [left_packet])
        self.assertEqual(right_transcoder.received, [right_packet])
        self.assertFalse(self.left_rtp.sent)
        self.assertFalse(self.right_rtp.sent)
        self.assertEqual(self.relay.transcoded_input_packets, 2)

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
        self.relay._transcoders = {"right": transcoder}  # noqa: SLF001
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
        output = rtp.build_packet(
            rtp.RtpPacket(105, 1, 9000, 0x5566, b"encoded")
        )

        self.relay.handle_transcoded_rtp("left", output)

        self.assertEqual(self.right_rtp.sent, [(output, (self.right.host, self.right.port))])
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
