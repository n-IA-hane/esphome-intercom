"""Independent wire and pre-send failures for the shared application sender."""

import errno

from .voip_phase1_support import asyncio, audio_format, sip, sip_endpoint, sip_listener, unittest, patch
from custom_components.voip_stack.core.sip_auth import SipDigestCredentials, verify_digest_authorization
from custom_components.voip_stack.core.sip_dialog import SubscriptionDialog


class FollowUpTransportTest(unittest.IsolatedAsyncioTestCase):
    async def setup_peer(self, *, body=b"state", uri_transport="udp"):
        queue = asyncio.Queue()

        class Peer(asyncio.DatagramProtocol):
            def datagram_received(self, raw, address):
                queue.put_nowait((sip.parse_message(raw), address, raw))

        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(Peer,
            local_addr=("127.0.0.1", 0))
        port = transport.get_extra_info("sockname")[1]
        manager = sip_endpoint.SipEndpointManager(host="127.0.0.1", port=0, local_ip="127.0.0.1",
            local_rtp_port=41000, supported_formats=[audio_format.AudioFormat(16000)], on_invite=lambda _: None)
        self.assertTrue(await manager.start())
        request = sip.parse_message(sip.build_request("SUBSCRIBE", "sip:desk@127.0.0.1", [
            ("From", "<sip:watcher@127.0.0.1>;tag=watcher"), ("To", "<sip:desk@127.0.0.1>"),
            ("Contact", f"<sip:watcher@127.0.0.1:{port};transport={uri_transport}>"),
            ("Call-ID", "transport-boundary"), ("CSeq", "1 SUBSCRIBE")]))
        state = SubscriptionDialog.from_request(request, "notifier")
        result = sip_listener.SipRequestResult(to_tag="notifier", follow_up_dialog=state,
            follow_up_credentials=SipDigestCredentials("watcher", "test-only"),
            follow_up=sip_listener.SipFollowUpRequest(method="NOTIFY", cseq=state.next_cseq(),
                headers=(("Event", "presence"), ("Subscription-State", "active;expires=60")), body=body))
        self.addAsyncCleanup(manager.stop)
        self.addCleanup(transport.close)
        return manager, transport, queue, port, request, state, result

    @staticmethod
    def answer(request, status=200, extra=()):
        return sip.build_response(status, "OK" if status == 200 else "Authentication required",
            [(name, request.header(name)) for name in ("Via", "From", "To", "Call-ID", "CSeq")] + list(extra))

    async def test_authorization_crossing_threshold_keeps_ids_and_uses_tcp(self):
        for status in (401, 407):
            with self.subTest(status=status):
                manager, udp, queue, port, request, state, result = await self.setup_peer()
                received = asyncio.get_running_loop().create_future()
                writers, handlers = [], set()

                async def target(reader, writer):
                    handlers.add(asyncio.current_task()); writers.append(writer)
                    try:
                        raw = await sip_listener._read_sip_stream_message(reader)
                        notification = sip.parse_message(raw)
                        verify_digest_authorization(
                            authorization_header=notification.header("Authorization" if status == 401 else "Proxy-Authorization"),
                            username="watcher", password="test-only", method="NOTIFY",
                            uri=notification.uri, realm="watcher", nonce=nonce)
                        writer.write(self.answer(notification))
                        await writer.drain()
                        received.set_result((notification, raw))
                        await reader.read()
                    finally:
                        handlers.discard(asyncio.current_task())
                        writer.close()
                        await writer.wait_closed()

                server = await asyncio.start_server(target, "127.0.0.1", port)
                task = asyncio.create_task(manager.udp_server.endpoint._send_application_follow_up(
                    request, ("127.0.0.1", port), result))
                try:
                    first, address, raw = await asyncio.wait_for(queue.get(), 1)
                    self.assertLessEqual(len(raw), 1300)
                    nonce = "opaque" * 190
                    challenge = "WWW-Authenticate" if status == 401 else "Proxy-Authenticate"
                    udp.sendto(self.answer(first, status, [(challenge,
                        f'Digest realm="watcher", nonce="{nonce}", qop="auth"')]), address)
                    retry, retried_raw = await asyncio.wait_for(asyncio.shield(received), 2)
                    self.assertGreater(len(retried_raw), 1300)
                    self.assertIn("SIP/2.0/TCP", retry.header("Via"))
                    self.assertIn("transport=tcp", retry.header("Contact"))
                    self.assertEqual(retry.uri, first.uri)
                    self.assertEqual(retry.body, first.body)
                    for name in ("Call-ID", "From", "To", "Route"):
                        self.assertEqual(retry.header(name), first.header(name))
                    self.assertEqual(sip.parse_cseq(retry.header("CSeq")).number, 2)
                    self.assertIn(nonce, retry.header("Authorization" if status == 401 else "Proxy-Authorization"))
                    self.assertEqual((await task).status_code, 200)
                    self.assertEqual(state.local_cseq, 3)
                    self.assertTrue(queue.empty())
                finally:
                    task.cancel(); await asyncio.gather(task, return_exceptions=True)
                    await manager.stop()
                    for writer in writers: writer.close()
                    await asyncio.gather(*handlers, return_exceptions=True)
                    server.close(); await server.wait_closed()

    async def test_only_pre_send_refusal_permits_udp_fallback(self):
        for mode in ("refused", "timeout", "explicit_tcp"):
            with self.subTest(mode=mode):
                manager, udp, queue, port, request, _, result = await self.setup_peer(
                    body=b"large" * 300, uri_transport="tcp" if mode == "explicit_tcp" else "udp")
                endpoint = manager.udp_server.endpoint
                tcp = manager.tcp_server.endpoint
                async def timeout(*_args):
                    raise TimeoutError("independent setup deadline")
                context = patch.object(tcp, "request_route", timeout) if mode == "timeout" else patch.object(tcp, "request_route", tcp.request_route)
                with context:
                    task = asyncio.create_task(endpoint._send_application_follow_up(request, ("127.0.0.1", port), result))
                    try:
                        if mode == "refused":
                            notification, address, raw = await asyncio.wait_for(queue.get(), 2)
                            self.assertGreater(len(raw), 1300)
                            self.assertIn("SIP/2.0/UDP", notification.header("Via"))
                            udp.sendto(self.answer(notification), address)
                            self.assertEqual((await task).status_code, 200)
                        else:
                            with self.assertRaises(TimeoutError if mode == "timeout" else OSError) as raised:
                                await task
                            if mode == "explicit_tcp": self.assertEqual(raised.exception.errno, errno.ECONNREFUSED)
                            self.assertTrue(queue.empty())
                    finally:
                        task.cancel(); await asyncio.gather(task, return_exceptions=True)
                        await manager.stop()
                self.assertFalse(endpoint._client_transaction_responses)
                self.assertFalse(tcp._client_transaction_responses)

    async def test_unanswered_tcp_request_never_falls_back_to_udp(self):
        manager, _, queue, port, request, _, result = await self.setup_peer(body=b"large" * 300)
        received = asyncio.get_running_loop().create_future()
        handlers, writers = set(), []

        async def target(reader, writer):
            handlers.add(asyncio.current_task()); writers.append(writer)
            try:
                raw = await sip_listener._read_sip_stream_message(reader)
                received.set_result(sip.parse_message(raw))
                await reader.read()
            finally:
                handlers.discard(asyncio.current_task())
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(target, "127.0.0.1", port)
        try:
            with patch.object(sip_listener, "_INVITE_2XX_TIMEOUT", .5):
                response = await manager.udp_server.endpoint._send_application_follow_up(
                    request, ("127.0.0.1", port), result)
            self.assertIsNone(response)
            self.assertTrue(received.done())
            self.assertIn("SIP/2.0/TCP", received.result().header("Via"))
            self.assertTrue(queue.empty())
        finally:
            await manager.stop()
            for writer in writers: writer.close()
            await asyncio.gather(*handlers, return_exceptions=True)
            server.close(); await server.wait_closed()
