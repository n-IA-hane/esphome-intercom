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
    audio_format, rtp, sdp, sip, sip_client,
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


def test_media_view_uses_the_canonical_codec_and_format_types():
    view = _load_audio_ws_runtime_module()
    assert view.RtpPayloadEncoder is sip_client.RtpPayloadEncoder
    selected = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
    assert view.RtpPayloadEncoder(selected).encode(b"\x34\x12" * 320) == b"\x12\x34" * 320


def test_dahua_answer_order_yields_to_explicit_policy_and_established_binding():
    from dataclasses import replace
    from importlib import import_module

    codec_policy = import_module(f"{sdp.__package__}.codec_policy")
    preferred = [audio_format.AudioFormat(rate, "s16le", 1, 20) for rate in (16000, 8000)]
    prefix = ("v=0\r\no=- 1 1 IN IP4 192.0.2.10\r\ns=Door\r\n"
              "c=IN IP4 192.0.2.10\r\nt=0 0\r\n")
    attributes = ("a=rtpmap:97 PCM/16000\r\na=rtpmap:0 PCMU/8000\r\n"
                  "a=ptime:20\r\na=maxptime:20\r\na=sendrecv\r\n")
    offer = prefix + "m=audio 40000 RTP/AVP 97 0\r\n" + attributes
    answer = prefix + "m=audio 41000 RTP/AVP 0 97\r\n" + attributes

    def apply(**kwargs):
        return sdp.negotiate_answer_directional(answer, preferred, preferred,
            local_offer_sdp=offer, allow_dahua_pcm=True, **kwargs)

    selected = apply()
    assert selected is not None
    assert selected.send.payload_type == 0
    assert selected.recv.payload_type == 97
    explicit = apply(codec_policy=codec_policy.CodecPolicy(audio=("PCM", "PCMU")))
    assert explicit is not None and explicit.send.payload_type == 97
    established = apply(established=replace(selected, send=selected.recv))
    assert established is not None and established.send.payload_type == 97


