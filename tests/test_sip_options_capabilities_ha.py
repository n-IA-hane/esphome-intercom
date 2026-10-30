"""RFC 3264 capability probes share SIP transactions without media ownership."""

import asyncio

import pytest

from custom_components.voip_stack.core import sdp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.sip_listener import SipUdpEndpoint
from .test_trunk_calls_ha import lab as lab, trunks as trunks

pytestmark = pytest.mark.ha

CAPS = b"\r\n".join([
    b"v=0", b"o=- 0 0 IN IP4 127.0.0.1", b"s=Capabilities", b"c=IN IP4 127.0.0.1", b"t=0 0",
    b"m=audio 0 RTP/AVP 96 97", b"a=rtpmap:96 L16/16000/1", b"a=rtpmap:97 L16/48000/1",
    b"a=ptime:10", b"a=x-voip-stack-flow:96 send", b"a=x-voip-stack-flow:97 recv",
    b"m=video 0 RTP/AVP 26", b"a=recvonly", b"a=rtpmap:26 JPEG/90000", b"",
])


def test_capability_port_zero_is_never_an_active_call():
    with pytest.raises(sdp.SdpError):
        sdp.parse_sdp(CAPS)
    assert not sdp.offered_video_formats(CAPS)
    result = sdp.options_capabilities(CAPS)
    assert result["sip_audio_tx_formats"] == ["L16/16000/1/10"]
    assert result["sip_audio_rx_formats"] == ["L16/48000/1/10"]
    assert result["sdp_features"] == ["directional_audio_v1"]
    assert result["sip_video_codec"] == "jpeg"


@pytest.mark.parametrize("transport", ["UDP", "TCP"])
async def test_options_uses_owned_transactions_and_creates_no_call(socket_enabled, transport):
    loop = asyncio.get_running_loop()
    received = []
    endpoint = SipUdpEndpoint(local_ip="127.0.0.1", local_rtp_port=40000,
        supported_formats=[AudioFormat(16000, frame_ms=20)], on_invite=None,
        signaling_transport=transport)
    async def response(raw):
        request = sip.parse_message(raw)
        received.append(request)
        headers = [(name, request.header(name)) for name in ("Via", "From", "Call-ID", "CSeq")]
        headers.extend([("To", request.header("To") + ";tag=peer"), ("Content-Type", "application/sdp")])
        endpoint.datagram_received(sip.build_response(200, "OK", headers, CAPS), ("127.0.0.1", 5070))
    def send(raw, addr):
        assert addr == ("127.0.0.1", 5070)
        loop.call_soon(lambda: asyncio.create_task(response(raw)))
        return True
    endpoint.send_override = send
    result = await endpoint.async_options("sip:phone@127.0.0.1:5070", ("127.0.0.1", 5070), timeout=1)
    assert result.status_code == 200
    assert received[0].method == "OPTIONS"
    assert received[0].header("Accept") == "application/sdp"
    assert endpoint.pending_invites == {}
    assert endpoint.active_dialogs == {}
    assert endpoint._client_transaction_responses == {}
    endpoint.cancel_request_tasks()


@pytest.mark.parametrize("role", ["full_duplex", "mic_only", "speaker_only"])
@pytest.mark.parametrize("time_hint", [b"", b"a=maxptime:32\r\n"])
def test_options_unknown_packet_quantum_preserves_role_and_video(role, time_hint):
    body = CAPS.replace(b"a=ptime:10\r\n", time_hint)
    if role == "mic_only":
        body = body.replace(b"RTP/AVP 96 97", b"RTP/AVP 96")
        body = body.replace(b"a=rtpmap:97 L16/48000/1\r\n", b"")
        body = body.replace(b"a=x-voip-stack-flow:97 recv\r\n", b"")
    elif role == "speaker_only":
        body = body.replace(b"RTP/AVP 96 97", b"RTP/AVP 97")
        body = body.replace(b"a=rtpmap:96 L16/16000/1\r\n", b"")
        body = body.replace(b"a=x-voip-stack-flow:96 send\r\n", b"")
    result = sdp.options_capabilities(body)
    assert result["sip_audio_tx_formats"] == []
    assert result["sip_audio_rx_formats"] == []
    assert result["audio_mode"] == role
    assert result["sip_video_codec"] == "jpeg"
    assert result["sdp_features"] == (["directional_audio_v1"] if role == "full_duplex" else [])


