"""Wire witnesses for timeout fallback before an automatic TCP upgrade sends."""

import errno
import ssl

from .voip_phase1_support import (
    asyncio, audio_format, patch, sip, sip_client, sip_endpoint,
    sip_listener, sip_resolution, unittest,
)
from custom_components.voip_stack.core.sip_dialog import SubscriptionDialog


class SipTcpConnectTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def make_udp_peer(self):
        received = []

        class Peer(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, raw, address):
                request = sip.parse_message(raw)
                received.append((request, raw, address))
                if request.method in {"INVITE", "NOTIFY"}:
                    headers = [(key, request.header(key))
                               for key in ("Via", "From", "Call-ID", "CSeq")]
                    headers.append(("To", request.header("To") +
                                    (";tag=udp-peer" if request.method == "INVITE" else "")))
                    status = 486 if request.method == "INVITE" else 200
                    self.transport.sendto(sip.build_response(status, "Peer response", headers), address)

        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            Peer, local_addr=("127.0.0.1", 0))
        self.addCleanup(transport.close)
        return transport.get_extra_info("sockname")[1], received

    def make_client(self, *, transport="UDP"):
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1", local_name="Caller " * 160,
            local_sip_port=0, local_rtp_port=41000,
            supported_formats=[audio_format.AudioFormat(16000)],
            signaling_transport=transport,
        )
        self.addAsyncCleanup(client.close)
        return client

    async def invite(self, client, port, *, timeout=1):
        uri = f"sip:panel@127.0.0.1:{port};line=preserved;transport={client.signaling_transport.lower()}"
        result = await client.invite(target="panel", remote_host="127.0.0.1",
            remote_sip_port=port, request_uri=uri, timeout=timeout)
        return result, uri

    async def test_oversize_invite_connect_timeout_reaches_original_udp_peer(self):
        for failure in (TimeoutError(), TimeoutError(errno.ETIMEDOUT, "connect timed out")):
            with self.subTest(failure=repr(failure)):
                port, received = await self.make_udp_peer()
                client = self.make_client()
                attempts = []
                before_connect = []

                async def connect(host, target_port, **kwargs):
                    attempts.append((host, target_port))
                    before_connect.append(sip.parse_message(client._build_pending_invite()))
                    raise failure

                try:
                    with patch.object(asyncio, "open_connection", connect):
                        result, uri = await self.invite(client, port)
                    self.assertEqual(result, "busy")
                    self.assertEqual(attempts, [("127.0.0.1", port)])
                    invites = [(message, raw, address) for message, raw, address in received
                               if message.method == "INVITE"]
                    self.assertEqual(len(invites), 1)
                    message, raw, address = invites[0]
                    self.assertGreater(len(raw), 1300)
                    self.assertEqual(message.uri, uri)
                    self.assertTrue(before_connect[0].body)
                    self.assertEqual(message.body, before_connect[0].body)
                    self.assertEqual(message.uri, before_connect[0].uri)
                    for name in ("Call-ID", "To", "CSeq", "Route"):
                        self.assertEqual(message.header(name), before_connect[0].header(name))
                    self.assertEqual(message.header("Call-ID"), client.dialog_ids.call_id)
                    self.assertEqual(message.header("CSeq"), "1 INVITE")
                    self.assertIn("SIP/2.0/UDP", message.header("Via"))
                    self.assertIn("transport=udp", message.header("Contact"))
                    self.assertEqual(address[1], client.local_sip_port)
                finally:
                    await client.close()
                self.assertIsNone(client.transport)
                self.assertIsNone(client.writer)
                self.assertIsNone(client.reader)
                self.assertIsNone(client._tcp_writer)
                self.assertIsNone(client._dialog_dispatcher)

    async def test_explicit_tcp_tls_permission_and_cancel_never_send_udp(self):
        for mode in ("TCP", "TLS", "permission", "cancel"):
            with self.subTest(mode=mode):
                port, received = await self.make_udp_peer()
                client = self.make_client(transport=mode if mode in {"TCP", "TLS"} else "UDP")
                if mode == "TLS":
                    client.tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

                async def connect(*args, **kwargs):
                    if mode == "cancel":
                        self.assertTrue(client.request_cancel())
                    if mode == "permission":
                        raise PermissionError(errno.EACCES, "permission denied")
                    raise TimeoutError()

                with patch.object(asyncio, "open_connection", connect):
                    result, _ = await self.invite(client, port)
                self.assertEqual(result, "cancelled" if mode == "cancel" else "transport_unreachable")
                await client.close()
                self.assertFalse(received)
                self.assertIsNone(client._dialog_dispatcher)
                self.assertIsNone(client.writer)

    async def test_existing_invite_connect_deadline_cancels_connect_before_udp_fallback(self):
        port, received = await self.make_udp_peer()
        client = self.make_client()
        cancelled = asyncio.Event()

        async def connect(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch.object(asyncio, "open_connection", connect):
            result, _ = await self.invite(client, port, timeout=.1)
        self.assertEqual(result, "busy")
        self.assertTrue(cancelled.is_set())
        self.assertEqual(sum(message.method == "INVITE" for message, _, _ in received), 1)

    async def test_upgrade_dns_timeout_is_not_a_tcp_connect_timeout(self):
        port, received = await self.make_udp_peer()
        client = self.make_client()
        resolve = client.target_resolver.resolve

        async def dns_timeout(target, *, transport):
            if transport == "TCP":
                raise TimeoutError("resolver deadline")
            return await resolve(target, transport=transport)

        with patch.object(client.target_resolver, "resolve", dns_timeout), \
                patch.object(asyncio, "open_connection") as connect:
            result, _ = await self.invite(client, port)
        self.assertEqual(result, "transport_unreachable")
        connect.assert_not_called()
        self.assertFalse(received)

    async def test_mixed_timeout_and_permission_candidates_do_not_fallback(self):
        port, received = await self.make_udp_peer()
        client = self.make_client()
        resolve = client.target_resolver.resolve

        async def candidates(target, *, transport):
            if transport == "TCP":
                return (sip_resolution.SipServerTarget("127.0.0.1", port, "TCP",
                    ("127.0.0.2", "127.0.0.1")),)
            return await resolve(target, transport=transport)

        attempts = []

        async def connect(host, target_port, **kwargs):
            attempts.append(host)
            if host == "127.0.0.2":
                raise TimeoutError()
            raise PermissionError(errno.EACCES, "permission denied")

        with patch.object(client.target_resolver, "resolve", candidates), \
                patch.object(asyncio, "open_connection", connect):
            result, _ = await self.invite(client, port)
        self.assertEqual(result, "transport_unreachable")
        self.assertEqual(attempts, ["127.0.0.2", "127.0.0.1"])
        self.assertFalse(received)

    async def test_second_tcp_candidate_is_used_after_first_connect_timeout(self):
        port, received_udp = await self.make_udp_peer()
        client = self.make_client()
        resolve = client.target_resolver.resolve
        real_connect = asyncio.open_connection
        received_tcp, handlers, writers = [], set(), []

        async def candidates(target, *, transport):
            if transport == "TCP":
                return (sip_resolution.SipServerTarget("127.0.0.1", port, "TCP",
                    ("127.0.0.2", "127.0.0.1")),)
            return await resolve(target, transport=transport)

        async def connect(host, target_port, **kwargs):
            if host == "127.0.0.2":
                raise TimeoutError()
            return await real_connect(host, target_port, **kwargs)

        async def peer(reader, writer):
            handlers.add(asyncio.current_task())
            writers.append(writer)
            try:
                raw = await sip_listener._read_sip_stream_message(reader)
                message = sip.parse_message(raw)
                received_tcp.append(message)
                headers = [(key, message.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
                headers.append(("To", message.header("To") + ";tag=tcp-peer"))
                writer.write(sip.build_response(486, "Busy Here", headers))
                await writer.drain()
                await reader.read()
            finally:
                handlers.discard(asyncio.current_task())
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(peer, "127.0.0.1", port)
        try:
            with patch.object(client.target_resolver, "resolve", candidates), \
                    patch.object(asyncio, "open_connection", connect):
                result, uri = await self.invite(client, port)
            self.assertEqual(result, "busy")
            self.assertFalse(received_udp)
            self.assertEqual(len(received_tcp), 1)
            self.assertEqual(received_tcp[0].uri, uri)
            self.assertEqual(received_tcp[0].body, client._pending_invite_body)
            self.assertIn("SIP/2.0/TCP", received_tcp[0].header("Via"))
        finally:
            await client.close()
            for writer in writers:
                writer.close()
            await asyncio.gather(*handlers, return_exceptions=True)
            server.close()
            await server.wait_closed()

    async def test_oversize_notify_connect_timeout_reaches_udp_and_releases_owners(self):
        for failure in (TimeoutError(), TimeoutError(errno.ETIMEDOUT, "connect timed out")):
            with self.subTest(failure=repr(failure)):
                port, received = await self.make_udp_peer()
                manager = sip_endpoint.SipEndpointManager(host="127.0.0.1", port=0,
                    local_ip="127.0.0.1", local_rtp_port=41000,
                    supported_formats=[audio_format.AudioFormat(16000)], on_invite=lambda _: None)
                self.assertTrue(await manager.start())
                self.addAsyncCleanup(manager.stop)
                tcp_server = manager.tcp_server
                request = sip.parse_message(sip.build_request("SUBSCRIBE", "sip:desk@127.0.0.1", [
                    ("From", "<sip:watcher@127.0.0.1>;tag=watcher"),
                    ("To", "<sip:desk@127.0.0.1>"),
                    ("Contact", f"<sip:watcher@127.0.0.1:{port};transport=udp>"),
                    ("Call-ID", "connect-timeout-subscription"), ("CSeq", "1 SUBSCRIBE")]))
                state = SubscriptionDialog.from_request(request, "notifier")
                body = b"presence-state" * 150
                result = sip_listener.SipRequestResult(to_tag="notifier", follow_up_dialog=state,
                    follow_up=sip_listener.SipFollowUpRequest(method="NOTIFY", cseq=state.next_cseq(),
                        headers=(("Event", "presence"), ("Subscription-State", "active;expires=60")),
                        body=body))

                async def connect(*args, **kwargs):
                    raise failure

                try:
                    with patch.object(asyncio, "open_connection", connect):
                        response = await manager.udp_server.endpoint._send_application_follow_up(
                            request, ("127.0.0.1", port), result)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(len(received), 1)
                    message, raw, _ = received[0]
                    self.assertGreater(len(raw), 1300)
                    self.assertEqual(message.body, body)
                    self.assertEqual(message.header("Call-ID"), "connect-timeout-subscription")
                    self.assertEqual(message.uri, f"sip:watcher@127.0.0.1:{port};transport=udp")
                    self.assertIn("SIP/2.0/UDP", message.header("Via"))
                    self.assertFalse(manager.udp_server.endpoint._client_transaction_responses)
                    self.assertFalse(manager.tcp_server.endpoint._client_transaction_responses)
                finally:
                    await manager.stop()
                self.assertFalse(tcp_server._writers)
                self.assertFalse(tcp_server._tcp_writers)
                self.assertFalse(tcp_server._client_tasks)
