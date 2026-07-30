"""Standards-based DTMF translation at the SIP RTP bridge boundary."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest


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


audio_format = _load("audio_format")
rtp = _load("rtp")
sip_rtp_bridge = _load("sip_rtp_bridge")


class AudioRtpSenderStateTest(unittest.TestCase):
    def test_source_identity_and_clock_are_mutable_across_handoffs(self) -> None:
        source = rtp.AudioRtpSenderState.create(ssrc=0x12345678)
        source.sequence = rtp.next_sequence(0xFFFF)
        source.timestamp = rtp.next_timestamp(0xFFFFFF00, 256)

        self.assertEqual(source.ssrc, 0x12345678)
        self.assertEqual(source.sequence, 0)
        self.assertEqual(source.timestamp, 0)


class _Transport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        self.sent.append((data, addr))

    def close(self) -> None:
        self.closed = True


class SipRtpDtmfTest(unittest.IsolatedAsyncioTestCase):
    def _relay(
        self,
        *,
        audio_rate: int = 8000,
        events: frozenset[int] = frozenset(range(16)),
    ):
        audio = audio_format.AudioFormat(audio_rate, "s16le", 1, 20)
        left = sip_rtp_bridge.RtpPeer(
            "192.0.2.10",
            40000,
            96,
            audio,
            dtmf_payload_type=101,
            dtmf_clock_rate=8000,
        )
        right = sip_rtp_bridge.RtpPeer(
            "192.0.2.20",
            41000,
            97,
            audio,
            dtmf_payload_type=110,
            dtmf_clock_rate=8000,
            dtmf_events=events,
            sequence=100,
            timestamp=1000,
            ssrc=0x1111,
            dtmf_sequence=200,
            dtmf_timestamp=2000,
            dtmf_ssrc=0x2222,
        )
        relay = sip_rtp_bridge.SipRtpRelay(
            left=left,
            right=right,
            left_port=42000,
            right_port=42002,
        )
        relay.left_transport = _Transport()
        relay.right_transport = _Transport()
        return relay

    async def test_legacy_info_ingress_originates_negotiated_rfc4733(self) -> None:
        relay = self._relay()
        self.assertTrue(relay.relay_dtmf("left", "5", duration_ms=40))
        await asyncio.sleep(0.16)

        transport = relay.right_transport
        assert isinstance(transport, _Transport)
        packets = [rtp.parse_packet(raw) for raw, _addr in transport.sent]
        self.assertEqual(len(packets), 4)
        self.assertEqual({packet.payload_type for packet in packets}, {110})
        self.assertEqual({packet.ssrc for packet in packets}, {0x1111})
        self.assertEqual([packet.sequence for packet in packets], list(range(100, 104)))
        self.assertEqual({packet.timestamp for packet in packets}, {1000})
        self.assertTrue(packets[0].marker)
        self.assertEqual(sum(bool(packet.payload[1] & 0x80) for packet in packets), 3)
        self.assertEqual(relay.right_dtmf_tx_events, 1)
        await relay.stop()

    async def test_destination_fmtp_restriction_is_enforced(self) -> None:
        relay = self._relay(events=frozenset({1}))
        self.assertFalse(relay.relay_dtmf("left", "2"))
        self.assertEqual(relay._dtmf_tasks, set())
        await relay.stop()

    async def test_different_clock_uses_a_separate_rtp_source(self) -> None:
        relay = self._relay(audio_rate=16000)
        incoming = rtp.build_packet(
            rtp.RtpPacket(
                payload_type=101,
                sequence=1,
                timestamp=800,
                ssrc=0x3333,
                payload=bytes((3, 0, 0, 80)),
                marker=True,
            )
        )
        relay.handle_packet("left", incoming, ("192.0.2.10", 40000))
        await asyncio.sleep(0)

        transport = relay.right_transport
        assert isinstance(transport, _Transport)
        generated = rtp.parse_packet(transport.sent[0][0])
        self.assertEqual(generated.payload_type, 110)
        self.assertEqual(generated.ssrc, 0x2222)
        self.assertEqual(generated.sequence, 200)
        self.assertIsNone(relay.left.rx_ssrc)
        await relay.stop()


if __name__ == "__main__":
    unittest.main()
