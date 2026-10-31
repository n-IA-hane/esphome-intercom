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


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("packet_time", [True, False])
@pytest.mark.parametrize("directional", [True, False])
async def test_generic_registered_peer_discovers_wire_profile_before_ordinary_call(
    trunks, monkeypatch, packet_time, directional,
):
    """No ESP registry, descriptor or injected media metadata participates."""
    from custom_components.voip_stack.core.sip_auth import build_digest_authorization
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.sip_registrar import SipAccount
    from custom_components.voip_stack.phonebook_runtime import registered_roster_entries
    from custom_components.voip_stack.endpoint_session import TerminationIntent
    from custom_components.voip_stack.const import CONF_SIP_VIDEO, CONF_VIDEO_CAMERA_SEND

    runtime = trunks
    username = "ordinary-wire-phone"
    endpoint = PhoneEndpoint(endpoint_id="sip:ordinary-wire-phone", name="Ordinary wire phone",
        username=username, kind=EndpointKind.SIP_ACCOUNT)
    browser = PhoneEndpoint(endpoint_id="browser:wire-caller", name="Caller",
        device_id="wire-caller-device", kind=EndpointKind.BROWSER,
        capabilities=frozenset({"audio", "dtmf", "video"}))
    runtime.runtime.endpoints.register(endpoint)
    runtime.runtime.endpoints.register(browser)
    assert not any(item.kind is EndpointKind.ESPHOME for item in runtime.runtime.endpoints.endpoints)
    runtime.runtime.transport_config.update({CONF_SIP_VIDEO: True, CONF_VIDEO_CAMERA_SEND: True})
    registrar = runtime.calls.component("registrar")
    registrar.enabled = True
    registrar.update_accounts([*registrar.accounts.values(), SipAccount(username, endpoint.name, "fixture-only")])
    options = asyncio.Event()
    options_ports = []
    invites = asyncio.Queue()
    replies = asyncio.Queue()
    caps = CAPS if packet_time else CAPS.replace(b"a=ptime:10\r\n", b"")
    if not directional:
        caps = caps.replace(b"a=x-voip-stack-flow:96 send\r\n", b"")
        caps = caps.replace(b"a=x-voip-stack-flow:97 recv\r\n", b"")

    class OrdinaryPeer(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, raw, address):
            message = sip.parse_message(raw)
            if message.status_code is not None:
                replies.put_nowait(message)
                return
            headers = [(key, message.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
            headers.append(("To", message.header("To") + ";tag=ordinary-peer"))
            body = b""
            if message.method == "OPTIONS":
                assert message.header("Accept") == "application/sdp"
                options_ports.append(self.transport.get_extra_info("sockname")[1])
                body = caps
                options.set()
            elif message.method == "INVITE":
                invites.put_nowait(message)
                mappings = sdp.offered_pcm_formats(message.body)
                recv = next(fmt for fmt in mappings if fmt.encoding == "L16" and fmt.sample_rate == 16000)
                send = next(fmt for fmt in mappings if fmt.encoding == "L16" and fmt.sample_rate == 48000) if directional else recv
                assert send.frame_ms == recv.frame_ms == 10
                assert sdp.offered_video_formats(message.body)[0].encoding == "JPEG"
                payloads = f"{send.payload_type} {recv.payload_type}" if directional else str(recv.payload_type)
                audio = f"a=rtpmap:{recv.payload_type} L16/16000/1\r\n"
                if directional:
                    audio += (f"a=rtpmap:{send.payload_type} L16/48000/1\r\n"
                        f"a=x-voip-stack-flow:{send.payload_type} recv\r\n"
                        f"a=x-voip-stack-flow:{recv.payload_type} send\r\n")
                body = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Ordinary phone\r\n"
                    "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                    f"m=audio 34000 RTP/AVP {payloads}\r\n{audio}"
                    f"a=ptime:{10 if directional or packet_time else 20}\r\na=sendrecv\r\n"
                    "m=video 34002 RTP/AVP 26\r\na=rtpmap:26 JPEG/90000\r\na=sendrecv\r\n").encode()
                headers.append(("Contact", f"<sip:{username}@127.0.0.1:{self.transport.get_extra_info('sockname')[1]}>"))
            elif message.method == "ACK":
                return
            if body:
                headers.append(("Content-Type", "application/sdp"))
            self.transport.sendto(sip.build_response(200, "OK", headers, body), address)

    transport, _ = await runtime.hass.loop.create_datagram_endpoint(OrdinaryPeer, local_addr=("127.0.0.1", 0))
    transports = [transport]
    local_port = transport.get_extra_info("sockname")[1]
    remote_port = runtime.runtime.transport_config["sip_port"]
    uri = f"sip:127.0.0.1:{remote_port}"

    def register(sequence, authorization=None, expires=60):
        headers = [("Via", f"SIP/2.0/UDP 127.0.0.1:{local_port};branch=z9hG4bK-ordinary-{sequence};rport"),
            ("From", f"<sip:{username}@127.0.0.1>;tag=ordinary"),
            ("To", f"<sip:{username}@127.0.0.1>"), ("Call-ID", "ordinary-registration"),
            ("CSeq", f"{sequence} REGISTER"), ("Contact", f"<sip:{username}@127.0.0.1:5060>"),
            ("Expires", str(expires)), ("User-Agent", "Ordinary SIP phone")]
        if authorization:
            headers.append(("Authorization", authorization))
        transport.sendto(sip.build_request("REGISTER", uri, headers), ("127.0.0.1", remote_port))

    async def register_response():
        async with asyncio.timeout(2):
            while True:
                reply = await replies.get()
                if reply.header("CSeq").endswith(" REGISTER"):
                    return reply

    def authorize(sequence):
        return build_digest_authorization(challenge_header=challenge.header("WWW-Authenticate"),
            username=username, password="fixture-only", method="REGISTER", uri=uri, nonce_count=sequence - 1)

    try:
        register(1)
        challenge = await register_response()
        assert challenge.status_code == 401
        authorization = build_digest_authorization(challenge_header=challenge.header("WWW-Authenticate"),
            username=username, password="fixture-only", method="REGISTER", uri=uri)
        register(2, authorization)
        assert (await register_response()).status_code == 200
        await asyncio.wait_for(options.wait(), 2)
        async with asyncio.timeout(2):
            while True:
                entry = next(item for item in registered_roster_entries(runtime.hass) if item.id == username)
                if "sdp_features" in entry.metadata:
                    break
                await asyncio.sleep(.01)
        assert "endpoint_kind" not in entry.metadata
        assert entry.metadata["sdp_features"] == (["directional_audio_v1"] if directional else [])
        expected_rx = ["L16/48000/1/10"] if directional else ["L16/16000/1/10", "L16/48000/1/10"]
        expected_tx = ["L16/16000/1/10"] if directional else expected_rx
        assert entry.metadata["sip_audio_rx_formats"] == (expected_rx if packet_time else [])
        assert entry.metadata["sip_audio_tx_formats"] == (expected_tx if packet_time else [])
        initial = registrar.registered_contacts(username)[0]
        learned = initial.capabilities
        register(3, authorize(3))
        assert (await register_response()).status_code == 200
        assert registrar.registered_contacts(username)[0].capabilities is learned
        assert options_ports == [local_port]
        # Renewing the same advertised Contact from a new real source flow
        # keeps the account registered but requires a fresh OPTIONS result.
        options.clear()
        transport, _ = await runtime.hass.loop.create_datagram_endpoint(OrdinaryPeer, local_addr=("127.0.0.1", 0))
        transports.append(transport)
        local_port = transport.get_extra_info("sockname")[1]
        register(4, authorize(4))
        assert (await register_response()).status_code == 200
        await asyncio.wait_for(options.wait(), 2)
        async with asyncio.timeout(2):
            while registrar.registered_contacts(username)[0].capabilities is None:
                await asyncio.sleep(.01)
        current, = registrar.registered_contacts(username)
        assert current.source_port == local_port and current.capabilities is not learned
        assert options_ports == [initial.source_port, local_port]
        assert not runtime.calls.sessions
        # This socket fixture does not load entity platforms. Export the real
        # directory result through the normal registered phonebook state.
        from custom_components.voip_stack.directory_runtime import async_refresh_directory
        from custom_components.voip_stack.roster import dump_roster_json
        directory = await async_refresh_directory(runtime.hass)
        runtime.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(directory)})
        await runtime.hass.services.async_call("voip_stack", "call", {
            "device_id": browser.device_id, "destination": endpoint.name, "send_video": True,
        }, blocking=True)
        request = await asyncio.wait_for(invites.get(), 2)
        client = runtime.calls.sip_client_for(request.header("Call-ID"))
        assert client.dialog.send_format.sample_rate == (48000 if directional else 16000)
        assert client.dialog.recv_format.sample_rate == 16000
        assert client.dialog.send_format.frame_ms == (10 if directional or packet_time else 20)
        # An answer's ptime controls HA transmission. Local receive framing
        # remains independent, bounded by the actual offer's maxptime.
        assert client.dialog.recv_format.frame_ms == 10
        assert client.dialog.video_format.encoding == "JPEG"
        assert client.dialog.local_video_direction == "sendrecv"
        await runtime.calls.request_termination(request.header("Call-ID"), TerminationIntent("test_done"))
        register(5, authorize(5), expires=1)
        assert (await register_response()).status_code == 200
        async with asyncio.timeout(3):
            while runtime.runtime.endpoints.get(endpoint.endpoint_id).availability.value != "offline":
                await asyncio.sleep(.05)
        assert registrar.registered_contacts(username) == ()
        assert not runtime.calls.sessions
    finally:
        for session in tuple(runtime.calls.sessions.values()):
            await runtime.calls.request_termination(session.call_id, TerminationIntent("test_done"))
        for transport in transports:
            transport.close()


