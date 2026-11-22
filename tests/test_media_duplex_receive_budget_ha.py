"""Duplex RX uses its own envelope and preserves the exact offered receiver."""

import asyncio
from dataclasses import replace
import hashlib
import json
import socket
import struct

import pytest

from custom_components.voip_stack.core import sdp
from custom_components.voip_stack.core.audio_capabilities import AudioCapabilities, HA_AUDIO_CAPABILITIES
from custom_components.voip_stack.core.audio_format import AudioFormat, HA_SIP_PCM_RX_FORMATS, HA_SIP_PCM_TX_FORMATS
from custom_components.voip_stack.core.media import shadow

pytestmark = pytest.mark.ha

TX = AudioFormat(16000, "s16le", 1, 20)


@pytest.fixture(autouse=True)
def active_engine():
    previous = shadow.engine_mode()
    shadow.set_engine_mode("active")
    try:
        yield
    finally:
        shadow.set_engine_mode(previous)


def directional_sdp(rate=44100, channels=2, *, local=False, ptime=20, maximum=0,
                    transmit_payload=96, receive_payload=97):
    rows = [
        "v=0", "o=- 1 1 IN IP4 192.0.2.2", "s=Duplex receiver",
        "c=IN IP4 192.0.2.2", "t=0 0",
        f"m=audio 5000 RTP/AVP {transmit_payload} {receive_payload}",
        f"a=rtpmap:{transmit_payload} L16/16000/1",
        f"a=x-voip-stack-flow:{transmit_payload} {'send' if local else 'recv'}",
        f"a=rtpmap:{receive_payload} L16/{rate}/{channels}",
        f"a=x-voip-stack-flow:{receive_payload} {'recv' if local else 'send'}",
        "a=x-voip-stack-independent-ptime:1", f"a=ptime:{ptime}", "a=sendrecv",
    ]
    if maximum:
        rows.append(f"a=maxptime:{maximum}")
    return ("\r\n".join(rows) + "\r\n").encode()


def test_duplex_receive_44100_stereo20_fits_rx_envelope_without_using_tx_budget():
    """F1: 3528 RX bytes fit 12 KiB while the independent TX remains 640 bytes."""
    body = directional_sdp()
    result = sdp.negotiate_directional(body, [TX], [AudioFormat(44100, "s16le", 2, 20)],
        local_capabilities=HA_AUDIO_CAPABILITIES)
    assert result is not None
    assert (result.send.payload_type, result.send.sample_rate, result.send.channels,
            result.send.frame_ms) == (96, 16000, 1, 20)
    assert (result.recv.payload_type, result.recv.sample_rate, result.recv.channels,
            result.recv.frame_ms) == (97, 44100, 2, 20)
    assert result.send.audio_format.nominal_frame_bytes == 640 <= HA_AUDIO_CAPABILITIES.transmit_payload_bytes
    assert HA_AUDIO_CAPABILITIES.transmit_payload_bytes < result.recv.audio_format.nominal_frame_bytes == 3528
    assert result.recv.audio_format.nominal_frame_bytes <= HA_AUDIO_CAPABILITIES.receive_payload_bytes
    answer = sdp.build_answer_directional("192.0.2.1", "192.0.2.1", 4000,
        result.send, result.recv, remote_sdp=body)
    assert "a=ptime:20\r\n" in answer


@pytest.mark.parametrize("rate,channels", [(44100, 2), (48000, 1)])
def test_apply_answer_preserves_the_offered_receive_time_and_bounds(rate, channels):
    """R1 and P3: the remote RX preference cannot rewrite the local RX contract."""
    offer = directional_sdp(rate, channels, local=True, maximum=20)
    answer = directional_sdp(rate, channels, ptime=10, maximum=10,
        transmit_payload=110, receive_payload=111)
    result = sdp.negotiate_answer_directional(answer, [TX], [AudioFormat(rate, "s16le", channels, 20)],
        local_offer_sdp=offer, local_capabilities=HA_AUDIO_CAPABILITIES)
    assert result is not None
    assert (result.send.payload_type, result.send.frame_ms) == (110, 10)
    assert (result.recv.payload_type, result.recv.sample_rate, result.recv.channels,
            result.recv.frame_ms, result.recv.max_frame_ms) == (97, rate, channels, 20, 20)
    assert {(fmt.payload_type, fmt.frame_ms, fmt.max_frame_ms) for fmt in result.recv_formats} == {(97, 20, 20)}


