"""A leased UDP socket preserves dialog routing after registration changes."""

import asyncio

import pytest

from .voip_phase1_support import (
    _load_intercom_module, audio_format, sip, sip_client, sip_trunk,
)

trunk_signaling = _load_intercom_module("trunk_signaling")


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["contact", "route", "proxy", "renewal", "lost", "contact_dns", "route_dns", "target_refresh_dns", "target_refresh_dns_fail", "cancel_dns", "incoming_refresh_dns"])
async def test_trunk_dialog_next_hop_and_registration_lease(scenario):
    loop = asyncio.get_running_loop()
    wire, failures = [], []
    reject_renewal = False
    transports = []
    incoming = []
    incoming_answer = asyncio.Event()
    dns_failure = scenario.endswith("_dns_fail")
    scenario = scenario.removesuffix("_fail")
    dns_target = scenario.endswith("_dns")
    scenario = scenario.removesuffix("_dns")

    class Peer(asyncio.DatagramProtocol):
        def __init__(self, label):
            self.label = label

        def connection_made(self, transport):
            self.transport = transport
            transports.append(transport)

        def datagram_received(self, raw, address):
            try:
                request = sip.parse_message(raw)
                wire.append((self.label, request, address))
                if request.is_response:
                    if incoming and request.status_code == 200:
                        original = incoming[0]
                        ack = build_dialog_request("ACK", call_id=request.header("Call-ID"),
                            local_tag=sip.extract_tag(request.header("From")),
                            remote_tag=sip.extract_tag(request.header("To")), cseq=2,
                            local_uri=str(sip.parse_sip_uri(request.header("From"))),
                            remote_uri=str(sip.parse_sip_uri(request.header("To"))),
                            remote_target_uri=sip.contact_target_uri(request) or original.uri,
                            via_sent_by=a_address)
                        self.transport.sendto(ack.raw, address)
                        incoming_answer.set()
                    return
                if request.method == "ACK":
                    return
                if scenario == "cancel" and request.method == "INVITE":
                    self.pending = (request, address)
                    self.transport.sendto(sip.build_uas_response(request, 180, "Ringing", to_tag="confirmed"), address)
                    return
                status = 503 if request.method == "REGISTER" and reject_renewal else 200
                target = b_address if scenario in {"contact", "proxy"} or (scenario == "incoming_refresh" and self.label == "B") else a_address
                if scenario == "target_refresh" and request.method == "INVITE" and sip.parse_cseq(request.header("CSeq")).number > 1:
                    target = b_address
                contact_host = "localhost" if dns_target and scenario == "contact" else target[0]
                if scenario in {"target_refresh", "incoming_refresh"} and target == b_address:
                    contact_host = "localhost"
                body = request.body if request.method == "INVITE" else b""
                response = sip.build_uas_response(
                    request, status, "Unavailable" if status == 503 else "OK",
                    to_tag="confirmed", contact_uri=f"sip:peer@{contact_host}:{target[1]}", body=body,
                )
                if scenario == "route" and request.method == "INVITE":
                    response = response.replace(b"Content-Length:",
                        f"Record-Route: <sip:{'localhost' if dns_target else b_address[0]}:{b_address[1]};lr>\r\nContent-Length:".encode())
                self.transport.sendto(response, address)
                if scenario == "cancel" and request.method == "CANCEL":
                    invite, caller = self.pending
                    self.transport.sendto(sip.build_uas_response(invite, 200, "OK", to_tag="confirmed",
                        contact_uri=f"sip:peer@localhost:{b_address[1]}", body=invite.body), caller)
            except BaseException as error:
                failures.append(error)

    a, _ = await loop.create_datagram_endpoint(lambda: Peer("A"), local_addr=("127.0.0.2" if dns_target else "127.0.0.1", 0))
    b, _ = await loop.create_datagram_endpoint(lambda: Peer("B"), local_addr=("127.0.0.1" if dns_target else "127.0.0.2", 0))
    a_address, b_address = a.get_extra_info("sockname"), b.get_extra_info("sockname")
    proxy = f"sip:{a_address[0]}:{a_address[1]};lr" if scenario == "proxy" else ""
    trunk = sip_trunk.SipTrunkClient(config=sip_trunk.SipTrunkConfig(options_interval=30,
        enabled=True, transport="udp", server=a_address[0], port=a_address[1],
        domain="fixture.test", username="line", auth_username="", password="",
        expires=300, outbound_proxy=proxy,
    ), local_ip="127.0.0.1", local_sip_port=5060)
    client = None
    monitoring = None
    try:
        await trunk._connect_udp()
        trunk._ensure_receive_task()
        assert await trunk.register(timeout=1) == "registered"
        socket_owner = trunk.transport
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1", local_name="HA", local_sip_port=trunk._udp_local_port,
            local_rtp_port=41000, supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            include_common_codecs=False, outbound_proxy=proxy,
        )
        assert trunk_signaling.reuse_registered_trunk_flow(trunk, client)
        initial = await client.invite(target="peer", remote_host=a_address[0], remote_sip_port=a_address[1], timeout=1)
        if scenario == "cancel":
            assert initial == "ringing"
            assert await client.terminate(timeout=1) == "cancelled"
            assert sum(label == "B" and message.method == "ACK" for label, message, _ in wire) == 1
            assert sum(label == "B" and message.method == "BYE" for label, message, _ in wire) == 1
            assert not failures
            return
        assert initial == "in_call"
        if scenario == "incoming_refresh":
            from custom_components.voip_stack.core import sdp
            from custom_components.voip_stack.core.sip_dialog import build_dialog_request

            monitoring = asyncio.create_task(client.wait_for_dialog_termination())
            await asyncio.sleep(0)
            initial_invite = next(message for _label, message, _address in wire if message.method == "INVITE")
            offer = sdp.rewrite_sdp_origin(initial_invite.body.decode(), 7001, 2)
            request = build_dialog_request("INVITE", call_id=client.dialog_ids.call_id,
                local_tag=client.dialog_ids.remote_tag, remote_tag=client.dialog_ids.local_tag, cseq=2,
                local_uri=client.dialog.remote_uri, remote_uri=client.dialog.local_uri,
                remote_target_uri=client._pending_contact_uri, contact_uri=f"sip:peer@localhost:{b_address[1]}",
                via_sent_by=a_address, content_type="application/sdp", body=offer.encode())
            incoming.append(sip.parse_message(request.raw))
            a.sendto(request.raw, ("127.0.0.1", trunk._udp_local_port))
            await asyncio.wait_for(incoming_answer.wait(), 2)
            async with asyncio.timeout(2):
                while "localhost" not in client.dialog.remote_target_uri:
                    await asyncio.sleep(0)
        if dns_failure:
            from custom_components.voip_stack.core.sip_resolution import SipServerResolver

            async def unavailable(_host, _port, _transport):
                raise OSError("Diagnostic resolver unavailable")

            client.target_resolver = SipServerResolver(address_query=unavailable)
        if scenario == "renewal":
            reject_renewal = True
            assert await trunk.register(timeout=1) == "sip_503"
            assert not trunk.registered and trunk.transport is socket_owner
            assert trunk.open_outbound_dialog("new-call") is None
        if scenario == "lost":
            socket_owner.close()
            await asyncio.sleep(0)
            assert await client.terminate(timeout=0.1) != "remote_hangup"
            assert not any(message.method == "BYE" for _, message, _ in wire)
        else:
            previous = client.dialog
            selection = (client._signaling_nominal, client._resolved_signaling_target)
            refreshed = await client._send_session_refresh("INVITE")
            if dns_failure:
                assert refreshed is None
                assert client.dialog is previous
                assert (client._signaling_nominal, client._resolved_signaling_target) == selection
                assert await client.terminate(timeout=1) == "remote_hangup"
                assert not any(label == "B" for label, _request, _address in wire)
                assert not failures
                return
            assert refreshed is not None and refreshed.status_code == 200
            assert await client.terminate(timeout=1) == "remote_hangup"
            # Allow the datagram callback to consume the final ACK/BYE.
            await asyncio.sleep(0)
            expected = "B" if scenario in {"contact", "route", "incoming_refresh"} else "A"
            for method in ("ACK", "BYE"):
                messages = [(label, message, address) for label, message, address in wire if message.method == method]
                assert len(messages) == (2 if method == "ACK" else 1), (method, wire)
                expected_labels = (["A", "B"] if method == "ACK" else ["B"]) if scenario in {"target_refresh", "incoming_refresh"} else [expected] * len(messages)
                assert [label for label, _, _ in messages] == expected_labels, (method, messages)
                assert all(address[1] == trunk._udp_local_port for _, _, address in messages)
            invites = [label for label, message, _ in wire if message.method == "INVITE"]
            assert invites == ["A", "A" if scenario == "target_refresh" else expected]
            assert trunk.transport is socket_owner and not socket_owner.is_closing()
        assert not failures, failures
    finally:
        if monitoring is not None:
            monitoring.cancel()
            await asyncio.gather(monitoring, return_exceptions=True)
        if client is not None:
            await client.close()
        trunk.registered = False
        await trunk.stop()
        for transport in transports:
            transport.close()
        assert not trunk._outbound_dialogs
        assert trunk.transport is None and trunk._receive_task is None


