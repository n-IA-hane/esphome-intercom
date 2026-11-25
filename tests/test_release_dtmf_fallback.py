"""Receive complete RFC4733 presses before falling back to one SIP INFO."""

import asyncio

import pytest

from . import test_sip_rtp_dtmf as fixture
from custom_components.voip_stack.core.sdp import RtpDtmfFormat

dtmf, rtp, sip_rtp_bridge = fixture.dtmf, fixture.rtp, fixture.sip_rtp_bridge


@pytest.mark.parametrize("rate,duration_ms", [(8000, 80), (8000, 1000), (48000, 1800)])
async def test_info_fallback_preserves_duration_and_long_event_segments(rate, duration_ms):
    relay = fixture.SipRtpDtmfTest()._relay()
    relay.right.dtmf_payload_type = relay.right.send_dtmf_payload_type = None
    relay._dtmf_decoders["left"] = dtmf.RtpDtmfDecoder((RtpDtmfFormat(101, rate),))
    calls = []
    async def fallback(side, digit, duration):
        calls.append((side, digit, duration))
        return True
    relay.on_dtmf_fallback = fallback
    sequence = 0
    def receive(offset, ticks, end, marker=False):
        nonlocal sequence
        sequence += 1
        relay.handle_packet("left", rtp.build_packet(rtp.RtpPacket(101, sequence, 8000 + offset, 0x3333,
            dtmf.build_telephone_event_payload("5", duration=ticks, end=end), marker)), ("192.0.2.10", 40000))
    try:
        receive(0, rate // 100, False, True)
        await asyncio.sleep(0)
        assert not calls  # Progress is not a completed 160ms press.
        total = rate * duration_ms // 1000
        segments = list(dtmf.telephone_event_segments(total, end=True))
        for offset, ticks, end in segments:
            receive(offset, ticks, end)
        for _ in range(2):
            receive(*segments[-1])
        await asyncio.gather(*tuple(relay._dtmf_tasks))
        assert calls == [("left", "5", duration_ms)]
    finally:
        await relay.stop()


@pytest.mark.parametrize("stop", [False, True])
async def test_missing_end_uses_last_observed_duration_and_stop_cancels_delivery(monkeypatch, stop):
    monkeypatch.setattr(sip_rtp_bridge, "_DTMF_REPORT_IDLE_SECONDS", .02, raising=False)
    relay = fixture.SipRtpDtmfTest()._relay()
    relay.right.dtmf_payload_type = relay.right.send_dtmf_payload_type = None
    calls = []
    async def fallback(side, digit, duration):
        calls.append((side, digit, duration))
        return True
    relay.on_dtmf_fallback = fallback
    try:
        for sequence, duration in ((1, 80), (2, 4000)):
            relay.handle_packet("left", rtp.build_packet(rtp.RtpPacket(101, sequence, 8000, 0x3333,
                dtmf.build_telephone_event_payload("5", duration=duration, end=False), sequence == 1)),
                ("192.0.2.10", 40000))
        if stop:
            await relay.stop()
        else:
            await asyncio.gather(*tuple(relay._dtmf_tasks))
        assert calls == ([] if stop else [("left", "5", 500)])
    finally:
        await relay.stop()


async def test_shared_pacer_keeps_browser_limit_and_uncapped_relay_contract(monkeypatch):
    async def no_sleep(_delay):
        pass
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    browser = []
    async def emit(duration, marker, end, offset):
        browser.append((duration + offset, marker, end))
        return True
    assert await dtmf.send_rtp_dtmf_event("5", clock_rate=8000, duration_ms=6000, emit=emit)
    assert [duration for duration, _marker, end in browser if end] == [40000] * 3
    relay = fixture.SipRtpDtmfTest()._relay()
    relay.right.send_dtmf_clock_rate = 48000
    try:
        await relay._send_dtmf_event("right", relay.right, "5", duration_ms=6000)
        packets = [rtp.parse_packet(raw) for raw, _address in relay.right_transport.sent]
        final = [packet for packet in packets if packet.payload[1] & 128]
        assert len(final) == 3
        assert all(packet.timestamp - packets[0].timestamp + int.from_bytes(packet.payload[2:4], "big")
                   == 288000 for packet in final)
        assert packets[0].marker and not any(packet.marker for packet in packets[1:])
    finally:
        await relay.stop()
