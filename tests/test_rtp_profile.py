#!/usr/bin/env python3
"""RTP packet, codec and DTMF contracts."""

from __future__ import annotations

import asyncio

from .voip_phase1_support import (
    audio_format,
    dtmf,
    rtp,
    sdp,
    sip_rtp_bridge,
    unittest,
)


class RtpProfileTest(unittest.TestCase):
    def test_rtp_packet_round_trip(self) -> None:
        packet = rtp.RtpPacket(
            payload_type=96,
            marker=True,
            sequence=65535,
            timestamp=0xFFFFFFF0,
            ssrc=0x12345678,
            payload=b"\x00\x01\x02\x03",
        )
        raw = rtp.build_packet(packet)
        parsed = rtp.parse_packet(raw)
        self.assertEqual(parsed, packet)
        self.assertEqual(rtp.next_sequence(packet.sequence), 0)
        self.assertEqual(rtp.next_timestamp(packet.timestamp, 32), 16)

    def test_rejects_bad_rtp_version(self) -> None:
        raw = bytearray(rtp.build_packet(rtp.RtpPacket(96, 1, 2, 3, b"x")))
        raw[0] = 0
        with self.assertRaises(rtp.RtpError):
            rtp.parse_packet(bytes(raw))

    def test_receiver_accepts_standard_mtu_payload_larger_than_tx_budget(self) -> None:
        packet = rtp.RtpPacket(96, 1, 2, 3, b"x" * rtp.MAX_RTP_PAYLOAD_BYTES)
        raw = rtp.build_packet(packet) + (b"y" * 60)
        parsed = rtp.parse_packet(raw)
        self.assertEqual(len(parsed.payload), rtp.MAX_RTP_PAYLOAD_BYTES + 60)
        with self.assertRaises(rtp.RtpError):
            rtp.build_packet(rtp.RtpPacket(96, 1, 2, 3, parsed.payload))

    def test_audio_receive_payload_limit_accepts_boundary_and_rejects_oversized(
        self,
    ) -> None:
        fmt = sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20)
        limit = rtp.audio_payload_size_limit(fmt)

        self.assertEqual(limit, 160)
        rtp.validate_audio_payload_size(b"x" * limit, fmt)
        with self.assertRaisesRegex(rtp.RtpError, r"161 bytes; max is 160"):
            rtp.validate_audio_payload_size(b"x" * (limit + 1), fmt)

    def test_opus_receive_payload_limit_is_ptime_aware_and_hard_capped(self) -> None:
        opus_20_ms = sdp.RtpPcmFormat(98, "OPUS", 48000, 2, 20)
        opus_120_ms = sdp.RtpPcmFormat(98, "OPUS", 48000, 2, 120)

        self.assertEqual(rtp.audio_payload_size_limit(opus_20_ms), 8 * 1277)
        self.assertEqual(
            rtp.audio_payload_size_limit(opus_120_ms),
            rtp.MAX_AUDIO_RTP_PAYLOAD_BYTES,
        )

    def test_g722_receive_limit_uses_the_rtp_octet_clock(self) -> None:
        fmt = sdp.RtpPcmFormat(9, "G722", 8000, 1, 20)

        self.assertEqual(rtp.audio_payload_size_limit(fmt), 160)

    def test_dahua_pcm_receive_limit_accepts_exact_twenty_ms_frame(self) -> None:
        fmt = sdp.RtpPcmFormat(97, "PCM", 16000, 1, 20)

        self.assertEqual(rtp.audio_payload_size_limit(fmt), 640)
        rtp.validate_audio_payload_size(bytes(640), fmt)
        with self.assertRaisesRegex(rtp.RtpError, r"642 bytes; max is 640"):
            rtp.validate_audio_payload_size(bytes(642), fmt)

    def test_rfc4733_decoder_emits_one_event_per_press(self) -> None:
        decoder = dtmf.RtpDtmfDecoder(101)

        def event(sequence: int, timestamp: int, code: int, *, ssrc: int = 0x1234, ended: bool = False) -> bytes:
            return rtp.build_packet(
                rtp.RtpPacket(
                    payload_type=101,
                    sequence=sequence,
                    timestamp=timestamp,
                    ssrc=ssrc,
                    payload=bytes((code, 0x80 if ended else 0x00, 0, 160)),
                )
            )

        self.assertEqual(decoder.decode(event(1, 1000, 1)), "1")
        self.assertEqual(decoder.decode(event(2, 1000, 1, ended=True)), "")
        self.assertEqual(decoder.decode(event(3, 2000, 10)), "*")
        self.assertEqual(decoder.decode(event(4, 3000, 12)), "A")
        self.assertEqual(decoder.decode(event(5, 4000, 2, ssrc=0x9999)), "")

    def test_legacy_sip_info_accepts_digit_and_event_code_forms(self) -> None:
        self.assertEqual(dtmf.parse_sip_info_digit("application/dtmf-relay", b"Signal=1\r\nDuration=160"), "1")
        self.assertEqual(dtmf.parse_sip_info_digit("application/dtmf-relay", b"Signal=10\r\nDuration=160"), "*")
        self.assertEqual(dtmf.parse_sip_info_digit("application/dtmf", b"#"), "#")

    def test_legacy_sip_info_body_is_bounded_and_round_trips(self) -> None:
        body = dtmf.build_sip_info_dtmf_body("#", duration_ms=1)
        self.assertEqual(body, b"Signal=#\r\nDuration=40\r\n")
        self.assertEqual(
            dtmf.parse_sip_info_digit("application/dtmf-relay", body),
            "#",
        )
        with self.assertRaisesRegex(ValueError, "unsupported DTMF digit"):
            dtmf.build_sip_info_dtmf_body("X")

    def test_rfc4733_payload_encodes_event_end_volume_and_duration(self) -> None:
        self.assertEqual(
            dtmf.build_telephone_event_payload("#", duration=1280, end=True),
            bytes((11, 0x8A, 0x05, 0x00)),
        )
        self.assertEqual(dtmf.telephone_event_code("d"), 15)
        self.assertIsNone(dtmf.telephone_event_code("x"))
        with self.assertRaisesRegex(ValueError, "duration"):
            dtmf.build_telephone_event_payload("1", duration=0)

    def test_relay_follows_same_ssrc_nat_port_rebind(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        fmt = audio_format.AudioFormat(16000, "s16le", 1, 16)
        left = sip_rtp_bridge.RtpPeer("192.0.2.10", 40000, 96, fmt)
        right = sip_rtp_bridge.RtpPeer("192.0.2.20", 41000, 96, fmt)
        relay = sip_rtp_bridge.SipRtpRelay(left=left, right=right, left_port=42000, right_port=42002)
        output = FakeTransport()
        relay.right_transport = output  # type: ignore[assignment]

        def packet(ssrc: int) -> bytes:
            return rtp.build_packet(
                rtp.RtpPacket(
                    payload_type=96,
                    sequence=1,
                    timestamp=1,
                    ssrc=ssrc,
                    payload=b"\0" * fmt.nominal_frame_bytes,
                )
            )

        relay.handle_packet("left", packet(0x1234), (left.host, 45000))
        self.assertEqual(left.port, 45000)
        relay.handle_packet("left", packet(0x1234), (left.host, 45002))
        self.assertEqual(left.port, 45002)
        relay.handle_packet("left", packet(0x9999), (left.host, 45004))
        self.assertEqual(left.port, 45002)
        self.assertEqual(len(output.sent), 2)

    def test_relay_accepts_authenticated_signaling_host_for_nat_media(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        left = sip_rtp_bridge.RtpPeer(
            "10.0.0.20",
            40000,
            96,
            fmt,
            signaling_host="198.51.100.20",
        )
        right = sip_rtp_bridge.RtpPeer("192.0.2.20", 41000, 96, fmt)
        relay = sip_rtp_bridge.SipRtpRelay(
            left=left,
            right=right,
            left_port=42000,
            right_port=42002,
        )
        output = FakeTransport()
        return_output = FakeTransport()
        relay.right_transport = output  # type: ignore[assignment]
        relay.left_transport = return_output  # type: ignore[assignment]
        packet = rtp.build_packet(
            rtp.RtpPacket(
                payload_type=96,
                sequence=1,
                timestamp=1,
                ssrc=0x1234,
                payload=b"\0" * fmt.nominal_frame_bytes,
            )
        )

        relay.handle_packet("left", packet, ("198.51.100.20", 45000))
        relay.handle_packet("left", packet, ("203.0.113.20", 45000))
        relay.handle_packet("right", packet, (right.host, right.port))

        self.assertEqual(left.host, "198.51.100.20")
        self.assertEqual(left.port, 45000)
        self.assertEqual(len(output.sent), 1)
        self.assertEqual(return_output.sent[-1][1], ("198.51.100.20", 45000))
        self.assertEqual(relay.dropped, 1)

    def test_relay_enforces_negotiated_audio_direction(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        left = sip_rtp_bridge.RtpPeer(
            "192.0.2.10", 40000, 96, fmt, can_send=False, can_receive=True
        )
        right = sip_rtp_bridge.RtpPeer(
            "192.0.2.20", 41000, 96, fmt, can_send=True, can_receive=True
        )
        relay = sip_rtp_bridge.SipRtpRelay(
            left=left, right=right, left_port=42000, right_port=42002
        )
        output = FakeTransport()
        relay.right_transport = output  # type: ignore[assignment]
        packet = rtp.build_packet(
            rtp.RtpPacket(
                payload_type=96,
                sequence=1,
                timestamp=1,
                ssrc=1,
                payload=b"\0" * fmt.nominal_frame_bytes,
            )
        )

        relay.handle_packet("left", packet, (left.host, left.port))
        self.assertEqual(output.sent, [])
        left.can_send = True
        right.can_receive = False
        relay.handle_packet("left", packet, (left.host, left.port))
        self.assertEqual(output.sent, [])
        right.can_receive = True
        relay.handle_packet("left", packet, (left.host, left.port))
        self.assertEqual(len(output.sent), 1)

    def test_relay_connection_hold_blocks_only_traffic_toward_held_leg(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        left = sip_rtp_bridge.RtpPeer(
            "192.0.2.10",
            40000,
            96,
            fmt,
            connection_held=True,
        )
        right = sip_rtp_bridge.RtpPeer("192.0.2.20", 41000, 96, fmt)
        relay = sip_rtp_bridge.SipRtpRelay(
            left=left,
            right=right,
            left_port=42000,
            right_port=42002,
        )
        toward_left = FakeTransport()
        toward_right = FakeTransport()
        relay.left_transport = toward_left  # type: ignore[assignment]
        relay.right_transport = toward_right  # type: ignore[assignment]
        packet = rtp.build_packet(
            rtp.RtpPacket(
                payload_type=96,
                sequence=1,
                timestamp=1,
                ssrc=1,
                payload=b"\0" * fmt.nominal_frame_bytes,
            )
        )

        relay.handle_packet("right", packet, (right.host, right.port))
        self.assertEqual(toward_left.sent, [])
        self.assertEqual(relay.drop_connection_hold, 1)
        relay.handle_packet("left", packet, (left.host, left.port))
        self.assertEqual(len(toward_right.sent), 1)

    def test_relay_reconfiguration_is_prepared_before_atomic_commit(self) -> None:
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        left = sip_rtp_bridge.RtpPeer("192.0.2.10", 40000, 96, fmt)
        right = sip_rtp_bridge.RtpPeer("192.0.2.20", 41000, 96, fmt)
        right.sequence = 100
        right.timestamp = 200
        right.ssrc = 300
        relay = sip_rtp_bridge.SipRtpRelay(
            left=left,
            right=right,
            left_port=42000,
            right_port=42002,
        )
        updated = sip_rtp_bridge.RtpPeer(
            "198.51.100.20",
            43000,
            97,
            fmt,
            send_payload_type=98,
            send_audio_format=fmt,
            can_send=False,
            can_receive=True,
        )

        commit = relay.prepare_peer_reconfiguration("right", updated)
        self.assertIs(relay.right, right)
        self.assertEqual(relay.right.port, 41000)
        # Model media continuing while the final SIP response is sent.
        right.sequence = 101
        right.timestamp = 520
        commit()

        self.assertIs(relay.right, updated)
        self.assertEqual((updated.host, updated.port), ("198.51.100.20", 43000))
        self.assertEqual((updated.sequence, updated.timestamp, updated.ssrc), (101, 520, 300))
        self.assertFalse(updated.can_send)
        self.assertTrue(updated.can_receive)

    def test_relay_preserves_codec_state_for_unchanged_audio_contract(self) -> None:
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 16)
        left = sip_rtp_bridge.RtpPeer("192.0.2.10", 40000, 96, fmt)
        right = sip_rtp_bridge.RtpPeer("192.0.2.20", 41000, 96, fmt)
        relay = sip_rtp_bridge.SipRtpRelay(
            left=left,
            right=right,
            left_port=42000,
            right_port=42002,
        )
        codec_state = (
            relay.left_to_right,
            relay.right_to_left,
            relay.left_decoder,
            relay.right_decoder,
            relay.left_encoder,
            relay.right_encoder,
        )
        updated = sip_rtp_bridge.RtpPeer(
            "198.51.100.10",
            43000,
            96,
            fmt,
            can_send=False,
        )

        relay.reconfigure_peer("left", updated)

        self.assertIs(relay.left, updated)
        self.assertEqual((updated.host, updated.port), ("198.51.100.10", 43000))
        self.assertFalse(updated.can_send)
        self.assertEqual(
            codec_state,
            (
                relay.left_to_right,
                relay.right_to_left,
                relay.left_decoder,
                relay.right_decoder,
                relay.left_encoder,
                relay.right_encoder,
            ),
        )

    def test_opposite_relay_reconfigurations_do_not_overwrite_each_other(self) -> None:
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        left = sip_rtp_bridge.RtpPeer("192.0.2.10", 40000, 96, fmt)
        right = sip_rtp_bridge.RtpPeer("192.0.2.20", 41000, 96, fmt)
        relay = sip_rtp_bridge.SipRtpRelay(
            left=left,
            right=right,
            left_port=42000,
            right_port=42002,
        )
        updated_left = sip_rtp_bridge.RtpPeer("198.51.100.10", 43000, 96, fmt)
        updated_right = sip_rtp_bridge.RtpPeer("198.51.100.20", 43002, 96, fmt)

        commit_left = relay.prepare_peer_reconfiguration("left", updated_left)
        commit_right = relay.prepare_peer_reconfiguration("right", updated_right)
        commit_left()
        commit_right()

        self.assertIs(relay.left, updated_left)
        self.assertIs(relay.right, updated_right)
        self.assertEqual(relay.left.host, "198.51.100.10")
        self.assertEqual(relay.right.host, "198.51.100.20")


class RtpPacketizationTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _packet(
        fmt: sdp.RtpPcmFormat,
        sequence: int,
        payload: bytes,
    ) -> bytes:
        return rtp.build_packet(
            rtp.RtpPacket(
                payload_type=fmt.payload_type,
                sequence=sequence,
                timestamp=sequence * fmt.rtp_timestamp_step,
                ssrc=1234,
                payload=payload,
            )
        )

    async def test_shorter_source_frames_are_accumulated_without_pacer(self) -> None:
        loop = asyncio.get_running_loop()

        class TimedTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[float, bytes]] = []

            def sendto(self, data: bytes, _addr: tuple[str, int]) -> None:
                self.sent.append((loop.time(), data))

            def close(self) -> None:
                pass

        pcma = sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20)
        l16 = sdp.RtpPcmFormat(96, "L16", 16000, 1, 10)
        left = sip_rtp_bridge.RtpPeer(
            "192.0.2.10",
            40000,
            pcma.payload_type,
            pcma.audio_format,
            rtp_format=pcma,
            send_rtp_format=pcma,
        )
        right = sip_rtp_bridge.RtpPeer(
            "192.0.2.20",
            41000,
            l16.payload_type,
            l16.audio_format,
            rtp_format=l16,
            send_rtp_format=l16,
        )
        relay = sip_rtp_bridge.SipRtpRelay(
            left=left,
            right=right,
            left_port=42000,
            right_port=42002,
        )
        output = TimedTransport()
        relay.left_transport = output  # type: ignore[assignment]
        started = loop.time()

        for sequence in range(2):
            relay.handle_packet(
                "right",
                self._packet(
                    l16,
                    sequence,
                    bytes(l16.audio_format.nominal_frame_bytes),
                ),
                (right.host, right.port),
            )

        self.assertEqual(len(output.sent), 1)
        self.assertLess(output.sent[0][0] - started, 0.01)
        await relay.stop()

    async def test_split_source_frame_preserves_rtp_timestamp_clock(self) -> None:
        loop = asyncio.get_running_loop()

        class TimedTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[float, bytes]] = []

            def sendto(self, data: bytes, _addr: tuple[str, int]) -> None:
                self.sent.append((loop.time(), data))

            def close(self) -> None:
                pass

        pcma = sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20)
        l16 = sdp.RtpPcmFormat(96, "L16", 16000, 1, 10)
        left = sip_rtp_bridge.RtpPeer(
            "192.0.2.10",
            40000,
            pcma.payload_type,
            pcma.audio_format,
            rtp_format=pcma,
            send_rtp_format=pcma,
        )
        right = sip_rtp_bridge.RtpPeer(
            "192.0.2.20",
            41000,
            l16.payload_type,
            l16.audio_format,
            rtp_format=l16,
            send_rtp_format=l16,
        )
        relay = sip_rtp_bridge.SipRtpRelay(
            left=left,
            right=right,
            left_port=42000,
            right_port=42002,
        )
        output = TimedTransport()
        relay.right_transport = output  # type: ignore[assignment]
        payload = sip_rtp_bridge.RtpPayloadEncoder(pcma).encode(
            bytes(pcma.audio_format.nominal_frame_bytes)
        )

        relay.handle_packet(
            "left",
            self._packet(pcma, 0, payload),
            (left.host, left.port),
        )
        await relay.stop()

        self.assertEqual(len(output.sent), 2)
        self.assertLess(output.sent[1][0] - output.sent[0][0], 0.007)
        packets = [rtp.parse_packet(raw) for _at, raw in output.sent]
        self.assertEqual(
            [
                (packets[index].timestamp - packets[index - 1].timestamp)
                & 0xFFFFFFFF
                for index in range(1, len(packets))
            ],
            [l16.rtp_timestamp_step],
        )

    async def test_split_source_burst_preserves_every_rtp_frame(self) -> None:
        class Transport:
            def __init__(self) -> None:
                self.sent: list[bytes] = []

            def sendto(self, data: bytes, _addr: tuple[str, int]) -> None:
                self.sent.append(data)

            def close(self) -> None:
                pass

        source = sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20)
        l16 = sdp.RtpPcmFormat(96, "L16", 48000, 1, 10)
        left = sip_rtp_bridge.RtpPeer(
            "192.0.2.10",
            40000,
            source.payload_type,
            source.audio_format,
            rtp_format=source,
            send_rtp_format=source,
        )
        right = sip_rtp_bridge.RtpPeer(
            "192.0.2.20",
            41000,
            l16.payload_type,
            l16.audio_format,
            rtp_format=l16,
            send_rtp_format=l16,
        )
        relay = sip_rtp_bridge.SipRtpRelay(
            left=left,
            right=right,
            left_port=42000,
            right_port=42002,
        )
        output = Transport()
        relay.right_transport = output  # type: ignore[assignment]
        encoder = sip_rtp_bridge.RtpPayloadEncoder(source)
        payload = encoder.encode(bytes(source.audio_format.nominal_frame_bytes))

        # Reproduce the 383 ms scheduler pause from the real capture. Incoming
        # datagrams are delivered as one burst when the HA loop resumes.
        for sequence in range(20):
            relay.handle_packet(
                "left",
                self._packet(source, sequence, payload),
                (left.host, left.port),
            )
        await relay.stop()

        self.assertEqual(len(output.sent), 40)
        packets = [rtp.parse_packet(raw) for raw in output.sent]
        self.assertEqual(
            [packet.sequence for packet in packets],
            list(range(packets[0].sequence, packets[0].sequence + 40)),
        )
        self.assertEqual(
            [
                (packets[index].timestamp - packets[index - 1].timestamp)
                & 0xFFFFFFFF
                for index in range(1, len(packets))
            ],
            [l16.rtp_timestamp_step] * 39,
        )