@pytest.mark.asyncio
async def test_default_final_wait_preserves_initial_transaction_deadline():
    """A waiter cannot cancel Timer B while the peer has sent no provisional."""
    received = asyncio.Event()

    class SilentPeer(asyncio.DatagramProtocol):
        def datagram_received(self, raw, address):
            received.set()

    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        SilentPeer, local_addr=("127.0.0.1", 0))
    client = sip_client.SipCallClient(local_ip="127.0.0.1", local_name="HA",
        local_sip_port=5060, local_rtp_port=41000,
        supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)])
    inviting = asyncio.create_task(client.invite(target="silent", remote_host="127.0.0.1",
        remote_sip_port=transport.get_extra_info("sockname")[1], timeout=.1))
    try:
        await received.wait()
        assert client._invite_transaction is not None
        result = await asyncio.wait_for(client.wait_for_final(), .5)
        assert result == "timeout"
        assert await inviting == "timeout"
    finally:
        inviting.cancel()
        await asyncio.gather(inviting, return_exceptions=True)
        await client.close()
        transport.close()


@pytest.mark.asyncio
async def test_dialog_target_admission_does_not_expand_trunk_source_policy():
    loop = asyncio.get_running_loop()
    trunk = sip_trunk.SipTrunkClient(config=sip_trunk.SipTrunkConfig(options_interval=30,
        enabled=True, transport="udp", server="127.0.0.1", port=5060,
        domain="fixture.test", username="line", auth_username="", password="", expires=300,
    ), local_ip="127.0.0.1", local_sip_port=5060)
    endpoints = []
    for host in ("127.0.0.2", "127.0.0.3"):
        endpoint, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, local_addr=(host, 0))
        endpoints.append(endpoint)
    try:
        await trunk._connect_udp()
        trunk._ensure_receive_task()
        trunk.registered = True
        _, responses = trunk.open_outbound_dialog("leased")
        address = endpoints[0].get_extra_info("sockname")
        assert trunk.send_outbound_dialog_to("leased", b"\r\n", address)
        assert not trunk.accepts_inbound_source(*address, "UDP")
        destination = ("127.0.0.1", trunk._udp_local_port)

        def reply(identity):
            return sip.build_response(200, "OK", [("Call-ID", identity), ("CSeq", "2 BYE")])

        # Wrong host and wrong Call-ID cannot borrow the newly selected route.
        endpoints[1].sendto(reply("leased"), destination)
        endpoints[0].sendto(reply("other-dialog"), destination)
        endpoints[0].sendto(reply("leased"), destination)
        assert await asyncio.wait_for(responses.get(), 1) == (reply("leased"), address)
        assert responses.empty()
        alias = trunk.bind_outbound_usage("leased", "refer-usage")
        endpoints[0].sendto(reply("refer-usage"), destination)
        assert await asyncio.wait_for(responses.get(), 1) == (reply("refer-usage"), address)
        alias()
        trunk.close_outbound_dialog("leased")
        assert not trunk.send_outbound_dialog_to("leased", b"\r\n", address)
    finally:
        trunk.registered = False
        await trunk.stop()
        for endpoint in endpoints:
            endpoint.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_resolution", [False, True])
