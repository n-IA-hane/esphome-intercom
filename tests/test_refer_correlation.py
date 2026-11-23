"""REFER subscription identity through both dialog owners on UDP loopback."""

from .voip_phase1_support import asyncio, audio_format, sdp, sip, sip_client, sip_listener, sip_transfer, sip_trunk, unittest


class Peer(asyncio.DatagramProtocol):
    def __init__(self):
        self.messages = asyncio.Queue()
        self.gruu = False

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.messages.put_nowait((sip.parse_message(data), addr))

    async def receive(self, method=None):
        async with asyncio.timeout(2):
            while True:
                message, addr = await self.messages.get()
                if method is None or message.method == method:
                    return message, addr

    def answer(self, request, addr, status=202, body=b""):
        headers = [(key, request.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
        to = request.header("To")
        headers.append(("To", to if ";tag=" in to else to + ";tag=peer"))
        gruu = ";gr=peer-instance" if self.gruu else ""
        headers.append(("Contact", f'<sip:peer@127.0.0.1:{self.transport.get_extra_info("sockname")[1]}{gruu}>'))
        if body:
            headers.append(("Content-Type", "application/sdp"))
        self.transport.sendto(sip.build_response(status, "Result", headers, body), addr)

    def notify(self, refer, addr, cseq, event, status=200, method="NOTIFY", event_header="Event"):
        raw = sip.build_request(method, "sip:HA@127.0.0.1", [
            ("Via", f"SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKnotify{cseq}"),
            ("From", refer.header("To") if sip.extract_tag(refer.header("To")) else refer.header("To") + ";tag=peer"),
            ("To", refer.header("From")),
            ("Call-ID", refer.header("Call-ID")), ("CSeq", f"{cseq} {method}"),
            (event_header, event), ("Content-Type", "message/sipfrag"),
            ("Subscription-State", "terminated;reason=noresource" if status >= 200 else "active;expires=30"),
        ], f"SIP/2.0 {status} Result\r\n".encode())
        self.transport.sendto(raw, addr)
        return raw


class ReferCorrelationTest(unittest.IsolatedAsyncioTestCase):
    async def test_uac_and_uas_consecutive_notify_transactions(self):
        for direction in ("uac", "uas"):
            with self.subTest(direction=direction):
                await self.exercise(direction)

    async def exercise(self, direction):
        loop = asyncio.get_running_loop()
        peer_transport, peer = await loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
        peer_port = peer_transport.get_extra_info("sockname")[1]
        audio = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20)
        forwarded = sip_transfer.SipReferTarget("sip:next@127.0.0.1",
            referred_by='"Original caller" <sip:caller@example.test>;cid=proof',
            referral_content_type="application/pkcs7-mime", referral_body=b"opaque signed bytes +/==\r\n",
            referral_body_headers=(("Content-ID", "<proof>"),))
        owner_transport = None
        owner = None
        task = None
        watcher = None
        try:
            if direction == "uac":
                owner = sip_client.SipCallClient(
                    local_ip="127.0.0.1", local_name="HA", local_sip_port=0, local_rtp_port=41000,
                    supported_send_rtp_formats=(audio,), supported_recv_rtp_formats=(audio,),
                )
                task = asyncio.create_task(owner.invite(target="peer", remote_host="127.0.0.1",
                                                       remote_sip_port=peer_port, timeout=2))
                invite, addr = await peer.receive("INVITE")
                peer.answer(invite, addr, 200, (
                    b"v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=test\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                    b"m=audio 45000 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\na=sendrecv\r\n"
                ))
                self.assertEqual(await task, "in_call")
                watcher = asyncio.create_task(owner.wait_for_dialog_termination())
                async def refer(timeout=1):
                    return await owner.refer(forwarded, timeout=timeout)
            else:
                owner = sip_listener.SipUdpEndpoint(
                    local_ip="127.0.0.1", local_sip_port=0, local_rtp_port=41000,
                    supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
                    on_invite=lambda _invite: None,
                )
                owner_transport, _ = await loop.create_datagram_endpoint(lambda: owner, local_addr=("127.0.0.1", 0))
                invite = sip.parse_message(sip.build_request("INVITE", "sip:HA@127.0.0.1", [
                    ("From", "<sip:peer@127.0.0.1>;tag=peer"), ("To", "<sip:HA@127.0.0.1>"),
                    ("Call-ID", "uas-correlation"), ("CSeq", "1 INVITE"),
                    ("Contact", f"<sip:peer@127.0.0.1:{peer_port}>"),
                ]))
                owner.active_dialogs["uas-correlation"] = sip_listener._ActiveDialog(
                    request=invite, addr=("127.0.0.1", peer_port), to_tag="ha", cseq=2, transport="UDP",
                    remote_uri=f"sip:peer@127.0.0.1:{peer_port}",
                )
                async def refer(timeout=1):
                    return await owner.async_refer("uas-correlation", forwarded, timeout=timeout)

            task = asyncio.create_task(refer())
            first, addr = await peer.receive("REFER")
            self.assertEqual(first.header("Referred-By"), forwarded.referred_by)
            self.assertEqual(first.header("Content-Type"), forwarded.referral_content_type)
            self.assertEqual(first.header("Content-ID"), "<proof>")
            self.assertEqual(first.body, forwarded.referral_body)
            # A NOTIFY may arrive before the REFER's final response.
            peer.notify(first, addr, 10, "refer", 100, event_header="o")
            response, _ = await peer.receive()
            if response.method == "ACK":
                response, _ = await peer.receive()
            self.assertEqual(response.status_code, 200)
            peer.answer(first, addr)
            first_id = sip.parse_cseq(first.header("CSeq")).number
            duplicate = peer.notify(first, addr, 11, f"refer;id={first_id}", 486)
            self.assertEqual((await peer.receive())[0].status_code, 200)
            self.assertEqual((await task).status, 486)

            task = asyncio.create_task(refer())
            second, addr = await peer.receive("REFER")
            second_id = sip.parse_cseq(second.header("CSeq")).number
            self.assertNotEqual(first_id, second_id)
            peer.answer(second, addr)
            peer_transport.sendto(duplicate, addr)
            self.assertEqual((await peer.receive())[0].status_code, 200)
            for seq, event in ((12, f"refer;id={first_id}"), (13, "refer;id=999999"), (14, "refer")):
                peer.notify(second, addr, seq, event)
                self.assertEqual((await peer.receive())[0].status_code, 481)
                self.assertFalse(task.done())
            progress = peer.notify(second, addr, 20, f"refer;id={second_id}", 100)
            self.assertEqual((await peer.receive())[0].status_code, 200)
            peer_transport.sendto(progress, addr)
            self.assertEqual((await peer.receive())[0].status_code, 200)
            peer.notify(second, addr, 19, f"refer;id={second_id}")
            self.assertEqual((await peer.receive())[0].status_code, 500)
            self.assertFalse(task.done())
            peer.notify(second, addr, 21, f"refer;id={second_id}")
            self.assertEqual((await peer.receive())[0].status_code, 200)
            self.assertTrue((await task).accepted)

            # Send independent wire forms through each real dialog owner.
            for sequence, targets, expected in (
                (22, (), 400),
                (23, (("Refer-To", "<sip:b@example.test>"), ("r", "<sip:c@example.test>")), 400),
                (24, (("Refer-To", "<sip:b@example.test>, <sip:c@example.test>"),), 400),
                (25, (("r", '"Bob" <sip:b@example.test?Replaces=call+id%3Bto-tag%3Dt%3Bfrom-tag%3Df>'),), 603),
            ):
                local_tag = owner.dialog_ids.local_tag if direction == "uac" else "ha"
                call_id = owner.dialog_ids.call_id if direction == "uac" else "uas-correlation"
                raw = sip.build_request("REFER", "sip:HA@127.0.0.1", [
                    ("Via", f"SIP/2.0/UDP 127.0.0.1:{peer_port};branch=z9hG4bKtarget{sequence}"),
                    ("From", "<sip:peer@127.0.0.1>;tag=peer"),
                    ("To", f"<sip:HA@127.0.0.1>;tag={local_tag}"),
                    ("Call-ID", call_id), ("CSeq", f"{sequence} REFER"), *targets])
                peer_transport.sendto(raw, addr)
                self.assertEqual((await peer.receive())[0].status_code, expected)

            task = asyncio.create_task(refer(timeout=0.05))
            third, addr = await peer.receive("REFER")
            peer.answer(third, addr)
            self.assertEqual((await asyncio.wait_for(task, 0.5)).state, "timeout")

            task = asyncio.create_task(refer())
            fourth, addr = await peer.receive("REFER")
            peer.answer(fourth, addr)
            peer.notify(fourth, addr, 60, "refer", method="BYE")
            self.assertEqual((await peer.receive())[0].status_code, 200)
            self.assertFalse((await task).accepted)
        finally:
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if direction == "uac" and owner is not None:
                await owner.close()
            if owner_transport is not None:
                owner_transport.close()
            peer_transport.close()

    async def test_modern_refer_owns_a_separate_dialog_after_bye(self):
        for borrowed in (False, True):
            for challenge in (False, True):
                with self.subTest(challenge=challenge, borrowed=borrowed):
                    await self.modern_client_dialog(challenge, borrowed)

    async def modern_client_dialog(self, challenge, borrowed):
        loop = asyncio.get_running_loop()
        transport, peer = await loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
        peer.gruu = True
        port = transport.get_extra_info("sockname")[1]
        audio = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20)
        client = sip_client.SipCallClient(local_ip="127.0.0.1", local_name="HA", local_sip_port=0,
            local_rtp_port=41000, supported_send_rtp_formats=(audio,), supported_recv_rtp_formats=(audio,),
            username="ha", auth_username="ha", password="test-only")
        trunk = None
        trunk_transport = None
        receive_task = None
        if borrowed:
            from custom_components.voip_stack.trunk_signaling import reuse_registered_trunk_flow
            trunk = sip_trunk.SipTrunkClient(config=sip_trunk.SipTrunkConfig(
                enabled=True, transport="udp", server="127.0.0.1", port=port,
                domain="127.0.0.1", username="ha", auth_username="ha", password="test-only", expires=300),
                local_ip="127.0.0.1", local_sip_port=0)
            trunk_transport, _ = await loop.create_datagram_endpoint(
                lambda: sip_trunk.SipDatagramQueueProtocol(trunk.queue), local_addr=("127.0.0.1", 0))
            trunk.transport = trunk_transport
            trunk.registered = True
            trunk._udp_local_port = trunk_transport.get_extra_info("sockname")[1]
            trunk._trusted_udp_hosts = frozenset({"127.0.0.1"})
            self.assertTrue(reuse_registered_trunk_flow(trunk, client))
            receive_task = asyncio.create_task(trunk._receive_loop())
        tasks = []
        try:
            invite_task = asyncio.create_task(client.invite(target="peer", remote_host="127.0.0.1", remote_sip_port=port))
            tasks.append(invite_task)
            invite, addr = await peer.receive("INVITE")
            peer.answer(invite, addr, 200, b"v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=test\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 45000 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\na=sendrecv\r\n")
            self.assertEqual(await invite_task, "in_call")
            watcher = asyncio.create_task(client.wait_for_dialog_termination())
            transfer = asyncio.create_task(client.refer(sip_transfer.SipReferTarget("sip:next@127.0.0.1"), timeout=2))
            tasks.extend((watcher, transfer))
            refer, addr = await peer.receive("REFER")
            if challenge:
                first = refer
                headers = [(key, first.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
                headers += [("To", first.header("To") + ";tag=challenge"),
                            ("WWW-Authenticate", 'Digest realm="peer", nonce="nonce", qop="auth"')]
                transport.sendto(sip.build_response(401, "Unauthorized", headers), addr)
                refer, addr = await peer.receive("REFER")
                self.assertEqual(refer.header("Call-ID"), first.header("Call-ID"))
                self.assertFalse(sip.extract_tag(refer.header("To")))
                self.assertEqual(sip.parse_cseq(refer.header("CSeq")).number, sip.parse_cseq(first.header("CSeq")).number + 1)
                self.assertNotEqual(refer.header("Via"), first.header("Via"))
                from custom_components.voip_stack.core.sip_auth import verify_digest_authorization
                verify_digest_authorization(authorization_header=refer.header("Authorization"),
                    username="ha", password="test-only", method="REFER", uri=refer.uri, realm="peer", nonce="nonce")
            self.assertNotEqual(refer.header("Call-ID"), invite.header("Call-ID"))
            self.assertFalse(sip.extract_tag(refer.header("To")))
            if borrowed:
                self.assertIs(trunk._outbound_dialogs[refer.header("Call-ID")].responses, client._reused_flow_responses)
            target = sip_transfer.request_target_dialog(refer)
            self.assertEqual(target.call_id, invite.header("Call-ID"))
            self.assertEqual(target.local_tag, "peer")
            self.assertEqual(target.remote_tag, sip.extract_tag(invite.header("From")))
            self.assertIn("tdialog", sip.option_tags(refer, "Require"))
            peer.notify(invite, addr, 10, f"refer;id={sip.parse_cseq(refer.header('CSeq')).number}")
            self.assertEqual((await peer.receive())[0].status_code, 481)
            self.assertFalse(transfer.done())
            peer.notify(refer, addr, 1, "refer", 100)
            self.assertEqual((await peer.receive())[0].status_code, 200)
            peer.answer(refer, addr, 200)
            bye = sip.build_request("BYE", "sip:HA@127.0.0.1", [
                ("Via", "SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKearlybye"),
                ("From", "<sip:peer@127.0.0.1>;tag=peer"), ("To", invite.header("From")),
                ("Call-ID", invite.header("Call-ID")), ("CSeq", "20 BYE"),
            ])
            transport.sendto(bye, addr)
            self.assertEqual((await peer.receive())[0].status_code, 200)
            self.assertEqual(await asyncio.wait_for(watcher, 1), "remote_hangup")
            self.assertFalse(transfer.done())
            peer.notify(refer, addr, 2, "refer", 200)
            self.assertEqual((await peer.receive())[0].status_code, 200)
            self.assertTrue((await transfer).accepted)
            if borrowed:
                self.assertNotIn(refer.header("Call-ID"), trunk._outbound_dialogs)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.close()
            if trunk is not None:
                self.assertFalse(trunk._outbound_dialogs)
                receive_task.cancel()
                await asyncio.gather(receive_task, return_exceptions=True)
                trunk_transport.close()
            transport.close()

    async def test_modern_listener_refer_keeps_event_usage_after_invite_bye(self):
        loop = asyncio.get_running_loop()
        transport, peer = await loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
        port = transport.get_extra_info("sockname")[1]
        endpoint = sip_listener.SipUdpEndpoint(local_ip="127.0.0.1", local_sip_port=0, local_rtp_port=41000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)], on_invite=lambda _invite: None)
        local_transport, _ = await loop.create_datagram_endpoint(lambda: endpoint, local_addr=("127.0.0.1", 0))
        original = sip.parse_message(sip.build_request("INVITE", "sip:HA@127.0.0.1", [
            ("From", "<sip:peer@127.0.0.1>;tag=peer"), ("To", "<sip:HA@127.0.0.1>"),
            ("Call-ID", "listener-modern"), ("CSeq", "1 INVITE"),
            ("Contact", f"<sip:peer@127.0.0.1:{port};gr=peer-instance>"),
        ]))
        endpoint.active_dialogs["listener-modern"] = sip_listener._ActiveDialog(
            request=original, addr=("127.0.0.1", port), to_tag="ha", cseq=2, transport="UDP",
            remote_uri=f"sip:peer@127.0.0.1:{port}", remote_target_uri=f"sip:peer@127.0.0.1:{port};gr=peer-instance")
        task = asyncio.create_task(endpoint.async_refer("listener-modern", sip_transfer.SipReferTarget("sip:next@127.0.0.1"), timeout=2))
        try:
            refer, addr = await peer.receive("REFER")
            self.assertNotEqual(refer.header("Call-ID"), "listener-modern")
            self.assertFalse(sip.extract_tag(refer.header("To")))
            target = sip_transfer.request_target_dialog(refer)
            self.assertEqual((target.call_id, target.local_tag, target.remote_tag), ("listener-modern", "peer", "ha"))
            peer.answer(refer, addr, 200)
            wrong_dialog = sip.parse_message(sip.build_request("REFER", "sip:peer@127.0.0.1", [
                ("From", "<sip:HA@127.0.0.1>;tag=ha"), ("To", original.header("From")),
                ("Call-ID", original.header("Call-ID")),
            ]))
            peer.notify(wrong_dialog, addr, 10, f"refer;id={sip.parse_cseq(refer.header('CSeq')).number}")
            self.assertEqual((await peer.receive())[0].status_code, 481)
            self.assertFalse(task.done())
            peer.notify(refer, addr, 1, "refer", 100)
            self.assertEqual((await peer.receive())[0].status_code, 200)
            bye = sip.build_request("BYE", "sip:HA@127.0.0.1", [
                ("Via", "SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKlistenerbye"),
                ("From", original.header("From")), ("To", "<sip:HA@127.0.0.1>;tag=ha"),
                ("Call-ID", "listener-modern"), ("CSeq", "3 BYE"),
            ])
            transport.sendto(bye, addr)
            self.assertEqual((await peer.receive())[0].status_code, 200)
            self.assertNotIn("listener-modern", endpoint.active_dialogs)
            self.assertFalse(task.done())
            peer.notify(refer, addr, 2, "refer", 200)
            self.assertEqual((await peer.receive())[0].status_code, 200)
            self.assertTrue((await task).accepted)
            self.assertFalse(endpoint._outside_refer)
            self.assertFalse(endpoint._event_dialogs)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            local_transport.close()
            await endpoint.wait_closed()
            transport.close()

    async def test_refer_unsubscribe_stops_notifications_not_the_accepted_action(self):
        loop = asyncio.get_running_loop()
        transport, peer = await loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
        port = transport.get_extra_info("sockname")[1]
        audio = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20)
        client = sip_client.SipCallClient(local_ip="127.0.0.1", local_name="HA", local_sip_port=0,
            local_rtp_port=41000, supported_send_rtp_formats=(audio,), supported_recv_rtp_formats=(audio,))
        entered, release = asyncio.Event(), asyncio.Event()
        completed = []
        async def action(_target):
            entered.set()
            await release.wait()
            completed.append(True)
            return 200
        client.on_refer = action
        tasks = []
        try:
            task = asyncio.create_task(client.invite(target="peer", remote_host="127.0.0.1", remote_sip_port=port))
            tasks.append(task)
            invite, addr = await peer.receive("INVITE")
            peer.answer(invite, addr, 200, b"v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=test\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 45000 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\na=sendrecv\r\n")
            self.assertEqual(await task, "in_call")
            await peer.receive("ACK")
            tasks.append(asyncio.create_task(client.wait_for_dialog_termination()))
            def send(method, sequence, extra):
                headers = [("Via", f"SIP/2.0/UDP 127.0.0.1:{port};branch=z9hG4bKusage{sequence}"),
                    ("From", invite.header("To") + ";tag=peer"), ("To", invite.header("From")),
                    ("Call-ID", invite.header("Call-ID")), ("CSeq", f"{sequence} {method}"),
                    ("Contact", f"<sip:peer@127.0.0.1:{port}>"), *extra]
                transport.sendto(sip.build_request(method, "sip:HA@127.0.0.1", headers), addr)
            send("REFER", 100, [("Refer-To", "<sip:next@127.0.0.1>")])
            seen = []
            while len(seen) < 2:
                message, source = await peer.receive()
                seen.append(message)
                if message.method == "NOTIFY":
                    peer.answer(message, source, 200)
            self.assertTrue(any(item.status_code == 200 for item in seen))
            await asyncio.wait_for(entered.wait(), 1)
            for sequence, expiry in ((101, 20), (102, 0)):
                send("SUBSCRIBE", sequence, [("Event", "refer;id=100"), ("Expires", str(expiry))])
                response = notification = None
                while response is None or notification is None:
                    message, source = await peer.receive()
                    if message.method == "NOTIFY":
                        notification = message
                        peer.answer(message, source, 200)
                    elif message.is_response and sip.parse_cseq(message.header("CSeq")).method == "SUBSCRIBE":
                        response = message
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.header("Expires"), str(expiry))
                self.assertEqual(notification.header("Event"), "refer;id=100")
                self.assertEqual(notification.header("Subscription-State").startswith("terminated"), expiry == 0)
            self.assertFalse(completed)
            operation = client._incoming_refer_task
            release.set()
            await asyncio.wait_for(operation, 1)
            self.assertEqual(completed, [True])
            self.assertFalse(client._refer_notifiers)
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.close()
            transport.close()

    async def test_listener_refer_unsubscribe_preserves_accepted_operation(self):
        loop = asyncio.get_running_loop()
        transport, peer = await loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
        port = transport.get_extra_info("sockname")[1]
        entered, release = asyncio.Event(), asyncio.Event()
        completed = []
        async def action(call_id, target):
            self.assertEqual(call_id, "refresh-uas")
            entered.set()
            await release.wait()
            completed.append(target.uri)
            return 200
        endpoint = sip_listener.SipUdpEndpoint(local_ip="127.0.0.1", local_sip_port=0, local_rtp_port=41000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=lambda _: None, on_refer=action)
        local, _ = await loop.create_datagram_endpoint(lambda: endpoint, local_addr=("127.0.0.1", 0))
        address = local.get_extra_info("sockname")
        def request(method, sequence, extra=()):
            return sip.build_request(method, "sip:HA@127.0.0.1", [
                ("Via", f"SIP/2.0/UDP 127.0.0.1:{port};branch=z9hG4bKrefresh{sequence}"),
                ("From", "<sip:peer@127.0.0.1>;tag=peer"), ("To", "<sip:HA@127.0.0.1>;tag=ha"),
                ("Call-ID", "refresh-uas"), ("CSeq", f"{sequence} {method}"),
                ("Contact", f"<sip:peer@127.0.0.1:{port}>"), *extra])
        dialog = sip_listener._ActiveDialog(request=sip.parse_message(request("INVITE", 1)),
            addr=("127.0.0.1", port), to_tag="ha", cseq=1, transport="UDP",
            remote_uri=f"sip:peer@127.0.0.1:{port}", remote_target_uri=f"sip:peer@127.0.0.1:{port}")
        endpoint.active_dialogs["refresh-uas"] = dialog
        try:
            for method, sequence, extra, terminal in (
                ("REFER", 2, (("Refer-To", "<sip:next@127.0.0.1>"),), False),
                ("SUBSCRIBE", 3, (("Event", "refer;id=2"), ("Expires", "20")), False),
                ("SUBSCRIBE", 4, (("Event", "refer;id=2"), ("Expires", "0")), True),
            ):
                transport.sendto(request(method, sequence, extra), address)
                response = notification = None
                while response is None or notification is None:
                    message, source = await peer.receive()
                    if message.method == "NOTIFY":
                        notification = message
                        peer.answer(message, source, 200)
                    elif message.is_response and message.header("CSeq") == f"{sequence} {method}":
                        response = message
                        self.assertEqual(response.status_code, 200)
                self.assertEqual(notification.header("Subscription-State").startswith("terminated"), terminal)
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(completed)
            operation = dialog.refer_task
            release.set()
            await asyncio.wait_for(operation, 1)
            self.assertEqual(completed, ["sip:next@127.0.0.1"])
            self.assertIsNone(dialog.refer_notifier)
        finally:
            release.set()
            local.close()
            await endpoint.wait_closed()
            transport.close()