@pytest.mark.parametrize("receive_bytes,expected_ms", [(2000, 10), (1000, None)])
def test_duplex_receive_obeys_the_actual_receiver_budget(receive_bytes, expected_ms):
    capabilities = AudioCapabilities(receive_payload_bytes=receive_bytes)
    result = sdp.negotiate_directional(directional_sdp(), [TX], [AudioFormat(44100, "s16le", 2, 20)],
        local_capabilities=capabilities)
    if expected_ms is None:
        assert result is None
    else:
        assert result is not None
        assert result.recv.frame_ms == expected_ms
        assert result.recv.audio_format.nominal_frame_bytes <= receive_bytes
        assert result.send.frame_ms == 20


def test_apply_answer_refuses_an_unusable_offered_receive_time_instead_of_retiming_it():
    """An inconsistent offer/capability input must not rewrite the advertised RX."""
    offer = directional_sdp(48000, 1, local=True, maximum=20)
    answer = directional_sdp(48000, 1, ptime=10, maximum=10)
    result = sdp.negotiate_answer_directional(answer, [TX], [AudioFormat(48000, "s16le", 1, 20)],
        local_offer_sdp=offer, local_capabilities=AudioCapabilities(receive_payload_bytes=1000))
    assert result is None


def test_directional_offer_tries_the_next_feasible_receive_mapping():
    body = directional_sdp().replace(b"RTP/AVP 96 97", b"RTP/AVP 96 97 98")
    body += b"a=rtpmap:98 L16/8000/1\r\na=x-voip-stack-flow:98 send\r\n"
    result = sdp.negotiate_directional(body, [TX],
        [AudioFormat(44100, "s16le", 2, 20), AudioFormat(8000, "s16le", 1, 20)],
        local_capabilities=AudioCapabilities(receive_payload_bytes=1000))
    assert result is not None
    assert (result.send.payload_type, result.send.frame_ms) == (96, 20)
    assert (result.recv.payload_type, result.recv.sample_rate, result.recv.channels,
            result.recv.frame_ms) == (98, 8000, 1, 20)


@pytest.mark.parametrize("receive_ms", [80, 120])
def test_apply_answer_preserves_opus_receive_times_supported_by_the_public_offer_planner(receive_ms):
    """An existing RX duration must not inherit the encoder's smaller frame set."""
    from custom_components.voip_stack.core.sdp_types import RtpPcmFormat

    receiver = RtpPcmFormat(97, "OPUS", 48000, 2, receive_ms)
    receive_pcm = receiver.audio_format
    assert HA_AUDIO_CAPABILITIES.supports_rtp(receiver, receive=True)
    offer = sdp.build_offer_directional("192.0.2.1", "192.0.2.1", 4000,
        [TX], [receive_pcm], send_rtp_formats=(RtpPcmFormat(96, "L16", 16000, 1, 20),),
        recv_rtp_formats=(receiver,), allow_directional_payloads=True,
        independent_audio_ptime=True, local_capabilities=HA_AUDIO_CAPABILITIES)
    assert f"a=ptime:{receive_ms}\r\n" in offer
    offered_receiver = next(fmt for fmt in sdp.offered_pcm_formats(offer) if fmt.encoding == "OPUS")
    assert (offered_receiver.frame_ms, offered_receiver.max_frame_ms) == (receive_ms, 120)
    answer = directional_sdp(48000, 2, ptime=20,
        transmit_payload=110, receive_payload=111).replace("L16/48000/2".encode(), b"OPUS/48000/2")
    result = sdp.negotiate_answer_directional(answer, [TX], [receive_pcm],
        local_offer_sdp=offer, local_capabilities=HA_AUDIO_CAPABILITIES)
    assert result is not None
    assert (result.send.payload_type, result.send.frame_ms) == (110, 20)
    assert (result.recv.payload_type, result.recv.encoding, result.recv.frame_ms,
            result.recv.max_frame_ms) == (offered_receiver.payload_type, "OPUS", receive_ms, 120)
    assert {(fmt.payload_type, fmt.frame_ms, fmt.max_frame_ms) for fmt in result.recv_formats} == {
        (offered_receiver.payload_type, receive_ms, 120)}