async def test_losing_dns_branch_does_not_replace_winner_or_repeat_bye(failed_resolution):
    loop = asyncio.get_running_loop()
    first_invite, destination, traffic = [], [], []
    losing_ack, losing_bye = asyncio.Event(), asyncio.Event()

    class Peer(asyncio.DatagramProtocol):
        def __init__(self, label):
            self.label = label

        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, raw, address):
            request = sip.parse_message(raw)
            traffic.append((self.label, request.method))
            if self.label == "B" and request.method == "ACK":
                losing_ack.set()
            if request.method == "ACK":
                return
            if request.method == "INVITE" and not first_invite:
                first_invite.append(request)
                destination.append(address)
            tag = "loser" if self.label == "B" else "winner"
            reply = sip.build_uas_response(request, 200, "OK", to_tag=tag,
                contact_uri=f"sip:peer@{a_address[0]}:{a_address[1]}",
                body=request.body if request.method == "INVITE" else b"")
            self.transport.sendto(reply, address)
            if self.label == "B" and request.method == "BYE":
                losing_bye.set()

    a, _ = await loop.create_datagram_endpoint(lambda: Peer("A"), local_addr=("127.0.0.2", 0))
    b, _ = await loop.create_datagram_endpoint(lambda: Peer("B"), local_addr=("127.0.0.1", 0))
    a_address, b_address = a.get_extra_info("sockname"), b.get_extra_info("sockname")
    trunk = sip_trunk.SipTrunkClient(config=sip_trunk.SipTrunkConfig(options_interval=30,
        enabled=True, transport="udp", server=a_address[0], port=a_address[1],
        domain="fixture.test", username="line", auth_username="", password="", expires=300),
        local_ip="127.0.0.1", local_sip_port=5060)
    client = None
    monitoring = None
    try:
        await trunk._connect_udp()
        trunk._ensure_receive_task()
        assert await trunk.register(timeout=1) == "registered"
        client = sip_client.SipCallClient(local_ip="127.0.0.1", local_name="HA",
            local_sip_port=trunk._udp_local_port, local_rtp_port=41000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)], include_common_codecs=False)
        assert trunk_signaling.reuse_registered_trunk_flow(trunk, client)
        assert await client.invite(target="peer", remote_host=a_address[0], remote_sip_port=a_address[1], timeout=1) == "in_call"
        winner = client.dialog
        monitoring = asyncio.create_task(client.wait_for_dialog_termination())
        await asyncio.sleep(0)
        selected = (client._signaling_nominal, client._resolved_signaling_target)
        failed = asyncio.Event()
        resolver = client.target_resolver
        if failed_resolution:
            class FlakyResolver:
                async def resolve(self, uri, *, transport):
                    if uri.host == "localhost" and not failed.is_set():
                        failed.set()
                        raise OSError("One transient resolver failure")
                    return await resolver.resolve(uri, transport=transport)

            client.target_resolver = FlakyResolver()
        fork = sip.build_uas_response(first_invite[0], 200, "OK", to_tag="loser",
            contact_uri=f"sip:peer@localhost:{b_address[1]}", body=first_invite[0].body)
        a.sendto(fork, destination[0])
        if failed_resolution:
            await asyncio.wait_for(failed.wait(), 2)
            if client._exceptional_bye_tasks:
                await asyncio.gather(*tuple(client._exceptional_bye_tasks), return_exceptions=True)
            assert client.dialog is winner
            assert (client._signaling_nominal, client._resolved_signaling_target) == selected
            a.sendto(fork, destination[0])
        await asyncio.wait_for(losing_bye.wait(), 2)
        assert losing_ack.is_set()
        if client._exceptional_bye_tasks:
            await asyncio.wait_for(asyncio.gather(*tuple(client._exceptional_bye_tasks)), 2)
        losing_ack.clear()
        sequence = client._local_dialog_cseq
        a.sendto(fork, destination[0])
        await asyncio.wait_for(losing_ack.wait(), 2)
        await asyncio.sleep(0)
        assert traffic.count(("B", "BYE")) == 1
        assert client._local_dialog_cseq == sequence
        assert client.dialog is winner
        assert (client._signaling_nominal, client._resolved_signaling_target) == selected
        assert await client.terminate(timeout=1) == "remote_hangup"
        assert traffic.count(("A", "BYE")) == 1
    finally:
        if monitoring is not None:
            monitoring.cancel()
            await asyncio.gather(monitoring, return_exceptions=True)
        if client is not None:
            await client.close()
        trunk.registered = False
        await trunk.stop()
        a.close()
        b.close()
    assert not trunk._outbound_dialogs and not client._exceptional_bye_tasks
