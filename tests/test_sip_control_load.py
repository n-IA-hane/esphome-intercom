"""Control transactions remain serviceable while ordinary routing is saturated."""

from .test_refer_correlation import Peer
from .voip_phase1_support import asyncio, audio_format, sdp, sip, sip_listener, sip_transfer, unittest


class ControlLoadTest(unittest.IsolatedAsyncioTestCase):
    async def test_control_progress_with_twenty_four_blocked_routes(self):
        for signaling in ("UDP", "TCP"):
            with self.subTest(signaling=signaling):
                await self.exercise(signaling)

    async def exercise(self, signaling):
        loop = asyncio.get_running_loop()
        release = asyncio.Event()
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        offer = sdp.build_offer_directional("127.0.0.1", "127.0.0.1", 45000, [fmt], [fmt]).encode()

        async def route(invite):
            if invite.call_id.startswith("load-"):
                await release.wait()
                return sip_listener.SipInviteResult(486, "Busy Here")
            if invite.call_id == "early":
                return sip_listener.SipInviteResult(180, "Ringing", defer_final=True)
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=sdp.build_answer_directional(
                "127.0.0.1", "127.0.0.1", 41000, invite.send_format, invite.recv_format,
                recv_formats=invite.recv_formats, dtmf_direction=invite.dtmf_direction,
                remote_sdp=invite.remote_sdp))

        server = None
        pump = None
        if signaling == "UDP":
            peer_transport, peer = await loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
            owner = sip_listener.SipUdpEndpoint(local_ip="127.0.0.1", local_sip_port=0,
                local_rtp_port=41000, supported_formats=[fmt], on_invite=route)
            transport, _ = await loop.create_datagram_endpoint(lambda: owner, local_addr=("127.0.0.1", 0))
            address = transport.get_extra_info("sockname")[:2]
        else:
            server = sip_listener.SipTcpServer(host="127.0.0.1", port=0, local_ip="127.0.0.1",
                local_rtp_port=41000, supported_formats=[fmt], on_invite=route)
            self.assertTrue(await server.start())
            address = server.server.sockets[0].getsockname()[:2]
            reader, writer = await asyncio.open_connection(*address)
            class StreamTransport:
                def sendto(self, raw, _address):
                    writer.write(raw)
                def get_extra_info(self, key):
                    return writer.get_extra_info(key)
                def close(self):
                    writer.close()
            peer_transport = StreamTransport()
            peer = Peer()
            peer.connection_made(peer_transport)
            async def receive_stream():
                while (raw := await sip_listener._read_sip_stream_message(reader)) is not None:
                    peer.datagram_received(raw, address)
            pump = asyncio.create_task(receive_stream())
            owner = server.endpoint
        peer_port = peer_transport.get_extra_info("sockname")[1]
        owner.local_sip_port = address[1]
        transfer = None

        def request(method, call_id, cseq=1, tag="", extra=()):
            headers = [
                ("Via", f"SIP/2.0/{signaling} 127.0.0.1:{peer_port};branch=z9hG4bK{call_id}-{cseq}"),
                ("From", "<sip:peer@127.0.0.1>;tag=peer"),
                ("To", "<sip:HA@127.0.0.1>" + (f";tag={tag}" if tag else "")),
                ("Contact", f"<sip:peer@127.0.0.1:{peer_port}>"),
                ("Call-ID", call_id), ("CSeq", f"{cseq} {method}"), *extra]
            body = offer if method == "INVITE" else b""
            if body:
                headers.append(("Content-Type", "application/sdp"))
            return sip.build_request(method, f"sip:HA@127.0.0.1:{address[1]}", headers, body)

        async def response(call_id, cseq, status):
            async with asyncio.timeout(2):
                while True:
                    message, _ = await peer.messages.get()
                    if (message.header("Call-ID") == call_id and message.header("CSeq") == cseq
                            and message.status_code == status):
                        return message

        try:
            peer_transport.sendto(request("INVITE", "active"), address)
            accepted = await response("active", "1 INVITE", 200)
            tag = sip.extract_tag(accepted.header("To"))
            peer_transport.sendto(request("ACK", "active", tag=tag), address)
            peer_transport.sendto(request("INVITE", "early", extra=(("Supported", "100rel"),)), address)
            ringing = await response("early", "1 INVITE", 180)
            early_tag = sip.extract_tag(ringing.header("To"))
            transfer = asyncio.create_task(owner.async_refer("active", sip_transfer.SipReferTarget("sip:next@127.0.0.1"), timeout=3))
            refer, refer_addr = await peer.receive("REFER")
            for index in range(24):
                call_id = f"load-{index}"
                peer_transport.sendto(request("INVITE", call_id), address)
                await response(call_id, "1 INVITE", 100)
            self.assertEqual(len(owner._request_tasks), 24)
            self.assertEqual(len(owner._invite_tasks), 24)

            # The real transaction consumes its response without an additional handler.
            peer.answer(refer, refer_addr, 200)
            refer_cseq = sip.parse_cseq(refer.header("CSeq")).number
            peer.notify(refer, refer_addr, 10, f"refer;id={refer_cseq}")
            await response("active", "10 NOTIFY", 200)
            self.assertTrue((await transfer).accepted)
            self.assertEqual(len(owner._request_tasks), 24)

            peer_transport.sendto(request("PRACK", "early", 2, early_tag,
                (("RAck", f'{ringing.header("RSeq")} 1 INVITE'),)), address)
            await response("early", "2 PRACK", 200)
            self.assertNotIn("early", owner._reliable_provisionals)
            peer_transport.sendto(request("BYE", "active", 11, tag), address)
            await response("active", "11 BYE", 200)
            self.assertNotIn("active", owner.active_dialogs)
            peer_transport.sendto(request("CANCEL", "load-0"), address)
            await response("load-0", "1 CANCEL", 200)
            self.assertTrue(owner.pending_invites.get("load-0") is None)
            self.assertEqual(owner.dropped_datagrams, 0)
        finally:
            release.set()
            if transfer is not None:
                transfer.cancel()
                await asyncio.gather(transfer, return_exceptions=True)
            await asyncio.gather(*tuple(owner._request_tasks), return_exceptions=True)
            peer_transport.close()
            if server is not None:
                await server.stop()
                await writer.wait_closed()
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
            else:
                transport.close()
                await owner.wait_closed()
