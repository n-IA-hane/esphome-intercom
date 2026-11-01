"""Local calls borrow the listener's signaling flow without owning its socket."""

import asyncio
import re
import socket
from unittest.mock import AsyncMock

import pytest

from custom_components.voip_stack import sip_runtime
from custom_components.voip_stack.core import sdp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_endpoint import SipEndpointManager
from custom_components.voip_stack.sip_registrar import SipAccount, SipRegistrar
from tests.test_trunk_calls_ha import lab as lab, trunks as trunks


pytestmark = pytest.mark.ha


async def test_udp_dialogs_use_registered_listener_port_and_close_independently(hass, socket_enabled, monkeypatch):
    registrar = SipRegistrar(enabled=True, accounts=[SipAccount("ordinary", "Ordinary", "fixture-only")],
        local_ip="127.0.0.1", local_sip_port=5060)
    manager = SipEndpointManager(host="127.0.0.1", port=0, local_ip="127.0.0.1", local_rtp_port=40000,
        supported_formats=[AudioFormat(16000, frame_ms=20)], on_invite=None, on_register=registrar.handle_register)
    assert await manager.start()
    monkeypatch.setattr(sip_runtime, "sip_endpoint_manager", lambda _hass: manager)
    requests, replies = [], asyncio.Queue()

    class Peer(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, raw, address):
            message = sip.parse_message(raw)
            if message.is_response:
                replies.put_nowait(message)
                return
            requests.append((message, address))
            if message.method == "ACK":
                return
            headers = [(key, message.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
            headers.append(("To", message.header("To") if ";tag=" in message.header("To") else message.header("To") + ";tag=independent"))
            body = b""
            if message.method == "INVITE":
                payload = re.search(rb"a=rtpmap:(\d+) L16/16000", message.body)[1].decode()
                body = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Independent peer\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                    f"m=audio 35000 RTP/AVP {payload}\r\na=rtpmap:{payload} L16/16000/1\r\na=ptime:20\r\na=sendrecv\r\n").encode()
                headers.append(("Content-Type", "application/sdp"))
            self.transport.sendto(sip.build_response(200, "OK", headers, body), address)

    transport, _ = await hass.loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
    remote_port = transport.get_extra_info("sockname")[1]
    listener = manager.udp_server.transport
    listener_port = listener.get_extra_info("sockname")[1]
    uri = sip.parse_sip_uri(f"sip:ordinary@127.0.0.1:{remote_port};transport=udp")
    clients = [SipCallClient(local_ip="127.0.0.1", local_name=f"Caller{index}", local_sip_port=listener_port,
        local_rtp_port=40000 + index * 2, supported_formats=[AudioFormat(16000, frame_ms=20)]) for index in range(2)]
    try:
        for client in clients:
            sip_runtime.enable_reused_signaling_flow(hass, client, uri, target="Ordinary", default_sip_port=listener_port)
        results = await asyncio.gather(*(client.invite(target="ordinary", remote_host="127.0.0.1",
            remote_sip_port=remote_port, request_uri=str(uri)) for client in clients))
        assert results == ["in_call", "in_call"]
        assert {address[1] for request, address in requests if request.method == "INVITE"} == {listener_port}
        assert all(client.transport is None for client in clients)
        await clients[0].close()
        assert clients[1].dialog is not None and not listener.is_closing()
        response = await manager.async_options(str(uri), ("127.0.0.1", remote_port), "UDP", timeout=2)
        assert response.status_code == 200
        await clients[1].close()
        assert not listener.is_closing()
        raw = sip.build_request("REGISTER", f"sip:127.0.0.1:{listener_port}", [
            ("Via", f"SIP/2.0/UDP 127.0.0.1:{remote_port};branch=z9hG4bK-still-live;rport"),
            ("From", "<sip:ordinary@127.0.0.1>;tag=register"), ("To", "<sip:ordinary@127.0.0.1>"),
            ("Call-ID", "registration-after-dialogs"), ("CSeq", "1 REGISTER"),
            ("Contact", f"<sip:ordinary@127.0.0.1:{remote_port}>")])
        transport.sendto(raw, ("127.0.0.1", listener_port))
        assert (await asyncio.wait_for(replies.get(), 2)).status_code == 401
    finally:
        await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)
        transport.close()
        await manager.stop()


