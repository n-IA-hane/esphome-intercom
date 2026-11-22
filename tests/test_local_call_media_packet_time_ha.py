"""Local application media normalizes valid wire packet times before resampling."""

import asyncio
import hashlib
import json
import socket
import struct
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.voip_stack.core import sdp
from custom_components.voip_stack.core.audio_capabilities import AudioCapabilities
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.local_call_media import LocalAudioContract, LocalCallMedia
from custom_components.voip_stack.media_ports import RtpPortReservation
from tests.test_automation_originate_ha import lab as lab

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("packet_times", [(10,) * 8, (20,) * 4, (10, 20, 10, 20, 20)])
async def test_local_media_preserves_variable_rtp_and_rejects_packets_above_its_envelope(
    lab, monkeypatch, tmp_path, packet_times
):
    reservation = RtpPortReservation.allocate(lab.hass)
    reservation.bind_audio_sockets()
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("127.0.0.1", 0))
    peer.setblocking(False)
    body = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Independent packet times\r\n"
        "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
        f"m=audio {peer.getsockname()[1]} RTP/AVP 96\r\na=rtpmap:96 L16/48000/1\r\n"
        "a=ptime:10\r\na=maxptime:10\r\na=sendrecv\r\n")
    selected = sdp.negotiate_directional(body, [AudioFormat(48000, frame_ms=10)],
        [AudioFormat(48000, frame_ms=20)], local_capabilities=AudioCapabilities(receive_payload_bytes=1920))
    assert selected is not None
    assert (selected.send.frame_ms, selected.recv.frame_ms, selected.recv.max_frame_ms) == (10, 20, 20)
    answer = sdp.build_answer_directional("127.0.0.1", "127.0.0.1", reservation.ports[0],
        selected.send, selected.recv, remote_sdp=body)
    assert "a=ptime:20\r\n" in answer and "a=maxptime:20\r\n" in answer
    contract = LocalAudioContract("variable-local-media", "127.0.0.1", peer.getsockname()[1],
        selected.send, selected.recv, recv_formats=selected.recv_formats,
        local_audio_direction="recvonly", remote_sdp=body)
    media = LocalCallMedia(lab.hass, invite=contract, local_rtp_port=reservation.ports[0],
        reservation=reservation, on_complete=AsyncMock())
    convert = Mock(wraps=media.rx_converter.convert)
    monkeypatch.setattr(media.rx_converter, "convert", convert)
    decoded, output = [], []
    unsubscribe = media.decoder.add_pcm_observer(lambda pcm, _fmt: decoded.append(pcm))

    async def consume():
        async for frame in media.input_pcm_stream():
            output.append(frame)

    async def wait_for(predicate):
        async with asyncio.timeout(1):
            while not predicate():
                await asyncio.sleep(0)

    async def send(payload, sequence, timestamp):
        # Independent RTP header and L16 bytes, without the production packet writer.
        raw = struct.pack("!BBHII", 0x80, 96, sequence, timestamp, 0x12345678) + payload
        await lab.hass.loop.sock_sendto(peer, raw, ("127.0.0.1", reservation.ports[0]))

    samples = tuple(range(-1920, 1920))
    expected = struct.pack(f"<{len(samples)}h", *samples)
    try:
        await media.start()
        media.start_consumer(consume)
        await asyncio.sleep(0)
        # Both malformed packets must remain rejected before touching the PCM stream.
        await send(bytes(3840), 0, 0)
        await send(b"\x00", 1, 0)
        await wait_for(lambda: media.counters["drop_decode"] == 2)
        cursor = 0
        for index, milliseconds in enumerate(packet_times, 2):
            count = 48 * milliseconds
            payload = struct.pack(f"!{count}h", *samples[cursor:cursor + count])
            await send(payload, index, cursor)
            cursor += count
        await wait_for(lambda: len(decoded) == len(packet_times))
        assert b"".join(decoded) == expected
        converter_frames = [call.args[0] for call in convert.call_args_list]
        assert [len(frame) for frame in converter_frames] == [1920] * 4
        assert b"".join(converter_frames) == expected
        await wait_for(lambda: len(output) == 4)
        assert all(len(frame) == 640 for frame in output) and any(any(frame) for frame in output)
        assert media.counters["drop_decode"] == 2
        (tmp_path / "wire-evidence.json").write_text(json.dumps({
            "packet_times_ms": packet_times, "input_samples": cursor,
            "converter_frame_bytes": [len(frame) for frame in converter_frames],
            "expected_pcm_sha256": hashlib.sha256(expected).hexdigest(),
            "converter_pcm_sha256": hashlib.sha256(b"".join(converter_frames)).hexdigest(),
            "output_frames": len(output), "rejected_packets": media.counters["drop_decode"],
        }, indent=2) + "\n")
    finally:
        unsubscribe()
        await media.stop()
        peer.close()
        assert reservation.released and not lab.runtime.rtp_port_pool.get("used")


