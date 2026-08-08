#!/usr/bin/env python3
"""SIP over TCP runtime contracts."""

from __future__ import annotations

from .voip_phase1_support import (
    _reserved_udp_ports,
    asyncio,
    audio_format,
    patch,
    sdp,
    sip,
    sip_auth,
    sip_client,
    sip_listener,
    sip_registrar,
    sip_runtime,
    types,
    unittest,
)


class SipTcpProfileTest(unittest.IsolatedAsyncioTestCase):
    async def test_tcp_listener_answers_rfc5626_crlf_keepalive(self) -> None:
        local = "127.0.0.1"
        with _reserved_udp_ports(2) as ports:
            sip_port, rtp_port = ports
        server = sip_listener.SipTcpServer(
            host=local,
            port=sip_port,
            local_ip=local,
            local_rtp_port=rtp_port,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
        )
        self.assertTrue(await server.start())
        _reader, writer = await asyncio.open_connection(local, sip_port)
        try:
            writer.write(b"\r\n\r\n")
            await writer.drain()
            self.assertEqual(
                await asyncio.wait_for(_reader.readexactly(2), timeout=0.5),
                b"\r\n",
            )
            self.assertEqual(len(server.endpoints), 1)
        finally:
            writer.close()
            await writer.wait_closed()
            await server.stop()

    async def test_tcp_listener_caps_connections_per_source(self) -> None:
        local = "127.0.0.1"
        with _reserved_udp_ports(2) as ports:
            sip_port, rtp_port = ports
        server = sip_listener.SipTcpServer(
            host=local,
            port=sip_port,
            local_ip=local,
            local_rtp_port=rtp_port,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            max_connections=2,
            max_connections_per_host=1,
            initial_message_timeout=1.0,
            frame_timeout=1.0,
        )
        self.assertTrue(await server.start())
        first_reader, first_writer = await asyncio.open_connection(local, sip_port)
        del first_reader
        first_writer.write(b"I")
        await first_writer.drain()
        for _ in range(20):
            if server.endpoints:
                break
            await asyncio.sleep(0.005)
        second_reader, second_writer = await asyncio.open_connection(local, sip_port)
        try:
            self.assertEqual(
                await asyncio.wait_for(second_reader.read(1), timeout=0.2),
                b"",
            )
            self.assertEqual(len(server.endpoints), 1)
        finally:
            first_writer.close()
            second_writer.close()
            await first_writer.wait_closed()
            await second_writer.wait_closed()
            await server.stop()

    async def test_tcp_listener_accepts_pcm_invite(self) -> None:
        local = "127.0.0.1"
        with _reserved_udp_ports(2) as ports:
            sip_port, rtp_port = ports
        audio = audio_format.AudioFormat(16000, "s16le", 1, 32)
        seen = {"invite": False}

        async def on_invite(invite):
            seen["invite"] = True
            answer = sdp.build_answer_directional(
                local,
                local,
                rtp_port,
                invite.send_format,
                invite.recv_format,
            )
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer)

        server = sip_listener.SipTcpServer(
            host=local,
            port=sip_port,
            local_ip=local,
            local_rtp_port=rtp_port,
            supported_formats=[audio],
            on_invite=on_invite,
        )
        self.assertTrue(await server.start())
        reader, writer = await asyncio.open_connection(local, sip_port)
        try:
            body = sdp.build_offer(local, local, rtp_port + 2, [audio]).encode()
            raw = sip.build_request(
                "INVITE",
                f"sip:HA@{local}:{sip_port}",
                [
                    ("Via", f"SIP/2.0/TCP {local}:43210;branch=z9hG4bKtcp;rport"),
                    ("From", f"<sip:ESP@{local}:43210>;tag=src"),
                    ("To", f"<sip:HA@{local}:{sip_port}>"),
                    ("Call-ID", "tcp-call-1"),
                    ("CSeq", "1 INVITE"),
                    ("Contact", f"<sip:ESP@{local}:43210>"),
                    ("Content-Type", "application/sdp"),
                    ("X-Voip-Stack-Caller-Name", "ESP"),
                    ("X-Voip-Stack-Dest-Name", "HA"),
                ],
                body,
            )
            writer.write(raw)
            await writer.drain()
            first_raw = await sip_listener._read_sip_stream_message(reader)
            second_raw = await sip_listener._read_sip_stream_message(reader)
            assert first_raw is not None and second_raw is not None
            first = sip.parse_message(first_raw)
            second = sip.parse_message(second_raw)
            statuses = {first.status_code, second.status_code}
            self.assertEqual(statuses, {100, 200})
            final = first if first.status_code == 200 else second
            self.assertIn(b"m=audio", final.body)
            self.assertTrue(seen["invite"])
        finally:
            writer.close()
            await writer.wait_closed()
            await server.stop()

    async def test_tcp_register_flow_can_carry_outbound_dahua_dialog(self) -> None:
        """A registered TCP flow is reusable for a new HA-originated dialog."""

        local = "127.0.0.1"
        with _reserved_udp_ports(3) as ports:
            sip_port, ha_rtp_port, dahua_rtp_port = ports
        audio = audio_format.AudioFormat(16000, "s16le", 1, 20)
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("100", "Dahua VTO", "secret")],
            local_ip=local,
            local_sip_port=sip_port,
        )

        async def unexpected_invite(_invite):
            raise AssertionError("registered client must receive, not originate, INVITE")

        server = sip_listener.SipTcpServer(
            host=local,
            port=sip_port,
            local_ip=local,
            local_rtp_port=ha_rtp_port,
            supported_formats=[audio],
            on_invite=unexpected_invite,
            on_register=registrar.handle_register,
        )
        self.assertTrue(await server.start())
        reader, writer = await asyncio.open_connection(local, sip_port)
        source = writer.get_extra_info("sockname")
        assert source is not None
        source_addr = (str(source[0]), int(source[1]))
        request_uri = f"sip:{local}:{sip_port}"
        base_headers = [
            (
                "Via",
                f"SIP/2.0/TCP {local}:{source_addr[1]};"
                "branch=z9hG4bKtcp-register;rport",
            ),
            ("From", "<sip:100@VDP>;tag=dahua-register"),
            ("To", "<sip:100@VDP>"),
            ("Call-ID", "dahua-tcp-register"),
            ("CSeq", "1 REGISTER"),
            (
                "Contact",
                "<sip:100@10.0.0.85:5060;transport=tcp>",
            ),
            ("Expires", "120"),
            ("User-Agent", "Dahua UAC/3.0"),
        ]

        async def send_and_read(raw: bytes) -> sip.SipMessage:
            writer.write(raw)
            await writer.drain()
            response = await asyncio.wait_for(
                sip_listener._read_sip_stream_message(reader),
                timeout=1,
            )
            assert response is not None
            return sip.parse_message(response)

        try:
            challenge = await send_and_read(
                sip.build_request("REGISTER", request_uri, base_headers, b"")
            )
            self.assertEqual(challenge.status_code, 401)
            authorization = sip_auth.build_digest_authorization(
                challenge_header=challenge.header("WWW-Authenticate"),
                username="100",
                password="secret",
                method="REGISTER",
                uri=request_uri,
            )
            registered = await send_and_read(
                sip.build_request(
                    "REGISTER",
                    request_uri,
                    [*base_headers, ("Authorization", authorization)],
                    b"",
                )
            )
            self.assertEqual(registered.status_code, 200)
            binding = registrar.registered_contacts("100")[0]
            self.assertEqual(
                (binding.source_host, binding.source_port, binding.transport),
                (source_addr[0], source_addr[1], "TCP"),
            )

            client = sip_client.SipCallClient(
                local_ip=local,
                local_name="HA-Test",
                local_sip_port=sip_port,
                local_rtp_port=ha_rtp_port,
                supported_formats=[audio],
                signaling_transport="TCP",
                include_common_codecs=True,
                peer_user_agent=binding.user_agent,
            )
            hass = types.SimpleNamespace()
            with patch.object(
                sip_runtime,
                "sip_endpoint_manager",
                return_value=types.SimpleNamespace(tcp_server=server),
            ):
                self.assertTrue(
                    sip_runtime.enable_reused_tcp_connection(
                        hass,
                        client,
                        sip.parse_sip_uri(binding.contact_uri),
                        target="Dahua VTO",
                        default_sip_port=sip_port,
                    )
                )

            async def dahua_answer() -> None:
                raw_invite = await asyncio.wait_for(
                    sip_listener._read_sip_stream_message(reader),
                    timeout=1,
                )
                assert raw_invite is not None
                invite = sip.parse_message(raw_invite)
                self.assertEqual(invite.method, "INVITE")
                offered = sdp.offered_pcm_formats(
                    invite.body,
                    allow_dahua_pcm=True,
                )
                selected = next(item for item in offered if item.encoding == "PCM")
                answer = sdp.build_answer_directional(
                    local,
                    local,
                    dahua_rtp_port,
                    selected,
                    selected,
                    remote_sdp=invite.body,
                ).encode()
                contact_uri = f"sip:100@{local}:{source_addr[1]};transport=tcp"
                top_via = sip_listener._response_via_header(
                    invite,
                    source_addr,
                )
                writer.write(
                    sip.build_uas_response(
                        invite,
                        180,
                        "Ringing",
                        to_tag="dahua-call",
                        top_via=top_via,
                    )
                )
                writer.write(
                    sip.build_uas_response(
                        invite,
                        200,
                        "OK",
                        contact_uri=contact_uri,
                        to_tag="dahua-call",
                        top_via=top_via,
                        body=answer,
                    )
                )
                await writer.drain()

            answer_task = asyncio.create_task(dahua_answer())
            result = await client.invite(
                target="100",
                remote_host=source_addr[0],
                remote_sip_port=source_addr[1],
                request_uri=(
                    f"sip:100@{source_addr[0]}:{source_addr[1]};transport=tcp"
                ),
            )
            if result == "ringing":
                result = await client.wait_for_final()
            await answer_task

            self.assertEqual(result, "in_call")
            self.assertIsNotNone(client.dialog)
            assert client.dialog is not None
            self.assertEqual(client.dialog.send_format.encoding, "PCM")
            self.assertEqual(client.dialog.recv_format.encoding, "PCM")
            client.bye()
            await client.close()
            self.assertEqual(server._dialog_queues, {})
        finally:
            writer.close()
            await writer.wait_closed()
            await server.stop()

    async def test_tcp_dialog_survives_connection_replacement_for_reinvite(self) -> None:
        """An in-dialog offer may arrive on a new RFC 3261 TCP connection."""

        local = "127.0.0.1"
        with _reserved_udp_ports(2) as ports:
            sip_port, rtp_port = ports
        audio = audio_format.AudioFormat(16000, "s16le", 1, 20)
        updates: list[tuple[int, str]] = []

        def answer(invite):
            return sdp.build_answer_directional(
                local,
                local,
                rtp_port,
                invite.send_format,
                invite.recv_format,
                remote_sdp=invite.remote_sdp,
                video_port=rtp_port + 2,
                video_format=invite.answer_video_format,
                video_direction=sdp.local_direction_for_offer(
                    invite.video_format.direction
                    if invite.video_format is not None
                    else "inactive"
                ),
            )

        async def on_invite(invite):
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer(invite))

        async def on_media_update(_previous, updated, _method):
            updates.append((updated.remote_rtp_port, updated.video_format.direction))
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer(updated))

        server = sip_listener.SipTcpServer(
            host=local,
            port=sip_port,
            local_ip=local,
            local_rtp_port=rtp_port,
            supported_formats=[audio],
            on_invite=on_invite,
            on_media_update=on_media_update,
            enable_video=True,
            max_connections_per_host=2,
        )
        self.assertTrue(await server.start())

        def invite(
            cseq: int,
            branch: str,
            remote_rtp: int,
            to_tag: str = "",
            video_direction: str = "recvonly",
        ) -> bytes:
            headers = [
                ("Via", f"SIP/2.0/TCP {local}:43210;branch={branch};rport"),
                ("From", f"<sip:Wildix@{local}:43210>;tag=remote"),
                ("To", f"<sip:HA@{local}:{sip_port}>" + (f";tag={to_tag}" if to_tag else "")),
                ("Call-ID", "tcp-reconnect-reinvite"),
                ("CSeq", f"{cseq} INVITE"),
                ("Contact", f"<sip:Wildix@{local}:43210>"),
                ("Content-Type", "application/sdp"),
            ]
            body = sdp.build_offer_directional(
                local,
                local,
                remote_rtp,
                [audio],
                [audio],
                video_port=remote_rtp + 2,
                video_formats=sdp.DEFAULT_VIDEO_FORMATS,
                video_direction=video_direction,
            ).encode()
            return sip.build_request("INVITE", f"sip:HA@{local}:{sip_port}", headers, body)

        first_reader, first_writer = await asyncio.open_connection(local, sip_port)
        second_writer = None
        try:
            first_writer.write(invite(1, "z9hG4bKinitial", 41000))
            await first_writer.drain()
            responses = []
            while 200 not in [item.status_code for item in responses]:
                raw = await asyncio.wait_for(
                    sip_listener._read_sip_stream_message(first_reader), timeout=1
                )
                assert raw is not None
                responses.append(sip.parse_message(raw))
            final = next(item for item in responses if item.status_code == 200)
            local_tag = sip.extract_tag(final.header("To"))
            self.assertTrue(local_tag)

            first_writer.close()
            await first_writer.wait_closed()
            await asyncio.sleep(0)

            second_reader, second_writer = await asyncio.open_connection(local, sip_port)
            second_writer.write(
                invite(
                    2,
                    "z9hG4bKvideo-on",
                    42000,
                    local_tag,
                    video_direction="sendrecv",
                )
            )
            await second_writer.drain()
            responses = []
            while 200 not in [item.status_code for item in responses]:
                raw = await asyncio.wait_for(
                    sip_listener._read_sip_stream_message(second_reader), timeout=1
                )
                assert raw is not None
                responses.append(sip.parse_message(raw))
            self.assertEqual(updates, [(42000, "sendrecv")])
            self.assertEqual(len(server.endpoint.active_dialogs), 1)
        finally:
            if second_writer is not None:
                second_writer.close()
                await second_writer.wait_closed()
            if not first_writer.is_closing():
                first_writer.close()
                await first_writer.wait_closed()
            await server.stop()

    async def test_tcp_client_establishes_pcm_dialog(self) -> None:
        local = "127.0.0.1"
        with _reserved_udp_ports(3) as ports:
            sip_port, server_rtp, client_rtp = ports
        audio = audio_format.AudioFormat(16000, "s16le", 1, 32)

        async def on_invite(invite):
            answer = sdp.build_answer_directional(
                local,
                local,
                server_rtp,
                invite.send_format,
                invite.recv_format,
            )
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer)

        server = sip_listener.SipTcpServer(
            host=local,
            port=sip_port,
            local_ip=local,
            local_rtp_port=server_rtp,
            supported_formats=[audio],
            on_invite=on_invite,
        )
        self.assertTrue(await server.start())
        client = sip_client.SipCallClient(
            local_ip=local,
            local_name="Casa",
            local_sip_port=5060,
            local_rtp_port=client_rtp,
            supported_formats=[audio],
            signaling_transport="TCP",
        )
        try:
            self.assertEqual(
                await client.invite(target="ESP", remote_host=local, remote_sip_port=sip_port),
                "in_call",
            )
            self.assertIsNotNone(client.dialog)
            assert client.dialog is not None
            self.assertEqual(client.dialog.remote_rtp_port, server_rtp)
            self.assertNotEqual(client.local_sip_port, 5060)
        finally:
            client.bye()
            await client.close()
            await server.stop()