@pytest.mark.parametrize("rate,channels,peer_ptime,transmit_ms", [
    (32000, 1, 20, 10), (44100, 1, 20, 10), (48000, 1, 10, 10), (48000, 2, 10, 5),
])
async def test_duplex_l16_udp_preserves_large_receive_packets_and_bounded_transmit(
    socket_enabled, tmp_path, rate, channels, peer_ptime, transmit_ms
):
    """Independent wire decoding distinguishes the RX envelope from the TX budget."""
    from custom_components.voip_stack.sip_rtp_bridge import RtpPeer, SipRtpRelay

    body = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Independent L16 peer\r\n"
        "c=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 5000 RTP/AVP 96\r\n"
        f"a=rtpmap:96 L16/{rate}/{channels}\r\na=ptime:{peer_ptime}\r\na=sendrecv\r\n").encode()
    selected = sdp.negotiate_directional(body, list(HA_SIP_PCM_TX_FORMATS), list(HA_SIP_PCM_RX_FORMATS),
        local_capabilities=HA_AUDIO_CAPABILITIES)
    assert selected is not None
    assert (selected.send.frame_ms, selected.recv.frame_ms) == (transmit_ms, 20)
    answer = sdp.build_answer_directional("127.0.0.1", "127.0.0.1", 4000,
        selected.send, selected.recv, remote_sdp=body)
    assert "a=ptime:20\r\n" in answer
    loop = asyncio.get_running_loop()
    peers = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(2)]
    for peer in peers:
        peer.bind(("127.0.0.1", 0))
        peer.setblocking(False)
    legs = [RtpPeer("127.0.0.1", peer.getsockname()[1], payload, selected.recv.audio_format,
        rtp_format=replace(selected.recv, payload_type=payload),
        send_rtp_format=replace(selected.send, payload_type=payload),
        send_audio_format=selected.send.audio_format) for peer, payload in zip(peers, (96, 110))]
    relay = SipRtpRelay(left=legs[0], right=legs[1], left_port=0, right_port=0)
    evidence = {"rate": rate, "channels": channels, "rx_ms": 20, "tx_ms": transmit_ms,
                "rx_budget": HA_AUDIO_CAPABILITIES.receive_payload_bytes,
                "tx_budget": HA_AUDIO_CAPABILITIES.transmit_payload_bytes, "directions": []}
    try:
        await relay.start()
        for index, side in enumerate(("left", "right")):
            samples = tuple(value + index * 2000 - 1000 for value in range(rate * 20 * channels // 1000))
            payload = struct.pack(f"!{len(samples)}h", *samples)
            assert HA_AUDIO_CAPABILITIES.transmit_payload_bytes < len(payload) <= HA_AUDIO_CAPABILITIES.receive_payload_bytes
            ingress = getattr(relay, f"{side}_transport").get_extra_info("sockname")
            await loop.sock_sendto(peers[index], struct.pack("!BBHII", 0x80, (96, 110)[index],
                1, 0, 101 + index) + payload, ingress)
            outgoing = [(await asyncio.wait_for(loop.sock_recvfrom(peers[1 - index], 16384), 1))[0]
                        for _ in range(20 // transmit_ms)]
            packet_headers = [struct.unpack("!BBHII", raw[:12]) for raw in outgoing]
            sizes = [len(raw) - 12 for raw in outgoing]
            assert all(header[0] == 0x80 and header[1] & 127 == (110, 96)[index] for header in packet_headers)
            assert sizes == [rate * transmit_ms * channels * 2 // 1000] * len(outgoing)
            assert all(size <= HA_AUDIO_CAPABILITIES.transmit_payload_bytes for size in sizes)
            assert all((later[2] - first[2]) & 65535 == 1
                       and (later[3] - first[3]) & 0xFFFFFFFF == rate * transmit_ms // 1000
                       and later[4] == first[4]
                       for first, later in zip(packet_headers, packet_headers[1:]))
            decoded = tuple(value for raw in outgoing for value in struct.unpack(f"!{(len(raw) - 12) // 2}h", raw[12:]))
            assert decoded == samples
            expected_pcm = struct.pack(f"<{len(samples)}h", *samples)
            decoded_pcm = struct.pack(f"<{len(decoded)}h", *decoded)
            evidence["directions"].append({"side": side, "input_payload_bytes": len(payload),
                "output_payload_bytes": sizes, "decoded_samples": len(decoded),
                "expected_pcm_sha256": hashlib.sha256(expected_pcm).hexdigest(),
                "decoded_pcm_sha256": hashlib.sha256(decoded_pcm).hexdigest()})
    finally:
        await relay.stop()
        for peer in peers:
            peer.close()
        (tmp_path / "wire-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
