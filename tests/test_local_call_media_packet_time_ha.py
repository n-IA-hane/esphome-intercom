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
