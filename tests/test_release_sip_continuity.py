"""Regression witnesses for live dialogs across registration and early responses."""

import asyncio

import pytest

from .voip_phase1_support import audio_format, sip, sip_client, sip_listener, sip_transaction, sip_trunk
from custom_components.voip_stack.sip_tcp_io import read_sip_stream_message
from custom_components.voip_stack.trunk_signaling import reuse_registered_trunk_flow


@pytest.mark.parametrize("response", [503, None])
async def test_register_failure_preserves_live_dialog_socket_and_can_recover(response):
    loop = asyncio.get_running_loop()
    phase = "initial"
    requests = []

    class Registrar(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, raw, address):
            request = sip.parse_message(raw)
            requests.append(request.method)
            if request.method != "REGISTER" or (phase == "refresh" and response is None):
                return
            status = response if phase == "refresh" else 200
            headers = [(name, request.header(name)) for name in ("Via", "From", "To", "Call-ID", "CSeq")]
            headers.append(("Expires", request.header("Expires") or "300"))
            self.transport.sendto(sip.build_response(status, "Witness", headers), address)

    transport, _ = await loop.create_datagram_endpoint(Registrar, local_addr=("127.0.0.1", 0))
    client = sip_trunk.SipTrunkClient(config=sip_trunk.SipTrunkConfig(options_interval=30,
        enabled=True, transport="udp", server="127.0.0.1", port=transport.get_extra_info("sockname")[1],
        domain="test", username="line", auth_username="", password="", expires=300),
        local_ip="127.0.0.1", local_sip_port=5060)
    old = None
    try:
        await client.start()
        assert await client.wait_for_registration(timeout=1)
        send, _responses = client.open_outbound_dialog("live-call")
        old = client.transport
        phase = "refresh"
        assert await client.register(timeout=.03) == ("timeout" if response is None else "sip_503")
        assert not old.is_closing()
        assert send(sip.build_request("BYE", "sip:peer@test", [("Call-ID", "live-call"), ("CSeq", "2 BYE")]))
        await asyncio.sleep(.01)
        assert "BYE" in requests
        phase = "recovered"
        assert await client.register(timeout=1) == "registered"
        assert client.transport is old
        client.close_outbound_dialog("live-call")
        assert not client._outbound_dialogs
    finally:
        phase = "cleanup"
        await client.stop()
        transport.close()
    assert old is not None and old.is_closing()


@pytest.mark.parametrize("status", [180, 183])
@pytest.mark.parametrize("option", ["Require", "Supported", None])
async def test_early_dialog_publishes_contact_used_by_prack(status, option):
    sent = []
    async def on_invite(_invite):
        return sip_listener.SipInviteResult(status, "Progress", defer_final=True)
    def capture(raw, _address):
        sent.append(sip.parse_message(raw))
        return True
    endpoint = sip_listener.SipUdpEndpoint(local_ip="192.0.2.1", local_rtp_port=42000,
        supported_formats=[audio_format.AudioFormat(16000, frame_ms=20)],
        on_invite=on_invite, send_override=capture)
    uri = "sip:receiver@192.0.2.1"
    headers = [("Via", "SIP/2.0/UDP 192.0.2.2:5060;branch=z9hG4bK-invite;rport"),
        ("From", "<sip:caller@192.0.2.2>;tag=remote"), ("To", f"<{uri}>"),
        ("Contact", "<sip:caller@192.0.2.2:5060>"), ("Call-ID", "early-contact"),
        ("CSeq", "1 INVITE"), ("Content-Type", "application/sdp")]
    if option:
        headers.append((option, "100rel"))
    offer = ("v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=-\r\nc=IN IP4 192.0.2.2\r\nt=0 0\r\n"
        "m=audio 40000 RTP/AVP 96\r\na=rtpmap:96 L16/16000\r\na=ptime:20\r\na=sendrecv\r\n").encode()
    try:
        assert endpoint.submit_datagram(sip.build_request("INVITE", uri, headers, offer), ("192.0.2.2", 5060))
        await asyncio.gather(*tuple(endpoint._request_tasks))
        provisional = next(item for item in sent if item.status_code == status)
        target = sip.contact_target_uri(provisional)
        assert target and sip.parse_sip_uri(target).host == "192.0.2.1"
        assert sip.extract_tag(provisional.header("To"))
        if option:
            assert provisional.header("Require") == "100rel"
            # The peer takes its remote target from the response, never from
            # its original routed INVITE URI.
            prack = sip.build_request("PRACK", target, [
                ("Via", "SIP/2.0/UDP 192.0.2.2:5060;branch=z9hG4bK-prack;rport"),
                ("From", provisional.header("From")), ("To", provisional.header("To")),
                ("Call-ID", provisional.header("Call-ID")), ("CSeq", "2 PRACK"),
                ("RAck", provisional.header("RSeq") + " 1 INVITE")])
            assert endpoint.submit_datagram(prack, ("192.0.2.2", 5060))
            await asyncio.gather(*tuple(endpoint._request_tasks))
            assert any(item.status_code == 200 and item.header("CSeq") == "2 PRACK" for item in sent)
        else:
            assert not provisional.header("Require")
        assert all(not item.header("Contact") for item in sent if item.status_code == 100)
    finally:
        endpoint.cancel_request_tasks()
        await endpoint.wait_closed()


