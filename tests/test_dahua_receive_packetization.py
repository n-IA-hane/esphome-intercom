"""Issue 161: nominal ptime must not become HA's receive capacity."""

import asyncio
from dataclasses import replace
import logging
import re
import socket
import struct
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from .test_dahua_audio_socket import _Browser
from .test_softphone_originate import (
    _Availability, _RouteAction, _call,
    softphone_originate as softphone_originate,
)
from .voip_phase1_support import (
    _load_audio_ws_runtime_module, _load_intercom_module,
    audio_format, rtp, sdp, sip, sip_client,
)


async def _factory_arguments(module, local_port):
    """Run the actual HA factory up to construction, without a second call owner."""
    # Both production modules share this class/catalog. The isolated factory
    # loader otherwise creates a second dataclass identity in its test package.
    module.HA_TRUNK_AUDIO_FORMATS = audio_format.HA_TRUNK_AUDIO_FORMATS
    endpoint = SimpleNamespace(
        endpoint_id="caller", device_id="browser", sip_uri_user="browser",
        availability=_Availability.AVAILABLE, supports=lambda _: False,
    )
    hass = SimpleNamespace(data={"voip_stack": {}}, states=SimpleNamespace(get=lambda _: None))
    entry = SimpleNamespace(
        metadata={"sip_profile": "dahua", "dahua_audio": "pcm"},
        sip_uri="sip:8001@127.0.0.1", display_name="Door",
    )
    route = SimpleNamespace(
        action=_RouteAction.DIRECT, reason=None, entry=entry,
        sip_uri=entry.sip_uri, target="8001",
    )
    module.ha_router_decision = Mock(return_value=route)
    module._async_resolve_browser_destination = AsyncMock(return_value=(route, "8001", None))
    module._get_transport_config = Mock(return_value={"sip_port": 0, "sip_video": False})
    module._ha_advertise_host = AsyncMock(return_value="127.0.0.1")
    module._allocate_sip_rtp_port = Mock(return_value=local_port)
    module._sip_uri_transport = lambda _: "UDP"
    captured = {}

    class Constructed(Exception):
        pass

    def capture(**kwargs):
        captured.update(kwargs)
        raise Constructed

    sys.modules[f"{module.__package__}.sip_client"].SipCallClient = capture
    with pytest.raises(Constructed):
        await module.async_originate_browser_call(
            _call(hass, destination="8001", send_video=False),
            endpoint_id="caller", browser_endpoint=endpoint,
        )
    assert captured["supported_recv_rtp_formats"] is None
    assert captured["supported_send_formats"] == list(module.HA_TRUNK_AUDIO_FORMATS)
    assert captured["include_dahua_pcm"] is True
    return captured