@pytest.mark.asyncio
@pytest.mark.parametrize("frame_ms", [2.5, 15, 32, 40])
async def test_browser_runtime_preserves_negotiated_fractional_playout_clock(monkeypatch, frame_ms):
    """Run the real media owner with a deterministic timer and no UDP I/O."""
    view = _load_audio_ws_runtime_module()
    browser = _Browser()
    clock = [10.0]
    delays = []
    packets = []
    sessions = {}
    transport = SimpleNamespace(sendto=lambda packet, _addr: packets.append(packet), close=lambda: None)

    class Loop:
        def time(self):
            return clock[0]

        async def create_datagram_endpoint(self, factory, **_kwargs):
            return transport, factory()

    class AsyncioProxy:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        def get_running_loop(self):
            return Loop()

        async def sleep(self, delay):
            delays.append(delay)
            clock[0] += delay
            if len(delays) == 4:
                browser.messages.put_nowait(None)
            await asyncio.sleep(0)

    hass = SimpleNamespace(store={"call_id": "generic-quantum"})
    monkeypatch.setattr(view, "asyncio", AsyncioProxy())
    monkeypatch.setattr(view, "media_capture_enabled", lambda _: False)
    monkeypatch.setattr(view, "debug_mode", lambda _: False)
    monkeypatch.setattr(view, "_ha_softphone_store", lambda *_args: hass.store)
    monkeypatch.setattr(view, "_publish_ha_softphone_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(view, "publish_media_route", lambda *_args: None)
    monkeypatch.setattr(view, "_listen_for_call_end", lambda *_args: (asyncio.Event(), lambda: None))
    monkeypatch.setattr(view, "require_runtime_data", lambda _: SimpleNamespace(
        sip=None, media=SimpleNamespace(sessions_for=lambda _: sessions),
    ))
    fmt = sdp.RtpPcmFormat(96, "L16", 8000, 1, frame_ms)
    session = view._SoftphoneMediaSession(
        call_id="generic-quantum", local_rtp_port=0,
        remote_rtp_host="192.0.2.1", remote_rtp_port=40000,
        send_format=fmt, recv_format=fmt,
    )

    await asyncio.wait_for(view._run_audio_session(
        hass, browser, session, endpoint_id="default",
    ), 1)

    assert delays[:4] == pytest.approx([frame_ms / 1000] * 4)
    assert browser.json[0]["selected_tx_format"] == "48000:s16le:1:10"
    assert len(packets) >= 4
    parsed = [rtp.parse_packet(packet) for packet in packets[:4]]
    assert all(len(packet.payload) == fmt.audio_format.nominal_frame_bytes for packet in parsed)
    assert [(right.timestamp - left.timestamp) & 0xFFFFFFFF for left, right in zip(parsed, parsed[1:])] == [fmt.rtp_timestamp_step] * 3
    assert not sessions
    assert session.handoff_transport is None


async def _exercise_dahua_call(monkeypatch, user_agent, *, symmetric=False, pcm_tx_pt=97, delayed_playback=False,
                               dtmf_reconfigure=False, dtmf_interrupt=""):
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
    pcm_rx_pt = 97

    class DoorStation(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, raw, address):
            nonlocal pcm_rx_pt
            request = sip.parse_message(raw)
            requests.append(request)
            if request.method not in {"INVITE", "BYE"}:
                return
            headers = [(key, request.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
            to = request.header("To")
            headers.append(("To", to if ";tag=" in to else to + ";tag=door"))
            body = b""
            if request.method == "INVITE":
                if not user_agent:
                    # Keep the manual contact's conflicting PCM answer tied
                    # to its offered L16/8000 mapping, independently of the
                    # automatic catalog's dynamic payload numbering.
                    pcm_rx_pt = next(int(line.split(b":", 1)[1].split()[0])
                        for line in request.body.splitlines()
                        if line.startswith(b"a=rtpmap:") and line.split()[1] == b"L16/8000/1")
                port = self.transport.get_extra_info("sockname")[1]
                headers += [("Contact", f"<sip:door@127.0.0.1:{port}>"), ("Content-Type", "application/sdp")]
                body = (
                    "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Door\r\n"
                    "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                    f"m=audio {remote_port} RTP/AVP 0 {pcm_tx_pt if user_agent else pcm_rx_pt} {100 if user_agent else 99}\r\n"
                    + (f"a=rtpmap:{pcm_tx_pt} PCM/16000\r\na=rtpmap:0 PCMU/8000\r\n" if user_agent else
                       f"a=rtpmap:0 PCMU/8000\r\na=rtpmap:{pcm_rx_pt} PCM/16000\r\n")
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
        sip=None,
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
            assert f"a=rtpmap:{pcm_rx_pt} L16/8000/1".encode() in requests[0].body
        assert b"a=rtpmap:0 PCMU/8000" in requests[0].body
        assert b"b=RS:0\r\nb=RR:0" in requests[0].body
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
        playback = None
        if delayed_playback:
            playback = _load_intercom_module("browser_playback").BrowserPlayback()
            playback.attach(browser)
        runtime = asyncio.create_task(view._run_audio_session(
            hass, browser, session, endpoint_id="default", playback=playback,
        ))
        await asyncio.wait_for(browser.ready.wait(), 1)
        capture = audio_format.parse_audio_format_token(browser.json[0]["tx_format"])
        receive = audio_format.parse_audio_format_token(browser.json[0]["rx_format"])
        assert (capture.sample_rate, capture.frame_ms) == (48000, 10)

        async def send(pt, sequence, timestamp, payload):
            packet = rtp.build_packet(rtp.RtpPacket(
                payload_type=pt, sequence=sequence, timestamp=timestamp,
                ssrc=123, payload=payload,
            ))
            await loop.sock_sendto(remote, packet, ("127.0.0.1", local_port))

        if delayed_playback:
            # The SIP answer can precede the browser's final ready message.
            # Preserve each initial PCMU frame while that handshake completes.
            for index, encoded in enumerate((0xCE, 0xBE, 0xAE, 0x9E)):
                await send(0, 65520 + index, index * 160, bytes([encoded]) * 160)
                await asyncio.sleep(0.02)
            assert browser.audio.empty()
            playback.ready(browser)
            for encoded in (0xCE, 0xBE, 0xAE, 0x9E):
                frame = audio_ws.decode_audio_frame(await asyncio.wait_for(browser.audio.get(), 1))
                value = encoded ^ 0xFF
                expected = (((value & 0x0F) * 8 + 132) << ((value >> 4) & 7)) - 132
                assert len(frame) == receive.nominal_frame_bytes
                assert all(abs(value - expected) <= 2 for value in struct.unpack(f"<{len(frame)//2}h", frame)[-64:])

        await send(pcm_rx_pt, 1, 0, struct.pack("<320h", *([1000] * 320)))
        expected_bytes = receive.nominal_frame_bytes
        if user_agent:
            first = audio_ws.decode_audio_frame(await asyncio.wait_for(browser.audio.get(), 1))
            assert len(first) == expected_bytes
            samples = struct.unpack(f"<{len(first) // 2}h", first)
            assert all(abs(sample - 1000) <= 2 for sample in samples[-64:])
        else:
            # The manual contact offers L16/8000. An answer's conflicting
            # PCM mapping must not silently reinterpret that payload as Dahua PCM.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(browser.audio.get(), 0.05)

        # A payload present in the same answer remains valid even when the
        # peer changes encoder without a new offer. Mu-law 0xff decodes to zero.
        await send(0, 2, 160, b"\xff" * 160)
        second = audio_ws.decode_audio_frame(await asyncio.wait_for(browser.audio.get(), 1))
        assert len(second) == expected_bytes
        assert all(abs(sample) <= 2 for sample in struct.unpack(f"<{len(second) // 2}h", second)[-64:])

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
        for frame, expected in enumerate(expected_frames):
            count = capture.sample_rate // 50  # The same independent 20 ms signal.
            samples = ([expected[-1]] * count if symmetric else
                [int(5500 * math.sin(2 * math.pi * (430 + frame * 37) * index / capture.sample_rate))
                 for index in range(count)])
            for offset in range(0, count, capture.nominal_frame_samples):
                chunk = samples[offset:offset + capture.nominal_frame_samples]
                await browser.messages.put(SimpleNamespace(type=WSMsgType.BINARY,
                    data=audio_ws.encode_audio_frame(struct.pack(f"<{len(chunk)}h", *chunk))))

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
            for frame_index, expected in enumerate(expected_frames):
                async with asyncio.timeout(1):
                    while True:
                        outgoing, address = await loop.sock_recvfrom(remote, 2048)
                        packet = rtp.parse_packet(outgoing)
                        # Capture starts after negotiation. Initial RTP silence
                        # can precede its first sample, but never replace one.
                        initial_silence = (
                            packet.payload_type == 0 and packet.payload == b"\xff" * 160
                        ) or (
                            packet.payload_type == pcm_tx_pt and packet.payload == bytes(640)
                        )
                        if outgoing_packets or not initial_silence:
                            break
                arrival_times.append(loop.time())
                outgoing_packets.append(packet)
                assert address == ("127.0.0.1", local_port)
                if symmetric:
                    assert packet.payload_type == pcm_tx_pt  # Answer mapping, not RX97.
                    assert len(packet.payload) == 640
                    decoded = struct.unpack("<320h", packet.payload)
                    assert all(abs(value - expected[-1]) <= 3 for value in decoded[64:])
                    assert browser.json[0]["tx_format"] == "48000:s16le:1:10"
                else:
                    assert packet.payload_type == 0
                    assert len(packet.payload) == 160
                    decoded = []
                    for value in packet.payload:
                        u = value ^ 0xff
                        magnitude = (((u & 15) << 3) + 132) << ((u >> 4) & 7)
                        decoded.append(132 - magnitude if u & 128 else magnitude - 132)
                    # The 48k microphone crosses the real FIR before mu-law.
                    # Fit only its phase, then retain the independent frequency,
                    # level and mu-law quantization budget after filter startup.
                    frequency = 430 + frame_index * 37
                    stable = decoded[64:]
                    sine = [math.sin(2 * math.pi * frequency * index / 8000) for index in range(len(stable))]
                    cosine = [math.cos(2 * math.pi * frequency * index / 8000) for index in range(len(stable))]
                    aa, bb = sum(value * value for value in sine), sum(value * value for value in cosine)
                    ab = sum(left * right for left, right in zip(sine, cosine))
                    ay = sum(left * right for left, right in zip(sine, stable))
                    by = sum(left * right for left, right in zip(cosine, stable))
                    a, b = (ay * bb - by * ab) / (aa * bb - ab * ab), (by * aa - ay * ab) / (aa * bb - ab * ab)
                    assert abs(math.hypot(a, b) - 5500) <= 128
                    assert math.sqrt(sum((value - a * left - b * right) ** 2
                        for value, left, right in zip(stable, sine, cosine)) / len(stable)) <= 128
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
            initial_dtmf_payload = session.send_dtmf_payload_type
            async with asyncio.timeout(2):
                while len([item for item in events if item.payload[1] & 128]) < 3:
                    raw, _address = await loop.sock_recvfrom(remote, 2048)
                    packet = rtp.parse_packet(raw)
                    if packet.payload_type in {initial_dtmf_payload, 110, 126} and len(packet.payload) == 4:
                        events.append(packet)
                        if len(events) == 1:
                            if dtmf_interrupt:
                                if dtmf_interrupt == "close":
                                    await browser.messages.put(None)
                                    await asyncio.wait_for(runtime, 1)
                                    # Drain packets already queued before close.
                                    while True:
                                        try:
                                            remote.recv(2048)
                                        except BlockingIOError:
                                            break
                                    with pytest.raises(TimeoutError):
                                        await asyncio.wait_for(loop.sock_recvfrom(remote, 2048), .05)
                                return tuple(decoded_frames)
                            if dtmf_reconfigure == "dialog":
                                # Use the production offer negotiator and the
                                # browser session's actual committed-update API.
                                body = (f"v=0\r\nc=IN IP4 127.0.0.1\r\nm=audio {remote_port} RTP/AVP 110 126\r\n"
                                        "a=rtpmap:110 L16/48000/1\r\na=rtpmap:126 telephone-event/48000\r\n"
                                        "a=fmtp:126 6\r\na=ptime:10\r\na=sendrecv\r\n")
                                reoffer = sip.parse_message(sip.build_request("INVITE", "sip:Browser@127.0.0.1",
                                    [("Content-Type", "application/sdp"), ("Contact", "<sip:door@127.0.0.1>")], body.encode()))
                                candidate, _ = client._answer_remote_offer(reoffer)
                                assert candidate is not None
                                browser.ready.clear()
                                _load_intercom_module("media_session_updates").commit_audio_session_update(
                                    session, candidate, dtmf_payload_type=candidate.dtmf_payload_type,
                                    dtmf_events=candidate.dtmf_events)
                                assert session.media_generation == 1
                            elif dtmf_reconfigure:
                                session.send_dtmf_payload_type = 110
                                session.send_dtmf_clock_rate = 48000
                                session.send_dtmf_events = frozenset({6})
                            await send(0, 40, 6400, b"\xff" * 160)
            assert {item.payload_type for item in events} == {initial_dtmf_payload}
            assert len({item.timestamp for item in events}) == 1
            assert len({item.ssrc for item in events}) == 1
            assert events[0].ssrc != outgoing_packets[-1].ssrc
            assert all(struct.unpack("!H", item.payload[2:])[0] == 320 for item in events[-3:])
            if dtmf_reconfigure:
                if dtmf_reconfigure == "dialog":
                    await asyncio.wait_for(browser.ready.wait(), 1)
                    assert browser.json[-1]["type"] == "media_update"
                await browser.messages.put(SimpleNamespace(type=WSMsgType.TEXT,
                    data=json.dumps({"type": "dtmf", "digit": "6", "duration_ms": 40})))
                following = []
                async with asyncio.timeout(2):
                    while len([item for item in following if item.payload[1] & 128]) < 3:
                        raw, _address = await loop.sock_recvfrom(remote, 2048)
                        packet = rtp.parse_packet(raw)
                        if packet.payload_type == (126 if dtmf_reconfigure == "dialog" else 110):
                            following.append(packet)
                assert all(struct.unpack("!H", item.payload[2:])[0] == 1920 for item in following[-3:])
                if dtmf_reconfigure == "dialog":
                    return tuple(decoded_frames)
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


@pytest.mark.asyncio
async def test_browser_digit_keeps_old_mapping_when_attributes_change_mid_event(monkeypatch):
    await _exercise_dahua_call(monkeypatch, "Dahua UAC/V4.511.0.0", symmetric=True, dtmf_reconfigure=True)


@pytest.mark.asyncio
async def test_browser_digit_stops_on_socket_close(monkeypatch):
    await _exercise_dahua_call(monkeypatch, "Dahua UAC/V4.511.0.0", symmetric=True, dtmf_interrupt="close")


@pytest.mark.asyncio
async def test_browser_reinvite_applies_after_digit_then_next_digit_uses_new_clock(monkeypatch):
    await _exercise_dahua_call(monkeypatch, "Dahua UAC/V4.511.0.0", symmetric=True, dtmf_reconfigure="dialog")


async def test_immediate_sip_answer_preserves_audio_until_browser_is_ready(monkeypatch):
    await _exercise_dahua_call(monkeypatch, "", delayed_playback=True)
