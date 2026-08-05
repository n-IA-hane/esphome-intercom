#!/usr/bin/env python3
"""Behavioral tests for the RFC 6184 media path."""

from __future__ import annotations

import importlib.util
import struct
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"
CORE_MODULES = {"audio_format", "rtp", "sdp", "video_rtcp", "video_rtp"}


def _load(name: str):
    if "custom_components" not in sys.modules:
        package = types.ModuleType("custom_components")
        package.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = package
    if PKG_NAME not in sys.modules:
        package = types.ModuleType(PKG_NAME)
        package.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = package
    is_core = name in CORE_MODULES
    if is_core and f"{PKG_NAME}.core" not in sys.modules:
        core_pkg = types.ModuleType(f"{PKG_NAME}.core")
        core_pkg.__path__ = [str(PKG_DIR / "core")]
        sys.modules[f"{PKG_NAME}.core"] = core_pkg
    full_name = f"{PKG_NAME}.{'core.' if is_core else ''}{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    path = PKG_DIR / ("core" if is_core else "") / f"{name}.py"
    spec = importlib.util.spec_from_file_location(full_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


rtp = _load("rtp")
video_rtp = _load("video_rtp")
video_rtcp = _load("video_rtcp")
audio_format = _load("audio_format")
sdp = _load("sdp")


class H264RtpTest(unittest.TestCase):
    def _round_trip(self, access_unit: bytes, *, max_payload: int = 1200):
        packets = video_rtp.packetize_annex_b(
            access_unit,
            payload_type=102,
            sequence=65534,
            timestamp=90000,
            ssrc=0x12345678,
            max_payload=max_payload,
        )
        depacketizer = video_rtp.H264Depacketizer()
        result = None
        for packet in packets:
            result = (
                depacketizer.push(rtp.parse_packet(rtp.build_packet(packet))) or result
            )
        return packets, result

    def test_single_nal_and_fu_a_round_trip(self) -> None:
        sps = b"\x67\x42\xe0\x1f\x11"
        pps = b"\x68\xce\x06\xe2"
        idr = b"\x65" + bytes(range(256)) * 8
        access_unit = b"".join(
            video_rtp.ANNEX_B_START_CODE + item for item in (sps, pps, idr)
        )
        packets, result = self._round_trip(access_unit, max_payload=220)
        self.assertGreater(len(packets), 3)
        self.assertTrue(packets[-1].marker)
        self.assertFalse(any(packet.marker for packet in packets[:-1]))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(video_rtp.split_annex_b(result.data), [sps, pps, idr])
        self.assertTrue(result.key_frame)
        self.assertEqual(result.timestamp, 90000)

    def test_stap_a_is_depacketized(self) -> None:
        sps = b"\x67\x42\xe0\x1f"
        pps = b"\x68\xce\x06\xe2"
        stap = (
            b"\x78"
            + len(sps).to_bytes(2, "big")
            + sps
            + len(pps).to_bytes(2, "big")
            + pps
        )
        depacketizer = video_rtp.H264Depacketizer()
        first = depacketizer.push(rtp.RtpPacket(102, 1, 90, 5, stap))
        self.assertIsNone(first)
        result = depacketizer.push(
            rtp.RtpPacket(102, 2, 90, 5, b"\x65\x99", marker=True)
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(video_rtp.split_annex_b(result.data), [sps, pps, b"\x65\x99"])

    def test_cached_parameter_sets_prefix_later_idr(self) -> None:
        depacketizer = video_rtp.H264Depacketizer()
        sps = b"\x67\x42\xe0\x1f"
        pps = b"\x68\xce\x06\xe2"
        depacketizer.push(rtp.RtpPacket(102, 1, 1, 5, sps))
        depacketizer.push(rtp.RtpPacket(102, 2, 1, 5, pps, marker=True))
        result = depacketizer.push(
            rtp.RtpPacket(102, 3, 2, 5, b"\x65\xaa", marker=True)
        )
        assert result is not None
        self.assertEqual(video_rtp.split_annex_b(result.data), [sps, pps, b"\x65\xaa"])
        self.assertEqual(depacketizer.parameter_sets, (sps, pps))

        replacement = video_rtp.H264Depacketizer(list(depacketizer.parameter_sets))
        resumed = replacement.push(
            rtp.RtpPacket(102, 100, 3, 9, b"\x65\xbb", marker=True)
        )
        assert resumed is not None
        self.assertEqual(
            video_rtp.split_annex_b(resumed.data),
            [sps, pps, b"\x65\xbb"],
        )

    def test_damaged_access_unit_cannot_poison_parameter_set_cache(self) -> None:
        good_sps = b"\x67\x42\xe0\x1f"
        good_pps = b"\x68\xce\x06\xe2"
        bad_sps = b"\x67\xff\xff\xff"
        bad_pps = b"\x68\xff\xff\xff"
        depacketizer = video_rtp.H264Depacketizer([good_sps, good_pps])

        depacketizer.push(rtp.RtpPacket(102, 10, 100, 5, bad_sps))
        damaged = depacketizer.push(
            rtp.RtpPacket(102, 12, 100, 5, bad_pps, marker=True)
        )
        recovered = depacketizer.push(
            rtp.RtpPacket(102, 13, 200, 5, b"\x65\xaa", marker=True)
        )

        self.assertIsNone(damaged)
        self.assertEqual(depacketizer.parameter_sets, (good_sps, good_pps))
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(
            video_rtp.split_annex_b(recovered.data),
            [good_sps, good_pps, b"\x65\xaa"],
        )

    def test_sequence_gap_discards_entire_access_unit(self) -> None:
        access_unit = video_rtp.ANNEX_B_START_CODE + b"\x65" + b"x" * 1000
        packets = video_rtp.packetize_annex_b(
            access_unit,
            payload_type=102,
            sequence=10,
            timestamp=123,
            ssrc=7,
            max_payload=200,
        )
        depacketizer = video_rtp.H264Depacketizer()
        result = None
        for packet in [packets[0], *packets[2:]]:
            result = depacketizer.push(packet) or result
        self.assertIsNone(result)
        self.assertEqual(depacketizer.sequence_gaps, 1)
        self.assertEqual(depacketizer.dropped_access_units, 1)


    def test_fu_a_contract_cannot_mutate_or_nest_packetization_units(self) -> None:
        cases = {
            "changed-nri": (b"\x7c\x85aa", b"\x5c\x45bb"),
            "changed-type": (b"\x7c\x85aa", b"\x7c\x41bb"),
            "reserved-bit": (b"\x7c\xa5aa", b"\x7c\x45bb"),
            "nested-fu": (b"\x7c\x9caa", b"\x7c\x5cbb"),
        }
        for name, (start, end) in cases.items():
            with self.subTest(name=name):
                depacketizer = video_rtp.H264Depacketizer()
                self.assertIsNone(
                    depacketizer.push(rtp.RtpPacket(102, 1, 90, 5, start))
                )
                self.assertIsNone(
                    depacketizer.push(rtp.RtpPacket(102, 2, 90, 5, end, marker=True))
                )
                self.assertEqual(depacketizer.dropped_access_units, 1)

        nested = b"\x78\x00\x02\x78\x00"
        depacketizer = video_rtp.H264Depacketizer()
        self.assertIsNone(
            depacketizer.push(rtp.RtpPacket(102, 1, 90, 5, nested, marker=True))
        )
        self.assertEqual(depacketizer.dropped_access_units, 1)

        with self.assertRaises(video_rtp.H264RtpError):
            video_rtp.packetize_annex_b(
                video_rtp.ANNEX_B_START_CODE + b"\x7c\x00",
                payload_type=102,
                sequence=1,
                timestamp=90,
                ssrc=5,
            )

    def test_timestamp_change_discards_unmarked_access_unit(self) -> None:
        depacketizer = video_rtp.H264Depacketizer()
        depacketizer.push(rtp.RtpPacket(102, 1, 100, 1, b"\x61\x01"))
        result = depacketizer.push(
            rtp.RtpPacket(102, 2, 200, 1, b"\x61\x02", marker=True)
        )
        self.assertIsNotNone(result)
        self.assertEqual(depacketizer.dropped_access_units, 1)

    def test_invalid_or_unbounded_payloads_are_rejected(self) -> None:
        self.assertEqual(
            video_rtp.split_annex_b(
                b"\x00\x00\x00\x00\x01\x67\x01\x00"
                b"\x00\x01\x68\x02\x00\x00"
            ),
            [b"\x67\x01", b"\x68\x02"],
        )
        with self.assertRaises(video_rtp.H264RtpError):
            video_rtp.split_annex_b(b"not annex b")
        with self.assertRaises(video_rtp.H264RtpError):
            video_rtp.packetize_annex_b(
                video_rtp.ANNEX_B_START_CODE + b"\x65\x00",
                payload_type=102,
                sequence=0,
                timestamp=0,
                ssrc=0,
                max_payload=2,
            )


class RtpSenderStateTest(unittest.TestCase):
    def test_keepalive_preserves_source_and_wraps_sequence(self) -> None:
        source = video_rtp.RtpSenderState(
            sequence=0xFFFF,
            ssrc=0x12345678,
            clock=video_rtp.RtpTimestampClock(
                clock_rate=90000,
                origin_timestamp=1234,
                origin_time=10.0,
            ),
        )

        first = rtp.parse_packet(source.build_keepalive(127, now=10.0))
        second = rtp.parse_packet(source.build_keepalive(127, now=10.1))

        self.assertEqual((first.sequence, second.sequence), (0xFFFF, 0))
        self.assertEqual((first.ssrc, second.ssrc), (0x12345678, 0x12345678))
        self.assertEqual((first.payload_type, second.payload_type), (127, 127))
        self.assertEqual((first.payload, second.payload), (b"", b""))
        self.assertGreater(second.timestamp, first.timestamp)
        self.assertEqual(source.sequence, 1)
        self.assertEqual(source.keepalives, 2)


class VideoTransportTest(unittest.TestCase):
    def test_keepalive_and_browser_media_share_one_continuous_rtp_clock(self) -> None:
        clock = video_rtp.RtpTimestampClock(
            clock_rate=90000,
            origin_timestamp=1000,
            origin_time=10.0,
        )
        self.assertEqual(clock.current(11.0), 91000)
        self.assertEqual(clock.map_browser(0, 11.0), 91000)
        self.assertEqual(clock.map_browser(9000, 11.1), 100000)
        self.assertEqual(clock.current(11.2), 109000)

        clock.reset_browser()
        self.assertEqual(clock.map_browser(0, 12.0), 181000)

    def test_extended_sequence_tracks_wrap_and_ignores_previous_cycle_late_packet(
        self,
    ) -> None:
        tracker = video_rtp.RtpExtendedSequenceTracker()

        self.assertEqual(tracker.observe(65534), 65534)
        self.assertEqual(tracker.observe(65535), 65535)
        self.assertEqual(tracker.observe(0), 0x10000)
        self.assertEqual(tracker.observe(1), 0x10001)
        self.assertEqual(tracker.observe(65535), 0x10001)
        self.assertEqual(tracker.highest, 0x10001)

        tracker.reset()
        self.assertEqual(tracker.highest, 0)
        self.assertEqual(tracker.observe(1234), 1234)

    def test_reorder_waits_for_gap_then_emits_in_order(self) -> None:
        reorder = video_rtp.RtpReorderBuffer(max_delay=0.020)
        self.assertEqual(reorder.push(10, "10", 1.000), ["10"])
        self.assertEqual(reorder.push(12, "12", 1.001), [])
        self.assertAlmostEqual(reorder.next_deadline, 1.021)
        self.assertEqual(reorder.push(11, "11", 1.010), ["11", "12"])
        self.assertEqual(reorder.lost, 0)

    def test_reorder_skips_gap_only_after_deadline(self) -> None:
        reorder = video_rtp.RtpReorderBuffer(max_delay=0.020)
        self.assertEqual(reorder.push(65535, "a", 1.0), ["a"])
        self.assertEqual(reorder.push(1, "c", 1.001), [])
        self.assertEqual(reorder.flush(1.020), [])
        self.assertEqual(reorder.flush(1.022), ["c"])
        self.assertEqual(reorder.lost, 1)
        self.assertEqual(reorder.push(1, "duplicate", 1.023), [])
        self.assertEqual(reorder.late, 1)

    def test_vp8_round_trip(self) -> None:
        frame = b"\x00\x9d\x01\x2a" + bytes(range(251)) * 9
        packets = video_rtp.packetize_vp8(
            frame,
            payload_type=103,
            sequence=65000,
            timestamp=123456,
            ssrc=42,
            max_payload=180,
        )
        depacketizer = video_rtp.Vp8Depacketizer()
        access_unit = None
        for packet in packets:
            access_unit = depacketizer.push(packet) or access_unit
        self.assertIsNotNone(access_unit)
        assert access_unit is not None
        self.assertEqual(access_unit.data, frame)
        self.assertTrue(access_unit.key_frame)
        self.assertEqual(access_unit.encoding, "VP8")

    def test_vp8_sequence_gap_discards_corrupt_frame(self) -> None:
        frame = b"\x00\x9d\x01\x2a" + bytes(range(251)) * 5
        packets = video_rtp.packetize_vp8(
            frame,
            payload_type=103,
            sequence=100,
            timestamp=123456,
            ssrc=42,
            max_payload=120,
        )
        self.assertGreater(len(packets), 3)
        depacketizer = video_rtp.Vp8Depacketizer()
        result = None
        for packet in [packets[0], *packets[2:]]:
            result = depacketizer.push(packet) or result
        self.assertIsNone(result)
        self.assertEqual(depacketizer.sequence_gaps, 1)
        self.assertEqual(depacketizer.dropped_access_units, 1)

    def test_vp8_tiny_fragment_flood_is_bounded_and_resets(self) -> None:
        depacketizer = video_rtp.Vp8Depacketizer()
        timestamp = 123456

        self.assertIsNone(
            depacketizer.push(
                rtp.RtpPacket(103, 1, timestamp, 42, b"\x10\x00")
            )
        )
        for index in range(1, video_rtp.MAX_ACCESS_UNIT_FRAGMENTS):
            self.assertIsNone(
                depacketizer.push(
                    rtp.RtpPacket(
                        103,
                        (index + 1) & 0xFFFF,
                        timestamp,
                        42,
                        b"\x00x",
                    )
                )
            )
        self.assertEqual(len(depacketizer._parts), video_rtp.MAX_ACCESS_UNIT_FRAGMENTS)
        self.assertEqual(
            depacketizer._parts_bytes,
            video_rtp.MAX_ACCESS_UNIT_FRAGMENTS,
        )

        self.assertIsNone(
            depacketizer.push(
                rtp.RtpPacket(
                    103,
                    (video_rtp.MAX_ACCESS_UNIT_FRAGMENTS + 1) & 0xFFFF,
                    timestamp,
                    42,
                    b"\x00x",
                )
            )
        )

        self.assertEqual(depacketizer._parts, [])
        self.assertEqual(depacketizer._parts_bytes, 0)
        self.assertEqual(depacketizer.dropped_access_units, 1)

    def test_rfc2435_jpeg_fragments_become_jfif(self) -> None:
        scan = b"\x11\x22\xff\x00\x33\x44"
        header = b"\x00\x00\x00\x00\x00\x32\x28\x1e"
        depacketizer = video_rtp.JpegDepacketizer()
        self.assertIsNone(
            depacketizer.push(rtp.RtpPacket(26, 1, 9000, 7, header + scan[:3]))
        )
        result = depacketizer.push(
            rtp.RtpPacket(
                26,
                2,
                9000,
                7,
                b"\x00\x00\x00\x03\x00\x32\x28\x1e" + scan[3:],
                marker=True,
            )
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.data.startswith(b"\xff\xd8\xff\xe0"))
        self.assertTrue(result.data.endswith(scan + b"\xff\xd9"))
        self.assertEqual(result.encoding, "JPEG")
        self.assertTrue(result.key_frame)

    def test_rfc2435_jpeg_access_unit_budget_covers_complete_jfif(self) -> None:
        scan = b"\x11\x22\x33\x44"
        payload = b"\x00\x00\x00\x00\x00\x32\x28\x1e" + scan
        jfif_header = video_rtp._jpeg_interchange_header(
            jpeg_type=0,
            width_blocks=40,
            height_blocks=30,
            quantizers=video_rtp._jpeg_quantizers(50),
            dri=0,
        )
        exact_budget = len(jfif_header) + len(scan) + 2

        depacketizer = video_rtp.JpegDepacketizer(
            max_access_unit_bytes=exact_budget
        )
        result = depacketizer.push(
            rtp.RtpPacket(26, 1, 9000, 7, payload, marker=True)
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(len(result.data), exact_budget)

        depacketizer = video_rtp.JpegDepacketizer(
            max_access_unit_bytes=exact_budget - 1
        )
        self.assertIsNone(
            depacketizer.push(
                rtp.RtpPacket(26, 1, 9000, 7, payload, marker=True)
            )
        )
        self.assertEqual(depacketizer.dropped_access_units, 1)

    def test_rfc2435_jpeg_rejects_invalid_access_unit_budgets(self) -> None:
        for limit in (0, -1, video_rtp.MAX_ACCESS_UNIT_BYTES + 1):
            with self.subTest(limit=limit), self.assertRaisesRegex(
                ValueError, "access-unit byte limit"
            ):
                video_rtp.JpegDepacketizer(max_access_unit_bytes=limit)

    def test_complete_jpeg_packetizer_round_trip(self) -> None:
        quantizers = bytes((index * 13 + 1) & 0xFF or 1 for index in range(128))
        scan = (b"\x11\x22\xff\x00\x33\x44" * 500) + b"\x55"
        source = (
            video_rtp._jpeg_interchange_header(
                jpeg_type=1,
                width_blocks=80,
                height_blocks=45,
                quantizers=quantizers,
                dri=0,
            )
            + scan
            + b"\xff\xd9"
        )

        packets = video_rtp.packetize_jpeg(
            source,
            payload_type=26,
            sequence=65534,
            timestamp=123456,
            ssrc=0x12345678,
            max_payload=300,
        )

        self.assertGreater(len(packets), 2)
        self.assertEqual([item.sequence for item in packets[:3]], [65534, 65535, 0])
        self.assertFalse(any(item.marker for item in packets[:-1]))
        self.assertTrue(packets[-1].marker)
        depacketizer = video_rtp.JpegDepacketizer()
        result = None
        for packet in packets:
            result = depacketizer.push(packet) or result
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.data, source)
        self.assertEqual(result.timestamp, 123456)

    def test_complete_jpeg_packetizer_preserves_restart_interval(self) -> None:
        quantizers = bytes(range(1, 129))
        source = (
            video_rtp._jpeg_interchange_header(
                jpeg_type=0,
                width_blocks=40,
                height_blocks=30,
                quantizers=quantizers,
                dri=16,
            )
            + b"\x11\x22\x33\x44" * 400
            + b"\xff\xd9"
        )

        packets = video_rtp.packetize_jpeg(
            source,
            payload_type=26,
            sequence=1,
            timestamp=9000,
            ssrc=7,
            max_payload=220,
        )

        self.assertTrue(all(packet.payload[4] == 0x40 for packet in packets))
        self.assertTrue(all(packet.payload[8:12] == b"\x00\x10\xff\xff" for packet in packets))
        depacketizer = video_rtp.JpegDepacketizer()
        result = None
        for packet in packets:
            result = depacketizer.push(packet) or result
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.data, source)

    def test_complete_jpeg_packetizer_rejects_nonstandard_huffman_tables(self) -> None:
        source = bytearray(
            video_rtp._jpeg_interchange_header(
                jpeg_type=1,
                width_blocks=80,
                height_blocks=45,
                quantizers=bytes(range(1, 129)),
                dri=0,
            )
            + b"\x11\x22\x33"
            + b"\xff\xd9"
        )
        dht = source.index(video_rtp._JPEG_STANDARD_DHT)
        source[dht + len(video_rtp._JPEG_STANDARD_DHT) - 1] ^= 0x01

        with self.assertRaisesRegex(ValueError, "standard JPEG Huffman"):
            video_rtp.packetize_jpeg(
                bytes(source),
                payload_type=26,
                sequence=1,
                timestamp=9000,
                ssrc=7,
            )

    def test_complete_jpeg_packetizer_requires_static_payload_type(self) -> None:
        source = (
            video_rtp._jpeg_interchange_header(
                jpeg_type=1,
                width_blocks=80,
                height_blocks=45,
                quantizers=bytes(range(1, 129)),
                dri=0,
            )
            + b"\x11\x22\x33"
            + b"\xff\xd9"
        )
        with self.assertRaisesRegex(ValueError, "static payload type 26"):
            video_rtp.packetize_jpeg(
                source,
                payload_type=103,
                sequence=1,
                timestamp=9000,
                ssrc=7,
            )

    def test_rfc2435_jpeg_rejects_fragment_gap(self) -> None:
        depacketizer = video_rtp.JpegDepacketizer()
        first = b"\x00\x00\x00\x00\x00\x32\x28\x1eabc"
        second = b"\x00\x00\x00\x04\x00\x32\x28\x1edef"
        self.assertIsNone(depacketizer.push(rtp.RtpPacket(26, 1, 9, 1, first)))
        self.assertIsNone(
            depacketizer.push(rtp.RtpPacket(26, 2, 9, 1, second, marker=True))
        )
        self.assertEqual(depacketizer.dropped_access_units, 1)

    def test_rfc2435_jpeg_rejects_changed_fragment_header(self) -> None:
        depacketizer = video_rtp.JpegDepacketizer()
        first = b"\x00\x00\x00\x00\x00\x32\x28\x1eabc"
        changed_type = b"\x00\x00\x00\x03\x01\x32\x28\x1edef"

        self.assertIsNone(depacketizer.push(rtp.RtpPacket(26, 1, 9, 1, first)))
        self.assertIsNone(
            depacketizer.push(rtp.RtpPacket(26, 2, 9, 1, changed_type, marker=True))
        )
        self.assertEqual(depacketizer.dropped_access_units, 1)

    def test_rfc2435_jpeg_rejects_zero_restart_interval(self) -> None:
        depacketizer = video_rtp.JpegDepacketizer()
        payload = b"\x00\x00\x00\x00\x40\x32\x28\x1e\x00\x00\x00\x00scan"

        self.assertIsNone(
            depacketizer.push(rtp.RtpPacket(26, 1, 9, 1, payload, marker=True))
        )
        self.assertEqual(depacketizer.dropped_access_units, 1)

    def test_rfc2435_jpeg_accepts_ffmpeg_single_dynamic_quantizer(self) -> None:
        depacketizer = video_rtp.JpegDepacketizer()
        quantizer = bytes(range(1, 65))
        scan = b"\x11\x22\x33"
        payload = (
            b"\x00\x00\x00\x00\x01\xff\x28\x17"
            b"\x00\x00\x00\x40" + quantizer + scan
        )

        result = depacketizer.push(
            rtp.RtpPacket(26, 1, 9, 1, payload, marker=True)
        )
        self.assertIsNotNone(result)
        assert result is not None
        parsed = video_rtp._parse_jpeg_for_rtp(result.data)
        self.assertEqual(parsed.quantizers, quantizer + quantizer)
        self.assertEqual(parsed.scan, scan)

    def test_rfc2435_jpeg_static_quantizer_cache_is_immutable(self) -> None:
        depacketizer = video_rtp.JpegDepacketizer()
        quantizer = bytes(range(1, 65))
        normalized = quantizer + quantizer
        changed = bytes(reversed(range(1, 129)))

        def payload(tables: bytes | None, scan: bytes) -> bytes:
            table_data = tables or b""
            return (
                b"\x00\x00\x00\x00\x01\x80\x28\x17"
                + b"\x00\x00"
                + len(table_data).to_bytes(2, "big")
                + table_data
                + scan
            )

        first = depacketizer.push(
            rtp.RtpPacket(
                26,
                1,
                9,
                1,
                payload(quantizer, b"\x11"),
                marker=True,
            )
        )
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(
            video_rtp._parse_jpeg_for_rtp(first.data).quantizers,
            normalized,
        )

        equivalent = depacketizer.push(
            rtp.RtpPacket(
                26,
                2,
                10,
                1,
                payload(normalized, b"\x22"),
                marker=True,
            )
        )
        self.assertIsNotNone(equivalent)

        self.assertIsNone(
            depacketizer.push(
                rtp.RtpPacket(
                    26,
                    3,
                    11,
                    1,
                    payload(changed, b"\x33"),
                    marker=True,
                )
            )
        )
        self.assertEqual(depacketizer.dropped_access_units, 1)

        cached = depacketizer.push(
            rtp.RtpPacket(
                26,
                4,
                12,
                1,
                payload(None, b"\x44"),
                marker=True,
            )
        )
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(
            video_rtp._parse_jpeg_for_rtp(cached.data).quantizers,
            normalized,
        )

    def test_rfc2435_jpeg_quality_255_tables_remain_dynamic(self) -> None:
        depacketizer = video_rtp.JpegDepacketizer()
        first_quantizers = bytes(range(1, 129))
        second_quantizers = bytes(reversed(range(1, 129)))

        def payload(tables: bytes | None, scan: bytes) -> bytes:
            table_data = tables or b""
            return (
                b"\x00\x00\x00\x00\x01\xff\x28\x17"
                + b"\x00\x00"
                + len(table_data).to_bytes(2, "big")
                + table_data
                + scan
            )

        for sequence, quantizers in enumerate(
            (first_quantizers, second_quantizers),
            start=1,
        ):
            result = depacketizer.push(
                rtp.RtpPacket(
                    26,
                    sequence,
                    sequence * 9,
                    1,
                    payload(quantizers, bytes((sequence,))),
                    marker=True,
                )
            )
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(
                video_rtp._parse_jpeg_for_rtp(result.data).quantizers,
                quantizers,
            )

        self.assertIsNone(
            depacketizer.push(
                rtp.RtpPacket(
                    26,
                    3,
                    27,
                    1,
                    payload(None, b"\x03"),
                    marker=True,
                )
            )
        )
        self.assertEqual(depacketizer.dropped_access_units, 1)

    def test_rfc2435_jpeg_rejects_nonzero_quantization_mbz(self) -> None:
        depacketizer = video_rtp.JpegDepacketizer()
        payload = (
            b"\x00\x00\x00\x00\x01\xff\x28\x17"
            b"\x01\x00\x00\x40" + bytes(range(1, 65)) + b"scan"
        )

        self.assertIsNone(
            depacketizer.push(rtp.RtpPacket(26, 1, 9, 1, payload, marker=True))
        )
        self.assertEqual(depacketizer.dropped_access_units, 1)

    def test_rfc2435_jpeg_rejects_zero_quantization_coefficient(self) -> None:
        depacketizer = video_rtp.JpegDepacketizer()
        payload = (
            b"\x00\x00\x00\x00\x01\xff\x28\x17"
            b"\x00\x00\x00\x40" + bytes(range(64)) + b"scan"
        )

        self.assertIsNone(
            depacketizer.push(rtp.RtpPacket(26, 1, 9, 1, payload, marker=True))
        )
        self.assertEqual(depacketizer.dropped_access_units, 1)

    def test_rfc2435_jpeg_rejects_invalid_dynamic_quantizer_length(self) -> None:
        depacketizer = video_rtp.JpegDepacketizer()
        payload = (
            b"\x00\x00\x00\x00\x00\x80\x28\x1e"
            b"\x00\x00\x00\x60" + bytes(range(96)) + b"scan"
        )

        self.assertIsNone(
            depacketizer.push(rtp.RtpPacket(26, 1, 9, 1, payload, marker=True))
        )
        self.assertEqual(depacketizer.dropped_access_units, 1)

    def test_rfc2435_jpeg_rejects_unimplemented_interlaced_type(self) -> None:
        depacketizer = video_rtp.JpegDepacketizer()
        payload = b"\x01\x00\x00\x00\x00\x32\x28\x1escan"

        self.assertIsNone(
            depacketizer.push(rtp.RtpPacket(26, 1, 9, 1, payload, marker=True))
        )
        self.assertEqual(depacketizer.dropped_access_units, 1)

    def test_rtcp_feedback_and_report_round_trip(self) -> None:
        pli = video_rtcp.build_pli(1, 2)
        fir = video_rtcp.build_fir(1, 2, 7)
        report = video_rtcp.build_receiver_compound(
            1,
            2,
            fraction_lost=3,
            cumulative_lost=4,
            highest_sequence=0x10002,
            jitter=90,
            feedback=pli + fir,
        )
        parsed = video_rtcp.parse_compound(report)
        self.assertEqual(
            [(item.packet_type, item.fmt) for item in parsed],
            [(201, 1), (202, 1), (206, 1), (206, 4)],
        )

    def test_active_video_sender_uses_rtcp_sender_report(self) -> None:
        report = video_rtcp.build_sender_compound(
            0x11111111,
            0x22222222,
            ntp_seconds=0x01020304,
            ntp_fraction=0x05060708,
            rtp_timestamp=0x090A0B0C,
            packet_count=17,
            octet_count=12345,
            cumulative_lost=2,
            highest_sequence=99,
        )

        parsed = video_rtcp.parse_compound(report)
        self.assertEqual(
            [(item.packet_type, item.fmt) for item in parsed],
            [(200, 1), (202, 1)],
        )
        self.assertEqual(
            struct.unpack_from("!IIIIII", parsed[0].payload),
            (0x11111111, 0x01020304, 0x05060708, 0x090A0B0C, 17, 12345),
        )

    def test_send_only_sender_report_has_no_reception_report_block(self) -> None:
        report = video_rtcp.build_sender_compound(
            0x11111111,
            None,
            ntp_seconds=0x01020304,
            ntp_fraction=0x05060708,
            rtp_timestamp=0x090A0B0C,
            packet_count=17,
            octet_count=12345,
        )

        parsed = video_rtcp.parse_compound(report)
        self.assertEqual(
            [(item.packet_type, item.fmt) for item in parsed],
            [(200, 0), (202, 1)],
        )
        self.assertEqual(len(parsed[0].payload), 24)
        self.assertEqual(
            struct.unpack("!IIIIII", parsed[0].payload),
            (0x11111111, 0x01020304, 0x05060708, 0x090A0B0C, 17, 12345),
        )

    def test_rtcp_parser_strips_valid_final_padding(self) -> None:
        pli = video_rtcp.build_pli(1, 2)
        payload = pli[4:] + b"\x00\x00\x00\x04"
        padded = struct.pack("!BBH", pli[0] | 0x20, pli[1], len(payload) // 4) + payload

        parsed = video_rtcp.parse_compound(padded)

        self.assertEqual([(item.packet_type, item.fmt) for item in parsed], [(206, 1)])
        self.assertEqual(parsed[0].payload, pli[4:])

    def test_rtcp_parser_rejects_invalid_padding_and_feedback_layouts(self) -> None:
        pli = video_rtcp.build_pli(1, 2)
        fir = video_rtcp.build_fir(1, 2, 7)

        def extend(packet: bytes, suffix: bytes, *, padding: bool = False) -> bytes:
            payload = packet[4:] + suffix
            first = packet[0] | (0x20 if padding else 0)
            return struct.pack("!BBH", first, packet[1], len(payload) // 4) + payload

        invalid_packets = {
            "empty": b"",
            "not_word_aligned": pli + b"\x00",
            "padding_not_last": extend(pli, b"\x00\x00\x00\x04", padding=True) + fir,
            "zero_padding": extend(pli, b"\x00\x00\x00\x00", padding=True),
            "oversized_padding": extend(pli, b"\x00\x00\x00\x20", padding=True),
            "pli_with_fci": extend(pli, b"\x00\x00\x00\x00"),
            "fir_without_fci": struct.pack("!BBH", fir[0], fir[1], 2) + fir[4:12],
            "fir_partial_fci": extend(fir, b"\x00\x00\x00\x00"),
            "fir_nonzero_media_ssrc": fir[:8] + struct.pack("!I", 2) + fir[12:],
        }

        for name, packet in invalid_packets.items():
            with self.subTest(name=name), self.assertRaises(video_rtcp.RtcpError):
                video_rtcp.parse_compound(packet)

        # RFC 5104 requires receivers to ignore, rather than reject, the FIR
        # FCI reserved bits if a non-conforming sender leaves them non-zero.
        parsed = video_rtcp.parse_compound(fir[:-1] + b"\x01")
        self.assertEqual([(item.packet_type, item.fmt) for item in parsed], [(206, 4)])


class H264SdpTest(unittest.TestCase):
    AUDIO_FORMATS = [audio_format.AudioFormat(16000, "s16le", 1, 20)]

    def test_browser_can_send_and_receive_standard_rtp_jpeg(self) -> None:
        jpeg = sdp.RtpVideoFormat(payload_type=26, encoding="JPEG")
        self.assertTrue(sdp.browser_video_send_supported(jpeg))
        self.assertTrue(sdp.browser_video_receive_supported(jpeg))

    def test_static_video_payload_type_cannot_be_remapped(self) -> None:
        invalid = (
            "v=0\r\nc=IN IP4 192.0.2.20\r\nt=0 0\r\n"
            "m=video 41002 RTP/AVP 26\r\n"
            "a=rtpmap:26 H264/90000\r\n"
        )
        self.assertEqual(sdp.offered_video_formats(invalid), [])

        canonical = invalid.replace("H264/90000", "JPEG/90000")
        offered = sdp.offered_video_formats(canonical)
        self.assertEqual(
            [(item.payload_type, item.encoding) for item in offered],
            [(26, "JPEG")],
        )

    def test_h264_without_fmtp_uses_rfc6184_baseline_level_one_defaults(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.0.2.20\r\nt=0 0\r\n"
            "m=video 41002 RTP/AVP 103\r\n"
            "a=rtpmap:103 H264/90000\r\n"
        )
        selected = sdp.negotiate_h264(offer)
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.profile_level_id, "42000a")
        self.assertEqual(selected.packetization_mode, 0)

    def test_standard_video_codecs_require_their_90khz_rtp_clock(self) -> None:
        for payload_type, mapping in (
            (102, "H264/8000"),
            (103, "VP8/48000"),
            (104, "JPEG/1000"),
            (105, "H263-1998/8000"),
        ):
            with self.subTest(mapping=mapping):
                offer = (
                    "v=0\r\nc=IN IP4 192.0.2.20\r\nt=0 0\r\n"
                    f"m=video 41002 RTP/AVP {payload_type}\r\n"
                    f"a=rtpmap:{payload_type} {mapping}\r\n"
                )
                self.assertEqual(sdp.offered_video_formats(offer), [])

    def test_offer_and_answer_negotiate_h264_mode_one(self) -> None:
        offer = sdp.build_offer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40000,
            self.AUDIO_FORMATS,
            self.AUDIO_FORMATS,
            video_port=40002,
            video_format=sdp.DEFAULT_H264_FORMAT,
        )
        default = sdp.DEFAULT_H264_FORMAT
        self.assertIn(f"m=video 40002 RTP/AVP {default.payload_type}", offer)
        selected = sdp.negotiate_h264(offer)
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.profile_level_id, default.profile_level_id)
        self.assertEqual(selected.packetization_mode, 1)
        parsed = sdp.parse_video_sdp(offer)
        assert parsed is not None
        self.assertEqual(parsed["connection_ip"], "192.168.1.10")
        self.assertEqual(parsed["media_port"], 40002)

    def test_h264_answer_may_use_a_different_dynamic_payload_type(self) -> None:
        offer = sdp.build_offer_directional(
            "192.0.2.10",
            "192.0.2.10",
            40000,
            self.AUDIO_FORMATS,
            self.AUDIO_FORMATS,
            video_port=40002,
            video_formats=(sdp.DEFAULT_H264_FORMAT,),
        )
        answer = (
            "v=0\r\no=- 2 1 IN IP4 192.0.2.20\r\n"
            "s=answer\r\nc=IN IP4 192.0.2.20\r\nt=0 0\r\n"
            "m=audio 41000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\n"
            "m=video 41002 RTP/AVP 120\r\n"
            "a=rtpmap:120 H264/90000\r\n"
            "a=fmtp:120 profile-level-id=42801f;packetization-mode=1;"
            "level-asymmetry-allowed=1\r\na=sendrecv\r\n"
        )

        sdp.validate_sdp_answer(offer, answer)
        selected = sdp.negotiate_video_answer_directional(
            answer,
            (sdp.DEFAULT_H264_FORMAT,),
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.send.payload_type, 120)
        self.assertEqual(
            selected.recv.payload_type,
            sdp.DEFAULT_H264_FORMAT.payload_type,
        )

    def test_default_browser_video_envelope_includes_p4_constrained_h264(self) -> None:
        self.assertEqual(sdp.DEFAULT_H264_FORMAT.profile_level_id, "42801f")
        self.assertEqual(
            sdp.CONSTRAINED_BASELINE_H264_FORMAT.payload_type,
            105,
        )
        self.assertEqual(
            sdp.CONSTRAINED_BASELINE_H264_FORMAT.profile_level_id,
            "42c01f",
        )
        self.assertEqual(
            sdp.DEFAULT_VIDEO_FORMATS[2].fmtp,
            "max-fr=20;max-fs=3600",
        )
        offer = sdp.build_offer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40000,
            self.AUDIO_FORMATS,
            self.AUDIO_FORMATS,
            video_port=40002,
            video_formats=sdp.DEFAULT_VIDEO_FORMATS,
        )
        self.assertIn(
            "a=fmtp:103 profile-level-id=42801f;packetization-mode=1;"
            "level-asymmetry-allowed=1",
            offer,
        )
        self.assertIn(
            "a=fmtp:105 profile-level-id=42c01f;packetization-mode=1;"
            "level-asymmetry-allowed=1",
            offer,
        )
        self.assertIn("m=video 40002 RTP/AVP 103 105 104 26", offer)
        self.assertIn("a=fmtp:104 max-fr=20;max-fs=3600", offer)

    def test_h264_sdp_serialization_preserves_complete_fmtp_contract(self) -> None:
        video = sdp.RtpVideoFormat(
            payload_type=108,
            profile_level_id="428015",
            packetization_mode=1,
            level_asymmetry_allowed=True,
            sprop_parameter_sets="Z0KAHtoCgPaEAAAAwAQAAAMAyPFCqWA=,aM48gA==",
            fmtp=(
                "profile-level-id=64001f;packetization-mode=0;"
                "level-asymmetry-allowed=0;sprop-parameter-sets=stale;"
                "max-fs=1200;max-mbps=36000;x-example=kept"
            ),
        )
        offer = sdp.build_offer_directional(
            "192.0.2.10",
            "192.0.2.10",
            40000,
            self.AUDIO_FORMATS,
            self.AUDIO_FORMATS,
            video_port=40002,
            video_format=video,
        )
        fmtp = next(
            line for line in offer.splitlines() if line.startswith("a=fmtp:108 ")
        )
        self.assertEqual(
            fmtp,
            "a=fmtp:108 profile-level-id=428015;packetization-mode=1;"
            "level-asymmetry-allowed=1;"
            "sprop-parameter-sets=Z0KAHtoCgPaEAAAAwAQAAAMAyPFCqWA=,aM48gA==;"
            "max-fs=1200;max-mbps=36000;x-example=kept",
        )
        reparsed = sdp.offered_video_formats(offer)
        self.assertEqual(len(reparsed), 1)
        self.assertEqual(reparsed[0].sprop_parameter_sets, video.sprop_parameter_sets)
        self.assertIn("max-fs=1200", reparsed[0].fmtp)
        self.assertIn("max-mbps=36000", reparsed[0].fmtp)

    def test_answer_rejects_video_with_port_zero_when_disabled(self) -> None:
        offer = sdp.build_offer_directional(
            "192.168.1.20",
            "192.168.1.20",
            41000,
            self.AUDIO_FORMATS,
            self.AUDIO_FORMATS,
            video_port=41002,
            video_format=sdp.DEFAULT_H264_FORMAT,
        )
        audio = sdp.negotiate_directional(offer, self.AUDIO_FORMATS, self.AUDIO_FORMATS)
        assert audio is not None
        answer = sdp.build_answer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40000,
            audio.send,
            audio.recv,
            remote_sdp=offer,
        )
        self.assertIn("m=audio 40000", answer)
        self.assertIn(
            f"m=video 0 RTP/AVP {sdp.DEFAULT_H264_FORMAT.payload_type}",
            answer,
        )

    def test_answer_preserves_video_first_media_order_and_direction(self) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.20\r\n"
            "s=-\r\n"
            "c=IN IP4 192.168.1.20\r\n"
            "t=0 0\r\n"
            "m=video 41002 RTP/AVP 103\r\n"
            "a=rtpmap:103 H264/90000\r\n"
            "a=fmtp:103 profile-level-id=42e01f;packetization-mode=1\r\n"
            "a=sendonly\r\n"
            "m=audio 41000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\n"
            "a=ptime:20\r\n"
            "a=sendrecv\r\n"
        )
        video = sdp.negotiate_h264(offer)
        audio = sdp.negotiate_directional(offer, self.AUDIO_FORMATS, self.AUDIO_FORMATS)
        assert video is not None and audio is not None
        answer = sdp.build_answer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40000,
            audio.send,
            audio.recv,
            remote_sdp=offer,
            video_port=40002,
            video_format=video,
        )
        self.assertLess(answer.index("m=video"), answer.index("m=audio"))
        self.assertIn("m=video 40002 RTP/AVP 103", answer)
        self.assertIn("a=recvonly", answer)

    def test_accepts_packetization_zero_and_standard_profiles(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.168.1.20\r\nt=0 0\r\n"
            "m=video 41002 RTP/AVP 102 103\r\n"
            "a=rtpmap:102 H264/90000\r\n"
            "a=fmtp:102 profile-level-id=42e01f;packetization-mode=0\r\n"
            "a=rtpmap:103 H264/90000\r\n"
            "a=fmtp:103 profile-level-id=64001f;packetization-mode=1\r\n"
        )
        formats = sdp.offered_h264_formats(offer)
        self.assertEqual([item.packetization_mode for item in formats], [0, 1])
        self.assertEqual(
            [item.profile_level_id for item in formats], ["42e01f", "64001f"]
        )

    def test_parses_all_rfc6184_table5_profiles_for_generic_relay(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.168.1.20\r\nt=0 0\r\n"
            "m=video 41002 RTP/AVP 102 103\r\n"
            "a=rtpmap:102 H264/90000\r\n"
            "a=fmtp:102 profile-level-id=58c00d;packetization-mode=1\r\n"
            "a=rtpmap:103 H264/90000\r\n"
            "a=fmtp:103 profile-level-id=6e000d;packetization-mode=1\r\n"
        )

        formats = sdp.offered_h264_formats(offer)

        self.assertEqual(
            [item.profile_level_id for item in formats],
            ["58c00d", "6e000d"],
        )
        self.assertEqual(sdp.negotiate_h264(offer), formats[0])

    def test_generic_video_formats_and_feedback(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.168.1.20\r\nt=0 0\r\n"
            "m=video 41002 RTP/AVPF 103 26 104\r\n"
            "a=rtpmap:103 VP8/90000\r\n"
            "a=rtcp-fb:103 nack pli\r\n"
            "a=rtpmap:104 H265/90000\r\n"
        )
        formats = sdp.offered_video_formats(offer)
        self.assertEqual([item.encoding for item in formats], ["VP8", "JPEG", "H265"])
        self.assertEqual(formats[0].rtcp_feedback, ("nack pli",))
        self.assertEqual(formats[0].transport_profile, "RTP/AVPF")
        self.assertEqual(sdp.negotiate_video(offer).encoding, "VP8")

    def test_avp_ignores_avpf_feedback_attributes(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.168.1.20\r\nt=0 0\r\n"
            "m=video 41002 RTP/AVP 103\r\n"
            "a=rtpmap:103 VP8/90000\r\n"
            "a=rtcp-fb:103 nack pli\r\n"
        )
        formats = sdp.offered_video_formats(offer)
        self.assertEqual(len(formats), 1)
        self.assertEqual(formats[0].transport_profile, "RTP/AVP")
        self.assertEqual(formats[0].rtcp_feedback, ())

    def test_rtcp_mux_offer_can_be_answered_with_separate_rtcp(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.168.1.20\r\nt=0 0\r\n"
            "m=audio 41000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\n"
            "m=video 41002 RTP/AVPF 103\r\n"
            "a=rtpmap:103 VP8/90000\r\na=rtcp-mux\r\n"
        )
        audio = sdp.negotiate_directional(offer, self.AUDIO_FORMATS, self.AUDIO_FORMATS)
        video = sdp.negotiate_video(offer)
        self.assertIsNotNone(audio)
        self.assertIsNotNone(video)
        assert audio is not None and video is not None
        answer = sdp.build_answer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40000,
            audio.send,
            audio.recv,
            remote_sdp=offer,
            video_port=40002,
            video_format=video,
        )
        # RFC 5761 makes rtcp-mux negotiable. Omitting it in the answer
        # declines multiplexing while retaining otherwise compatible video.
        self.assertNotIn("a=rtcp-mux", answer)
        self.assertIn("a=rtcp:40003", answer)
        self.assertIn("m=video 40002 RTP/AVPF 103", answer)

    def test_rtcp_mux_only_offer_rejects_video_but_preserves_audio(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.168.1.20\r\nt=0 0\r\n"
            "m=audio 41000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\n"
            "m=video 41002 RTP/AVPF 103\r\n"
            "a=rtpmap:103 VP8/90000\r\n"
            "a=rtcp-mux\r\na=rtcp-mux-only\r\n"
        )
        audio = sdp.negotiate_directional(offer, self.AUDIO_FORMATS, self.AUDIO_FORMATS)

        self.assertIsNotNone(audio)
        self.assertIsNone(sdp.parse_video_sdp(offer))
        self.assertIsNone(sdp.negotiate_video(offer))
        assert audio is not None
        answer = sdp.build_answer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40000,
            audio.send,
            audio.recv,
            remote_sdp=offer,
            video_port=40002,
            video_format=sdp.RtpVideoFormat(payload_type=103, encoding="VP8"),
        )
        self.assertIn("m=audio 40000 RTP/AVP 96", answer)
        self.assertIn("m=video 0 RTP/AVPF 103", answer)
        self.assertNotIn("a=rtcp-mux", answer)

    def test_answer_preserves_offer_time_description(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.168.1.20\r\n"
            "t=123 456\r\nr=604800 3600 0 90000\r\n"
            "m=audio 41000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\n"
        )
        audio = sdp.negotiate_directional(offer, self.AUDIO_FORMATS, self.AUDIO_FORMATS)
        assert audio is not None

        answer = sdp.build_answer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40000,
            audio.send,
            audio.recv,
            remote_sdp=offer,
        )

        self.assertIn("t=123 456\r\nr=604800 3600 0 90000\r\n", answer)
        self.assertNotIn("t=0 0", answer)

    def test_parses_standard_rtcp_port_with_distinct_ipv4_address(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.0.2.20\r\nt=0 0\r\n"
            "m=video 41002 RTP/AVPF 103\r\n"
            "a=rtpmap:103 VP8/90000\r\n"
            "a=rtcp:53020 IN IP4 198.51.100.22\r\n"
        )
        parsed = sdp.parse_video_sdp(offer)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["connection_ip"], "192.0.2.20")
        self.assertEqual(parsed["rtcp_port"], 53020)
        self.assertEqual(parsed["rtcp_address"], "198.51.100.22")

    def test_unsupported_rtcp_address_family_rejects_only_video(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.0.2.20\r\nt=0 0\r\n"
            "m=audio 41000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\n"
            "m=video 41002 RTP/AVPF 103\r\n"
            "a=rtpmap:103 VP8/90000\r\n"
            "a=rtcp:53020 IN IP6 2001:db8::22\r\n"
        )
        self.assertIsNotNone(
            sdp.negotiate_directional(offer, self.AUDIO_FORMATS, self.AUDIO_FORMATS)
        )
        self.assertIsNone(sdp.parse_video_sdp(offer))

    def test_answer_preserves_avpf_profile_and_feedback(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.168.1.20\r\nt=0 0\r\n"
            "m=audio 41000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\n"
            "m=video 41002 RTP/AVPF 103\r\n"
            "a=rtpmap:103 VP8/90000\r\n"
            "a=rtcp-fb:103 nack pli\r\na=sendonly\r\n"
        )
        audio = sdp.negotiate_directional(offer, self.AUDIO_FORMATS, self.AUDIO_FORMATS)
        video = sdp.negotiate_video(offer)
        assert audio is not None and video is not None
        answer = sdp.build_answer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40000,
            audio.send,
            audio.recv,
            remote_sdp=offer,
            video_port=40002,
            video_format=video,
        )
        self.assertIn("m=video 40002 RTP/AVPF 103", answer)
        self.assertIn("a=rtcp-fb:103 nack pli", answer)

    def test_avpf_answer_advertises_only_implemented_exact_feedback(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.168.1.20\r\nt=0 0\r\n"
            "m=audio 41000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\n"
            "m=video 41002 RTP/AVPF 103\r\n"
            "a=rtpmap:103 VP8/90000\r\n"
            "a=rtcp-fb:103 nack pli\r\n"
            "a=rtcp-fb:103 ccm fir\r\n"
            "a=rtcp-fb:103 nack\r\n"
            "a=rtcp-fb:103 goog-remb\r\n"
            "a=rtcp-fb:103 NACK PLI\r\n"
        )
        audio = sdp.negotiate_directional(offer, self.AUDIO_FORMATS, self.AUDIO_FORMATS)
        video = sdp.negotiate_video(offer)
        assert audio is not None and video is not None
        self.assertEqual(video.rtcp_feedback, ("nack pli", "ccm fir"))

        answer = sdp.build_answer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40000,
            audio.send,
            audio.recv,
            remote_sdp=offer,
            video_port=40002,
            video_format=video,
        )

        self.assertIn("a=rtcp-fb:103 nack pli", answer)
        self.assertIn("a=rtcp-fb:103 ccm fir", answer)
        self.assertNotIn("a=rtcp-fb:103 nack\r\n", answer)
        self.assertNotIn("goog-remb", answer)
        self.assertNotIn("NACK PLI", answer)

    def test_h263p_uses_the_standard_rtpmap_token_in_answers(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.168.1.20\r\nt=0 0\r\n"
            "m=audio 41000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\n"
            "m=video 41002 RTP/AVP 105\r\n"
            "a=rtpmap:105 H263-1998/90000\r\n"
            "a=sendonly\r\n"
        )
        audio = sdp.negotiate_directional(offer, self.AUDIO_FORMATS, self.AUDIO_FORMATS)
        video = sdp.negotiate_video(offer, accepted_encodings=("H263P",))
        assert audio is not None and video is not None
        answer = sdp.build_answer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40000,
            audio.send,
            audio.recv,
            remote_sdp=offer,
            video_port=40002,
            video_format=video,
        )
        self.assertIn("a=rtpmap:105 H263-1998/90000", answer)
        parsed = sdp.offered_video_formats(answer)
        self.assertEqual([item.encoding for item in parsed], ["H263P"])

    def test_camera_send_prefers_packetizable_codec_from_same_offer(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.168.1.20\r\nt=0 0\r\n"
            "m=video 41002 RTP/AVP 102 103\r\n"
            "a=rtpmap:102 H264/90000\r\n"
            "a=fmtp:102 profile-level-id=42e01f;packetization-mode=0\r\n"
            "a=rtpmap:103 VP8/90000\r\n"
            "a=sendrecv\r\n"
        )
        self.assertEqual(sdp.negotiate_video(offer).encoding, "H264")
        selected = sdp.negotiate_video(offer, prefer_browser_send=True)
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.encoding, "VP8")

    def test_direct_browser_codec_precedes_optional_transcoding(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.168.1.20\r\nt=0 0\r\n"
            "m=video 41002 RTP/AVP 104 102\r\n"
            "a=rtpmap:104 H265/90000\r\n"
            "a=rtpmap:102 H264/90000\r\n"
            "a=fmtp:102 profile-level-id=42e01f;packetization-mode=1\r\n"
            "a=sendonly\r\n"
        )
        selected = sdp.negotiate_video(
            offer,
            accepted_encodings=("H264", "H265"),
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.encoding, "H264")

    def test_rejects_static_h264_payload_type(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.168.1.20\r\nt=0 0\r\n"
            "m=audio 41000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\n"
            "m=video 41002 RTP/AVP 35\r\n"
            "a=rtpmap:35 H264/90000\r\n"
            "a=fmtp:35 profile-level-id=42e01f;packetization-mode=1\r\n"
        )
        self.assertEqual(sdp.offered_h264_formats(offer), [])

    def test_does_not_select_later_video_after_unsupported_first_section(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.168.1.20\r\nt=0 0\r\n"
            "m=audio 41000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\n"
            "m=video 41002 RTP/SAVP 102\r\n"
            "a=rtpmap:102 H264/90000\r\n"
            "a=fmtp:102 profile-level-id=42e01f;packetization-mode=1\r\n"
            "m=video 41004 RTP/AVP 103\r\n"
            "a=rtpmap:103 H264/90000\r\n"
            "a=fmtp:103 profile-level-id=42e01f;packetization-mode=1\r\n"
        )
        self.assertEqual(sdp.offered_h264_formats(offer), [])
        audio = sdp.negotiate_directional(offer, self.AUDIO_FORMATS, self.AUDIO_FORMATS)
        assert audio is not None
        answer = sdp.build_answer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40000,
            audio.send,
            audio.recv,
            remote_sdp=offer,
            video_port=40002,
            video_format=sdp.DEFAULT_H264_FORMAT,
        )
        self.assertIn("m=video 0 RTP/SAVP 102", answer)
        self.assertIn("m=video 0 RTP/AVP 103", answer)

    def test_baresip_answer_is_accepted(self) -> None:
        answer = (
            "v=0\r\no=- 1 2 IN IP4 192.168.1.48\r\ns=-\r\n"
            "c=IN IP4 192.168.1.48\r\nt=0 0\r\n"
            "m=audio 44652 RTP/AVP 8 0 99\r\n"
            "a=rtpmap:8 PCMA/8000\r\na=sendrecv\r\n"
            "m=video 19728 RTP/AVP 102\r\n"
            "a=rtpmap:102 H264/90000\r\n"
            "a=fmtp:102 packetization-mode=1;profile-level-id=42e01f\r\n"
            "a=sendrecv\r\na=framerate:15.00\r\na=rtcp-fb:* nack pli\r\n"
        )
        selected = sdp.negotiate_h264(answer)
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.payload_type, 102)
        self.assertEqual(selected.direction, "sendrecv")
        self.assertEqual(selected.max_framerate, 15.0)

    def test_jpeg_answer_framerate_limits_local_browser_sender(self) -> None:
        offer = sdp.RtpVideoFormat(payload_type=26, encoding="JPEG")
        answer = (
            "v=0\r\nc=IN IP4 192.0.2.57\r\nt=0 0\r\n"
            "m=video 40002 RTP/AVP 26\r\n"
            "a=rtpmap:26 JPEG/90000\r\n"
            "a=framerate:3\r\n"
            "a=sendrecv\r\n"
        )

        directional = sdp.negotiate_video_answer_directional(answer, offer)

        self.assertIsNotNone(directional)
        assert directional is not None
        self.assertEqual(directional.send.max_framerate, 3.0)
        self.assertIsNone(directional.recv.max_framerate)
        self.assertIn("framerate=3", directional.send.wire_token())

    def test_video_sdp_serializes_media_level_framerate(self) -> None:
        video_format = sdp.RtpVideoFormat(
            payload_type=26,
            encoding="JPEG",
            max_framerate=3,
        )

        answer = sdp.build_offer_directional(
            "192.0.2.10",
            "192.0.2.10",
            40000,
            [audio_format.AudioFormat(16000, "s16le", 1, 20)],
            [audio_format.AudioFormat(16000, "s16le", 1, 20)],
            video_port=40002,
            video_formats=(video_format,),
        )

        self.assertIn("a=framerate:3\r\n", answer)

    def test_answer_cannot_select_an_unoffered_payload_type(self) -> None:
        answer = (
            "v=0\r\nc=IN IP4 192.168.1.48\r\nt=0 0\r\n"
            "m=audio 44652 RTP/AVP 8\r\n"
            "m=video 19728 RTP/AVP 103\r\n"
            "a=rtpmap:103 H264/90000\r\n"
            "a=fmtp:103 packetization-mode=1;profile-level-id=42e01f\r\n"
        )
        self.assertIsNone(
            sdp.negotiate_video_answer_directional(
                answer, sdp.DEFAULT_H264_FORMAT
            )
        )

    def test_answer_cannot_change_h264_packetization_or_profile_family(self) -> None:
        offered = sdp.DEFAULT_H264_FORMAT

        def answer(fmtp: str) -> str:
            return (
                "v=0\r\nc=IN IP4 192.168.1.48\r\nt=0 0\r\n"
                f"m=video 19728 RTP/AVP {offered.payload_type}\r\n"
                f"a=rtpmap:{offered.payload_type} H264/90000\r\n"
                f"a=fmtp:{offered.payload_type} {fmtp}\r\n"
            )

        self.assertIsNone(
            sdp.negotiate_video_answer_directional(
                answer(
                    f"packetization-mode=0;profile-level-id={offered.profile_level_id}"
                ),
                offered,
            )
        )
        self.assertIsNone(
            sdp.negotiate_video_answer_directional(
                answer("packetization-mode=1;profile-level-id=64001f"), offered
            )
        )
        self.assertIsNone(
            sdp.negotiate_video_answer_directional(
                answer("packetization-mode=1;profile-level-id=428020"), offered
            )
        )
        downgraded = sdp.negotiate_video_answer_directional(
            answer("packetization-mode=1;profile-level-id=42800a"), offered
        )
        self.assertIsNotNone(downgraded)
        assert downgraded is not None
        self.assertEqual(downgraded.send.profile_level_id, "42800a")
        selected = sdp.negotiate_video_answer_directional(
            answer(
                "packetization-mode=1;profile-level-id=428015;level-asymmetry-allowed=1"
            ),
            offered,
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.send.profile_level_id, "428015")

    def test_h264_answer_accepts_rfc_level_1b_downgrade(self) -> None:
        offered = sdp.RtpVideoFormat(
            payload_type=98,
            profile_level_id="42a00b",
            level_asymmetry_allowed=False,
        )
        answer = (
            "v=0\r\nc=IN IP4 192.0.2.2\r\nt=0 0\r\n"
            "m=video 49170 RTP/AVP 98\r\n"
            "a=rtpmap:98 H264/90000\r\n"
            "a=fmtp:98 profile-level-id=42b00b;packetization-mode=1\r\n"
        )

        selected = sdp.negotiate_video_answer_directional(answer, offered)

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.send.profile_level_id, "42b00b")

    def test_h264_passthrough_checks_directional_stream_level(self) -> None:
        high = sdp.RtpVideoFormat(
            profile_level_id="42e01f",
            level_asymmetry_allowed=True,
        )
        low = sdp.RtpVideoFormat(
            profile_level_id="42e015",
            level_asymmetry_allowed=True,
        )
        self.assertTrue(sdp.video_formats_passthrough_compatible(low, high))
        self.assertFalse(sdp.video_formats_passthrough_compatible(high, low))

    def test_h264_passthrough_enforces_receiver_maximum_extensions(self) -> None:
        large_frames = sdp.RtpVideoFormat(
            profile_level_id="428015",
            fmtp="max-fs=3600;max-mbps=108000",
        )
        small_frames = sdp.RtpVideoFormat(
            profile_level_id="428015",
            fmtp="max-fs=1200;max-mbps=36000",
        )

        self.assertFalse(
            sdp.video_formats_passthrough_compatible(large_frames, small_frames)
        )
        self.assertTrue(
            sdp.video_formats_passthrough_compatible(small_frames, large_frames)
        )
        # Conservatively reject an extension that the destination did not
        # advertise; an encoded relay cannot prove the stream fits.
        self.assertFalse(
            sdp.video_formats_passthrough_compatible(
                large_frames,
                sdp.RtpVideoFormat(profile_level_id="428015"),
            )
        )
        # RFC 6184 max-* parameters extend the source's signalled level.
        # A higher destination level already includes those limits even when
        # it does not repeat the optional extension parameters.
        self.assertTrue(
            sdp.video_formats_passthrough_compatible(
                large_frames,
                sdp.RtpVideoFormat(profile_level_id="428028"),
            )
        )

    def test_h264_passthrough_uses_complete_effective_level_limits(self) -> None:
        extended_level_21 = sdp.RtpVideoFormat(
            profile_level_id="428015",
            fmtp=(
                "max-mbps=108000;max-smbps=108000;max-fs=3600;"
                "max-cpb=5000;max-dpb=2000;max-br=5000"
            ),
        )
        level_40 = sdp.RtpVideoFormat(profile_level_id="428028")

        self.assertTrue(
            sdp.video_formats_passthrough_compatible(extended_level_21, level_40)
        )
        self.assertFalse(
            sdp.video_formats_passthrough_compatible(
                sdp.RtpVideoFormat(
                    profile_level_id="428015",
                    fmtp="max-br=30000",
                ),
                level_40,
            )
        )

    def test_h264_passthrough_rejects_invalid_receiver_extensions(self) -> None:
        level_31 = sdp.RtpVideoFormat(profile_level_id="42801f")
        invalid_formats = (
            sdp.RtpVideoFormat(
                profile_level_id="428015",
                fmtp="max-fs=700",
            ),
            sdp.RtpVideoFormat(
                profile_level_id="428015",
                fmtp="max-mbps=10000",
            ),
            sdp.RtpVideoFormat(
                profile_level_id="428015",
                fmtp="max-mbps=36000;max-smbps=30000",
            ),
        )

        for invalid in invalid_formats:
            with self.subTest(fmtp=invalid.fmtp):
                self.assertFalse(
                    sdp.video_formats_passthrough_compatible(invalid, level_31)
                )

    def test_h264_max_recv_level_changes_directional_receive_envelope(self) -> None:
        extended = sdp.RtpVideoFormat(
            profile_level_id="42800d",
            fmtp="max-recv-level=801f",
        )
        level_21 = sdp.RtpVideoFormat(profile_level_id="428015")
        level_31 = sdp.RtpVideoFormat(profile_level_id="42801f")

        self.assertFalse(sdp.video_formats_passthrough_compatible(extended, level_21))
        self.assertTrue(sdp.video_formats_passthrough_compatible(extended, level_31))
        invalid = sdp.RtpVideoFormat(
            profile_level_id="428015",
            fmtp="max-recv-level=800d",
        )
        self.assertFalse(sdp.video_formats_passthrough_compatible(invalid, level_31))
        wrong_subprofile = sdp.RtpVideoFormat(
            profile_level_id="428015",
            fmtp="max-recv-level=e01f",
        )
        malformed = sdp.RtpVideoFormat(
            profile_level_id="428015",
            fmtp="max-recv-level=not-hex",
        )
        self.assertFalse(
            sdp.video_formats_passthrough_compatible(wrong_subprofile, level_31)
        )
        self.assertFalse(sdp.video_formats_passthrough_compatible(malformed, level_31))

    def test_outbound_h264_answer_retains_bilateral_directional_levels(self) -> None:
        offered = sdp.RtpVideoFormat(
            payload_type=103,
            profile_level_id="42801f",
            level_asymmetry_allowed=True,
        )
        answer = (
            "v=0\r\nc=IN IP4 192.0.2.2\r\nt=0 0\r\n"
            "m=video 49170 RTP/AVP 103\r\n"
            "a=rtpmap:103 H264/90000\r\n"
            "a=fmtp:103 profile-level-id=42800d;packetization-mode=1;"
            "level-asymmetry-allowed=1\r\n"
            "a=sendrecv\r\n"
        )

        directional = sdp.negotiate_video_answer_directional(answer, offered)

        self.assertIsNotNone(directional)
        assert directional is not None
        self.assertEqual(directional.send.profile_level_id, "42800d")
        self.assertEqual(directional.recv.profile_level_id, "42801f")
        self.assertTrue(directional.send.level_asymmetry_allowed)
        self.assertTrue(directional.recv.level_asymmetry_allowed)

    def test_outbound_h264_answer_without_bilateral_asymmetry_is_symmetric(
        self,
    ) -> None:
        offered = sdp.RtpVideoFormat(
            payload_type=103,
            profile_level_id="42801f",
            level_asymmetry_allowed=True,
        )
        answer = (
            "v=0\r\nc=IN IP4 192.0.2.2\r\nt=0 0\r\n"
            "m=video 49170 RTP/AVP 103\r\n"
            "a=rtpmap:103 H264/90000\r\n"
            "a=fmtp:103 profile-level-id=42800d;packetization-mode=1\r\n"
        )

        directional = sdp.negotiate_video_answer_directional(answer, offered)

        self.assertIsNotNone(directional)
        assert directional is not None
        self.assertEqual(directional.send.profile_level_id, "42800d")
        self.assertEqual(directional.recv.profile_level_id, "42800d")
        self.assertFalse(directional.send.level_asymmetry_allowed)

    def test_inbound_h264_offer_serializes_local_receive_level_in_answer(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.0.2.2\r\nt=0 0\r\n"
            "m=video 49170 RTP/AVP 103\r\n"
            "a=rtpmap:103 H264/90000\r\n"
            "a=fmtp:103 profile-level-id=42800d;packetization-mode=1;"
            "level-asymmetry-allowed=1\r\n"
            "a=sendrecv\r\n"
        )
        local = sdp.RtpVideoFormat(
            payload_type=120,
            profile_level_id="42801f",
            level_asymmetry_allowed=True,
        )

        directional = sdp.negotiate_video_offer_directional(
            offer,
            local_formats=(local,),
        )

        self.assertIsNotNone(directional)
        assert directional is not None
        self.assertEqual(directional.send.profile_level_id, "42800d")
        self.assertEqual(directional.recv.profile_level_id, "42801f")
        self.assertEqual(directional.recv.payload_type, 103)

    def test_h264_parameter_sets_follow_the_sender_not_receiver_limits(self) -> None:
        remote_sets = "Z0IAHw==,aM4G4g=="
        local_sets = "Z0IAHQ==,aM4G4g=="
        remote_answer_sets = "Z0IAFA==,aM4G4g=="
        offer = (
            "v=0\r\nc=IN IP4 192.0.2.2\r\nt=0 0\r\n"
            "m=video 49170 RTP/AVP 103\r\n"
            "a=rtpmap:103 H264/90000\r\n"
            "a=fmtp:103 profile-level-id=42800d;packetization-mode=1;"
            "level-asymmetry-allowed=1;"
            f"sprop-parameter-sets={remote_sets}\r\n"
        )
        local = sdp.RtpVideoFormat(
            payload_type=103,
            profile_level_id="42801f",
            level_asymmetry_allowed=True,
            sprop_parameter_sets=local_sets,
        )

        inbound = sdp.negotiate_video_offer_directional(
            offer,
            local_formats=(local,),
        )

        self.assertIsNotNone(inbound)
        assert inbound is not None
        self.assertEqual(inbound.send.profile_level_id, "42800d")
        self.assertEqual(inbound.send.sprop_parameter_sets, local_sets)
        self.assertEqual(inbound.recv.profile_level_id, "42801f")
        self.assertEqual(inbound.recv.sprop_parameter_sets, remote_sets)
        self.assertEqual(inbound.answer_format.sprop_parameter_sets, local_sets)

        answer = offer.replace("42800d", "428015").replace(
            remote_sets,
            remote_answer_sets,
        )
        outbound = sdp.negotiate_video_answer_directional(answer, local)
        self.assertIsNotNone(outbound)
        assert outbound is not None
        self.assertEqual(outbound.send.profile_level_id, "428015")
        self.assertEqual(outbound.send.sprop_parameter_sets, local_sets)
        self.assertEqual(outbound.recv.profile_level_id, "42801f")
        self.assertEqual(outbound.recv.sprop_parameter_sets, remote_answer_sets)

    def test_vp8_offer_and_answer_keep_independent_receiver_limits(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.0.2.2\r\nt=0 0\r\n"
            "m=video 49170 RTP/AVP 104\r\n"
            "a=rtpmap:104 VP8/90000\r\n"
            "a=fmtp:104 max-fr=15;max-fs=1200\r\n"
        )
        local = sdp.RtpVideoFormat(
            payload_type=104,
            encoding="VP8",
            fmtp="max-fr=30;max-fs=3600",
        )

        inbound = sdp.negotiate_video_offer_directional(
            offer,
            local_formats=(local,),
        )

        self.assertIsNotNone(inbound)
        assert inbound is not None
        self.assertEqual(inbound.send.fmtp, "max-fr=15;max-fs=1200")
        self.assertEqual(inbound.recv.fmtp, "max-fr=30;max-fs=3600")

        answer = offer.replace("max-fr=15;max-fs=1200", "max-fr=10;max-fs=600")
        outbound = sdp.negotiate_video_answer_directional(answer, local)
        self.assertIsNotNone(outbound)
        assert outbound is not None
        self.assertEqual(outbound.send.fmtp, "max-fr=10;max-fs=600")
        self.assertEqual(outbound.recv.fmtp, "max-fr=30;max-fs=3600")

    def test_b2bua_projects_h264_answer_level_onto_source_payload(self) -> None:
        source_offer = sdp.RtpVideoFormat(
            payload_type=102,
            profile_level_id="42e01f",
            level_asymmetry_allowed=False,
        )
        destination_answer = sdp.RtpVideoFormat(
            payload_type=110,
            profile_level_id="42e015",
            level_asymmetry_allowed=False,
        )

        source_answer = sdp.video_answer_contract(
            source_offer,
            destination_answer,
        )

        self.assertIsNotNone(source_answer)
        assert source_answer is not None
        self.assertEqual(source_answer.payload_type, 102)
        self.assertEqual(source_answer.profile_level_id, "42e015")
        self.assertTrue(
            sdp.video_formats_passthrough_compatible(
                source_answer,
                destination_answer,
            )
        )

    def test_b2bua_encodes_level_1b_on_the_source_subprofile(self) -> None:
        source_offer = sdp.RtpVideoFormat(
            payload_type=102,
            profile_level_id="42a00b",
            level_asymmetry_allowed=False,
        )
        destination_answer = sdp.RtpVideoFormat(
            payload_type=110,
            profile_level_id="42b00b",
            level_asymmetry_allowed=False,
        )

        source_answer = sdp.video_answer_contract(
            source_offer,
            destination_answer,
        )

        self.assertIsNotNone(source_answer)
        assert source_answer is not None
        self.assertEqual(source_answer.profile_level_id, "42b00b")

    def test_h264_equivalent_rfc_subprofile_encodings_interoperate(self) -> None:
        constrained_baseline = sdp.RtpVideoFormat(profile_level_id="42e015")
        main_compatible = sdp.RtpVideoFormat(profile_level_id="4d8015")

        self.assertTrue(
            sdp.video_formats_passthrough_compatible(
                constrained_baseline,
                main_compatible,
            )
        )

    def test_b2bua_projects_vp8_receiver_limits_without_blocking_relay(self) -> None:
        caller = sdp.RtpVideoFormat(
            payload_type=103,
            encoding="VP8",
            fmtp="max-fr=30;max-fs=3600",
        )
        callee = sdp.RtpVideoFormat(
            payload_type=110,
            encoding="VP8",
            fmtp="max-fr=15;max-fs=1200",
        )

        answer = sdp.video_answer_contract(caller, callee)

        self.assertIsNotNone(answer)
        assert answer is not None
        self.assertEqual(answer.payload_type, caller.payload_type)
        self.assertEqual(answer.fmtp, callee.fmtp)
        self.assertTrue(sdp.video_formats_passthrough_compatible(caller, callee))
        self.assertTrue(sdp.video_formats_passthrough_compatible(callee, caller))
        self.assertTrue(sdp.video_formats_renegotiation_compatible(caller, answer))

    def test_camera_authorization_survives_video_hold_and_resume(self) -> None:
        authorized = True

        initial = sdp.constrained_video_direction("sendrecv", allow_send=authorized)
        held = sdp.constrained_video_direction("sendonly", allow_send=authorized)
        resumed = sdp.constrained_video_direction("sendrecv", allow_send=authorized)

        self.assertEqual((initial, held, resumed), ("sendrecv", "recvonly", "sendrecv"))
        self.assertEqual(
            sdp.constrained_video_direction("sendrecv", allow_send=False),
            "recvonly",
        )

    def test_recvonly_offer_may_enable_camera_with_sendrecv_reinvite(self) -> None:
        previous_send = sdp.RtpVideoFormat(
            payload_type=104,
            encoding="VP8",
            fmtp="max-fr=20;max-fs=3600",
            direction="recvonly",
        )
        # The unused local receive candidate may differ before the remote
        # camera is enabled. It was not part of a live receive path.
        previous_recv = sdp.RtpVideoFormat(
            payload_type=105,
            encoding="H264",
            direction="recvonly",
        )
        updated = sdp.RtpVideoFormat(
            payload_type=104,
            encoding="VP8",
            fmtp="max-fr=30;max-fs=3600",
            direction="sendrecv",
        )

        self.assertTrue(
            sdp.directional_video_renegotiation_compatible(
                previous_send,
                previous_recv,
                updated,
                updated,
            )
        )

    def test_active_video_codec_change_still_requires_compatibility(self) -> None:
        previous = sdp.RtpVideoFormat(encoding="VP8", direction="sendrecv")
        updated = sdp.RtpVideoFormat(encoding="H264", direction="sendrecv")

        self.assertFalse(
            sdp.directional_video_renegotiation_compatible(
                previous,
                previous,
                updated,
                updated,
            )
        )

    def test_unsupported_media_level_connection_does_not_break_audio(self) -> None:
        offer = (
            "v=0\r\no=- 1 2 IN IP4 192.168.1.48\r\ns=-\r\n"
            "c=IN IP4 192.168.1.48\r\nt=0 0\r\n"
            "m=audio 44652 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\na=sendrecv\r\n"
            "m=video 19728 RTP/AVP 102\r\n"
            "c=IN IP6 2001:db8::1\r\n"
            "a=rtpmap:102 H264/90000\r\n"
            "a=fmtp:102 packetization-mode=1;profile-level-id=42e01f\r\n"
        )
        audio = sdp.negotiate_directional(offer, self.AUDIO_FORMATS, self.AUDIO_FORMATS)
        self.assertIsNotNone(audio)
        self.assertEqual(sdp.negotiate_h264(offer), None)
        assert audio is not None
        answer = sdp.build_answer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40000,
            audio.send,
            audio.recv,
            remote_sdp=offer,
        )
        self.assertIn("m=audio 40000 RTP/AVP 96", answer)
        self.assertIn("m=video 0 RTP/AVP 102", answer)

    def test_unsupported_session_connection_still_rejects_call(self) -> None:
        offer = (
            "v=0\r\nc=IN IP6 2001:db8::1\r\nt=0 0\r\n"
            "m=audio 44652 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\n"
        )
        with self.assertRaises(sdp.SdpError):
            sdp.build_answer_directional(
                "192.168.1.10",
                "192.168.1.10",
                40000,
                sdp.RtpPcmFormat(96, "L16", 16000, 1, 20),
                sdp.RtpPcmFormat(96, "L16", 16000, 1, 20),
                remote_sdp=offer,
            )

    def test_answer_rejects_additional_audio_section_in_offered_order(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.168.1.20\r\nt=0 0\r\n"
            "m=audio 41000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\n"
            "m=audio 41002 RTP/AVP 97\r\n"
            "a=rtpmap:97 L16/16000/1\r\na=ptime:20\r\n"
        )
        audio = sdp.negotiate_directional(offer, self.AUDIO_FORMATS, self.AUDIO_FORMATS)
        assert audio is not None
        answer = sdp.build_answer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40000,
            audio.send,
            audio.recv,
            remote_sdp=offer,
        )
        self.assertLess(
            answer.index("m=audio 40000"), answer.index("m=audio 0 RTP/AVP 97")
        )

    def test_answer_rejects_unsupported_audio_before_selected_rtp_avp(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.168.1.20\r\nt=0 0\r\n"
            "m=audio 40998 RTP/SAVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\n"
            "m=audio 41000 RTP/AVP 97\r\n"
            "a=rtpmap:97 L16/16000/1\r\na=ptime:20\r\n"
        )
        audio = sdp.negotiate_directional(offer, self.AUDIO_FORMATS, self.AUDIO_FORMATS)
        assert audio is not None
        answer = sdp.build_answer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40000,
            audio.send,
            audio.recv,
            remote_sdp=offer,
        )
        self.assertLess(
            answer.index("m=audio 0 RTP/SAVP 96"),
            answer.index("m=audio 40000 RTP/AVP 97"),
        )


if __name__ == "__main__":
    unittest.main()