@pytest.mark.parametrize("use_trunk", [False, True])
async def test_tcp_invite_keeps_stable_contact_reachable_on_another_connection(use_trunk):
    writers, requests, tasks = [], [], set()
    async def stable_contact(_reader, writer):
        writers.append(writer)
        writer.close()
        await writer.wait_closed()
    stable = await asyncio.start_server(stable_contact, "127.0.0.1", 0)
    stable_port = stable.sockets[0].getsockname()[1]
    async def peer(reader, writer):
        tasks.add(asyncio.current_task())
        writers.append(writer)
        try:
            while raw := await read_sip_stream_message(reader):
                request = sip.parse_message(raw)
                requests.append(request)
                if request.method == "ACK":
                    continue
                writer.write(sip.build_uas_response(request, 200, "OK", to_tag="peer",
                    contact_uri=f"sip:callee@127.0.0.1:{writer.get_extra_info('sockname')[1]};transport=tcp",
                    body=request.body if request.method == "INVITE" else b""))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(asyncio.current_task())
    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    peer_port = server.sockets[0].getsockname()[1]
    client = sip_client.SipCallClient(local_ip="127.0.0.1", local_name="office", local_uri_user="office",
        local_sip_port=stable_port, local_rtp_port=40000, signaling_transport="TCP")
    client.local_contact_uri = f"sip:office@127.0.0.1:{stable_port};transport=tcp"
    trunk = sip_trunk.SipTrunkClient(config=sip_trunk.SipTrunkConfig(options_interval=30, enabled=True, transport="tcp",
        server="127.0.0.1", port=peer_port, domain="test", username="office", auth_username="",
        password="", expires=300, register=False, allowed_ips=("127.0.0.1",)),
        local_ip="127.0.0.1", local_sip_port=stable_port)
    try:
        if use_trunk:
            reuse_registered_trunk_flow(trunk, client)
        assert await client.invite(target="callee", remote_host="127.0.0.1", remote_sip_port=peer_port, timeout=2) == "in_call"
        request = next(item for item in requests if item.method == "INVITE")
        contact = sip.parse_sip_uri(request.header("Contact"))
        _reader, writer = await asyncio.open_connection(contact.host, contact.port)
        writer.close()
        await writer.wait_closed()
        assert contact.port == stable_port
        assert not client.writer.is_closing()
    finally:
        await client.close()
        for writer in writers:
            writer.close()
            await writer.wait_closed()
        if tasks:
            await asyncio.gather(*tuple(tasks))
        for server in (server, stable):
            server.close()
            await server.wait_closed()


@pytest.mark.parametrize("method", ["UPDATE", "INVITE"])
@pytest.mark.parametrize("status", [500, 503])
async def test_session_refresh_retries_transient_failure_within_original_lease(method, status):
    now = 1000.0
    state = sip.SipSessionTimer(interval=1800, local_refresher=True, deadline=1900, refresh_at=1000)
    attempts = []
    async def send(actual, **_kwargs):
        attempts.append(actual)
        code = status if len(attempts) == 1 else 200
        return sip.parse_message(sip.build_response(code, "Response", [
            ("Retry-After", "30"), ("Session-Expires", "1800;refresher=uac")]))
    driver = sip_transaction.SessionTimerDriver(state, send, "uac", lambda: now, method)
    assert await driver.advance() == "waiting"
    assert state.deadline == 1900 and state.refresh_at == 1030
    now = 1029
    assert await driver.advance() == "waiting" and len(attempts) == 1
    now = 1030
    assert await driver.advance() == "refreshed"
    assert state.deadline == 2830 and attempts == [method, method]


@pytest.mark.parametrize("retry_after", [None, "2000", "bad"])
async def test_failed_refresh_preserves_lease_but_retry_budget_never_extends_it(retry_after):
    now = 1000.0
    state = sip.SipSessionTimer(interval=1800, local_refresher=True, deadline=1900, refresh_at=1000)
    calls = 0
    async def send(_method, **_kwargs):
        nonlocal calls
        calls += 1
        return sip.parse_message(sip.build_response(503, "Unavailable", [("Retry-After", retry_after)] if retry_after else []))
    driver = sip_transaction.SessionTimerDriver(state, send, "uac", lambda: now)
    assert await driver.advance() == "waiting"
    now = state.refresh_at
    if now < state.deadline:
        assert await driver.advance() == "waiting"
        assert state.refresh_at == 1900
        now = 1900
    assert await driver.advance() == "session_timer_failed"
    assert state.deadline == 1900 and calls <= 2


@pytest.mark.parametrize("status", [None, 408, 481])
async def test_lost_dialog_refresh_still_terminates(status):
    state = sip.SipSessionTimer(interval=1800, local_refresher=True, deadline=1900, refresh_at=1000)
    async def send(_method, **_kwargs):
        return sip.parse_message(sip.build_response(status, "Failure", [])) if status else None
    assert await sip_transaction.SessionTimerDriver(state, send, "uac", lambda: 1000).advance() == "session_timer_failed"