def test_capability_port_zero_is_never_an_active_call():
    with pytest.raises(sdp.SdpError):
        sdp.parse_sdp(CAPS)
    assert not sdp.offered_video_formats(CAPS)
    result = sdp.options_capabilities(CAPS)
    assert result["sip_audio_tx_formats"] == ["L16/16000/1/10"]
    assert result["sip_audio_rx_formats"] == ["L16/48000/1/10"]
    assert result["sdp_features"] == ["directional_audio_v1"]
    assert result["sip_video_codec"] == "jpeg"


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_optional_probe_does_not_publish_caps_from_a_replaced_flow(trunks, monkeypatch):
    from types import SimpleNamespace
    from custom_components.voip_stack import sip_capabilities as module
    from custom_components.voip_stack.sip_registrar import SipAccount, SipRegistration

    registrar = trunks.calls.component("registrar")
    registrar.enabled = True
    registrar.update_accounts([SipAccount("ordinary", "Ordinary", "fixture-only")])
    original = SipRegistration("ordinary", "sip:ordinary@127.0.0.1:41000", "127.0.0.1", 41000, "UDP", 9999999999)
    registrar.registrations["ordinary"] = original
    started = asyncio.Event()
    response = asyncio.get_running_loop().create_future()

    async def options(*_args):
        started.set()
        return await response

    monkeypatch.setattr(module, "sip_endpoint_manager", lambda _hass: SimpleNamespace(async_options=options))
    task = asyncio.create_task(module.async_probe(trunks.hass, "ordinary"))
    await asyncio.wait_for(started.wait(), 2)
    replacement = SipRegistration("ordinary", "sip:ordinary@127.0.0.1:42000", "127.0.0.1", 42000, "UDP", 9999999999)
    registrar.registrations["ordinary"] = replacement
    response.set_result(sip.SipMessage(status_code=200, reason="OK", body=CAPS))
    await asyncio.wait_for(task, 2)
    assert original.capabilities is None
    assert replacement.capabilities is None
    assert "sdp_features" not in registrar.registered_roster_entries()[0].metadata


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
            assert registrar.registered_contacts(username)[0].capabilities is None
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