@pytest.mark.asyncio
@pytest.mark.parametrize("durations", [(20,) * 6, (40,) * 6, (20, 40, 20, 40, 40, 20)])
@pytest.mark.parametrize("remote_max", [None, 20])
async def test_dahua_factory_sip_to_browser_accepts_receive_packetization(
    monkeypatch, caplog, softphone_originate, durations, remote_max,
):
    """Independent SIP and PCM exercise the actual factory, dialog and media owner."""
    view = _load_audio_ws_runtime_module()
    audio_ws = _load_intercom_module("audio_ws")
    # Match production's shared exception class across the isolated view loader.
    monkeypatch.setattr(view, "rtp", rtp)
    caplog.set_level(logging.INFO, logger=view.__name__)
    loop = asyncio.get_running_loop()
    remote = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    remote.bind(("127.0.0.1", 0))
    remote.setblocking(False)
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    local_port = probe.getsockname()[1]
    probe.close()
    requests = []

    class Door(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, raw, address):
            request = sip.parse_message(raw)
            requests.append(request)
            if request.method not in {"INVITE", "BYE"}:
                return
            headers = [(key, request.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
            headers.append(("To", request.header("To") + ";tag=door"))
            body = b""
            if request.method == "INVITE":
                port = self.transport.get_extra_info("sockname")[1]
                headers += [("Contact", f"<sip:door@127.0.0.1:{port}>"), ("Content-Type", "application/sdp")]
                body = (
                    "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Door\r\n"
                    "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                    f"m=audio {remote.getsockname()[1]} RTP/AVP 97\r\n"
                    "a=rtpmap:97 PCM/16000/1\r\na=ptime:20\r\n"
                    + (f"a=maxptime:{remote_max}\r\n" if remote_max else "")
                    + "a=sendrecv\r\n"
                ).encode()
            self.transport.sendto(sip.build_response(200, "OK", headers, body), address)

    transport, _ = await loop.create_datagram_endpoint(Door, local_addr=("127.0.0.1", 0))
    client = sip_client.SipCallClient(**await _factory_arguments(softphone_originate, local_port))
    browser, sessions, runtime = _Browser(), {}, None
    hass = SimpleNamespace(
        data={"voip_stack": {}}, store={"state": "in_call"},
        bus=SimpleNamespace(async_listen=lambda *_: lambda: None),
    )
    monkeypatch.setattr(view, "require_runtime_data", lambda _: SimpleNamespace(
        sip=None, media=SimpleNamespace(sessions_for=lambda _: sessions),
    ))
    try:
        assert await client.invite(
            target="door", remote_host="127.0.0.1",
            remote_sip_port=transport.get_extra_info("sockname")[1], timeout=2,
        ) == "in_call"
        offer = requests[0].body.decode()
        assert "a=ptime:20\r\n" in offer
        maximum = float(re.search(r"a=maxptime:([^\r\n]+)", offer)[1])
        assert maximum >= 40
        hass.store["call_id"] = client.dialog.call_id
        registry = SimpleNamespace(resource_for=lambda *_: None, sip_client_for=lambda _: client)
        monkeypatch.setattr(view, "active_media_call", lambda *_: SimpleNamespace(
            call_id=client.dialog.call_id, registry=registry,
        ))
        session = view._active_softphone_media_session(hass, "default")
        assert (session.send_format.encoding, session.send_format.frame_ms) == ("PCM", 20)
        assert session.recv_format.frame_ms == 20
        assert session.recv_format.max_frame_ms == maximum
        if remote_max:
            assert session.send_format.max_frame_ms == remote_max
        runtime = asyncio.create_task(view._run_audio_session(hass, browser, session, endpoint_id="default"))
        await asyncio.wait_for(browser.ready.wait(), 1)

        # Independent RTP construction also permits an intentionally oversized
        # receive datagram, beyond both negotiated capacity and the 12 KiB cap.
        oversized = struct.pack("!BBHII", 0x80, 97, 65528, 0, 123) + b"\x01\x02" * 6145
        await loop.sock_sendto(remote, oversized, ("127.0.0.1", local_port))
        expected, timestamp = [], 0
        for index, (duration, delay) in enumerate(zip(durations, (.036, .044, .052, .028, .040, .047), strict=True)):
            count = 16 * duration
            payload = struct.pack(f"<{count}h", *(200 + ((timestamp + i) * 7) % 2000 for i in range(count)))
            expected.append(payload)
            packet = struct.pack("!BBHII", 0x80, 97, (65529 + index) & 65535, timestamp, 123) + payload
            await loop.sock_sendto(remote, packet, ("127.0.0.1", local_port))
            timestamp += count
            await asyncio.sleep(delay)
        frames = [audio_ws.decode_audio_frame(await asyncio.wait_for(browser.audio.get(), 1))
                  for _ in range(sum(durations) // 20)]
        assert all(len(frame) == 640 for frame in frames)
        assert b"".join(frames) == b"".join(expected)
        assert browser.audio.empty()
        # Actual sender pacing/geometry remains 20 ms despite 40 ms receive.
        outgoing = [rtp.parse_packet((await asyncio.wait_for(loop.sock_recvfrom(remote, 2048), 1))[0])
                    for _ in range(4)]
        assert all(packet.payload_type == 97 and len(packet.payload) == 640 for packet in outgoing)
        assert [(right.timestamp - left.timestamp) & 0xFFFFFFFF
                for left, right in zip(outgoing, outgoing[1:])] == [320] * 3
    finally:
        await browser.messages.put(None)
        if runtime is not None:
            await asyncio.wait_for(runtime, 2)
        await client.terminate(timeout=1)
        await client.close()
        transport.close()
        remote.close()
    assert hass.store["rtp_rx_packets"] == len(durations)
    assert hass.store["rtp_rx_bytes"] == sum(durations) * 32 + 12 * len(durations)
    assert f"rtp_rx={len(durations)} ws_tx={sum(durations) // 20}" in caplog.text
    assert "drop_size=1 drop_error=0" in caplog.text
    assert client.dialog is None and client.transport is None and not sessions
    assert any(request.method == "BYE" for request in requests)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reopened:
        reopened.bind(("127.0.0.1", local_port))


@pytest.mark.parametrize("encoding,rate,pt", [("L16", 16000, 96), ("PCMU", 8000, 0), ("PCM", 16000, 97)])
@pytest.mark.parametrize("explicit_max", [None, 20])
def test_local_offer_receive_capacity_and_explicit_limit(encoding, rate, pt, explicit_max):
    capabilities = sip_client.HA_AUDIO_CAPABILITIES
    receiver = _load_intercom_module("rtp_audio_receiver").RtpAudioReceiver
    fmt = sdp.RtpPcmFormat(pt, encoding, rate, 1, 20)
    options = ({"send_rtp_formats": (fmt,), "recv_rtp_formats": (replace(fmt, max_frame_ms=explicit_max),)}
               if explicit_max else {})
    offer = sdp.build_offer_directional(
        "127.0.0.1", "127.0.0.1", 40000, [fmt.audio_format], [fmt.audio_format],
        include_common_codecs=encoding == "PCMU", include_dahua_pcm=encoding == "PCM",
        local_capabilities=capabilities, **options,
    )
    answer = (
        "v=0\r\no=- 1 1 IN IP4 127.0.0.2\r\ns=Peer\r\n"
        "c=IN IP4 127.0.0.2\r\nt=0 0\r\n"
        f"m=audio 41000 RTP/AVP {pt}\r\na=rtpmap:{pt} {encoding}/{rate}/1\r\n"
        "a=ptime:20\r\na=maxptime:20\r\na=sendrecv\r\n"
    )
    selected = sdp.negotiate_answer_directional(
        answer, [fmt.audio_format], [fmt.audio_format], local_offer_sdp=offer,
        allow_dahua_pcm=encoding == "PCM", local_capabilities=capabilities,
    )
    assert selected is not None
    assert selected.send.frame_ms == 20 and selected.send.max_frame_ms == 20
    assert selected.recv.frame_ms == 20
    payload = b"\xce" * (rate * 40 // 1000) if encoding == "PCMU" else b"\x01\x02" * (rate * 40 // 1000)
    decoder = receiver(selected.recv)
    if explicit_max:
        assert selected.recv.max_frame_ms == explicit_max
        with pytest.raises(rtp.RtpError, match="payload too large"):
            decoder.decode(selected.recv.payload_type, payload, normalize_frames=True)
    else:
        assert selected.recv.max_frame_ms > 20
        assert len(decoder.decode(selected.recv.payload_type, payload, normalize_frames=True)) == 2
        with pytest.raises(rtp.RtpError, match="payload too large"):
            decoder.decode(selected.recv.payload_type, b"\x00" * (12 * 1024 + 2))


@pytest.mark.parametrize("frame_ms", [10, 16])
def test_explicit_esp_receiver_contract_is_unchanged(frame_ms):
    send = sdp.RtpPcmFormat(96, "L16", 48000, 1, 10)
    recv = sdp.RtpPcmFormat(97, "L16", 16000, 1, frame_ms, max_frame_ms=frame_ms)
    args = ("127.0.0.1", "127.0.0.1", 40000, [send.audio_format], [recv.audio_format])
    options = dict(
        send_rtp_formats=(send,), recv_rtp_formats=(recv,),
        allow_directional_payloads=True, independent_audio_ptime=True,
    )
    previous = sdp.build_offer_directional(*args, **options)
    current = sdp.build_offer_directional(
        *args, **options, local_capabilities=sip_client.HA_AUDIO_CAPABILITIES,
    )
    assert current == previous
    assert f"a=ptime:{frame_ms}\r\n" in current
    assert f"a=maxptime:{frame_ms}\r\n" not in current  # Equal to ptime, redundant on D1.