@pytest.mark.parametrize("routing,host", [
    (mode, "127.0.0.1") for mode in ("same-contact", "contact-moves", "record-route", "outbound-proxy")
] + [(mode, "::1") for mode in ("same-contact", "contact-moves", "record-route", "outbound-proxy")]
  + [(mode, address) for address in ("127.0.0.1", "::1")
     for mode in ("contact-fqdn", "record-route-fqdn", "fork-fqdn", "remote-bye")]
  + [(mode, "127.0.0.1") for mode in ("resolution-timeout", "initial-untagged", "options-30", "legacy-30")])
async def test_reused_udp_listener_honors_dialog_next_hop_and_response_source(
    hass, socket_enabled, monkeypatch, routing, host,
):
    manager = SipEndpointManager(host=host, port=0, local_ip=host, local_rtp_port=40000,
        supported_formats=[AudioFormat(16000, frame_ms=20)], on_invite=None)
    assert await manager.start()
    monkeypatch.setattr(sip_runtime, "sip_endpoint_manager", lambda _hass: manager)
    listener = manager.udp_server.endpoint
    listener_port = manager.udp_server.transport.get_extra_info("sockname")[1]
    wire, peers = [], {}
    cleanup = False
    initial_ok = initial_request = None

    def response(request, overrides=None, body=b""):
        headers = {key: request.header(key) for key in ("Via", "From", "To", "Call-ID", "CSeq")}
        if ";tag=" not in headers["To"]:
            headers["To"] += ";tag=independent-target"
        headers.update(overrides or {})
        if body:
            headers["Content-Type"] = "application/sdp"
        return sip.build_response(200, "OK", list(headers.items()), body)

    class Peer(asyncio.DatagramProtocol):
        def __init__(self, name):
            self.name = name
            self.requests = asyncio.Queue()

        def connection_made(self, transport):
            self.transport = transport
            self.address = transport.get_extra_info("sockname")[:2]

        def datagram_received(self, raw, address):
            nonlocal initial_ok, initial_request
            message = sip.parse_message(raw)
            wire.append((self.name, message, address))
            self.requests.put_nowait((message, address))
            if message.method == "OPTIONS" and self.name == "a":
                body = ("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=Capabilities\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                    "m=audio 0 RTP/AVP 96\r\na=rtpmap:96 L16/16000/1\r\na=ptime:30\r\n").encode()
                self.transport.sendto(response(message, body=body), address)
            if message.method == "INVITE" and self.name == "a":
                initial_request = message
                fmt = next(fmt for fmt in sdp.offered_pcm_formats(message.body) if fmt.encoding == "L16")
                assert fmt.frame_ms == 20
                body = sdp.build_answer_directional(host, host, 35000,
                    fmt, fmt, remote_sdp=message.body).encode()
                target = peers["a" if routing == "same-contact" else "b"].address
                contact = ("localhost", target[1]) if routing in {"contact-fqdn", "resolution-timeout", "initial-untagged"} else target
                headers = {"Contact": f"<{sip.SipUri('target', *contact)}>"}
                if routing == "initial-untagged":
                    headers["To"] = message.header("To")
                if routing.startswith("record-route"):
                    route = peers["proxy"].address
                    if routing == "record-route-fqdn":
                        route = ("localhost", route[1])
                    headers["Record-Route"] = f"<{sip.SipUri('', *route, params=(('lr', None),))}>"
                initial_ok = response(message, headers, body)
                self.transport.sendto(initial_ok, address)
            elif cleanup and message.method == "BYE":
                self.transport.sendto(response(message), address)

        async def receive(self, method):
            async with asyncio.timeout(2):
                while True:
                    message, address = await self.requests.get()
                    if message.method == method:
                        return message, address

    for name in ("a", "b", "proxy", "spoof"):
        _transport, peers[name] = await hass.loop.create_datagram_endpoint(
            lambda name=name: Peer(name), local_addr=(host, 0))
    first = peers["a"].address
    uri = sip.SipUri("target", *first, params=(("transport", "udp"),))
    client = SipCallClient(local_ip=host, local_name="Borrowed UDP caller",
        local_sip_port=listener_port, local_rtp_port=40000,
        supported_formats=[AudioFormat(16000, frame_ms=20)],
        outbound_proxy=str(sip.SipUri("", *first)) if routing == "outbound-proxy" else "")
    received = asyncio.Queue()
    resolutions = []
    read_response = client._read_response
    resolve = client.target_resolver.resolve

    async def observe_resolution(uri, **kwargs):
        resolutions.append((uri.host, uri.port))
        if routing == "resolution-timeout":
            await asyncio.Event().wait()
        return await resolve(uri, **kwargs)

    monkeypatch.setattr(client.target_resolver, "resolve", observe_resolution)

    async def observe_response(*args, **kwargs):
        result = await read_response(*args, **kwargs)
        if result is not None and result[0].header("CSeq").endswith(" BYE"):
            received.put_nowait(result)
        return result

    monkeypatch.setattr(client, "_read_response", observe_response)
    spoof_seen = asyncio.Event()
    submit = listener.submit_datagram

    def observe_submit(raw, address):
        accepted = submit(raw, address)
        if address[:2] == peers["spoof"].address:
            spoof_seen.set()
        return accepted

    monkeypatch.setattr(listener, "submit_datagram", observe_submit)
    terminal = monitor = None
    try:
        if routing in {"options-30", "legacy-30"}:
            from custom_components.voip_stack.endpoint_routing import (
                sip_target_has_unspecified_audio, sip_target_rtp_audio_profile,
            )
            from custom_components.voip_stack.roster import RosterEntry

            probe = await manager.async_options(str(uri), first, "UDP", timeout=2)
            assert probe.status_code == 200
            metadata = sdp.options_capabilities(probe.body)
            assert metadata["sip_audio_tx_formats"] == metadata["sip_audio_rx_formats"] == []
            if routing == "legacy-30":
                metadata.update(sip_audio_tx_formats=["L16/16000/1/30"], sip_audio_rx_formats=["L16/16000/1/30"])
            entry = RosterEntry("target", metadata=metadata)
            assert sip_target_rtp_audio_profile(None, entry) is None
            client.include_common_codecs = sip_target_has_unspecified_audio(None, entry)
            assert client.include_common_codecs
        assert sip_runtime.enable_reused_signaling_flow(hass, client, uri,
            target="Target", default_sip_port=listener_port)
        result = await asyncio.wait_for(client.invite(target="target", remote_host=first[0],
            remote_sip_port=first[1], request_uri=str(uri),
            timeout=0.2 if routing == "resolution-timeout" else 2), 3)
        if routing == "initial-untagged":
            assert result == "media_incompatible" and not resolutions
            assert client.dialog is None and peers["b"].requests.empty()
            assert not client._prepared_udp_targets
            return
        if routing == "resolution-timeout":
            assert result == "transport_unreachable" and resolutions
            assert client.dialog is None and peers["b"].requests.empty()
            await client.close()
            assert not listener._dialog_queues and not listener._reused_dialog_keys
            assert not client._prepared_udp_targets
            return
        assert result == "in_call"
        monitor = asyncio.create_task(client.wait_for_dialog_termination())
        next_peer = peers["proxy" if routing.startswith("record-route") else
            "a" if routing in {"same-contact", "outbound-proxy"} else "b"]
        ack, address = await next_peer.receive("ACK")
        assert address[1] == listener_port and client.transport is None
        if routing.startswith("record-route"):
            route = ("localhost", next_peer.address[1]) if routing == "record-route-fqdn" else next_peer.address
            assert ack.header("Route") == f"<{sip.SipUri('', *route, params=(('lr', None),))}>"
            assert ack.uri.endswith(f":{peers['b'].address[1]}")
        aliases = set(listener._dialog_queues)
        client.bind_signaling_usage(client.dialog_ids.call_id)()
        assert set(listener._dialog_queues) == aliases
        original_branch = sip.parse_via(initial_request.header("Via")).branch
        untrusted_contact = f"<{sip.SipUri('untrusted', 'localhost', peers['spoof'].address[1])}>"
        for overrides in (
            {"Via": initial_request.header("Via").replace(original_branch, original_branch + "-wrong")},
            {"Call-ID": "not-this-call"}, {"From": "<sip:attacker@localhost>;tag=wrong"},
            {"To": initial_request.header("To")},
        ):
            peers["a"].transport.sendto(response(initial_request,
                {**overrides, "Contact": untrusted_contact}, sip.parse_message(initial_ok).body), (host, listener_port))
        # An old final response still comes from A after the target moved to B.
        peers["a"].transport.sendto(initial_ok, (host, listener_port))
        await next_peer.receive("ACK")
        assert ("localhost", peers["spoof"].address[1]) not in resolutions
        assert not any(key[1] == peers["spoof"].address[1] for key in client._prepared_udp_targets)
        assert (peers["spoof"].address, client.dialog_ids.call_id) not in listener._dialog_queues
        assert peers["spoof"].requests.empty()
        if routing == "fork-fqdn":
            winner = client.dialog
            fork = response(initial_request, {
                "To": initial_request.header("To") + ";tag=losing-fork",
                "Contact": f"<{sip.SipUri('fork', 'localhost', peers['proxy'].address[1])}>",
            }, sip.parse_message(initial_ok).body)
            peers["a"].transport.sendto(fork, (host, listener_port))
            await peers["proxy"].receive("ACK")
            fork_bye, fork_address = await peers["proxy"].receive("BYE")
            peers["proxy"].transport.sendto(response(fork_bye), fork_address)
            await asyncio.wait_for(asyncio.gather(*tuple(client._exceptional_bye_tasks)), 2)
            _message, fork_source = await asyncio.wait_for(received.get(), 2)
            assert fork_source == peers["proxy"].transport.get_extra_info("sockname")
            peers["a"].transport.sendto(fork, (host, listener_port))
            await peers["proxy"].receive("ACK")
            assert client.dialog is winner and not client._exceptional_bye_tasks
        if routing == "remote-bye":
            remote_bye = sip.build_request("BYE", client._pending_contact_uri, [
                ("Via", f"SIP/2.0/UDP {str(sip.SipUri('', *next_peer.address)).removeprefix('sip:')};branch=z9hG4bK-remote-bye"),
                ("From", sip.parse_message(initial_ok).header("To")), ("To", initial_request.header("From")),
                ("Call-ID", client.dialog_ids.call_id), ("CSeq", "2 BYE"),
            ])
            for _ in range(2):
                next_peer.transport.sendto(remote_bye, (host, listener_port))
                reply, address = await next_peer.receive(None)
                assert reply.status_code == 200 and address[1] == listener_port
            assert await asyncio.wait_for(monitor, 2) == "remote_hangup"
            await client.close()
            assert not listener._dialog_queues and not listener._reused_dialog_keys
            assert not client._prepared_udp_targets
            return
        terminal = asyncio.create_task(client.terminate(timeout=2))
        bye, address = await next_peer.receive("BYE")
        assert address[1] == listener_port
        dropped = listener.dropped_datagrams
        peers["spoof"].transport.sendto(response(bye), (host, listener_port))
        await asyncio.wait_for(spoof_seen.wait(), 2)
        assert listener.dropped_datagrams == dropped + 1 and not terminal.done()
        # A known source and Call-ID are insufficient without the transaction.
        branch = sip.parse_via(bye.header("Via")).branch
        for overrides in ({"Via": bye.header("Via").replace(branch, branch + "-forged")},
                          {"CSeq": f"{sip.parse_cseq(bye.header('CSeq')).number + 1} BYE"},
                          {"From": "<sip:attacker@127.0.0.1>;tag=wrong"},
                          {"To": "<sip:target@127.0.0.1>;tag=wrong"}):
            next_peer.transport.sendto(response(bye, overrides), (host, listener_port))
            _message, actual_source = await asyncio.wait_for(received.get(), 2)
            assert actual_source == next_peer.transport.get_extra_info("sockname") and not terminal.done()
        next_peer.transport.sendto(response(bye), (host, listener_port))
        assert await terminal == "remote_hangup"
        _message, actual_source = await asyncio.wait_for(received.get(), 2)
        assert actual_source == next_peer.transport.get_extra_info("sockname")
        assert {source[1] for _name, request, source in wire if request.is_request} == {listener_port}
        await client.close()
        assert listener._dialog_queues == {}
        assert listener._reused_dialog_keys == {}
        assert client._prepared_udp_targets == {}
        assert manager.udp_server.transport is not None and not manager.udp_server.transport.is_closing()
    finally:
        cleanup = True
        await client.close()
        if terminal is not None:
            await asyncio.gather(terminal, return_exceptions=True)
        if monitor is not None:
            await asyncio.gather(monitor, return_exceptions=True)
        await manager.stop()
        for peer in peers.values():
            peer.transport.close()


async def test_reused_udp_failed_send_and_usage_release_remove_only_owned_aliases(
    hass, socket_enabled, monkeypatch,
):
    manager = SipEndpointManager(host="127.0.0.1", port=0, local_ip="127.0.0.1", local_rtp_port=40000,
        supported_formats=[AudioFormat(16000, frame_ms=20)], on_invite=None)
    assert await manager.start()
    monkeypatch.setattr(sip_runtime, "sip_endpoint_manager", lambda _hass: manager)
    endpoint = manager.udp_server.endpoint
    original, target = ("127.0.0.1", 35001), ("127.0.0.1", 35002)
    client = SipCallClient(local_ip="127.0.0.1", local_name="Owner", local_sip_port=0,
        local_rtp_port=40000, supported_formats=[AudioFormat(16000, frame_ms=20)])
    call_id = client.dialog_ids.call_id
    uri = sip.parse_sip_uri(f"sip:peer@{original[0]}:{original[1]}")
    try:
        assert sip_runtime.enable_reused_signaling_flow(hass, client, uri,
            target="Peer", default_sip_port=5060)
        release_same = client.bind_signaling_usage(call_id)
        release_same()
        assert set(endpoint._dialog_queues) == {(original, call_id)}
        raw = sip.build_request("INVITE", str(uri), [("Call-ID", call_id)])
        monkeypatch.setattr(endpoint, "_send", lambda _raw, _target: False)
        with pytest.raises(ConnectionError, match="not writable"):
            await client._send_raw(raw, *target)
        assert set(endpoint._dialog_queues) == {(original, call_id)}
        monkeypatch.setattr(endpoint, "_send", lambda _raw, _target: True)
        release = client.bind_signaling_usage("subscription")
        subscribe = sip.build_request("SUBSCRIBE", str(uri), [("Call-ID", "subscription")])
        await client._send_raw(subscribe, *target)
        assert (original, "subscription") in endpoint._dialog_queues
        assert (target, "subscription") in endpoint._dialog_queues
        release()
        assert set(endpoint._dialog_queues) == {(original, call_id)}
        queue = endpoint._dialog_queues[(original, call_id)]
        assert endpoint._reused_dialog_keys[queue] == {(original, call_id)}
        for sequence in range(300):
            client.bind_signaling_usage(f"subscription-{sequence}")()
        assert endpoint._reused_dialog_keys[queue] == {(original, call_id)}
        sibling = endpoint.open_reused_dialog(target, call_id)
        assert sibling is not None
        with pytest.raises(ConnectionError, match="not writable"):
            await client._send_raw(raw, *target)
        assert endpoint._dialog_queues[(target, call_id)] is sibling[1]
        await client.close()
        assert set(endpoint._dialog_queues) == {(target, call_id)}
        endpoint.close_reused_dialog(target, call_id)
        assert endpoint._dialog_queues == {}
        assert endpoint._reused_dialog_keys == {}
        assert manager.udp_server.transport is not None and not manager.udp_server.transport.is_closing()
    finally:
        await client.close()
        await manager.stop()


@pytest.mark.parametrize("transport", ["TCP", "UDP"])
async def test_connected_flow_stays_pinned_without_new_dns(transport, monkeypatch):
    sent, closed = [], []
    client = SipCallClient(local_ip="127.0.0.1", local_name="Pinned", local_sip_port=5060,
        local_rtp_port=40000, signaling_transport=transport)
    resolve = AsyncMock(side_effect=AssertionError("An existing connected flow must not perform DNS"))
    monkeypatch.setattr(client.target_resolver, "resolve", resolve)
    client.use_reused_signaling_flow(send=lambda raw: sent.append(raw),
        responses=asyncio.Queue(), close=lambda: closed.append(True))
    try:
        await client._send_raw(b"initial", "unresolved.invalid", 5060)
        assert client._send_dialog_request(b"dialog", "another.invalid", 5070)
        assert sent == [b"initial", b"dialog"]
        resolve.assert_not_called()
    finally:
        await client.close()
    assert closed == [True]


async def test_endpoint_shutdown_owns_bridge_client_before_first_provisional(lab, monkeypatch):
    from custom_components.voip_stack import endpoint_runtime, invite_router, peer_snapshot
    from custom_components.voip_stack.inbound_routing import bridge
    from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime

    lab.runtime.sip = lab.calls = SipEndpointRuntime(allow_dark_sessions=True)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp, socket.socket() as tcp:
        udp.bind(("127.0.0.1", 0))
        port = udp.getsockname()[1]
        tcp.bind(("127.0.0.1", port))
    lab.runtime.transport_config["sip_port"] = port
    lab.runtime.endpoints.update("sip:bedroom0", availability="available")
    monkeypatch.setattr(peer_snapshot, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", AsyncMock(return_value=[]))
    monkeypatch.setattr(endpoint_runtime, "_roster_from_peers", lambda *_args: lab.roster)
    monkeypatch.setattr(invite_router, "_roster_from_peers", lambda *_args: lab.roster)
    assert await endpoint_runtime.async_start_sip_endpoint(lab.hass)
    target = lab.phones[0]
    target.auto_answer = False
    respond = target.respond
    monkeypatch.setattr(target, "respond", lambda request, address, status, body=b"":
        respond(request, address, status, body) if status != 180 else None)
    created = []

    def client_factory(**kwargs):
        client = SipCallClient(**kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(bridge, "SipCallClient", client_factory)
    caller = SipCallClient(local_ip="127.0.0.1", local_name="Outside", local_sip_port=5060,
        local_rtp_port=35000, supported_formats=[AudioFormat(16000, frame_ms=20)])
    pending = asyncio.create_task(caller.invite(target="bedroom0", remote_host="127.0.0.1", remote_sip_port=port,
        request_uri=f"sip:bedroom0@127.0.0.1:{port}"))
    try:
        await asyncio.wait_for(target.invited.wait(), 3)
        assert len(created) == 1 and not created[0]._invite_task.done()
        await endpoint_runtime.async_stop_sip_endpoint(lab.hass)
        assert created[0]._closed
        assert created[0]._invite_task.done()
        assert not lab.calls.sessions
        assert not lab.runtime.rtp_port_pool.get("used")
    finally:
        # These are this test's concrete handles, never a global task/port scrub.
        await asyncio.gather(*(client.close() for client in [caller, *created]), return_exceptions=True)
        await asyncio.gather(pending, return_exceptions=True)
        await endpoint_runtime.async_stop_sip_endpoint(lab.hass)
