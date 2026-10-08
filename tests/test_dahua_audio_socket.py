"""Issue 115: all negotiated RTP audio payloads must reach browser playback."""

import asyncio
import math
import json
import socket
import struct
import sys
from types import SimpleNamespace

import pytest

from .voip_phase1_support import (
    _load_audio_ws_runtime_module, _load_intercom_module,
    rtp, sdp, sip, sip_client,
)


class _Browser:
    def __init__(self):
        self.json = []
        self.audio = asyncio.Queue()
        self.messages = asyncio.Queue()
        self.ready = asyncio.Event()

    async def send_json(self, value):
        self.json.append(value)
        self.ready.set()

    async def send_bytes(self, value):
        await self.audio.put(bytes(value))

    def force_close(self):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.messages.get()
        if message is None:
            raise StopAsyncIteration
        return message


async def _exercise_dahua_call(monkeypatch, user_agent, *, symmetric=False, pcm_tx_pt=97):
    """Independent SDP and PCM bytes reproduce the reporter's PT97/0 mismatch."""
    view = _load_audio_ws_runtime_module()
    audio_ws = _load_intercom_module("audio_ws")
    loop = asyncio.get_running_loop()
    remote = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    remote.bind(("127.0.0.1", 0))
    remote.setblocking(False)
    remote_port = remote.getsockname()[1]
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    local_port = probe.getsockname()[1]
    probe.close()
    requests = []

    class DoorStation(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, raw, address):
            request = sip.parse_message(raw)
            requests.append(request)
            if request.method not in {"INVITE", "BYE"}:
                return
            headers = [(key, request.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
            to = request.header("To")
            headers.append(("To", to if ";tag=" in to else to + ";tag=door"))
            body = b""
            if request.method == "INVITE":
                port = self.transport.get_extra_info("sockname")[1]
                headers += [("Contact", f"<sip:door@127.0.0.1:{port}>"), ("Content-Type", "application/sdp")]
                body = (
                    "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Door\r\n"
                    "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                    f"m=audio {remote_port} RTP/AVP 0 {pcm_tx_pt} {100 if user_agent else 99}\r\n"
                    + (f"a=rtpmap:{pcm_tx_pt} PCM/16000\r\na=rtpmap:0 PCMU/8000\r\n" if user_agent else
                       "a=rtpmap:0 PCMU/8000\r\na=rtpmap:97 PCM/16000\r\n")
                    + f"a=rtpmap:{100 if user_agent else 99} telephone-event/8000\r\n"
                    f"a=fmtp:{100 if user_agent else 99} 0-15\r\na=sendrecv\r\n"
                    "m=video 45002 RTP/AVP 105\r\na=framerate:20.000000\r\n"
                    "a=rtpmap:105 H264/90000\r\na=sendrecv\r\n"
                ).encode()
            self.transport.sendto(sip.build_response(200, "OK", headers, body), address)

    transport, _ = await loop.create_datagram_endpoint(DoorStation, local_addr=("127.0.0.1", 0))
    pcmu = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20)
    formats = _load_intercom_module("audio_format").HA_TRUNK_AUDIO_FORMATS
    client = sip_client.SipCallClient(
        local_ip="127.0.0.1", local_name="Browser", local_sip_port=0,
        local_rtp_port=local_port, supported_send_formats=list(formats),
        supported_recv_formats=list(formats), include_common_codecs=True,
        peer_user_agent=user_agent, match_received_codec=symmetric, local_video_rtp_port=41002,
        video_formats=(sdp.RtpVideoFormat(
            payload_type=105, profile_level_id="42001f",
            packetization_mode=0, level_asymmetry_allowed=False,
        ),), video_direction="recvonly",
    )
    browser = _Browser()
    runtime = None
    sessions = {}
    hass = SimpleNamespace(
        data={"voip_stack": {}}, store={"state": "in_call"},
        bus=SimpleNamespace(async_listen=lambda *_args: lambda: None),
    )
    monkeypatch.setattr(view, "require_runtime_data", lambda _: SimpleNamespace(
        media=SimpleNamespace(sessions_for=lambda _: sessions),
    ))
    try:
        assert await client.invite(
            target="door", remote_host="127.0.0.1",
            remote_sip_port=transport.get_extra_info("sockname")[1], timeout=2,
        ) == "in_call"
        assert client.dialog.local_video_direction == "recvonly"
        assert client.dialog.video_format.packetization_mode == 0
        assert b"a=recvonly" in requests[0].body
        assert (b"a=rtpmap:97 PCM/16000" in requests[0].body) is bool(user_agent)
        if not user_agent:
            assert b"a=rtpmap:97 L16/8000" in requests[0].body
        assert b"a=rtpmap:0 PCMU/8000" in requests[0].body
        assert (b"b=RS:0\r\nb=RR:0" in requests[0].body) is symmetric
        hass.store["call_id"] = client.dialog.call_id
        registry = SimpleNamespace(
            resource_for=lambda *_args: None,
            sip_client_for=lambda _: client,
        )
        monkeypatch.setattr(view, "active_media_call", lambda *_args: SimpleNamespace(
            call_id=client.dialog.call_id, registry=registry,
        ))
        # Use the actual dialog-to-browser adapter, so this test also catches
        # negotiated receive payloads lost between signaling and the media view.
        session = view._active_softphone_media_session(hass, "default")
        runtime = asyncio.create_task(view._run_audio_session(hass, browser, session, endpoint_id="default"))
        await asyncio.wait_for(browser.ready.wait(), 1)

        async def send(pt, sequence, timestamp, payload):
            packet = rtp.build_packet(rtp.RtpPacket(
                payload_type=pt, sequence=sequence, timestamp=timestamp,
                ssrc=123, payload=payload,
            ))
            await loop.sock_sendto(remote, packet, ("127.0.0.1", local_port))

        await send(97, 1, 0, struct.pack("<320h", *([1000] * 320)))
        expected_bytes = session.recv_format.audio_format.nominal_frame_bytes
        if user_agent:
            first = audio_ws.decode_audio_frame(await asyncio.wait_for(browser.audio.get(), 1))
            assert len(first) == expected_bytes
            samples = struct.unpack(f"<{len(first) // 2}h", first)
            assert all(abs(sample - 1000) <= 2 for sample in samples[16:])
        else:
            # Manual contact offers PT97 as L16/8000. An answer's conflicting
            # PCM mapping must not silently reinterpret that payload as Dahua PCM.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(browser.audio.get(), 0.05)

        # A payload present in the same answer remains valid even when the
        # peer changes encoder without a new offer. Mu-law 0xff decodes to zero.
        await send(0, 2, 160, b"\xff" * 160)
        second = audio_ws.decode_audio_frame(await asyncio.wait_for(browser.audio.get(), 1))
        assert len(second) == expected_bytes
        assert all(abs(sample) <= 2 for sample in struct.unpack(f"<{len(second) // 2}h", second)[16:])

        await send(111, 3, 320, b"\xff" * 160)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(browser.audio.get(), 0.05)
        assert not runtime.done()

        from aiohttp import WSMsgType

        # Nonzero, frame-distinct microphone samples exercise the real browser
        # PCM -> RTP PCMU encoder. The receiver is the UDP socket advertised in
        # the VTO answer, independently decoding mu-law without our codec helper.
        expected_frames = [
            tuple(int(5500 * math.sin(2 * math.pi * (430 + frame * 37) * i / 8000))
                  for i in range(160))
            for frame in range(8)
        ]

        if symmetric:
            expected_frames = [tuple([1200 + frame * 100] * 160) for frame in range(8)]

        if symmetric:
            # Establish the received codec before starting microphone capture;
            # frames transmitted before that observation legitimately use PCMU.
            await send(97, 19, 2880, struct.pack("<320h", *([1000] * 320)))
            await asyncio.wait_for(browser.audio.get(), 1)

        # Queue less than the production 200 ms limit before measuring. This
        # tests the encoder and its pacing, not synthetic microphone scheduler
        # starvation when the test process first loads a receive resampler.
        for samples in expected_frames:
            await browser.messages.put(SimpleNamespace(
                type=WSMsgType.BINARY,
                data=audio_ws.encode_audio_frame(struct.pack("<160h", *samples)),
            ))

        async def microphone_and_door():
            for index in range(len(expected_frames)):
                # The automatic contact receives PCM while it sends PCMU;
                # the manual contact receives PCMU. Neither may alter TX.
                await send(
                    97 if user_agent else 0, 20 + index,
                    3200 + index * (320 if user_agent else 160),
                    struct.pack("<320h", *([1000] * 320)) if user_agent else b"\xce" * 160,
                )
                await asyncio.sleep(.02)

        producer = asyncio.create_task(microphone_and_door())
        outgoing_packets = []
        arrival_times = []
        decoded_frames = []
        try:
            for expected in expected_frames:
                outgoing, address = await asyncio.wait_for(loop.sock_recvfrom(remote, 2048), 1)
                arrival_times.append(loop.time())
                packet = rtp.parse_packet(outgoing)
                outgoing_packets.append(packet)
                assert address == ("127.0.0.1", local_port)
                if symmetric:
                    assert packet.payload_type == pcm_tx_pt  # Answer mapping, not RX97.
                    assert len(packet.payload) == 640
                    decoded = struct.unpack("<320h", packet.payload)
                    assert all(abs(value - expected[-1]) <= 3 for value in decoded[64:])
                    assert browser.json[0]["tx_format"].startswith("8000:")
                else:
                    assert packet.payload_type == 0
                    assert len(packet.payload) == 160
                    decoded = []
                    for value in packet.payload:
                        u = value ^ 0xff
                        magnitude = (((u & 15) << 3) + 132) << ((u >> 4) & 7)
                        decoded.append(132 - magnitude if u & 128 else magnitude - 132)
                    assert max(abs(actual - wanted) for actual, wanted in zip(decoded, expected)) <= 128
                    assert sum(sample * sample for sample in decoded) / 160 > 10_000_000
                decoded_frames.append(tuple(decoded))
                received = audio_ws.decode_audio_frame(await asyncio.wait_for(browser.audio.get(), 1))
                assert len(received) == expected_bytes
                assert any(received)
            await producer
        finally:
            if not producer.done():
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
        for before, after in zip(outgoing_packets, outgoing_packets[1:]):
            assert (after.sequence - before.sequence) % 65536 == 1
            assert (after.timestamp - before.timestamp) % (1 << 32) == (320 if symmetric else 160)
            assert after.ssrc == before.ssrc
        # Prevent an implementation that bursts all frames immediately.
        assert .10 <= arrival_times[-1] - arrival_times[0] < .8
        assert client.dialog.send_format == pcmu

        if symmetric:
            # The telephone-event clock stays 8 kHz while audio now uses
            # 16 kHz. It needs its own RTP source, and a key press must not
            # be split by a new incoming codec choice.
            await browser.messages.put(SimpleNamespace(
                type=WSMsgType.TEXT,
                data=json.dumps({"type": "dtmf", "digit": "5", "duration_ms": 40}),
            ))
            events = []
            async with asyncio.timeout(2):
                while len([item for item in events if item.payload[1] & 128]) < 3:
                    raw, _address = await loop.sock_recvfrom(remote, 2048)
                    packet = rtp.parse_packet(raw)
                    if packet.payload_type == session.send_dtmf_payload_type:
                        events.append(packet)
                        if len(events) == 1:
                            await send(0, 40, 6400, b"\xff" * 160)
            assert len({item.timestamp for item in events}) == 1
            assert len({item.ssrc for item in events}) == 1
            assert events[0].ssrc != outgoing_packets[-1].ssrc
            assert all(struct.unpack("!H", item.payload[2:])[0] == 320 for item in events[-3:])
            # Once the key press has finished, the next valid PCMU packet may
            # change TX back. RFC 7160 starts a new source for the new clock.
            await send(0, 41, 6560, b"\xff" * 160)
            async with asyncio.timeout(1):
                while True:
                    raw, _address = await loop.sock_recvfrom(remote, 2048)
                    packet = rtp.parse_packet(raw)
                    if packet.payload_type == 0:
                        assert packet.ssrc != outgoing_packets[-1].ssrc
                        break
            for _ in range(2):
                await asyncio.wait_for(browser.audio.get(), 1)

        if user_agent:
            # RFC 3264 overlap accepts in-flight packets under the old mapping,
            # but that overlap must expire rather than keeping stale codecs forever.
            receiver_module = sys.modules[view.RtpAudioReceiver.__module__]
            now = [receiver_module.monotonic()]
            monkeypatch.setattr(receiver_module, "monotonic", lambda: now[0])
            session.recv_formats = (pcmu,)
            browser.ready.clear()
            session.media_generation += 1
            session.update_event.set()
            await asyncio.wait_for(browser.ready.wait(), 1)
            assert browser.json[-1]["type"] == "media_update"
            await send(97, 4, 640, struct.pack("<320h", *([1000] * 320)))
            overlap = audio_ws.decode_audio_frame(await asyncio.wait_for(browser.audio.get(), 1))
            assert len(overlap) == expected_bytes
            now[0] += 61
            await send(97, 5, 960, struct.pack("<320h", *([1000] * 320)))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(browser.audio.get(), 0.05)
            await send(0, 6, 640, b"\xff" * 160)
            current = audio_ws.decode_audio_frame(await asyncio.wait_for(browser.audio.get(), 1))
            assert len(current) == expected_bytes
            assert current == bytes(expected_bytes)
    finally:
        await browser.messages.put(None)
        if runtime is not None:
            await asyncio.wait_for(runtime, 2)
        await client.terminate(timeout=1)
        await client.close()
        transport.close()
        remote.close()
        assert client.dialog is None
        assert client.transport is None
        assert not sessions
        assert any(request.method == "BYE" for request in requests)
    return tuple(decoded_frames)


@pytest.mark.asyncio
@pytest.mark.parametrize("user_agent", ["Dahua UAC/V4.511.0.0", ""])
async def test_dahua_pcm_and_pcmu_answer_delivers_actual_rtp_to_browser(monkeypatch, user_agent):
    """Full automatic/manual contact offers, video and two clean call cycles."""
    for _ in range(2):
        with monkeypatch.context() as patcher:
            await _exercise_dahua_call(patcher, user_agent)


@pytest.mark.asyncio
async def test_registered_and_manual_dahua_transmit_identical_nonzero_pcmu(monkeypatch):
    captured = []
    for user_agent in ("Dahua UAC/V4.511.0.0", ""):
        with monkeypatch.context() as patcher:
            captured.append(await _exercise_dahua_call(patcher, user_agent))
    assert captured[0] == captured[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("pcm_tx_pt", [97, 112])
async def test_symmetric_dahua_transmits_pcm_using_answer_mapping(monkeypatch, pcm_tx_pt):
    for _ in range(2):
        await _exercise_dahua_call(monkeypatch, "Dahua UAC/V4.511.0.0", symmetric=True, pcm_tx_pt=pcm_tx_pt)