def test_options_zero_rtcp_transport_is_ignored_only_for_zero_port_capability():
    body = CAPS.replace(b"a=rtpmap:26 JPEG/90000", b"a=rtcp:0 IN IP4 127.0.0.1\r\na=rtpmap:26 JPEG/90000")
    result = sdp.options_capabilities(body)
    assert result["sip_audio_tx_formats"] == ["L16/16000/1/10"]
    assert result["sip_audio_rx_formats"] == ["L16/48000/1/10"]
    assert result["sip_video_codec"] == "jpeg"
    for active in (body, body.replace(b"m=video 0", b"m=video 40002")):
        with pytest.raises(sdp.SdpError, match="RTCP|rtcp"):
            sdp.parse_video_sdp(active)
    with pytest.raises(sdp.SdpError, match="RTCP|rtcp"):
        sdp.parse_video_sdp(body.replace(b"m=video 0", b"m=video 40002"), capabilities=True)


async def test_options_real_udp_and_existing_tcp_connection(hass, socket_enabled):
    """An independently framed peer replies on the actual registered flow."""
    from custom_components.voip_stack.sip_endpoint import SipEndpointManager

    manager = SipEndpointManager(host="127.0.0.1", port=0, local_ip="127.0.0.1",
        local_rtp_port=40000, supported_formats=[AudioFormat(16000, frame_ms=20)], on_invite=None)
    peers = []

    def answer(raw):
        request = sip.parse_message(raw)
        assert request.method == "OPTIONS"
        headers = [(key, request.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
        headers += [("To", request.header("To") + ";tag=independent"), ("Content-Type", "application/sdp")]
        return sip.build_response(200, "OK", headers, CAPS)

    class Peer(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport
        def datagram_received(self, data, addr):
            peers.append(("UDP", sip.parse_message(data).method))
            self.transport.sendto(answer(data), addr)

    async def frame(reader):
        header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
        length = next(int(line.partition(b":")[2]) for line in header.split(b"\r\n")
                      if line.lower().startswith(b"content-length:"))
        return header + await reader.readexactly(length)

    assert await manager.start()
    transport = writer = None
    try:
        transport, _ = await hass.loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
        address = transport.get_extra_info("sockname")
        result = await manager.async_options(f"sip:phone@127.0.0.1:{address[1]}", address, "UDP", timeout=2)
        assert result.status_code == 200 and sdp.options_capabilities(result.body)["sip_video_codec"] == "jpeg"

        server = manager.tcp_server
        port = server.server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        address = writer.get_extra_info("sockname")
        # A normal incoming SIP request establishes the existing TCP flow.
        writer.write((f"OPTIONS sip:ha@127.0.0.1:{port} SIP/2.0\r\n"
            f"Via: SIP/2.0/TCP 127.0.0.1:{address[1]};branch=z9hG4bK-initial\r\n"
            "From: <sip:phone@127.0.0.1>;tag=first\r\nTo: <sip:ha@127.0.0.1>\r\n"
            "Call-ID: options-peer-initial\r\nCSeq: 1 OPTIONS\r\nContent-Length: 0\r\n\r\n").encode())
        await writer.drain()
        assert sip.parse_message(await frame(reader)).status_code == 200
        task = asyncio.create_task(manager.async_options(f"sip:phone@127.0.0.1:{address[1]};transport=tcp", address, "TCP", timeout=2))
        request = await frame(reader)
        peers.append(("TCP", sip.parse_message(request).method))
        writer.write(answer(request))
        await writer.drain()
        result = await task
        assert result.status_code == 200 and result.body == CAPS
        assert len(server._writers) == 1
        for endpoint in (manager.udp_server.endpoint, manager.tcp_server.endpoint):
            assert not endpoint.active_dialogs and not endpoint.pending_invites
            assert not endpoint._client_transaction_responses
        assert peers == [("UDP", "OPTIONS"), ("TCP", "OPTIONS")]
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        if transport is not None:
            transport.close()
        await manager.stop()


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered,legacy_profile,expected,hint_only", [(True, False, 200, False),
    (True, False, 200, True), (False, False, 200, False), (True, True, 488, False)])
async def test_native_directional_offer_without_optional_options_metadata(trunks, monkeypatch, registered, legacy_profile, expected, hint_only):
    """Real REGISTER/INVITE: capability absence differs from a concrete limit."""
    from custom_components.voip_stack import peer_snapshot
    from custom_components.voip_stack.core.sip_auth import build_digest_authorization
    from custom_components.voip_stack.peer import Peer
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.sip_registrar import SipAccount
    from custom_components.voip_stack.endpoint_session import TerminationIntent

    runtime = trunks
    username, identity = "native-no-options", "esphome:native-no-options"
    runtime.runtime.endpoints.register(PhoneEndpoint(endpoint_id=identity, name=username,
        username=username, kind=EndpointKind.ESPHOME))
    peer_snapshot.async_build_peer_snapshot.return_value = [Peer(name=username, host="127.0.0.2",
        endpoint_id=identity, endpoint_kind="esphome", sip_uri_user=username,
        tx_formats=["16000:s16le:1:16"] if legacy_profile else None,
        rx_formats=["48000:s16le:1:10"] if legacy_profile else None,
        sdp_features=frozenset({"directional_audio_v1"}) if hint_only else frozenset())]
    registrar = runtime.calls.component("registrar")
    registrar.enabled = True
    registrar.update_accounts([*registrar.accounts.values(), SipAccount(username, username, "fixture-only", endpoint_id=identity)])
    replies = asyncio.Queue()
    options = asyncio.Event()

    class NativePeer(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport
        def datagram_received(self, raw, address):
            message = sip.parse_message(raw)
            if message.method == "OPTIONS":
                headers = [(key, message.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
                headers += [("To", message.header("To") + ";tag=unsupported")]
                self.transport.sendto(sip.build_response(200, "OK", headers), address)
                options.set()
            else:
                replies.put_nowait(message)

    transport, _ = await runtime.hass.loop.create_datagram_endpoint(NativePeer, local_addr=("127.0.0.2", 0))
    local_port = transport.get_extra_info("sockname")[1]
    remote_port = runtime.runtime.transport_config["sip_port"]
    register_uri = f"sip:127.0.0.1:{remote_port}"
    target = f"sip:External@127.0.0.1:{remote_port}"
    local_uri = f"sip:{username}@127.0.0.1"
    call_id = "native-options-contract"

    def request(method, uri, sequence, *, body=b"", authorization=None, to=None, register=False, error_ack=False):
        branch_method = "INVITE" if error_ack else method
        headers = [("Via", f"SIP/2.0/UDP 127.0.0.2:{local_port};branch=z9hG4bK-native-{branch_method}-{sequence}"),
            ("From", f"<{local_uri}>;tag=native"), ("To", to or f"<{local_uri if register else uri}>"),
            ("Call-ID", "native-registration" if register else call_id), ("CSeq", f"{sequence} {method}"),
            ("Contact", f"<{local_uri.partition('@')[0]}@127.0.0.2:{local_port}>")]
        if register:
            headers += [("Expires", "60")]
        if authorization:
            headers += [("Authorization", authorization)]
        if body:
            headers += [("Content-Type", "application/sdp")]
        transport.sendto(sip.build_request(method, uri, headers, body), ("127.0.0.1", remote_port))

    async def final(method):
        async with asyncio.timeout(4):
            while True:
                message = await replies.get()
                if message.header("CSeq").endswith(" " + method) and message.status_code >= 200:
                    return message

    try:
        if registered:
            request("REGISTER", register_uri, 1, register=True)
            challenge = await final("REGISTER")
            assert challenge.status_code == 401
            authorization = build_digest_authorization(challenge_header=challenge.header("WWW-Authenticate"),
                username=username, password="fixture-only", method="REGISTER", uri=register_uri)
            request("REGISTER", register_uri, 2, register=True, authorization=authorization)
            assert (await final("REGISTER")).status_code == 200
            await asyncio.wait_for(options.wait(), 2)
            assert identity not in runtime.runtime.peer_capabilities
        # Independent wire fixture matches the actual ESP 16 kHz TX / 48 kHz RX.
        offer = ("v=0\r\no=- 1 1 IN IP4 127.0.0.2\r\ns=Native\r\nc=IN IP4 127.0.0.2\r\nt=0 0\r\n"
            "m=audio 34000 RTP/AVP 96 97\r\na=rtpmap:96 L16/48000/1\r\na=rtpmap:97 L16/16000/1\r\n"
            "a=ptime:10\r\na=x-voip-stack-flow:96 recv\r\na=x-voip-stack-flow:97 send\r\n").encode()
        request("INVITE", target, 1, body=offer)
        response = await final("INVITE")
        assert response.status_code == expected
        remote_target = str(sip.parse_sip_uri(response.header("Contact"))) if expected == 200 else target
        request("ACK", remote_target, 1, to=response.header("To"), error_ack=expected >= 300)
        if expected == 200:
            assert runtime.phones[0].invites
            assert call_id in runtime.calls.sessions
            request("BYE", remote_target, 2, to=response.header("To"))
            assert (await final("BYE")).status_code == 200
    finally:
        for session in tuple(runtime.calls.sessions.values()):
            await runtime.calls.request_termination(session.call_id, TerminationIntent("test_done"))
        transport.close()
