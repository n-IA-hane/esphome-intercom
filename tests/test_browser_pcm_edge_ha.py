"""Independent high-frequency PCM proves the browser edge stays at 48 kHz."""

import asyncio
import math
import socket
import struct
from types import SimpleNamespace

from aiohttp import WSMsgType
import pytest

from custom_components.voip_stack import audio_ws, audio_ws_view
from custom_components.voip_stack.core import rtp, sdp
from custom_components.voip_stack.core.media.bridge import plan_browser_audio
from custom_components.voip_stack.rtp_audio_receiver import RtpAudioReceiver
from tests.test_automation_originate_ha import lab as lab

pytestmark = pytest.mark.ha


def tone(rate, milliseconds, frequency=10000):
    return tuple(round(12000 * math.sin(2 * math.pi * frequency * index / rate))
                 for index in range(rate * milliseconds // 1000))


@pytest.mark.parametrize("milliseconds", [10, 20, 32])
def test_native_48k_alternative_avoids_selected_16k_intermediate(milliseconds):
    primary = sdp.RtpPcmFormat(97, "L16", 16000, 1, 20, max_frame_ms=32)
    alternate = sdp.RtpPcmFormat(96, "L16", 48000, 1, 20, max_frame_ms=32)
    edge = plan_browser_audio(alternate, primary)
    receiver = RtpAudioReceiver(primary, (primary, alternate), output_pcm=edge.speaker)
    observed = []
    receiver.add_pcm_observer(lambda pcm, fmt: observed.append((pcm, fmt)))
    samples = tone(48000, milliseconds)
    expected = struct.pack(f"<{len(samples)}h", *samples)
    packet = rtp.RtpPacket(96, 1, 0, 55, struct.pack(f"!{len(samples)}h", *samples))
    try:
        assert not receiver.requires_fixed_input(alternate)
        assert receiver.decode_rtp(packet, normalize_frames=receiver.requires_fixed_input(alternate)) == [expected]
        assert observed[0][0] == expected and observed[0][1].sample_rate == 48000
        assert edge.microphone.sample_rate == edge.speaker.sample_rate == 48000
    finally:
        receiver.close()


@pytest.mark.parametrize("milliseconds", [10, 20, 32])
def test_native_16k_source_uses_one_edge_converter_with_matching_input_quantum(milliseconds):
    wire = sdp.RtpPcmFormat(96, "L16", 16000, 1, milliseconds, max_frame_ms=milliseconds)
    edge = plan_browser_audio(wire, wire)
    receiver = RtpAudioReceiver(wire, output_pcm=edge.speaker)
    samples = tone(16000, milliseconds, 1000)
    observed = []
    receiver.add_pcm_observer(lambda pcm, fmt: observed.append((pcm, fmt)))
    try:
        assert receiver.requires_fixed_input(wire)
        frames = receiver.decode_rtp(rtp.RtpPacket(96, 1, 0, 55,
            struct.pack(f"!{len(samples)}h", *samples)), normalize_frames=receiver.requires_fixed_input(wire))
        assert len(frames) == 1 and len(frames[0]) == 48000 * milliseconds // 1000 * 2
        assert observed[0][1].sample_rate == 16000
        assert len(observed[0][0]) == 16000 * milliseconds // 1000 * 2
    finally:
        receiver.close()


class WebSocketBoundary:
    def __init__(self):
        self.json = []
        self.messages = asyncio.Queue()
        self.audio = asyncio.Queue()
        self.ready = asyncio.Event()

    async def send_json(self, value):
        self.json.append(value)
        self.ready.set()

    async def send_bytes(self, value):
        self.audio.put_nowait(value)

    def force_close(self):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.messages.get()
        if message is None:
            raise StopAsyncIteration
        return message


async def test_ten_ms_browser_capture_preserves_sixty_ms_wire_payload_and_tone(lab, monkeypatch):
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("127.0.0.1", 0))
    peer.setblocking(False)
    bound = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    bound.bind(("127.0.0.1", 0))
    bound.setblocking(False)
    wire = sdp.RtpPcmFormat(96, "L16", 8000, 1, 60, max_frame_ms=60)
    session = audio_ws_view._SoftphoneMediaSession(call_id="capture10-wire60",
        local_rtp_port=bound.getsockname()[1], rtp_socket=bound,
        remote_rtp_host="127.0.0.1", remote_rtp_port=peer.getsockname()[1],
        send_format=wire, recv_format=wire, local_audio_direction="sendrecv")
    ws, store = WebSocketBoundary(), {"call_id": session.call_id}
    monkeypatch.setattr(audio_ws_view, "_ha_softphone_store", lambda *_args: store)
    monkeypatch.setattr(audio_ws_view, "_publish_ha_softphone_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(audio_ws_view, "publish_media_route", lambda *_args: None)
    monkeypatch.setattr(audio_ws_view, "_listen_for_call_end", lambda *_args: (asyncio.Event(), lambda: None))
    monkeypatch.setattr(audio_ws_view, "media_capture_enabled", lambda *_args: False)
    monkeypatch.setattr(audio_ws_view, "debug_mode", lambda *_args: False)
    task = asyncio.create_task(audio_ws_view._run_audio_session(lab.hass, ws, session,
        endpoint_id="bedroom0"))
    try:
        await asyncio.wait_for(ws.ready.wait(), 1)
        assert ws.json[0]["tx_format"] == "48000:s16le:1:10"
        assert session.send_format.frame_ms == 60
        assert audio_ws_view._browser_audio_message_limit(session) <= 4096
        samples = tone(48000, 180, 650)
        for offset in range(0, len(samples), 480):
            capture = struct.pack("<480h", *samples[offset:offset + 480])
            assert len(capture) + 1 <= audio_ws_view._browser_audio_message_limit(session)
            ws.messages.put_nowait(SimpleNamespace(type=WSMsgType.BINARY,
                data=audio_ws.encode_audio_frame(capture)))
        received = []
        async with asyncio.timeout(2):
            while len(received) < 2:
                raw, _address = await lab.hass.loop.sock_recvfrom(peer, 8192)
                packet = rtp.parse_packet(raw)
                assert packet.payload_type == 96 and len(packet.payload) == 960
                if any(packet.payload):
                    received.append(packet)
        assert (received[1].timestamp - received[0].timestamp) & 0xFFFFFFFF == 480
        assert (received[1].sequence - received[0].sequence) & 65535 == 1
        pcm = struct.unpack("!960h", b"".join(value.payload for value in received))
        assert len(pcm) / 8000 == .12
        stable = pcm[160:]
        def power(frequency):
            return abs(sum(value * complex(math.cos(2 * math.pi * frequency * index / 8000),
                math.sin(2 * math.pi * frequency * index / 8000)) for index, value in enumerate(stable)))
        assert power(650) > 10 * max(power(300), power(1000))
        assert math.sqrt(sum(value * value for value in stable) / len(stable)) > 5000
    finally:
        ws.messages.put_nowait(None)
        await asyncio.wait_for(task, 2)
        peer.close()
        bound.close()


async def test_actual_ws_microphone_and_speaker_keep_a_10khz_tone_with_selected_16k_rx(lab, monkeypatch):
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("127.0.0.1", 0))
    peer.setblocking(False)
    bound = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    bound.bind(("127.0.0.1", 0))
    bound.setblocking(False)
    local_port = bound.getsockname()[1]
    wide = sdp.RtpPcmFormat(96, "L16", 48000, 1, 10, max_frame_ms=32)
    selected = sdp.RtpPcmFormat(97, "L16", 16000, 1, 20, max_frame_ms=32)
    session = audio_ws_view._SoftphoneMediaSession(call_id="browser-48k-edge", local_rtp_port=local_port,
        rtp_socket=bound, remote_rtp_host="127.0.0.1", remote_rtp_port=peer.getsockname()[1],
        send_format=wide, recv_format=selected, recv_formats=(selected, wide),
        send_formats=(wide,), local_audio_direction="sendrecv")
    ws = WebSocketBoundary()
    store = {"call_id": session.call_id}
    ended = asyncio.Event()
    monkeypatch.setattr(audio_ws_view, "_ha_softphone_store", lambda *_args: store)
    monkeypatch.setattr(audio_ws_view, "_publish_ha_softphone_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(audio_ws_view, "publish_media_route", lambda *_args: None)
    monkeypatch.setattr(audio_ws_view, "_listen_for_call_end", lambda *_args: (ended, lambda: None))
    monkeypatch.setattr(audio_ws_view, "media_capture_enabled", lambda *_args: False)
    monkeypatch.setattr(audio_ws_view, "debug_mode", lambda *_args: False)
    task = asyncio.create_task(audio_ws_view._run_audio_session(lab.hass, ws, session,
                                                               endpoint_id="bedroom0"))
    samples = tone(48000, 10)
    microphone = struct.pack(f"<{len(samples)}h", *samples)
    wire = struct.pack(f"!{len(samples)}h", *samples)
    try:
        await asyncio.wait_for(ws.ready.wait(), 1)
        assert ws.json[0]["tx_format"] == "48000:s16le:1:10"
        assert ws.json[0]["rx_format"] == "48000:s16le:1:20"
        for _ in range(8):
            ws.messages.put_nowait(SimpleNamespace(type=WSMsgType.BINARY,
                                                   data=audio_ws.encode_audio_frame(microphone)))
        async with asyncio.timeout(2):
            while True:
                raw, _address = await lab.hass.loop.sock_recvfrom(peer, 8192)
                packet = rtp.parse_packet(raw)
                if packet.payload == wire:
                    assert packet.payload_type == 96
                    break
        incoming = struct.pack("!BBHII", 0x80, 96, 1, 0, 55) + wire
        await lab.hass.loop.sock_sendto(peer, incoming, ("127.0.0.1", local_port))
        received = audio_ws.decode_audio_frame(await asyncio.wait_for(ws.audio.get(), 1))
        assert received == microphone
    finally:
        ws.messages.put_nowait(None)
        await asyncio.wait_for(task, 2)
        peer.close()
        bound.close()
