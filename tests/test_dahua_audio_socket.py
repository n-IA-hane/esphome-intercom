"""Issue 115: all negotiated RTP audio payloads must reach browser playback."""

import asyncio
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


async def _exercise_dahua_call(monkeypatch, user_agent):
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
                    f"m=audio {remote_port} RTP/AVP 0 97\r\n"
                    "a=rtpmap:0 PCMU/8000\r\na=rtpmap:97 PCM/16000\r\n"
                    "a=ptime:20\r\na=sendrecv\r\n"
                    "m=video 45002 RTP/AVP 105\r\na=rtpmap:105 H264/90000\r\na=sendrecv\r\n"
                ).encode()
            self.transport.sendto(sip.build_response(200, "OK", headers, body), address)

    transport, _ = await loop.create_datagram_endpoint(DoorStation, local_addr=("127.0.0.1", 0))
    pcmu = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20)
    formats = _load_intercom_module("audio_format").HA_TRUNK_AUDIO_FORMATS
    client = sip_client.SipCallClient(
        local_ip="127.0.0.1", local_name="Browser", local_sip_port=0,
        local_rtp_port=local_port, supported_send_formats=list(formats),
        supported_recv_formats=list(formats), include_common_codecs=True,
        peer_user_agent=user_agent, local_video_rtp_port=41002,
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

        await browser.messages.put(SimpleNamespace(
            type=WSMsgType.BINARY,
            data=audio_ws.encode_audio_frame(bytes(session.send_format.audio_format.nominal_frame_bytes)),
        ))
        outgoing, _ = await asyncio.wait_for(loop.sock_recvfrom(remote, 2048), 1)
        packet = rtp.parse_packet(outgoing)
        assert packet.payload_type == 0
        assert packet.payload == b"\xff" * 160

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


@pytest.mark.asyncio
@pytest.mark.parametrize("user_agent", ["Dahua UAC/V4.511.0.0", ""])
async def test_dahua_pcm_and_pcmu_answer_delivers_actual_rtp_to_browser(monkeypatch, user_agent):
    """Full automatic/manual contact offers, video and two clean call cycles."""
    for _ in range(2):
        with monkeypatch.context() as patcher:
            await _exercise_dahua_call(patcher, user_agent)