@pytest.mark.parametrize("accept, body_expected", [("application/sdp", True), ("", False), ("application/json", False),
    ("application/sdp;q=0", False), ("application/sdp;q=0.5", True)])
async def test_server_options_explicit_capabilities_allocates_no_media(accept, body_expected, monkeypatch, socket_enabled):
    """A real OPTIONS response declares local capabilities, not a call offer."""
    from custom_components.voip_stack.core.audio_format import HA_SIP_PCM_FORMATS
    from unittest.mock import Mock
    from custom_components.voip_stack import media_ports

    replies = asyncio.Queue()

    class Peer(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, raw, address):
            replies.put_nowait(sip.parse_message(raw))

    loop = asyncio.get_running_loop()
    endpoint = SipUdpEndpoint(local_ip="127.0.0.1", local_rtp_port=44000,
        supported_formats=list(HA_SIP_PCM_FORMATS), enable_video=True,
        on_invite=lambda invite: None)
    server, _ = await loop.create_datagram_endpoint(lambda: endpoint, local_addr=("127.0.0.1", 0))
    transport, peer = await loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
    allocate = Mock(side_effect=AssertionError("OPTIONS must allocate no media"))
    monkeypatch.setattr(media_ports, "allocate_sip_rtp_port_pair", allocate)
    try:
        headers = [("Via", "SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKcapabilities"),
            ("From", "<sip:ordinary@127.0.0.1>;tag=ordinary"), ("To", "<sip:service@127.0.0.1>"),
            ("Call-ID", "ordinary-options"), ("CSeq", "1 OPTIONS")]
        if accept:
            headers.append(("Accept", accept))
        transport.sendto(sip.build_request("OPTIONS", "sip:127.0.0.1", headers), server.get_extra_info("sockname"))
        response = await asyncio.wait_for(replies.get(), 2)
        assert response.status_code == 200
        assert bool(response.body) is body_expected
        if body_expected:
            assert response.header("Content-Type") == "application/sdp"
            origin = next(line for line in response.body.splitlines() if line.startswith(b"o=")).split()
            assert int(origin[1]) > 0
            assert b"m=audio 0 " in response.body
            assert b"m=video 0 " in response.body
            assert b"a=x-voip-stack-flow:" in response.body
            assert b"L16/48000/1" in response.body and b"L16/16000/1" in response.body
            second_headers = [(key, "2 OPTIONS" if key == "CSeq" else
                value.replace("z9hG4bKcapabilities", "z9hG4bKcapabilities2")) for key, value in headers]
            transport.sendto(sip.build_request("OPTIONS", "sip:127.0.0.1", second_headers), server.get_extra_info("sockname"))
            second = await asyncio.wait_for(replies.get(), 2)
            second_origin = next(line for line in second.body.splitlines() if line.startswith(b"o=")).split()
            assert second_origin[1] != origin[1]
        allocate.assert_not_called()
        assert not endpoint.active_dialogs and not endpoint.pending_invites
    finally:
        transport.close()
        endpoint.cancel_request_tasks()
        server.close()
        await endpoint.wait_closed()
