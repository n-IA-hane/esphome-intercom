"""Independent UDP packets prove that one digit keeps its negotiated identity."""

import asyncio
import struct
from dataclasses import replace

import pytest

from .voip_phase1_support import sdp, sip_rtp_bridge


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize("initial_clock,next_clock", [(8000, 48000), (48000, 8000)])
async def test_relay_freezes_mapping_and_source_until_terminal_repeats(side, initial_clock, next_clock):
    queue = asyncio.Queue()

    class Inbox(asyncio.DatagramProtocol):
        def datagram_received(self, raw, _address):
            version, payload, sequence, timestamp, ssrc = struct.unpack("!BBHII", raw[:12])
            digit, flags, duration = struct.unpack("!BBH", raw[12:])
            assert version == 0x80
            queue.put_nowait((payload & 127, sequence, timestamp, ssrc, digit, flags & 128, duration))

    loop = asyncio.get_running_loop()
    inbox, _ = await loop.create_datagram_endpoint(Inbox, local_addr=("127.0.0.1", 0))
    target = inbox.get_extra_info("sockname")
    output, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0))
    pcm = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
    peers = {name: sip_rtp_bridge.RtpPeer(*target, 96, pcm.audio_format,
        rtp_format=pcm, send_rtp_format=pcm, clock_rate=16000, ssrc=111, dtmf_ssrc=222,
        dtmf_payload_type=101, dtmf_clock_rate=initial_clock,
        send_dtmf_payload_type=101, send_dtmf_clock_rate=initial_clock,
        send_dtmf_events=frozenset({5})) for name in ("left", "right")}
    relay = sip_rtp_bridge.SipRtpRelay(left=peers["left"], right=peers["right"], left_port=0, right_port=0)
    if side == "left":
        relay.left_transport = output
    else:
        relay.right_transport = output
    task = None
    try:
        peer = peers[side]
        task = asyncio.create_task(relay._send_dtmf_event(side, peer, "5", duration_ms=40))
        packets = [await asyncio.wait_for(queue.get(), 1)]
        peer.send_dtmf_payload_type = 110
        peer.send_dtmf_clock_rate = next_clock
        peer.send_dtmf_events = frozenset({6})
        peer.dtmf_ssrc = 444
        await asyncio.wait_for(task, 1)
        async with asyncio.timeout(1):
            while sum(bool(packet[5]) for packet in packets) < 3:
                packets.append(await queue.get())
        assert {packet[0] for packet in packets} == {101}
        assert {packet[2] for packet in packets} == {packets[0][2]}
        assert {packet[3] for packet in packets} == {222}
        assert {packet[4] for packet in packets} == {5}
        assert [packet[6] for packet in packets if packet[5]] == [initial_clock * 40 // 1000] * 3
        assert all((after[1]-before[1]) % 65536 == 1 for before, after in zip(packets, packets[1:]))
        task = asyncio.create_task(relay._send_dtmf_event(side, peer, "6", duration_ms=40))
        next_packets = [await asyncio.wait_for(queue.get(), 1)]
        await asyncio.wait_for(task, 1)
        async with asyncio.timeout(1):
            while sum(bool(packet[5]) for packet in next_packets) < 3:
                next_packets.append(await queue.get())
        assert {packet[0] for packet in next_packets} == {110}
        assert {packet[3] for packet in next_packets} == {444}
        assert [packet[6] for packet in next_packets if packet[5]] == [next_clock * 40 // 1000] * 3
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        output.close()
        inbox.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("retirement", ["stop", "replacement"])
async def test_relay_retirement_interrupts_an_active_digit_without_late_packets(retirement):
    queue = asyncio.Queue()

    class Inbox(asyncio.DatagramProtocol):
        def datagram_received(self, raw, _address):
            queue.put_nowait(raw)

    loop = asyncio.get_running_loop()
    inbox, _ = await loop.create_datagram_endpoint(Inbox, local_addr=("127.0.0.1", 0))
    output, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0))
    pcm = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
    peer = sip_rtp_bridge.RtpPeer(*inbox.get_extra_info("sockname"), 96, pcm.audio_format,
        rtp_format=pcm, send_rtp_format=pcm, dtmf_payload_type=101, dtmf_clock_rate=8000)
    relay = sip_rtp_bridge.SipRtpRelay(left=peer, right=replace(peer), left_port=0, right_port=0)
    relay.left_transport = output
    task = asyncio.create_task(relay._send_dtmf_event("left", peer, "5", duration_ms=160))
    try:
        assert (await asyncio.wait_for(queue.get(), 1))[1] & 127 == 101
        if retirement == "stop":
            await relay.stop()
        else:
            relay.left = replace(peer)
        await asyncio.wait_for(task, 1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(queue.get(), .04)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        output.close()
        inbox.close()
