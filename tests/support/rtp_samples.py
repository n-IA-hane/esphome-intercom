"""Collect independently packetized mono L16 without losing sample evidence."""

from custom_components.voip_stack.core import rtp


async def receive_l16_samples(receive, expected_bytes, payload_type):
    packets, received = [], 0
    while received < expected_bytes:
        packet = rtp.parse_packet(await receive())
        assert packet.payload_type == payload_type
        assert packet.payload and len(packet.payload) % 2 == 0
        if packets:
            previous = packets[-1]
            assert packet.ssrc == previous.ssrc
            assert packet.sequence == (previous.sequence + 1) & 0xFFFF
            assert packet.timestamp == (previous.timestamp + len(previous.payload) // 2) & 0xFFFFFFFF
        packets.append(packet)
        received += len(packet.payload)
    assert received == expected_bytes
    return b"".join(packet.payload for packet in packets)