@pytest.mark.parametrize("response", [503, None])
@pytest.mark.parametrize("established", [False, True])
async def test_register_failure_preserves_incoming_trunk_dialog_socket(response, established):
    """Inbound INVITE uses the registration flow's real endpoint/send override."""
    from types import SimpleNamespace

    loop = asyncio.get_running_loop()
    phase = "initial"
    received = asyncio.Queue()
    registrar_address = None

    class Registrar(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, raw, address):
            nonlocal registrar_address
            message = sip.parse_message(raw)
            if message.method != "REGISTER":
                received.put_nowait(message)
                return
            registrar_address = address
            if phase == "refresh" and response is None:
                return
            status = response if phase == "refresh" else 200
            headers = [(name, message.header(name)) for name in ("Via", "From", "To", "Call-ID", "CSeq")]
            headers.append(("Expires", message.header("Expires") or "300"))
            self.transport.sendto(sip.build_response(status, "Witness", headers), address)

    transport, peer = await loop.create_datagram_endpoint(Registrar, local_addr=("127.0.0.1", 0))
    peer_port = transport.get_extra_info("sockname")[1]
    client = sip_trunk.SipTrunkClient(config=sip_trunk.SipTrunkConfig(options_interval=30,
        enabled=True, transport="udp", server="127.0.0.1", port=peer_port,
        domain="test", username="line", auth_username="", password="", expires=300),
        local_ip="127.0.0.1", local_sip_port=5060)
    offer = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
        "m=audio 40000 RTP/AVP 96\r\na=rtpmap:96 L16/16000\r\na=ptime:20\r\na=sendrecv\r\n")

    async def on_invite(invite):
        from custom_components.voip_stack.core.sdp import build_answer_directional

        answer = build_answer_directional("127.0.0.1", "127.0.0.1", 42000,
            invite.send_format, invite.recv_format, recv_formats=invite.recv_formats,
            send_formats=invite.send_formats, remote_sdp=invite.remote_sdp,
            dtmf_direction=invite.dtmf_direction)
        return sip_listener.SipInviteResult(200 if established else 180, "Witness",
            answer_sdp=answer if established else "", defer_final=not established)

    client.attach_endpoint_manager(SimpleNamespace(local_ip="127.0.0.1", port=5060, local_rtp_port=42000,
        supported_formats=[audio_format.AudioFormat(16000, frame_ms=20)], supported_send_formats=None,
        supported_recv_formats=None, on_invite=on_invite, on_terminated=None))
    call_id = "incoming-owned-flow"
    uri = "sip:line@127.0.0.1"
    headers = [("Via", f"SIP/2.0/UDP 127.0.0.1:{peer_port};branch=z9hG4bK-inbound;rport"),
        ("From", f"<sip:outside@127.0.0.1:{peer_port}>;tag=caller"), ("To", f"<{uri}>"),
        ("Contact", f"<sip:outside@127.0.0.1:{peer_port}>"), ("Call-ID", call_id),
        ("CSeq", "1 INVITE"), ("Content-Type", "application/sdp")]

    async def reply_for(cseq, status):
        async with asyncio.timeout(1):
            while True:
                message = await received.get()
                if message.header("CSeq") == cseq and message.status_code == status:
                    return message

    old = None
    try:
        await client.start()
        assert await client.wait_for_registration(timeout=1)
        assert registrar_address is not None
        transport.sendto(sip.build_request("INVITE", uri, headers, offer.encode()), registrar_address)
        progress = await reply_for("1 INVITE", 200 if established else 180)
        dialog_headers = [(name, progress.header(name)) for name in ("From", "To", "Call-ID")]
        if established:
            transport.sendto(sip.build_request("ACK", uri,
                [("Via", f"SIP/2.0/UDP 127.0.0.1:{peer_port};branch=z9hG4bK-ack"), *dialog_headers,
                 ("CSeq", "1 ACK")]), registrar_address)
            assert call_id in client.inbound_endpoint.active_dialogs
        else:
            assert call_id in client.inbound_endpoint.pending_invites
        assert not client._outbound_dialogs
        old = client.transport
        phase = "refresh"
        assert await client.register(timeout=.03) == ("timeout" if response is None else "sip_503")
        assert client.transport is old and not old.is_closing()
        method = "BYE" if established else "CANCEL"
        sequence = "2 BYE" if established else "1 CANCEL"
        via = headers[0][1] if not established else f"SIP/2.0/UDP 127.0.0.1:{peer_port};branch=z9hG4bK-bye"
        transport.sendto(sip.build_request(method, uri, [("Via", via), *dialog_headers,
            ("CSeq", sequence)]), registrar_address)
        await reply_for(sequence, 200)
        if not established:
            await reply_for("1 INVITE", 487)
        assert call_id not in client.inbound_endpoint.active_dialogs
        assert call_id not in client.inbound_endpoint.pending_invites
        phase = "recovered"
        assert await client.register(timeout=1) == "registered"
        assert client.transport is old
    finally:
        phase = "cleanup"
        await client.stop()
        transport.close()
    assert old is not None and old.is_closing()