@pytest.mark.parametrize("packet_samples", [(160, 160), (320,), (256, 64), (159, 161), (160, 160, 159), (640,)])
async def test_local_media_reframes_pcma_once_without_stranding_pipeline_samples(lab, packet_samples):
    """Exercise the legacy 16 kHz A-law media contract, without claiming SIP admission."""
    from custom_components.voip_stack.assist_runtime import AssistMediaSession

    reservation = RtpPortReservation.allocate(lab.hass)
    fmt = sdp.RtpPcmFormat(120, "PCMA", 16000, 1, 16, max_frame_ms=40)
    contract = LocalAudioContract("fritzbox-short-packets", "192.0.2.2", 40000, fmt, fmt,
                                  local_audio_direction="recvonly")
    media = AssistMediaSession(lab.hass, invite=contract, local_rtp_port=reservation.ports[0],
        reservation=reservation, on_complete=AsyncMock(), pipeline_id="preferred", call_connected_intent="")
    # Independent G.711 A-law codewords: D5 is +8 and 55 is -8.
    total = sum(packet_samples)
    wire = (bytes((0xD5, 0x55)) * ((total + 1) // 2))[:total]
    expected = struct.pack(f"<{total}h", *(8 if index % 2 == 0 else -8 for index in range(total)))
    expected_frames = [expected[offset:offset + 640] for offset in range(0, (total // 320) * 640, 640)]
    decoded, output = [], []
    unsubscribe = media.decoder.add_pcm_observer(lambda pcm, _fmt: decoded.append(pcm))
    ready, received = asyncio.Event(), asyncio.Event()

    async def consume():
        ready.set()
        async for frame in media.input_pcm_stream():
            output.append(frame)
            received.set()

    try:
        media.start_consumer(consume)
        await ready.wait()
        cursor = 0
        for sequence, count in enumerate(packet_samples, 1):
            raw = struct.pack("!BBHII", 0x80, 120, sequence, cursor, 100) + wire[cursor:cursor + count]
            media.handle_rtp(raw, ("192.0.2.2", 40000))
            cursor += count
        assert cursor == total and b"".join(decoded) == expected
        assert media.counters["drop_decode"] == 0
        assert media.counters["rtp_rx"] == len(packet_samples)
        await asyncio.wait_for(received.wait(), 1)
        assert output == expected_frames
        assert media.rx_converter.has_pending_pcm is bool(total % 320)
        assert not media.decoder.has_pending_pcm
    finally:
        unsubscribe()
        await media.stop()
        assert reservation.released and media._consumer_task.done()
    counters = dict(media.counters)
    media.handle_rtp(struct.pack("!BBHII", 0x80, 120, 100, 0, 999) + wire[:160], ("192.0.2.2", 40001))
    assert media.counters == counters and media.remote_ssrc == 100 and output == expected_frames


@pytest.mark.parametrize("packet_samples", [(80, 80), (160,)])
async def test_standard_8khz_pcma_keeps_fixed_resampling_quantum_and_assist_16khz_output(lab, packet_samples):
    from custom_components.voip_stack.assist_runtime import AssistMediaSession

    reservation = RtpPortReservation.allocate(lab.hass)
    fmt = sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20, max_frame_ms=20)
    contract = LocalAudioContract("standard-g711", "192.0.2.2", 40000, fmt, fmt,
                                  local_audio_direction="recvonly")
    media = AssistMediaSession(lab.hass, invite=contract, local_rtp_port=reservation.ports[0],
        reservation=reservation, on_complete=AsyncMock(), pipeline_id="preferred", call_connected_intent="")
    decoded, converted = [], []
    detach = media.decoder.add_pcm_observer(lambda pcm, _fmt: decoded.append(pcm))
    convert = media.rx_converter.convert

    def observe_conversion(pcm):
        converted.append(pcm)
        return convert(pcm)

    media.rx_converter.convert = observe_conversion
    media._accepting_input = True
    try:
        cursor = 0
        for sequence, count in enumerate(packet_samples, 1):
            media.handle_rtp(struct.pack("!BBHII", 0x80, 8, sequence, cursor, 100) + bytes([0xD5]) * count,
                             ("192.0.2.2", 40000))
            cursor += count
        expected_native = struct.pack("<160h", *([8] * 160))
        assert b"".join(decoded) == expected_native
        assert converted == [expected_native]  # One fixed 8 kHz/20 ms resampler input.
        assert media.counters["rtp_rx"] == len(packet_samples) and media.counters["drop_decode"] == 0
        output = media.rx_queue.get_nowait()
        assert len(output) == 640  # Assist consumes 16 kHz/20 ms signed 16-bit mono.
        samples = struct.unpack("<320h", output)
        assert all(7 <= sample <= 8 for sample in samples[-64:])
    finally:
        detach()
        await media.stop()
        assert reservation.released
