#!/usr/bin/env python3
"""SIP protocol regression and asynchronous transaction contracts."""

from __future__ import annotations

from .voip_phase1_support import (
    Path,
    _load_intercom_module,
    asyncio,
    audio_format,
    contextlib,
    dtmf,
    patch,
    sdp,
    sip,
    sip_auth,
    sip_client,
    sip_listener,
    sip_transfer,
    sip_rtp_bridge,
    sip_resolution,
    sip_tcp_io,
    sip_trunk,
    socket,
    tempfile,
    types,
    unittest,
)


class SipProtocolBugFixTest(unittest.TestCase):
    def test_tls_via_round_trip(self) -> None:
        ids = sip.SipDialogIds("call", "local", branch="z9hG4bKtls")
        headers = sip.dialog_headers(
            request_uri="sips:peer@example.test",
            local_uri="sips:ha@192.0.2.10:5061;transport=tls",
            remote_uri="sips:peer@example.test",
            dialog=ids,
            method="INVITE",
            contact_uri="sips:ha@192.0.2.10:5061;transport=tls",
            transport="TLS",
        )

        via = sip.parse_via(dict(headers)["Via"])

        self.assertEqual(via.transport, "TLS")
        self.assertEqual(via.branch, "z9hG4bKtls")

    def test_message_is_advertised_as_supported_application_method(self) -> None:
        self.assertIn("MESSAGE", sip.SUPPORTED_METHODS)
        self.assertNotIn("MESSAGE", sip.KNOWN_UNSUPPORTED_METHODS)
        self.assertIn("PUBLISH", sip.SUPPORTED_METHODS)
        self.assertNotIn("PUBLISH", sip.KNOWN_UNSUPPORTED_METHODS)

    def test_refer_target_round_trip_preserves_attended_dialog(self) -> None:
        target = sip_transfer.SipReferTarget(
            "sip:desk@example.test",
            sip_transfer.SipReplaces(
                "call@example.test",
                to_tag="remote",
                from_tag="local",
                early_only=True,
            ),
        )

        self.assertEqual(sip_transfer.parse_refer_to(target.as_header()), target)
        self.assertEqual(
            sip_transfer.parse_sipfrag_status(b"SIP/2.0 200 OK\r\n"),
            200,
        )

    def test_refer_target_rejects_incomplete_replaces(self) -> None:
        with self.assertRaises(sip.SipError):
            sip_transfer.parse_refer_to(
                "<sip:desk@example.test?Replaces=call%3Bto-tag%3Dremote>"
            )

    def test_session_timer_headers_are_strict_and_typed(self) -> None:
        self.assertEqual(
            sip.parse_session_expires("1800;refresher=uas"),
            sip.SipSessionExpires(1800, "uas"),
        )
        self.assertEqual(
            sip.parse_session_expires("90"),
            sip.SipSessionExpires(90),
        )
        for invalid in ("", "0", "abc", "90;refresher=peer", "90;refresher=uac;refresher=uas"):
            with self.subTest(invalid=invalid), self.assertRaises(sip.SipError):
                sip.parse_session_expires(invalid)

    def test_session_timer_negotiation_selects_refresher_and_minimum(self) -> None:
        request = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:phone@pbx.local",
                [
                    ("Supported", "timer"),
                    ("Session-Expires", "1800"),
                    ("Min-SE", "120"),
                ],
            )
        )
        self.assertEqual(
            sip.negotiate_uas_session_timer(request),
            sip.SipSessionExpires(1800, "uac"),
        )
        self.assertEqual(
            sip.session_timer_response_headers(request),
            (
                ("Require", "timer"),
                ("Session-Expires", "1800;refresher=uac"),
            ),
        )
        legacy_request = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:phone@pbx.local",
                [("Session-Expires", "1800")],
            )
        )
        self.assertEqual(
            sip.session_timer_response_headers(legacy_request),
            (("Session-Expires", "1800;refresher=uas"),),
        )
        too_short = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:phone@pbx.local",
                [("Supported", "timer"), ("Session-Expires", "90"), ("Min-SE", "120")],
            )
        )
        with self.assertRaises(sip.SipSessionIntervalTooSmall) as raised:
            sip.negotiate_uas_session_timer(too_short)
        self.assertEqual(raised.exception.minimum, 120)

    def test_rack_parser_preserves_case_sensitive_method(self) -> None:
        self.assertEqual(
            sip.parse_rack("776656 1 INVITE"),
            sip.SipRAck(776656, 1, "INVITE"),
        )
        for invalid in ("", "0 1 INVITE", "1 -1 INVITE", "1 1 bad method"):
            with self.subTest(invalid=invalid), self.assertRaises(sip.SipError):
                sip.parse_rack(invalid)

    def test_required_option_tags_are_checked_as_a_set(self) -> None:
        request = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:phone@pbx.local",
                [
                    ("Require", "100rel"),
                    ("Require", "timer, from-change"),
                ],
            )
        )

        self.assertEqual(
            sip.unsupported_required_options(request),
            (),
        )

    def test_dialog_headers_advertise_connected_identity_support(self) -> None:
        headers = sip.dialog_headers(
            request_uri="sip:427@192.0.2.20:5060",
            local_uri="sip:Casa@192.0.2.10:5060",
            remote_uri="sip:427@192.0.2.20:5060",
            dialog=sip.SipDialogIds(call_id="identity", local_tag="local"),
            method="INVITE",
            contact_uri="sip:Casa@192.0.2.10:5060",
        )
        message = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:427@192.0.2.20:5060",
                headers,
            )
        )

        self.assertTrue(sip.supports_option(message, "from-change"))
        self.assertEqual(
            sip.option_tags(message),
            frozenset({"100rel", "from-change", "replaces", "timer"}),
        )

    def test_dtmf_collector_emits_one_digit_per_event(self) -> None:
        digits: list[str] = []
        proto = dtmf._DtmfProtocol(101, digits.append, remote_host="127.0.0.1")

        def packet(*, sequence: int, timestamp: int, ended: bool, ssrc: bytes = b"ssrc") -> bytes:
            header = bytearray(12)
            header[0] = 0x80
            header[1] = 101
            header[2:4] = int(sequence).to_bytes(2, "big")
            header[4:8] = int(timestamp).to_bytes(4, "big")
            header[8:12] = ssrc
            payload = bytes([5, 0x80 if ended else 0x00, 0x00, 0xA0])
            return bytes(header) + payload

        proto.datagram_received(packet(sequence=0, timestamp=999, ended=False), ("127.0.0.2", 5000))
        proto.datagram_received(packet(sequence=1, timestamp=1234, ended=False), ("127.0.0.1", 5000))
        proto.datagram_received(packet(sequence=2, timestamp=1234, ended=True), ("127.0.0.1", 5000))
        proto.datagram_received(packet(sequence=3, timestamp=1234, ended=True), ("127.0.0.1", 5000))
        proto.datagram_received(
            packet(sequence=4, timestamp=5678, ended=False, ssrc=b"evil"),
            ("127.0.0.1", 5000),
        )
        self.assertEqual(digits, ["5"])

    def test_response_contact_uses_configured_local_sip_port(self) -> None:
        request = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:HA@192.168.1.10:9999",
                [
                    ("Via", "SIP/2.0/UDP 192.168.1.30:5060;branch=z9hG4bKx;rport"),
                    ("From", "<sip:ESP@192.168.1.30>;tag=a"),
                    ("To", "<sip:HA@192.168.1.10>"),
                    ("Call-ID", "contact-port"),
                    ("CSeq", "1 INVITE"),
                    ("Content-Length", "0"),
                ],
                b"",
            )
        )
        uri = sip_listener._response_contact_uri(
            request,
            local_ip="192.168.1.10",
            local_sip_port=5060,
            transport="UDP",
        )
        self.assertEqual(uri, "sip:HA@192.168.1.10:5060;transport=udp")

    def test_invite_error_ack_uses_invite_transaction(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

            def close(self) -> None:
                return

        client = sip_client.SipCallClient(local_ip="192.168.1.10", local_name="HA", local_sip_port=5060, local_rtp_port=41000)
        client.transport = FakeTransport()  # type: ignore[assignment]
        client.dialog_ids = sip.SipDialogIds(call_id="call-error", local_tag="ltag", cseq=3, branch="z9hG4bKorig")
        client._invite_cseq = 3
        client._pending_request_uri = "sip:ESP@192.168.1.30:5060"
        client._pending_local_uri = "sip:HA@192.168.1.10:5060"
        client._pending_remote_uri = "sip:ESP@192.168.1.30:5060"
        msg = sip.parse_message(
            sip.build_response(
                486,
                "Busy Here",
                [
                    ("Via", "SIP/2.0/UDP 192.168.1.10:5060;branch=z9hG4bKorig"),
                    ("From", "<sip:HA@192.168.1.10>;tag=ltag"),
                    ("To", "<sip:ESP@192.168.1.30>;tag=rtag"),
                    ("Call-ID", "call-error"),
                    ("CSeq", "3 INVITE"),
                ],
                b"",
            )
        )
        client._send_invite_error_ack(msg, "192.168.1.30", 5060)
        raw, addr = client.transport.sent[0]  # type: ignore[union-attr]
        parsed = sip.parse_message(raw)
        self.assertEqual(addr, ("192.168.1.30", 5060))
        self.assertEqual(parsed.method, "ACK")
        self.assertEqual(parsed.header("CSeq"), "3 ACK")
        self.assertIn("z9hG4bKorig", parsed.header("Via"))

    def test_dialog_offer_accepts_legacy_connection_hold_and_resume(self) -> None:
        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        negotiated = sdp.audio_format_to_rtp(pcm, 96)
        client = sip_client.SipCallClient(
            local_ip="192.0.2.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[pcm],
        )
        client.dialog_ids.remote_tag = "remote"
        client.dialog = sip_client.SipDialog(
            target="ESP",
            remote_host="192.0.2.20",
            remote_sip_port=5060,
            remote_rtp_host="192.0.2.20",
            remote_rtp_port=42000,
            local_rtp_port=41000,
            call_id=client.dialog_ids.call_id,
            local_uri="sip:HA@192.0.2.10:5060",
            remote_uri="sip:ESP@192.0.2.20:5060",
            send_format=negotiated,
            recv_format=negotiated,
            local_sdp_body=sdp.build_answer_directional(
                "192.0.2.10",
                "192.0.2.10",
                41000,
                negotiated,
                negotiated,
            ),
        )

        def offer(connection: str, cseq: int) -> sip.SipMessage:
            body = sdp.build_offer_directional(
                "192.0.2.20",
                connection,
                42000,
                [pcm],
                [pcm],
            )
            return sip.parse_message(
                sip.build_request(
                    "UPDATE",
                    "sip:HA@192.0.2.10:5060",
                    [
                        ("From", "<sip:ESP@192.0.2.20>;tag=remote"),
                        (
                            "To",
                            f"<sip:HA@192.0.2.10>;tag={client.dialog_ids.local_tag}",
                        ),
                        ("Call-ID", client.dialog_ids.call_id),
                        ("CSeq", f"{cseq} UPDATE"),
                        ("Content-Type", "application/sdp"),
                    ],
                    body.encode("utf-8"),
                )
            )

        held_result = client._answer_remote_offer(offer("0.0.0.0", 2))
        self.assertIsNotNone(held_result)
        assert held_result is not None
        held, held_answer = held_result
        self.assertTrue(held.remote_audio_connection_held)
        self.assertEqual(held.local_audio_direction, "recvonly")
        self.assertIn("m=audio 41000", held_answer)
        self.assertIn("a=recvonly", held_answer)

        client.dialog = held
        resumed_result = client._answer_remote_offer(offer("192.0.2.20", 3))
        self.assertIsNotNone(resumed_result)
        assert resumed_result is not None
        resumed, resumed_answer = resumed_result
        self.assertFalse(resumed.remote_audio_connection_held)
        self.assertEqual(resumed.local_audio_direction, "sendrecv")
        self.assertIn("a=sendrecv", resumed_answer)

    def test_dialog_video_reinvite_keeps_directional_levels_through_hold(self) -> None:
        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        negotiated = sdp.audio_format_to_rtp(pcm, 96)
        high = sdp.RtpVideoFormat(
            payload_type=103,
            profile_level_id="42801f",
            level_asymmetry_allowed=True,
        )
        low = sdp.RtpVideoFormat(
            payload_type=103,
            profile_level_id="42800d",
            level_asymmetry_allowed=True,
        )
        client = sip_client.SipCallClient(
            local_ip="192.0.2.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[pcm],
            local_video_rtp_port=41002,
            video_formats=(high,),
            video_direction="sendrecv",
        )
        client.dialog_ids.remote_tag = "remote"
        client.dialog = sip_client.SipDialog(
            target="ESP",
            remote_host="192.0.2.20",
            remote_sip_port=5060,
            remote_rtp_host="192.0.2.20",
            remote_rtp_port=42000,
            local_rtp_port=41000,
            call_id=client.dialog_ids.call_id,
            local_uri="sip:HA@192.0.2.10:5060",
            remote_uri="sip:ESP@192.0.2.20:5060",
            send_format=negotiated,
            recv_format=negotiated,
            video_format=high,
            local_video_format=high,
            local_video_rtp_port=41002,
            local_video_direction="sendrecv",
        )

        def offer(video_direction: str, cseq: int) -> sip.SipMessage:
            body = sdp.build_offer_directional(
                "192.0.2.20",
                "192.0.2.20",
                42000,
                [pcm],
                [pcm],
                video_port=42002,
                video_formats=(low,),
                video_direction=video_direction,
            )
            return sip.parse_message(
                sip.build_request(
                    "UPDATE",
                    "sip:HA@192.0.2.10:5060",
                    [
                        ("From", "<sip:ESP@192.0.2.20>;tag=remote"),
                        (
                            "To",
                            f"<sip:HA@192.0.2.10>;tag={client.dialog_ids.local_tag}",
                        ),
                        ("Call-ID", client.dialog_ids.call_id),
                        ("CSeq", f"{cseq} UPDATE"),
                        ("Content-Type", "application/sdp"),
                    ],
                    body.encode(),
                )
            )

        held_result = client._answer_remote_offer(offer("sendonly", 2))
        self.assertIsNotNone(held_result)
        assert held_result is not None
        held, held_answer = held_result
        self.assertEqual(held.local_video_direction, "recvonly")
        self.assertEqual(held.send_video_format.profile_level_id, "42800d")
        self.assertEqual(held.recv_video_format.profile_level_id, "42801f")
        self.assertIn("profile-level-id=42801f", held_answer)
        self.assertIn("a=recvonly", held_answer)

        client.dialog = held
        resumed_result = client._answer_remote_offer(offer("sendrecv", 3))
        self.assertIsNotNone(resumed_result)
        assert resumed_result is not None
        resumed, resumed_answer = resumed_result
        self.assertEqual(resumed.local_video_direction, "sendrecv")
        self.assertEqual(resumed.send_video_format.profile_level_id, "42800d")
        self.assertEqual(resumed.recv_video_format.profile_level_id, "42801f")
        self.assertIn("a=sendrecv", resumed_answer)


class SipProtocolBugFixAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_trunk_start_schedules_rfc_registration_without_blocking_setup(
        self,
    ) -> None:
        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="udp",
            server="127.0.0.1",
            port=5060,
            domain="fritz.box",
            username="ha",
            auth_username="ha",
            password="secret",
            expires=300,
        )
        trunk = sip_trunk.SipTrunkClient(
            config=config,
            local_ip="127.0.0.1",
            local_sip_port=5060,
        )
        registration_started = asyncio.Event()

        async def connect() -> None:
            return None

        async def register(*, expires=None, timeout=sip_trunk.SIP_TIMER_F) -> str:
            self.assertIsNone(expires)
            self.assertEqual(timeout, sip_trunk.SIP_TIMER_F)
            registration_started.set()
            await asyncio.Event().wait()
            return "registered"

        trunk._connect_udp = connect
        trunk._ensure_receive_task = lambda: None
        trunk.register = register

        await asyncio.wait_for(trunk.start(), timeout=0.1)
        await asyncio.wait_for(registration_started.wait(), timeout=0.1)
        await trunk.stop()

    @staticmethod
    def _confirmed_audio_client():
        sent: list[tuple[bytes, tuple[str, int]]] = []
        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        negotiated = sdp.audio_format_to_rtp(pcm, 96)
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[pcm],
        )
        client.transport = types.SimpleNamespace(
            sendto=lambda data, addr: sent.append((data, addr))
        )
        client.dialog_ids.remote_tag = "remote"
        initial_sdp = sdp.build_offer_directional(
            "127.0.0.1", "127.0.0.1", 41000, [pcm], [pcm]
        )
        current = sip_client.SipDialog(
            target="P4",
            remote_host="127.0.0.2",
            remote_sip_port=5060,
            remote_rtp_host="127.0.0.2",
            remote_rtp_port=42000,
            local_rtp_port=41000,
            call_id=client.dialog_ids.call_id,
            local_uri="sip:HA@127.0.0.1:5060",
            remote_uri="sip:P4@127.0.0.2:5060",
            send_format=negotiated,
            recv_format=negotiated,
            remote_target_uri="sip:P4@127.0.0.2:5060",
            local_sdp_session_id=4242,
            local_sdp_body=initial_sdp,
        )
        client.dialog = current
        return client, current, sent, negotiated

    @staticmethod
    async def _wait_for_sent_request(
        sent: list[tuple[bytes, tuple[str, int]]], method: str
    ) -> sip.SipMessage:
        for _ in range(100):
            requests = [
                sip.parse_message(raw)
                for raw, _addr in sent
                if sip.parse_message(raw).method == method
            ]
            if requests:
                return requests[-1]
            await asyncio.sleep(0)
        raise AssertionError(f"SIP {method} was not sent")

    async def test_outbound_session_refresh_uses_owned_update_transaction(self) -> None:
        client, dialog, sent, _negotiated = self._confirmed_audio_client()
        dialog.session_timer.interval = 1800
        refresh = asyncio.create_task(client._refresh_session(dialog))
        request = await self._wait_for_sent_request(sent, "UPDATE")
        self.assertEqual(request.header("Session-Expires"), "1800;refresher=uac")
        response_headers = [
            *(("Via", value) for value in request.header_values("Via")),
            ("From", request.header("From")),
            ("To", request.header("To")),
            ("Call-ID", request.header("Call-ID")),
            ("CSeq", request.header("CSeq")),
            ("Session-Expires", "900;refresher=uas"),
        ]
        client.queue.put_nowait(
            (
                sip.build_response(200, "OK", response_headers),
                ("127.0.0.2", 5060),
            )
        )

        self.assertEqual(
            await asyncio.wait_for(refresh, timeout=0.2),
            "refreshed",
        )
        self.assertEqual(dialog.session_timer.interval, 900)
        self.assertFalse(dialog.session_timer.local_refresher)
        self.assertGreater(
            dialog.session_timer.deadline,
            asyncio.get_running_loop().time(),
        )

    async def test_remote_bye_wins_over_local_session_refresh(self) -> None:
        client, dialog, sent, _negotiated = self._confirmed_audio_client()
        dialog.session_timer.interval = 1800
        refresh = asyncio.create_task(client._refresh_session(dialog))
        await self._wait_for_sent_request(sent, "UPDATE")
        client.queue.put_nowait(
            (
                self._remote_dialog_request(client, "BYE", 2),
                ("127.0.0.2", 5060),
            )
        )

        self.assertEqual(
            await asyncio.wait_for(refresh, timeout=0.2),
            "remote_hangup",
        )
        self.assertIsNone(client.dialog)
        requests = [
            message.method
            for raw, _addr in sent
            if (message := sip.parse_message(raw)).is_request
        ]
        self.assertEqual(requests, ["UPDATE"])

    async def test_successful_refresh_can_disable_session_timer(self) -> None:
        client, dialog, sent, _negotiated = self._confirmed_audio_client()
        dialog.session_timer.interval = 1800
        refresh = asyncio.create_task(client._refresh_session(dialog))
        request = await self._wait_for_sent_request(sent, "UPDATE")
        client.queue.put_nowait(
            (
                self._response_to_request(request, 200, "OK"),
                ("127.0.0.2", 5060),
            )
        )

        self.assertEqual(await asyncio.wait_for(refresh, timeout=0.2), "refreshed")
        self.assertEqual(dialog.session_timer.interval, 0)
        self.assertEqual(dialog.session_timer.deadline, 0.0)

    async def test_session_refresh_retries_422_with_minimum_immediately(self) -> None:
        client, dialog, sent, _negotiated = self._confirmed_audio_client()
        dialog.session_timer.interval = 900
        refresh = asyncio.create_task(client._refresh_session(dialog))
        first = await self._wait_for_sent_request(sent, "UPDATE")
        client.queue.put_nowait(
            (
                sip.build_response(
                    422,
                    "Session Interval Too Small",
                    [
                        *(("Via", value) for value in first.header_values("Via")),
                        ("From", first.header("From")),
                        ("To", first.header("To")),
                        ("Call-ID", first.header("Call-ID")),
                        ("CSeq", first.header("CSeq")),
                        ("Min-SE", "1800"),
                    ],
                ),
                ("127.0.0.2", 5060),
            )
        )
        for _ in range(100):
            updates = [
                message
                for raw, _addr in sent
                if (message := sip.parse_message(raw)).method == "UPDATE"
            ]
            if len(updates) == 2:
                break
            await asyncio.sleep(0)
        self.assertEqual(len(updates), 2)
        second = updates[-1]
        self.assertEqual(second.header("Session-Expires"), "1800;refresher=uac")
        self.assertNotEqual(second.header("CSeq"), first.header("CSeq"))
        client.queue.put_nowait(
            (
                sip.build_response(
                    200,
                    "OK",
                    [
                        *(("Via", value) for value in second.header_values("Via")),
                        ("From", second.header("From")),
                        ("To", second.header("To")),
                        ("Call-ID", second.header("Call-ID")),
                        ("CSeq", second.header("CSeq")),
                        ("Session-Expires", "1800;refresher=uac"),
                    ],
                ),
                ("127.0.0.2", 5060),
            )
        )

        self.assertEqual(await asyncio.wait_for(refresh, timeout=0.2), "refreshed")

    @staticmethod
    def _response_to_request(
        request: sip.SipMessage,
        status: int,
        reason: str,
        body: str = "",
    ) -> bytes:
        headers = [
            *(("Via", value) for value in request.header_values("Via")),
            ("From", request.header("From")),
            ("To", request.header("To")),
            ("Call-ID", request.header("Call-ID")),
            ("CSeq", request.header("CSeq")),
        ]
        if 200 <= status < 300:
            headers.append(("Contact", "<sip:P4@127.0.0.2:5060>"))
        if body:
            headers.append(("Content-Type", "application/sdp"))
        return sip.build_response(status, reason, headers, body.encode())

    @staticmethod
    def _remote_dialog_request(
        client: sip_client.SipCallClient,
        method: str,
        cseq: int,
        *,
        body: str = "",
    ) -> bytes:
        headers = [
            (
                "Via",
                f"SIP/2.0/UDP 127.0.0.2:5060;branch=z9hG4bK{method.lower()}{cseq}",
            ),
            ("From", "<sip:P4@127.0.0.2>;tag=remote"),
            (
                "To",
                f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}",
            ),
            ("Call-ID", client.dialog_ids.call_id),
            ("CSeq", f"{cseq} {method}"),
        ]
        if body:
            headers.append(("Content-Type", "application/sdp"))
        return sip.build_request(
            method,
            "sip:HA@127.0.0.1:5060",
            headers,
            body.encode(),
        )

    async def test_connected_identity_update_changes_remote_dialog_uri(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        negotiated = sdp.audio_format_to_rtp(fmt, 96)
        client = sip_client.SipCallClient(
            local_ip="192.0.2.10",
            local_name="Casa",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[fmt],
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client.dialog_ids.remote_tag = "remote"
        client.dialog = sip_client.SipDialog(
            target="427",
            remote_host="192.0.2.20",
            remote_sip_port=5060,
            remote_rtp_host="192.0.2.20",
            remote_rtp_port=42000,
            local_rtp_port=41000,
            call_id=client.dialog_ids.call_id,
            local_uri="sip:Casa@192.0.2.10:5060",
            remote_uri="sip:427@192.0.2.20:5060",
            send_format=negotiated,
            recv_format=negotiated,
            remote_target_uri="sip:427@192.0.2.20:5060",
            peer_supports_from_change=True,
        )
        identities: list[tuple[str, str]] = []
        client.on_connected_identity = lambda name, uri: identities.append(
            (name, uri)
        )
        update = sip.parse_message(
            sip.build_request(
                "UPDATE",
                "sip:Casa@192.0.2.10:5060",
                [
                    ("Via", "SIP/2.0/UDP 192.0.2.20:5060;branch=z9hG4bKidentity"),
                    ("From", '"Cucina" <sip:428@192.0.2.20:5060>;tag=remote'),
                    (
                        "To",
                        f'"Casa" <sip:Casa@192.0.2.10:5060>;tag={client.dialog_ids.local_tag}',
                    ),
                    ("Call-ID", client.dialog_ids.call_id),
                    ("CSeq", "2 UPDATE"),
                ],
            )
        )

        result = await client._handle_dialog_media_request(
            update,
            "192.0.2.20",
            5060,
        )

        self.assertIsNone(result)
        self.assertEqual(client.connected_party, "Cucina")
        self.assertEqual(client.dialog.remote_uri, "sip:428@192.0.2.20:5060")
        self.assertEqual(
            identities,
            [("Cucina", "sip:428@192.0.2.20:5060")],
        )
        response = sip.parse_message(transport.sent[-1][0])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(client.bye())
        bye = sip.parse_message(transport.sent[-1][0])
        self.assertEqual(
            sip.name_addr_identity(bye.header("To")),
            "Cucina",
        )
        self.assertEqual(
            str(sip.parse_sip_uri(bye.header("To"))),
            "sip:428@192.0.2.20:5060",
        )

    async def test_rtp_relay_concurrent_stop_is_single_owner_and_failure_safe(self) -> None:
        released: list[tuple[int, int]] = []
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        relay = sip_rtp_bridge.SipRtpRelay(
            left=sip_rtp_bridge.RtpPeer("127.0.0.2", 40000, 96, fmt),
            right=sip_rtp_bridge.RtpPeer("127.0.0.3", 41000, 96, fmt),
            left_port=42000,
            right_port=42002,
            on_release=released.append,
        )

        class FakeTransport:
            def __init__(self) -> None:
                self.closed = 0

            def close(self) -> None:
                self.closed += 1

        class FailingVideoRelay:
            def __init__(self) -> None:
                self.calls = 0
                self.entered = asyncio.Event()
                self.release = asyncio.Event()

            async def stop(self) -> None:
                self.calls += 1
                self.entered.set()
                await self.release.wait()
                raise OSError("video teardown failed")

        left_transport = FakeTransport()
        right_transport = FakeTransport()
        video = FailingVideoRelay()
        relay.left_transport = left_transport  # type: ignore[assignment]
        relay.right_transport = right_transport  # type: ignore[assignment]
        relay.video_relay = video

        first = asyncio.create_task(relay.stop())
        second = asyncio.create_task(relay.stop())
        await video.entered.wait()
        self.assertEqual(released, [(42000, 42002)])
        self.assertEqual(left_transport.closed, 1)
        self.assertEqual(right_transport.closed, 1)
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        self.assertFalse(first.done())
        video.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await first
        await second
        await relay.stop()

        self.assertEqual(video.calls, 1)
        self.assertEqual(released, [(42000, 42002)])
        self.assertEqual(left_transport.closed, 1)
        self.assertEqual(right_transport.closed, 1)

    async def test_rtp_relay_stop_cancels_and_joins_inflight_start(self) -> None:
        released: list[tuple[int, int]] = []
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        relay = sip_rtp_bridge.SipRtpRelay(
            left=sip_rtp_bridge.RtpPeer("127.0.0.2", 40000, 96, fmt),
            right=sip_rtp_bridge.RtpPeer("127.0.0.3", 41000, 96, fmt),
            left_port=42000,
            right_port=42002,
            on_release=released.append,
        )

        class FakeSocket:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        class FakeTransport:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        sockets = [FakeSocket(), FakeSocket()]
        transports: list[FakeTransport] = []
        entered = asyncio.Event()
        resume = asyncio.Event()

        def fake_socket(_port: int) -> FakeSocket:
            return sockets.pop(0)

        async def delayed_endpoint(*_args, **_kwargs):
            entered.set()
            try:
                await resume.wait()
            except asyncio.CancelledError:
                # Model an endpoint factory that completes ownership transfer
                # while cancellation is already in flight.
                await resume.wait()
            transport = FakeTransport()
            transports.append(transport)
            return transport, object()

        relay._rtp_socket = fake_socket  # type: ignore[method-assign]
        loop = asyncio.get_running_loop()
        with patch.object(
            loop,
            "create_datagram_endpoint",
            side_effect=delayed_endpoint,
        ):
            start = asyncio.create_task(relay.start())
            await entered.wait()
            stop = asyncio.create_task(relay.stop())
            await asyncio.sleep(0)
            self.assertFalse(stop.done())
            resume.set()
            with self.assertRaises(asyncio.CancelledError):
                await start
            await stop
            await relay.stop()

        self.assertEqual(released, [(42000, 42002)])
        self.assertIsNone(relay.left_transport)
        self.assertIsNone(relay.right_transport)
        self.assertTrue(all(transport.closed for transport in transports))
        with self.assertRaisesRegex(RuntimeError, "already been stopped"):
            await relay.start()

    async def test_rtp_relay_debug_write_failure_does_not_break_teardown(self) -> None:
        released: list[tuple[int, int]] = []
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        relay = sip_rtp_bridge.SipRtpRelay(
            left=sip_rtp_bridge.RtpPeer("127.0.0.2", 40000, 96, fmt),
            right=sip_rtp_bridge.RtpPeer("127.0.0.3", 41000, 96, fmt),
            left_port=42000,
            right_port=42002,
            on_release=released.append,
        )
        relay._capture_buffers["left"] = bytearray(b"audio")

        def fail_write() -> None:
            raise OSError("diagnostic filesystem unavailable")

        relay._write_debug_capture_files = fail_write  # type: ignore[method-assign]

        await relay.stop()
        await relay.stop()

        self.assertEqual(released, [(42000, 42002)])
        self.assertEqual(relay._capture_buffers, {})

    def test_rtp_relay_debug_capture_rolls_back_partial_leg_group(self) -> None:
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        relay = sip_rtp_bridge.SipRtpRelay(
            left=sip_rtp_bridge.RtpPeer("127.0.0.2", 40000, 96, fmt),
            right=sip_rtp_bridge.RtpPeer("127.0.0.3", 41000, 96, fmt),
            left_port=42000,
            right_port=42002,
        )
        real_commit = sip_rtp_bridge.commit_capture_file
        commits = 0

        def fail_second_commit(temporary: Path, destination: Path) -> None:
            nonlocal commits
            commits += 1
            if commits == 2:
                raise OSError("relay diagnostic rename failed")
            real_commit(temporary, destination)

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(
                sip_rtp_bridge,
                "debug_capture_transaction",
                side_effect=lambda: contextlib.nullcontext(),
            ),
            patch.object(
                sip_rtp_bridge,
                "commit_capture_file",
                side_effect=fail_second_commit,
            ),
            patch.object(sip_rtp_bridge, "prune_debug_captures"),
        ):
            directory = Path(temp_dir)
            relay._capture_snapshot = {
                "left": (b"left", fmt, directory / "left.wav"),
                "right": (b"right", fmt, directory / "right.wav"),
            }
            with self.assertRaisesRegex(OSError, "rename failed"):
                relay._write_debug_capture_files()
            self.assertEqual(list(directory.iterdir()), [])

    async def test_rtp_relay_child_cancellation_does_not_poison_teardown(self) -> None:
        released: list[tuple[int, int]] = []
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        relay = sip_rtp_bridge.SipRtpRelay(
            left=sip_rtp_bridge.RtpPeer("127.0.0.2", 40000, 96, fmt),
            right=sip_rtp_bridge.RtpPeer("127.0.0.3", 41000, 96, fmt),
            left_port=42000,
            right_port=42002,
            on_release=released.append,
        )

        class CancelledVideoRelay:
            calls = 0

            async def stop(self) -> None:
                self.calls += 1
                raise asyncio.CancelledError

        video = CancelledVideoRelay()
        relay.video_relay = video

        await relay.stop()
        await relay.stop()

        self.assertEqual(video.calls, 1)
        self.assertEqual(released, [(42000, 42002)])
        self.assertIsNone(relay.video_relay)

    async def test_rtp_relay_drops_debug_snapshot_when_writer_pool_is_full(self) -> None:
        capture_limits = _load_intercom_module("debug_capture")
        reserved = 0
        released: list[tuple[int, int]] = []
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        relay = sip_rtp_bridge.SipRtpRelay(
            left=sip_rtp_bridge.RtpPeer("127.0.0.2", 40000, 96, fmt),
            right=sip_rtp_bridge.RtpPeer("127.0.0.3", 41000, 96, fmt),
            left_port=42000,
            right_port=42002,
            debug_capture=True,
            capture_name="bounded-relay-debug",
            on_release=released.append,
        )
        relay._capture_buffers["left"].extend(b"audio")
        try:
            for _index in range(
                capture_limits.DEBUG_CAPTURE_MAX_PENDING_WRITES
            ):
                self.assertTrue(capture_limits.try_reserve_debug_capture_write())
                reserved += 1
            await relay.stop()
        finally:
            for _index in range(reserved):
                capture_limits.release_debug_capture_write()

        self.assertEqual(relay.debug_capture_dropped_writes, 1)
        self.assertEqual(relay._capture_snapshot, {})
        self.assertEqual(released, [(42000, 42002)])

    async def test_rtp_relay_partial_bind_failure_releases_first_socket(self) -> None:
        first = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        first.bind(("127.0.0.1", 0))
        blocker.bind(("127.0.0.1", 0))
        left_port = first.getsockname()[1]
        right_port = blocker.getsockname()[1]
        first.close()
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        relay = sip_rtp_bridge.SipRtpRelay(
            left=sip_rtp_bridge.RtpPeer("127.0.0.2", 40000, 96, fmt),
            right=sip_rtp_bridge.RtpPeer("127.0.0.3", 41000, 96, fmt),
            left_port=left_port,
            right_port=right_port,
        )
        try:
            with self.assertRaises(OSError):
                await relay.start()
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.bind(("0.0.0.0", left_port))
            finally:
                probe.close()
        finally:
            blocker.close()

    def test_incompatible_invite_200_is_acked_then_closed_with_bye(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client._invite_cseq = 7
        response = sip.parse_message(
            sip.build_response(
                200,
                "OK",
                [
                    ("Via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKtest"),
                    ("From", "<sip:HA@127.0.0.1>;tag=local"),
                    ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                    ("Contact", '"ESP handset" <sip:dialog@127.0.0.2:5090;transport=udp>'),
                    ("Call-ID", client.dialog_ids.call_id),
                    ("CSeq", "7 INVITE"),
                ],
                b"",
            )
        )
        compatible = client._commit_200_ok(
            response,
            "ESP",
            "127.0.0.2",
            5060,
            "sip:ESP@127.0.0.2:5060",
            "sip:HA@127.0.0.1:5060",
            "sip:ESP@127.0.0.2:5060",
        )

        self.assertFalse(compatible)
        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        methods = [message.method for message in messages]
        self.assertEqual(methods, ["ACK", "BYE"])
        self.assertEqual([message.uri for message in messages], ["sip:dialog@127.0.0.2:5090;transport=udp"] * 2)
        self.assertEqual(
            [message.header("To") for message in messages],
            ["<sip:ESP@127.0.0.2:5060>;tag=remote"] * 2,
        )

    def test_invite_200_does_not_select_one_target_from_contact_list(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client._invite_cseq = 7
        request_uri = "sip:ESP@127.0.0.2:5060"
        response = sip.parse_message(
            sip.build_response(
                200,
                "OK",
                [
                    ("Via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKtest"),
                    ("From", "<sip:HA@127.0.0.1>;tag=local"),
                    ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                    (
                        "Contact",
                        "<sip:first@127.0.0.2:5090>, <sip:second@127.0.0.3:5090>",
                    ),
                    ("Call-ID", client.dialog_ids.call_id),
                    ("CSeq", "7 INVITE"),
                ],
                b"",
            )
        )

        self.assertFalse(
            client._commit_200_ok(
                response,
                "ESP",
                "127.0.0.2",
                5060,
                request_uri,
                "sip:HA@127.0.0.1:5060",
                request_uri,
            )
        )

        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        self.assertEqual([message.method for message in messages], ["ACK", "BYE"])
        self.assertEqual([message.uri for message in messages], [request_uri] * 2)

    def test_invite_200_commits_directional_audio_and_dtmf_payloads(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        audio = audio_format.AudioFormat(16000, "s16le", 1, 20)
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_send_formats=[audio],
            supported_recv_formats=[audio],
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client._invite_cseq = 7
        client._local_sdp_body = sdp.build_offer_directional(
            "127.0.0.1",
            "127.0.0.1",
            41000,
            [audio],
            [audio],
        )
        offered_audio = sdp.offered_pcm_formats(client._local_sdp_body)[0]
        offered_dtmf = sdp.offered_dtmf_formats(client._local_sdp_body)[0]
        answer = (
            "v=0\r\no=- 2 1 IN IP4 127.0.0.2\r\n"
            "s=answer\r\nc=IN IP4 127.0.0.2\r\nt=0 0\r\n"
            f"m=audio 42000 RTP/AVP 120 {offered_dtmf.payload_type} 121\r\n"
            "a=rtpmap:120 L16/16000/1\r\n"
            f"a=rtpmap:{offered_dtmf.payload_type} telephone-event/16000\r\n"
            f"a=fmtp:{offered_dtmf.payload_type} 0-16\r\n"
            "a=rtpmap:121 telephone-event/8000\r\n"
            "a=fmtp:121 0-16\r\na=ptime:20\r\na=sendrecv\r\n"
        ).encode()
        response = sip.parse_message(
            sip.build_response(
                200,
                "OK",
                [
                    ("Via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKtest"),
                    ("From", "<sip:HA@127.0.0.1>;tag=local"),
                    ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                    ("Contact", "<sip:ESP@127.0.0.2:5060>"),
                    ("Call-ID", client.dialog_ids.call_id),
                    ("CSeq", "7 INVITE"),
                    ("Content-Type", "application/sdp"),
                ],
                answer,
            )
        )

        self.assertTrue(
            client._commit_200_ok(
                response,
                "ESP",
                "127.0.0.2",
                5060,
                "sip:ESP@127.0.0.2:5060",
                "sip:HA@127.0.0.1:5060",
                "sip:ESP@127.0.0.2:5060",
            )
        )

        self.assertIsNotNone(client.dialog)
        assert client.dialog is not None
        self.assertEqual(client.dialog.send_format.payload_type, 120)
        self.assertEqual(
            client.dialog.recv_format.payload_type,
            offered_audio.payload_type,
        )
        self.assertEqual(client.dialog.dtmf_payload_type, offered_dtmf.payload_type)
        self.assertEqual(client.dialog.send_dtmf_payload_type, 121)
        self.assertEqual(client.dialog.send_dtmf_clock_rate, 8000)
        self.assertEqual(
            [sip.parse_message(raw).method for raw, _addr in transport.sent],
            ["ACK"],
        )

    def test_zoiper_200_replay_commits_dialog_without_defensive_bye(self) -> None:
        """A sanitized Zoiper answer must establish, not tear down, the call."""

        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="192.0.2.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            include_common_codecs=True,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client._invite_cseq = 7
        client._local_sdp_body = sdp.build_offer_directional(
            "192.0.2.10",
            "192.0.2.10",
            41000,
            client.supported_send_formats,
            client.supported_recv_formats,
            include_common_codecs=True,
        )
        answer = (
            Path(__file__).parent
            / "fixtures"
            / "peer_sdp"
            / "zoiper-audio-200.sdp"
        ).read_bytes()
        response = sip.parse_message(
            sip.build_response(
                200,
                "OK",
                [
                    ("Via", "SIP/2.0/UDP 192.0.2.10:5060;branch=z9hG4bKzoiper"),
                    ("From", "<sip:HA@192.0.2.10>;tag=local"),
                    ("To", "<sip:peer@192.0.2.20>;tag=remote"),
                    ("Contact", "<sip:peer@192.0.2.20:5090>"),
                    ("Call-ID", client.dialog_ids.call_id),
                    ("CSeq", "7 INVITE"),
                    ("Content-Type", "application/sdp"),
                ],
                answer,
            )
        )

        self.assertTrue(
            client._commit_200_ok(
                response,
                "peer",
                "192.0.2.20",
                5060,
                "sip:peer@192.0.2.20:5060",
                "sip:HA@192.0.2.10:5060",
                "sip:peer@192.0.2.20:5060",
            )
        )
        self.assertIsNotNone(client.dialog)
        self.assertEqual(
            [sip.parse_message(raw).method for raw, _addr in transport.sent],
            ["ACK"],
        )

    def test_invite_200_routes_ack_and_bye_through_reversed_record_route(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[pcm],
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client._invite_cseq = 7
        client._local_sdp_body = sdp.build_offer(
            "127.0.0.1",
            "127.0.0.1",
            41000,
            [pcm],
        )
        offered = sdp.offered_pcm_formats(client._local_sdp_body)[0]
        answer = sdp.build_answer_directional(
            "127.0.0.2",
            "127.0.0.2",
            42000,
            offered,
            offered,
        ).encode()
        response = sip.parse_message(
            sip.build_response(
                200,
                "OK",
                [
                    ("Via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKtest"),
                    ("From", "<sip:HA@127.0.0.1>;tag=local"),
                    ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                    ("Contact", "<sip:dialog@127.0.0.2:5090>"),
                    ("Record-Route", "<sip:core@127.0.0.3:5070;lr>"),
                    ("Record-Route", "<sip:edge@127.0.0.4:5080;lr>"),
                    ("Call-ID", client.dialog_ids.call_id),
                    ("CSeq", "7 INVITE"),
                    ("Content-Type", "application/sdp"),
                ],
                answer,
            )
        )

        self.assertTrue(
            client._commit_200_ok(
                response,
                "ESP",
                "127.0.0.2",
                5060,
                "sip:ESP@127.0.0.2:5060",
                "sip:HA@127.0.0.1:5060",
                "sip:ESP@127.0.0.2:5060",
            )
        )
        self.assertIsNotNone(client.dialog)
        assert client.dialog is not None
        self.assertEqual(
            client.dialog.route_set,
            (
                "<sip:edge@127.0.0.4:5080;lr>",
                "<sip:core@127.0.0.3:5070;lr>",
            ),
        )
        self.assertTrue(client.bye())

        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        self.assertEqual([message.method for message in messages], ["ACK", "BYE"])
        self.assertEqual(
            [message.uri for message in messages],
            ["sip:dialog@127.0.0.2:5090"] * 2,
        )
        self.assertEqual(
            [message.header_values("Route") for message in messages],
            [
                [
                    "<sip:edge@127.0.0.4:5080;lr>",
                    "<sip:core@127.0.0.3:5070;lr>",
                ],
            ]
            * 2,
        )
        self.assertEqual(
            [addr for _raw, addr in transport.sent],
            [("127.0.0.4", 5080)] * 2,
        )

    async def test_sip_tcp_reader_rejects_oversized_header(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"OPTIONS sip:ha SIP/2.0\r\nX-Fill: " + b"x" * sip.MAX_SIP_MESSAGE_BYTES + b"\r\n\r\n")
        reader.feed_eof()
        self.assertIsNone(await sip_tcp_io.read_sip_stream_message(reader))

    async def test_sip_tcp_reader_expires_idle_and_partial_frames(self) -> None:
        idle = asyncio.StreamReader()
        self.assertIsNone(
            await sip_tcp_io.read_sip_stream_message(
                idle,
                first_byte_timeout=0.005,
                frame_timeout=0.02,
            )
        )

        partial = asyncio.StreamReader()
        partial.feed_data(b"I")
        self.assertIsNone(
            await sip_tcp_io.read_sip_stream_message(
                partial,
                first_byte_timeout=None,
                frame_timeout=0.005,
            )
        )

    async def test_sip_tcp_reader_rejects_oversized_combined_record_without_waiting_for_body(self) -> None:
        reader = asyncio.StreamReader()
        padding = b"x" * (sip.MAX_SIP_MESSAGE_BYTES - sip.MAX_SIP_BODY_BYTES)
        reader.feed_data(
            b"OPTIONS sip:ha SIP/2.0\r\nContent-Length: "
            + str(sip.MAX_SIP_BODY_BYTES).encode()
            + b"\r\nX-Fill: "
            + padding
            + b"\r\n\r\n"
        )
        self.assertIsNone(await asyncio.wait_for(sip_tcp_io.read_sip_stream_message(reader), timeout=0.1))

    async def test_sip_tcp_reader_rejects_ambiguous_content_length(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(
            b"OPTIONS sip:ha SIP/2.0\r\nContent-Length: 0\r\nContent-Length: 1\r\n\r\n"
        )
        reader.feed_eof()
        self.assertIsNone(await sip_tcp_io.read_sip_stream_message(reader))

    async def test_sip_tcp_reader_accepts_compact_content_length(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"OPTIONS sip:ha SIP/2.0\r\nl: 4\r\n\r\ntest")
        reader.feed_eof()
        self.assertEqual(
            await sip_tcp_io.read_sip_stream_message(reader),
            b"OPTIONS sip:ha SIP/2.0\r\nl: 4\r\n\r\ntest",
        )

    async def test_sip_tcp_reader_exposes_crlf_keepalive_records(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"\r\n\r\n")

        self.assertEqual(await sip_tcp_io.read_sip_stream_message(reader), b"\r\n")
        self.assertEqual(await sip_tcp_io.read_sip_stream_message(reader), b"\r\n")

    async def test_cancelled_tcp_send_cannot_enqueue_later(self) -> None:
        class BlockingWriter:
            def __init__(self) -> None:
                self.release = asyncio.Event()

            def is_closing(self) -> bool:
                return False

            def write(self, _data: bytes) -> None:
                pass

            async def drain(self) -> None:
                await self.release.wait()

        stream = BlockingWriter()
        writer = sip_tcp_io.SipTcpWriter(stream, label="test", max_queue=1)
        self.assertTrue(writer.send_nowait(b"in-flight"))
        await asyncio.sleep(0)
        self.assertTrue(writer.send_nowait(b"queued"))

        blocked_send = asyncio.create_task(writer.send(b"stale"))
        await asyncio.sleep(0)
        blocked_send.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await blocked_send

        self.assertEqual(writer.queue.get_nowait(), b"queued")
        await asyncio.sleep(0)
        self.assertTrue(writer.queue.empty())
        stream.release.set()
        await writer.close()

    async def test_tcp_send_unblocks_when_writer_task_dies_with_full_queue(self) -> None:
        class FailingWriter:
            def __init__(self) -> None:
                self.release = asyncio.Event()
                self.writes: list[bytes] = []

            def is_closing(self) -> bool:
                return False

            def write(self, data: bytes) -> None:
                self.writes.append(data)

            async def drain(self) -> None:
                await self.release.wait()
                raise OSError("connection lost")

        stream = FailingWriter()
        writer = sip_tcp_io.SipTcpWriter(stream, label="test", max_queue=1)
        self.assertTrue(writer.send_nowait(b"first"))
        await asyncio.sleep(0)
        self.assertTrue(writer.send_nowait(b"second"))
        blocked_send = asyncio.create_task(writer.send(b"third"))
        await asyncio.sleep(0)

        stream.release.set()
        self.assertFalse(await asyncio.wait_for(blocked_send, timeout=0.2))
        self.assertTrue(writer.task.done())

    async def test_client_ignores_response_for_another_call_id(self) -> None:
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        right_call_id = client.dialog_ids.call_id

        def response(call_id: str) -> bytes:
            return sip.build_response(
                180,
                "Ringing",
                [
                    ("Via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKtest"),
                    ("From", "<sip:HA@127.0.0.1>;tag=local"),
                    ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                    ("Call-ID", call_id),
                    ("CSeq", "1 INVITE"),
                ],
                b"",
            )

        client.queue.put_nowait((response("stale-call"), ("127.0.0.2", 5060)))
        client.queue.put_nowait((response(right_call_id), ("127.0.0.2", 5060)))
        received = await client._read_response(0.1)
        self.assertIsNotNone(received)
        assert received is not None
        self.assertEqual(received[0].header("Call-ID"), right_call_id)

    async def test_trunk_registration_filters_call_id_method_and_cseq(self) -> None:
        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="udp",
            server="127.0.0.1",
            port=5060,
            domain="127.0.0.1",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
        )
        trunk = sip_trunk.SipTrunkClient(config=config, local_ip="127.0.0.1", local_sip_port=5060)

        def response(call_id: str, cseq: str) -> sip.SipMessage:
            return sip.parse_message(
                sip.build_response(
                    200,
                    "OK",
                    [
                        ("Via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKtest"),
                        ("From", "<sip:ha@127.0.0.1>;tag=local"),
                        ("To", "<sip:ha@127.0.0.1>;tag=remote"),
                        ("Call-ID", call_id),
                        ("CSeq", cseq),
                    ],
                    b"",
                )
            )

        trunk.responses.put_nowait(response("other", "2 REGISTER"))
        trunk.responses.put_nowait(response(trunk.call_id, "2 INVITE"))
        trunk.responses.put_nowait(response(trunk.call_id, "1 REGISTER"))
        provisional = sip.parse_message(
            sip.build_response(
                100,
                "Trying",
                [
                    ("Via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKtest"),
                    ("From", "<sip:ha@127.0.0.1>;tag=local"),
                    ("To", "<sip:ha@127.0.0.1>;tag=remote"),
                    ("Call-ID", trunk.call_id),
                    ("CSeq", "2 REGISTER"),
                ],
                b"",
            )
        )
        trunk.responses.put_nowait(provisional)
        trunk.responses.put_nowait(response(trunk.call_id, "2 REGISTER"))
        received = await trunk._read_response(0.1, expected_cseq=2)
        self.assertEqual(received.header("CSeq"), "2 REGISTER")

    async def test_udp_trunk_completes_authenticated_registration_on_one_flow(
        self,
    ) -> None:
        class FritzRegistrar(asyncio.DatagramProtocol):
            def __init__(self) -> None:
                self.transport: asyncio.DatagramTransport | None = None
                self.requests: list[tuple[sip.SipMessage, tuple[str, int]]] = []
                self.complete = asyncio.Event()

            def connection_made(self, transport: asyncio.BaseTransport) -> None:
                self.transport = transport  # type: ignore[assignment]

            def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                request = sip.parse_message(data)
                self.requests.append((request, addr))
                headers = [
                    *[("Via", value) for value in request.header_values("Via")],
                    ("From", request.header("From")),
                    ("To", request.header("To") + ";tag=fritz"),
                    ("Call-ID", request.header("Call-ID")),
                    ("CSeq", request.header("CSeq")),
                ]
                if request.header("Authorization"):
                    status, reason = 200, "OK"
                    headers.extend(
                        [
                            ("Contact", request.header("Contact")),
                            ("Expires", "300"),
                        ]
                    )
                    self.complete.set()
                else:
                    status, reason = 401, "Unauthorized"
                    headers.extend(
                        (
                            (
                                "WWW-Authenticate",
                                'Digest realm="fritz.box", nonce="fritz-md5", '
                                'algorithm=MD5, qop="auth"',
                            ),
                            (
                                "WWW-Authenticate",
                                'Digest realm="fritz.box", nonce="fritz-sha", '
                                'algorithm=SHA-256, qop="auth"',
                            ),
                        )
                    )
                assert self.transport is not None
                self.transport.sendto(
                    sip.build_response(status, reason, headers),
                    addr,
                )

        loop = asyncio.get_running_loop()
        registrar = FritzRegistrar()
        server_transport, _ = await loop.create_datagram_endpoint(
            lambda: registrar,
            local_addr=("127.0.0.1", 0),
        )
        server_port = server_transport.get_extra_info("sockname")[1]
        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="udp",
            server="127.0.0.1",
            port=server_port,
            domain="fritz.box",
            username="ha",
            auth_username="ha",
            password="secret",
            expires=300,
        )
        trunk = sip_trunk.SipTrunkClient(
            config=config,
            local_ip="127.0.0.1",
            local_sip_port=5060,
        )
        try:
            await trunk._connect_udp()
            trunk._ensure_receive_task()

            result = await trunk.register(timeout=1.0)
            await asyncio.wait_for(registrar.complete.wait(), timeout=1.0)

            self.assertEqual(result, "registered")
            self.assertTrue(trunk.registered)
            self.assertEqual(trunk.status_code, 200)
            self.assertEqual(len(registrar.requests), 2)
            first, second = registrar.requests
            self.assertEqual(first[0].uri, "sip:fritz.box")
            self.assertEqual(second[0].uri, "sip:fritz.box")
            self.assertIn("<sip:ha@fritz.box>", first[0].header("To"))
            self.assertEqual(first[1], second[1])
            self.assertFalse(first[0].header("Authorization"))
            self.assertTrue(second[0].header("Authorization").startswith("Digest "))
            self.assertEqual(
                sip_auth.parse_digest_challenge(
                    second[0].header("Authorization")
                )["algorithm"],
                "SHA-256",
            )
            self.assertNotEqual(first[0].header("CSeq"), second[0].header("CSeq"))
            first_via = sip.parse_via(first[0].header("Via"))
            second_via = sip.parse_via(second[0].header("Via"))
            self.assertNotEqual(first_via.branch, second_via.branch)
        finally:
            trunk.registered = False
            await trunk.stop()
            server_transport.close()

    async def test_tcp_trunk_connects_to_next_rfc3263_target(self) -> None:
        class Resolver:
            async def resolve(self, *_args, **_kwargs):
                return (
                    sip_resolution.SipServerTarget(
                        "first.example",
                        5060,
                        "TCP",
                        ("192.0.2.10",),
                    ),
                    sip_resolution.SipServerTarget(
                        "second.example",
                        5060,
                        "TCP",
                        ("192.0.2.11",),
                    ),
                )

        class Writer:
            def __init__(self) -> None:
                self.closed = False

            def is_closing(self) -> bool:
                return self.closed

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                return None

            def get_extra_info(self, _name: str):
                return None

            def write(self, _data: bytes) -> None:
                return None

            async def drain(self) -> None:
                return None

        attempts: list[tuple[str, int]] = []
        writer = Writer()

        async def open_connection(host: str, port: int):
            attempts.append((host, port))
            if host == "192.0.2.10":
                raise OSError("first target unavailable")
            return asyncio.StreamReader(), writer

        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="tcp",
            server="pbx.example",
            port=5060,
            domain="pbx.example",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
        )
        trunk = sip_trunk.SipTrunkClient(
            config=config,
            local_ip="127.0.0.1",
            local_sip_port=5060,
            target_resolver=Resolver(),
        )
        with patch.object(
            sip_trunk.asyncio,
            "open_connection",
            new=open_connection,
        ):
            await trunk._connect_tcp()

        self.assertEqual(
            attempts,
            [("192.0.2.10", 5060), ("192.0.2.11", 5060)],
        )
        self.assertEqual(trunk.active_registrar_target, ("192.0.2.11", 5060))
        await trunk.stop()

    async def test_tls_trunk_verifies_logical_registrar_name(self) -> None:
        context = object()
        observed: dict[str, object] = {}

        class Resolver:
            async def resolve(self, *_args, **_kwargs):
                return (
                    sip_resolution.SipServerTarget(
                        "edge.example",
                        5061,
                        "TLS",
                        ("192.0.2.30",),
                    ),
                )

        class Writer:
            def is_closing(self) -> bool:
                return False

            def close(self) -> None:
                return None

            async def wait_closed(self) -> None:
                return None

            def get_extra_info(self, _name: str):
                return None

            def write(self, _data: bytes) -> None:
                return None

            async def drain(self) -> None:
                return None

        async def open_connection(host: str, port: int, **kwargs):
            observed.update(host=host, port=port, **kwargs)
            return asyncio.StreamReader(), Writer()

        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="tls",
            server="registrar.example",
            port=5061,
            domain="registrar.example",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
        )
        trunk = sip_trunk.SipTrunkClient(
            config=config,
            local_ip="127.0.0.1",
            local_sip_port=5061,
            target_resolver=Resolver(),
            tls_context=context,  # type: ignore[arg-type]
        )
        with patch.object(
            sip_trunk.asyncio,
            "open_connection",
            new=open_connection,
        ):
            await trunk._connect_tcp()

        self.assertEqual(observed["host"], "192.0.2.30")
        self.assertEqual(observed["port"], 5061)
        self.assertIs(observed["ssl"], context)
        self.assertEqual(observed["server_hostname"], "registrar.example")
        await trunk.stop()

    def test_trunk_outbound_proxy_uri_selects_host_and_port(self) -> None:
        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="tcp",
            server="pbx.example",
            port=5060,
            domain="pbx.example",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
            outbound_proxy="sip:proxy.example:5070;transport=tcp",
        )
        trunk = sip_trunk.SipTrunkClient(config=config, local_ip="127.0.0.1", local_sip_port=5060)

        self.assertEqual(trunk.registrar_target, ("proxy.example", 5070))

    async def test_udp_trunk_drops_packets_outside_resolved_proxy_hosts(self) -> None:
        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="udp",
            server="pbx.example",
            port=5060,
            domain="pbx.example",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
        )
        trunk = sip_trunk.SipTrunkClient(
            config=config,
            local_ip="127.0.0.1",
            local_sip_port=5060,
        )
        trunk._trusted_udp_hosts = frozenset({"192.0.2.10"})
        handled: list[tuple[str, int]] = []
        received = asyncio.Event()

        async def handler(_raw: bytes, addr: tuple[str, int]) -> None:
            handled.append(addr)
            received.set()

        trunk.set_request_handler(handler)
        raw = sip.build_request(
            "OPTIONS",
            "sip:ha@pbx.example",
            [
                ("Via", "SIP/2.0/UDP pbx.example;branch=z9hG4bKsource"),
                ("From", "<sip:pbx@pbx.example>;tag=remote"),
                ("To", "<sip:ha@pbx.example>"),
                ("Call-ID", "udp-source-policy"),
                ("CSeq", "1 OPTIONS"),
            ],
            b"",
        )
        task = asyncio.create_task(trunk._receive_loop())
        try:
            trunk.queue.put_nowait((raw, ("198.51.100.66", 5060)))
            trunk.queue.put_nowait((raw, ("192.0.2.10", 5099)))
            await asyncio.wait_for(received.wait(), timeout=1)
            await asyncio.sleep(0)
            self.assertEqual(handled, [("192.0.2.10", 5099)])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def test_video_trunk_endpoint_rejects_missing_media_update_handler(self) -> None:
        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="tcp",
            server="pbx.example",
            port=5060,
            domain="pbx.example",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
        )
        trunk = sip_trunk.SipTrunkClient(
            config=config,
            local_ip="127.0.0.1",
            local_sip_port=5060,
        )
        audio = audio_format.AudioFormat(16000, "s16le", 1, 20)
        manager = types.SimpleNamespace(
            local_ip="127.0.0.1",
            port=5060,
            local_rtp_port=41000,
            supported_formats=[audio],
            supported_send_formats=[audio],
            supported_recv_formats=[audio],
            on_invite=lambda _invite: None,
            on_terminated=None,
            on_media_update=None,
            enable_video=True,
            enable_video_transcoding=False,
            prefer_browser_video_send=True,
        )

        with self.assertRaisesRegex(ValueError, "media-update handler"):
            trunk.attach_endpoint_manager(manager)

        self.assertIsNone(trunk.inbound_endpoint)

    def test_trunk_inbound_endpoint_inherits_video_policy(self) -> None:
        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="tcp",
            server="pbx.example",
            port=5060,
            domain="pbx.example",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
        )
        trunk = sip_trunk.SipTrunkClient(
            config=config,
            local_ip="127.0.0.1",
            local_sip_port=5060,
        )
        audio = audio_format.AudioFormat(16000, "s16le", 1, 20)
        manager = types.SimpleNamespace(
            local_ip="127.0.0.1",
            port=5060,
            local_rtp_port=41000,
            supported_formats=[audio],
            supported_send_formats=[audio],
            supported_recv_formats=[audio],
            on_invite=lambda _invite: None,
            on_terminated=None,
            on_media_update=lambda _old, _new, _method: None,
            enable_video=True,
            enable_video_transcoding=True,
            prefer_browser_video_send=True,
        )

        trunk.attach_endpoint_manager(manager)

        endpoint = trunk.inbound_endpoint
        self.assertIsNotNone(endpoint)
        assert endpoint is not None
        self.assertTrue(endpoint.enable_video)
        self.assertTrue(endpoint.enable_video_transcoding)
        self.assertTrue(endpoint.prefer_browser_video_send)
        self.assertIs(endpoint.on_media_update, manager.on_media_update)
        self.assertEqual(endpoint.signaling_transport, "TCP")
        self.assertTrue(endpoint.trusted_trunk)
        request = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:ha@127.0.0.1:5060",
                [
                    ("Via", "SIP/2.0/TCP 192.0.2.10:5060;branch=z9hG4bKtrunk"),
                    ("From", "<sip:caller@pbx.example>;tag=remote"),
                    ("To", "<sip:ha@127.0.0.1>"),
                    ("Call-ID", "trusted-trunk-call"),
                    ("CSeq", "1 INVITE"),
                    ("Content-Type", "application/sdp"),
                ],
                sdp.build_offer(
                    "192.0.2.10",
                    "192.0.2.10",
                    42000,
                    [audio],
                ).encode(),
            )
        )
        parsed_invite = endpoint._parse_invite(request, ("192.0.2.10", 5060))
        self.assertIsNotNone(parsed_invite)
        assert parsed_invite is not None
        self.assertTrue(parsed_invite.received_via_trunk)
        self.assertEqual(parsed_invite.signaling_transport, "TCP")

    def test_trunk_refresh_precedes_short_granted_registration_expiry(self) -> None:
        self.assertEqual(sip_trunk._registration_refresh_delay(300, 1020.0, 1000.0), 10.0)
        self.assertEqual(sip_trunk._registration_refresh_delay(300, 1005.0, 1000.0), 1.0)
        self.assertEqual(sip_trunk._registration_refresh_delay(300, 1300.0, 1000.0), 240.0)

    async def test_confirmed_dialog_accepts_proxy_bye_without_ending_on_cancel(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1", local_name="HA", local_sip_port=5060, local_rtp_port=41000
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client.dialog = types.SimpleNamespace(remote_host="127.0.0.2")  # type: ignore[assignment]
        client.dialog_ids.remote_tag = "remote"
        call_id = client.dialog_ids.call_id

        def request(method: str, cseq: int) -> bytes:
            return sip.build_request(
                method,
                "sip:HA@127.0.0.1:5060",
                [
                    ("Via", "SIP/2.0/UDP 127.0.0.2:5060;branch=z9hG4bKdialog"),
                    ("From", "<sip:ESP@127.0.0.2>;tag=remote"),
                    ("To", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                    ("Call-ID", call_id),
                    ("CSeq", f"{cseq} {method}"),
                ],
                b"",
            )

        client.queue.put_nowait((request("CANCEL", 1), ("127.0.0.2", 5060)))
        # RFC 3261 dialog identity is Call-ID plus the two tags.  A proxy/SBC
        # may originate a sequential request from a different signaling IP.
        client.queue.put_nowait((request("BYE", 2), ("127.0.0.99", 5060)))
        self.assertEqual(await client.wait_for_dialog_termination(timeout=0.1), "remote_hangup")
        self.assertEqual(
            [sip.parse_message(raw).status_code for raw, _addr in transport.sent],
            [481, 200],
        )

    async def test_confirmed_dialog_rejects_reinvite_but_keeps_call_alive(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1", local_name="HA", local_sip_port=5060, local_rtp_port=41000
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client.dialog = types.SimpleNamespace(
            remote_host="127.0.0.2",
            remote_sip_port=5060,
            remote_target_uri="sip:ESP@127.0.0.2:5060",
        )  # type: ignore[assignment]
        client.dialog_ids.remote_tag = "remote"

        def request(method: str, cseq: int, *, remote_tag: str = "remote") -> bytes:
            return sip.build_request(
                method,
                "sip:HA@127.0.0.1:5060",
                [
                    ("Via", "SIP/2.0/UDP 127.0.0.2:5060;branch=z9hG4bKdialog"),
                    ("From", f"<sip:ESP@127.0.0.2>;tag={remote_tag}"),
                    ("To", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                    ("Call-ID", client.dialog_ids.call_id),
                    ("CSeq", f"{cseq} {method}"),
                    ("Content-Type", "application/sdp"),
                ],
                b"v=0\r\na=sendonly\r\n",
            )

        client.queue.put_nowait((request("INVITE", 2, remote_tag="wrong"), ("127.0.0.2", 5060)))
        client.queue.put_nowait((request("INVITE", 3), ("127.0.0.2", 5060)))
        client.queue.put_nowait((request("ACK", 3), ("127.0.0.2", 5060)))
        client.queue.put_nowait((request("BYE", 4), ("127.0.0.2", 5060)))

        self.assertEqual(await client.wait_for_dialog_termination(timeout=0.1), "remote_hangup")
        responses = [sip.parse_message(raw) for raw, _addr in transport.sent]
        self.assertEqual([response.status_code for response in responses], [481, 488, 200])
        self.assertIn("Session renegotiation is not supported", responses[1].header("Warning"))

    async def test_local_reinvite_adds_video_without_stealing_dialog_reader(self) -> None:
        client, current, sent, negotiated = self._confirmed_audio_client()
        watcher = asyncio.create_task(
            client.wait_for_dialog_termination(timeout=1.0)
        )
        jpeg = sdp.DEFAULT_VIDEO_FORMATS[3]
        prepared = asyncio.create_task(
            client.async_prepare_video_reinvite(
                local_video_rtp_port=43000,
                video_formats=(jpeg,),
            )
        )
        request = await self._wait_for_sent_request(sent, "INVITE")
        answer = sdp.build_answer_directional(
            "127.0.0.2",
            "127.0.0.2",
            42000,
            negotiated,
            negotiated,
            remote_sdp=request.body,
            video_port=44000,
            video_format=jpeg,
            video_direction="sendrecv",
        )
        response = self._response_to_request(request, 200, "OK", answer)
        client.queue.put_nowait((response, ("127.0.0.2", 5060)))
        candidate = await asyncio.wait_for(prepared, timeout=0.2)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertIs(client.dialog, current)
        self.assertEqual(candidate.video_format.encoding, "JPEG")
        self.assertTrue(client.commit_prepared_reinvite(current, candidate))
        self.assertIs(client.dialog, candidate)

        bye = self._remote_dialog_request(client, "BYE", 2)
        client.queue.put_nowait((bye, ("127.0.0.2", 5060)))
        self.assertEqual(await watcher, "remote_hangup")
        methods = [sip.parse_message(raw).method for raw, _addr in sent]
        self.assertIn("ACK", methods)

    async def test_local_reinvite_pracks_reliable_early_video_answer(self) -> None:
        client, current, sent, negotiated = self._confirmed_audio_client()
        jpeg = sdp.DEFAULT_VIDEO_FORMATS[3]
        prepared = asyncio.create_task(
            client.async_prepare_video_reinvite(
                local_video_rtp_port=43000,
                video_formats=(jpeg,),
            )
        )
        request = await self._wait_for_sent_request(sent, "INVITE")
        answer = sdp.build_answer_directional(
            "127.0.0.2",
            "127.0.0.2",
            42000,
            negotiated,
            negotiated,
            remote_sdp=request.body,
            video_port=44000,
            video_format=jpeg,
            video_direction="sendrecv",
        )
        provisional_headers = [
            *(("Via", value) for value in request.header_values("Via")),
            ("From", request.header("From")),
            ("To", request.header("To")),
            ("Contact", "<sip:P4@127.0.0.2:5060>"),
            ("Call-ID", request.header("Call-ID")),
            ("CSeq", request.header("CSeq")),
            ("Require", "100rel"),
            ("RSeq", "301"),
            ("Content-Type", "application/sdp"),
        ]
        client.queue.put_nowait(
            (
                sip.build_response(
                    183,
                    "Session Progress",
                    provisional_headers,
                    answer.encode(),
                ),
                ("127.0.0.2", 5060),
            )
        )
        prack = await self._wait_for_sent_request(sent, "PRACK")
        client.queue.put_nowait(
            (
                self._response_to_request(prack, 200, "OK"),
                ("127.0.0.2", 5060),
            )
        )
        client.queue.put_nowait(
            (
                self._response_to_request(request, 200, "OK"),
                ("127.0.0.2", 5060),
            )
        )

        candidate = await asyncio.wait_for(prepared, timeout=0.2)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertIs(client.dialog, current)
        self.assertEqual(candidate.video_format.encoding, "JPEG")

    async def test_local_reinvite_removes_video_with_port_zero(self) -> None:
        client, current, sent, negotiated = self._confirmed_audio_client()
        jpeg = sdp.DEFAULT_VIDEO_FORMATS[3]
        current.video_format = jpeg
        current.local_video_format = jpeg
        current.local_video_rtp_port = 43000
        current.local_video_direction = "sendrecv"
        current.local_sdp_body = sdp.build_offer_directional(
            "127.0.0.1",
            "127.0.0.1",
            41000,
            [negotiated.audio_format],
            [negotiated.audio_format],
            video_port=43000,
            video_formats=(jpeg,),
        )
        client.video_formats = (jpeg,)
        client.video_format = jpeg
        client.video_direction = "sendrecv"
        client.local_video_rtp_port = 43000

        prepared = asyncio.create_task(
            client.async_prepare_video_reinvite(
                local_video_rtp_port=0,
                video_formats=(jpeg,),
                video_direction="inactive",
            )
        )
        request = await self._wait_for_sent_request(sent, "INVITE")
        self.assertIn("m=video 0 RTP/AVP 26", request.body.decode())
        answer = sdp.build_answer_directional(
            "127.0.0.2",
            "127.0.0.2",
            42000,
            negotiated,
            negotiated,
            remote_sdp=request.body,
        )
        response = self._response_to_request(request, 200, "OK", answer)
        client.queue.put_nowait((response, ("127.0.0.2", 5060)))

        candidate = await asyncio.wait_for(prepared, timeout=0.2)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertIsNone(candidate.video_format)
        self.assertTrue(client.commit_prepared_reinvite(current, candidate))
        self.assertEqual(client.video_formats, ())
        self.assertIsNone(client.video_format)
        self.assertEqual(client.video_direction, "inactive")
        self.assertEqual(client.local_video_rtp_port, 0)

    async def test_local_reinvite_rejection_preserves_audio_dialog(self) -> None:
        client, current, sent, _negotiated = self._confirmed_audio_client()
        watcher = asyncio.create_task(
            client.wait_for_dialog_termination(timeout=1.0)
        )
        prepared = asyncio.create_task(
            client.async_prepare_video_reinvite(
                local_video_rtp_port=43000,
                video_formats=(sdp.DEFAULT_VIDEO_FORMATS[3],),
            )
        )
        request = await self._wait_for_sent_request(sent, "INVITE")
        client.queue.put_nowait(
            (
                self._response_to_request(request, 488, "Not Acceptable Here"),
                ("127.0.0.2", 5060),
            )
        )

        self.assertIsNone(await asyncio.wait_for(prepared, timeout=0.2))
        self.assertIs(client.dialog, current)
        self.assertFalse(watcher.done())
        self.assertIn(
            "ACK",
            [sip.parse_message(raw).method for raw, _addr in sent],
        )

        client.queue.put_nowait(
            (
                self._remote_dialog_request(client, "BYE", 2),
                ("127.0.0.2", 5060),
            )
        )
        self.assertEqual(await watcher, "remote_hangup")

    async def test_remote_bye_wins_during_local_reinvite(self) -> None:
        client, _current, sent, _negotiated = self._confirmed_audio_client()
        watcher = asyncio.create_task(
            client.wait_for_dialog_termination(timeout=1.0)
        )
        prepared = asyncio.create_task(
            client.async_prepare_video_reinvite(
                local_video_rtp_port=43000,
                video_formats=(sdp.DEFAULT_VIDEO_FORMATS[3],),
            )
        )
        await self._wait_for_sent_request(sent, "INVITE")
        client.queue.put_nowait(
            (
                self._remote_dialog_request(client, "BYE", 2),
                ("127.0.0.2", 5060),
            )
        )

        self.assertIsNone(await asyncio.wait_for(prepared, timeout=0.2))
        self.assertEqual(await watcher, "remote_hangup")
        self.assertIsNone(client.dialog)
        responses = [
            sip.parse_message(raw)
            for raw, _addr in sent
            if sip.parse_message(raw).is_response
        ]
        self.assertEqual(responses[-1].status_code, 200)

    async def test_remote_reinvite_gets_491_during_local_offer(self) -> None:
        client, current, sent, negotiated = self._confirmed_audio_client()
        watcher = asyncio.create_task(
            client.wait_for_dialog_termination(timeout=1.0)
        )
        jpeg = sdp.DEFAULT_VIDEO_FORMATS[3]
        prepared = asyncio.create_task(
            client.async_prepare_video_reinvite(
                local_video_rtp_port=43000,
                video_formats=(jpeg,),
            )
        )
        request = await self._wait_for_sent_request(sent, "INVITE")
        collision = self._remote_dialog_request(
            client,
            "INVITE",
            2,
            body=sdp.build_offer_directional(
                "127.0.0.2",
                "127.0.0.2",
                42000,
                [negotiated.audio_format],
                [negotiated.audio_format],
            ),
        )
        client.queue.put_nowait((collision, ("127.0.0.2", 5060)))
        answer = sdp.build_answer_directional(
            "127.0.0.2",
            "127.0.0.2",
            42000,
            negotiated,
            negotiated,
            remote_sdp=request.body,
            video_port=44000,
            video_format=jpeg,
            video_direction="sendrecv",
        )
        client.queue.put_nowait(
            (
                self._response_to_request(request, 200, "OK", answer),
                ("127.0.0.2", 5060),
            )
        )

        candidate = await asyncio.wait_for(prepared, timeout=0.2)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertTrue(client.commit_prepared_reinvite(current, candidate))
        responses = [
            sip.parse_message(raw)
            for raw, _addr in sent
            if sip.parse_message(raw).is_response
        ]
        self.assertEqual([item.status_code for item in responses], [491])

        client.queue.put_nowait(
            (
                self._remote_dialog_request(client, "BYE", 3),
                ("127.0.0.2", 5060),
            )
        )
        self.assertEqual(await watcher, "remote_hangup")

    async def test_confirmed_dialog_commits_remote_update_once_after_200(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        negotiated = sdp.audio_format_to_rtp(pcm, 96)
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[pcm],
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client.dialog_ids.remote_tag = "remote"
        initial_local_sdp = sdp.rewrite_sdp_origin(
            sdp.build_answer_directional(
                "127.0.0.1",
                "127.0.0.1",
                41000,
                negotiated,
                negotiated,
            ),
            4242,
            0,
        )
        client.dialog = sip_client.SipDialog(
            target="ESP",
            remote_host="127.0.0.2",
            remote_sip_port=5060,
            remote_rtp_host="127.0.0.2",
            remote_rtp_port=42000,
            local_rtp_port=41000,
            call_id=client.dialog_ids.call_id,
            local_uri="sip:HA@127.0.0.1:5060",
            remote_uri="sip:ESP@127.0.0.2:5060",
            send_format=negotiated,
            recv_format=negotiated,
            remote_target_uri="sip:ESP@127.0.0.2:5060",
            local_sdp_session_id=4242,
            local_sdp_session_version=0,
            local_sdp_body=initial_local_sdp,
        )
        prepared: list[int] = []
        committed: list[int] = []

        async def on_media_update(_previous, updated, method):
            self.assertEqual(method, "UPDATE")
            prepared.append(updated.remote_rtp_port)

            async def commit() -> None:
                committed.append(updated.remote_rtp_port)

            return commit

        client.on_media_update = on_media_update

        def request(method: str, cseq: int, branch: str, body: bytes = b"") -> bytes:
            headers = [
                ("Via", f"SIP/2.0/UDP 127.0.0.2:5060;branch={branch}"),
                ("From", "<sip:ESP@127.0.0.2>;tag=remote"),
                ("To", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", f"{cseq} {method}"),
            ]
            if body:
                headers.append(("Content-Type", "application/sdp"))
            return sip.build_request(
                method,
                "sip:HA@127.0.0.1:5060",
                headers,
                body,
            )

        offer = sdp.build_offer_directional(
            "127.0.0.2",
            "127.0.0.2",
            43000,
            [pcm],
            [pcm],
            audio_direction="sendonly",
        ).encode()
        update = request("UPDATE", 2, "z9hG4bKupdate", offer)
        client.queue.put_nowait((update, ("127.0.0.2", 5060)))
        client.queue.put_nowait((update, ("127.0.0.2", 5060)))
        client.queue.put_nowait(
            (request("OPTIONS", 3, "z9hG4bKoptions"), ("127.0.0.2", 5060))
        )
        # A delayed UDP retransmission of the older UPDATE remains the same
        # transaction even after a newer in-dialog request has completed.
        client.queue.put_nowait((update, ("127.0.0.3", 5060)))
        client.queue.put_nowait(
            (request("BYE", 3, "z9hG4bKstale-bye"), ("127.0.0.2", 5060))
        )
        client.queue.put_nowait(
            (request("BYE", 4, "z9hG4bKbye"), ("127.0.0.2", 5060))
        )

        self.assertEqual(
            await client.wait_for_dialog_termination(timeout=0.1),
            "remote_hangup",
        )
        self.assertEqual(prepared, [43000])
        self.assertEqual(committed, [43000])
        responses = [sip.parse_message(raw) for raw, _addr in transport.sent]
        self.assertEqual(
            [response.status_code for response in responses],
            [200, 200, 200, 200, 500, 200],
        )
        self.assertIn(b"m=audio 41000", responses[0].body)
        self.assertIn(b"o=- 4242 1 IN IP4 127.0.0.1", responses[0].body)
        self.assertEqual(responses[0].body, responses[1].body)
        self.assertEqual(responses[0].body, responses[3].body)
        self.assertEqual(responses[4].header("Retry-After"), "1")

    async def test_outbound_dialog_commits_offerless_reinvite_answer_from_ack(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

            def close(self) -> None:
                return

        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        negotiated = sdp.audio_format_to_rtp(pcm, 96)
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[pcm],
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client.dialog_ids.remote_tag = "remote"
        local_sdp = sdp.rewrite_sdp_origin(
            sdp.build_answer_directional(
                "127.0.0.1",
                "127.0.0.1",
                41000,
                negotiated,
                negotiated,
            ),
            4242,
            0,
        )
        client.dialog = sip_client.SipDialog(
            target="ESP",
            remote_host="127.0.0.2",
            remote_sip_port=5060,
            remote_rtp_host="127.0.0.2",
            remote_rtp_port=42000,
            local_rtp_port=41000,
            call_id=client.dialog_ids.call_id,
            local_uri="sip:HA@127.0.0.1:5060",
            remote_uri="sip:ESP@127.0.0.2:5060",
            send_format=negotiated,
            recv_format=negotiated,
            remote_target_uri="sip:ESP@127.0.0.2:5060",
            local_sdp_session_id=4242,
            local_sdp_session_version=0,
            local_sdp_body=local_sdp,
        )
        committed: list[int] = []

        async def on_media_update(_previous, updated, method):
            self.assertEqual(method, "INVITE")

            async def commit() -> None:
                committed.append(updated.remote_rtp_port)

            return commit

        client.on_media_update = on_media_update

        def request(method: str, body: bytes = b"") -> sip.SipMessage:
            headers = [
                ("Via", f"SIP/2.0/UDP 127.0.0.2:5060;branch=z9hG4bK{method.lower()}"),
                ("From", "<sip:ESP@127.0.0.2>;tag=remote"),
                ("To", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", f"2 {method}"),
            ]
            if body:
                headers.append(("Content-Type", "application/sdp"))
            return sip.parse_message(
                sip.build_request(
                    method,
                    "sip:HA@127.0.0.1:5060",
                    headers,
                    body,
                )
            )

        self.assertIsNone(
            await client._dispatch_in_dialog_request(
                request("INVITE"), ("127.0.0.2", 5060)
            )
        )
        responses = [sip.parse_message(raw) for raw, _addr in transport.sent]
        offer = next(
            response.body
            for response in responses
            if response.status_code == 200
        )
        answer = sdp.build_answer_directional(
            "127.0.0.2",
            "127.0.0.2",
            43000,
            negotiated,
            negotiated,
            remote_sdp=offer,
        ).encode()
        self.assertIsNone(
            await client._dispatch_in_dialog_request(
                request("ACK", answer), ("127.0.0.2", 5060)
            )
        )

        self.assertEqual(committed, [43000])
        self.assertIsNotNone(client.dialog)
        assert client.dialog is not None
        self.assertEqual(client.dialog.remote_rtp_port, 43000)
        self.assertIsNone(client._uas_delayed_offer)
        client.dialog = None
        await client.close()

    async def test_remote_reinvite_2xx_retransmits_over_tcp_until_ack(self) -> None:
        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        negotiated = sdp.audio_format_to_rtp(pcm, 96)
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[pcm],
            signaling_transport="TCP",
        )
        sent: list[bytes] = []
        incoming: asyncio.Queue[bytes] = asyncio.Queue()
        client.use_reused_tcp_connection(
            send=lambda raw: sent.append(raw) is None,
            responses=incoming,
            close=lambda: None,
        )
        client._pending_remote_host = "127.0.0.2"
        client._pending_remote_sip_port = 5060
        client.dialog_ids.remote_tag = "remote"
        client.dialog = sip_client.SipDialog(
            target="ESP",
            remote_host="127.0.0.2",
            remote_sip_port=5060,
            remote_rtp_host="127.0.0.2",
            remote_rtp_port=42000,
            local_rtp_port=41000,
            call_id=client.dialog_ids.call_id,
            local_uri="sip:HA@127.0.0.1:5060",
            remote_uri="sip:ESP@127.0.0.2:5060",
            send_format=negotiated,
            recv_format=negotiated,
            remote_target_uri="sip:ESP@127.0.0.2:5060",
        )

        def request(method: str, cseq: int, branch: str, body: bytes = b"") -> bytes:
            headers = [
                ("Via", f"SIP/2.0/TCP 127.0.0.2:5060;branch={branch}"),
                ("From", "<sip:ESP@127.0.0.2>;tag=remote"),
                ("To", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", f"{cseq} {method}"),
            ]
            if body:
                headers.append(("Content-Type", "application/sdp"))
            return sip.build_request(method, "sip:HA@127.0.0.1:5060", headers, body)

        offer = sdp.build_offer_directional(
            "127.0.0.2",
            "127.0.0.2",
            42000,
            [pcm],
            [pcm],
        ).encode()
        with (
            patch.object(sip_client, "SIP_T1", 0.002),
            patch.object(sip_client, "SIP_T2", 0.004),
            patch.object(sip_client, "SIP_TIMER_B", 0.1),
        ):
            waiter = asyncio.create_task(client.wait_for_dialog_termination(timeout=0.2))
            incoming.put_nowait(request("INVITE", 2, "z9hG4bKreinvite", offer))
            for _ in range(20):
                invite_responses = [
                    message
                    for raw in sent
                    if (message := sip.parse_message(raw)).is_response
                    and message.header("CSeq") == "2 INVITE"
                    and message.status_code == 200
                ]
                if len(invite_responses) >= 2:
                    break
                await asyncio.sleep(0.002)
            incoming.put_nowait(request("ACK", 2, "z9hG4bKack"))
            incoming.put_nowait(request("BYE", 3, "z9hG4bKbye"))
            self.assertEqual(await waiter, "remote_hangup")

        invite_responses = [
            message
            for raw in sent
            if (message := sip.parse_message(raw)).is_response
            and message.header("CSeq") == "2 INVITE"
            and message.status_code == 200
        ]
        self.assertGreaterEqual(len(invite_responses), 2)
        self.assertEqual(client.snapshot()["pending_remote_invite_ack"], 0)
        self.assertGreaterEqual(client.snapshot()["remote_invite_2xx_retransmissions"], 1)
        await client.close()

    async def test_remote_reinvite_ack_timeout_sends_bye_and_ends_dialog(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

            def close(self) -> None:
                return

        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        negotiated = sdp.audio_format_to_rtp(pcm, 96)
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[pcm],
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client.dialog_ids.remote_tag = "remote"
        client.dialog = sip_client.SipDialog(
            target="ESP",
            remote_host="127.0.0.2",
            remote_sip_port=5060,
            remote_rtp_host="127.0.0.2",
            remote_rtp_port=42000,
            local_rtp_port=41000,
            call_id=client.dialog_ids.call_id,
            local_uri="sip:HA@127.0.0.1:5060",
            remote_uri="sip:ESP@127.0.0.2:5060",
            send_format=negotiated,
            recv_format=negotiated,
            remote_target_uri="sip:ESP@127.0.0.2:5060",
        )
        offer = sdp.build_offer_directional(
            "127.0.0.2",
            "127.0.0.2",
            42000,
            [pcm],
            [pcm],
        ).encode()
        reinvite = sip.build_request(
            "INVITE",
            "sip:HA@127.0.0.1:5060",
            [
                ("Via", "SIP/2.0/UDP 127.0.0.2:5060;branch=z9hG4bKtimeout"),
                ("From", "<sip:ESP@127.0.0.2>;tag=remote"),
                ("To", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", "2 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            offer,
        )
        client.queue.put_nowait((reinvite, ("127.0.0.2", 5060)))
        with (
            patch.object(sip_client, "SIP_T1", 0.001),
            patch.object(sip_client, "SIP_T2", 0.002),
            patch.object(sip_client, "SIP_TIMER_B", 0.006),
        ):
            self.assertEqual(
                await client.wait_for_dialog_termination(timeout=1.0),
                "ack_timeout",
            )

        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        self.assertGreaterEqual(
            sum(message.status_code == 200 for message in messages if message.is_response),
            1,
        )
        self.assertTrue(any(message.method == "BYE" for message in messages))
        self.assertIsNone(client.dialog)
        self.assertEqual(client.snapshot()["pending_remote_invite_ack"], 0)
        await client.close()

    async def test_confirmed_dialog_reacks_retransmitted_invite_2xx(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        fmt = sdp.audio_format_to_rtp(audio_format.AudioFormat(16000, "s16le", 1, 20), 96)
        client.dialog_ids.remote_tag = "remote"
        client.dialog = sip_client.SipDialog(
            target="ESP",
            remote_host="127.0.0.2",
            remote_sip_port=5060,
            remote_rtp_host="127.0.0.2",
            remote_rtp_port=42000,
            local_rtp_port=41000,
            call_id=client.dialog_ids.call_id,
            local_uri="sip:HA@127.0.0.1:5060",
            remote_uri="sip:ESP@127.0.0.2:5060",
            send_format=fmt,
            recv_format=fmt,
            remote_target_uri="sip:ESP@127.0.0.2:5060",
        )
        duplicate_ok = sip.build_response(
            200,
            "OK",
            [
                ("Via", f"SIP/2.0/UDP 127.0.0.1:5060;branch={client.dialog_ids.branch}"),
                ("From", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", f"{client._invite_cseq} INVITE"),
            ],
        )
        bye = sip.build_request(
            "BYE",
            "sip:HA@127.0.0.1:5060",
            [
                ("Via", "SIP/2.0/UDP 127.0.0.2:5060;branch=z9hG4bKbye"),
                ("Via", "SIP/2.0/UDP 127.0.0.3:5060;branch=z9hG4bKproxy"),
                ("From", "<sip:ESP@127.0.0.2>;tag=remote"),
                ("To", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", "2 BYE"),
            ],
        )
        client.queue.put_nowait((duplicate_ok, ("127.0.0.2", 5060)))
        client.queue.put_nowait((bye, ("127.0.0.2", 5060)))

        self.assertEqual(await client.wait_for_dialog_termination(timeout=0.1), "remote_hangup")
        self.assertEqual(sip.parse_message(transport.sent[0][0]).method, "ACK")
        bye_response = sip.parse_message(transport.sent[1][0])
        self.assertEqual(bye_response.status_code, 200)
        self.assertEqual(len(bye_response.header_values("Via")), 2)

    async def test_cancelled_invite_final_response_is_acked(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1", local_name="HA", local_sip_port=5060, local_rtp_port=41000
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client._pending_remote_host = "127.0.0.2"
        client._pending_remote_sip_port = 5060
        client._pending_request_uri = "sip:ESP@127.0.0.2:5060"
        client._pending_local_uri = "sip:HA@127.0.0.1:5060"
        client._pending_remote_uri = "sip:ESP@127.0.0.2:5060"
        client._invite_transaction_active = True
        client._received_provisional = True
        call_id = client.dialog_ids.call_id
        client.queue.put_nowait(
            (
                sip.build_response(
                    200,
                    "OK",
                    [
                        ("Via", f"SIP/2.0/UDP 127.0.0.1:5060;branch={client.dialog_ids.branch}"),
                        ("From", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                        ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                        ("Call-ID", call_id),
                        ("CSeq", f"{client._invite_cseq} CANCEL"),
                    ],
                    b"",
                ),
                ("127.0.0.2", 5060),
            )
        )
        client.queue.put_nowait(
            (
                sip.build_response(
                    487,
                    "Request Terminated",
                    [
                        ("Via", f"SIP/2.0/UDP 127.0.0.1:5060;branch={client.dialog_ids.branch}"),
                        ("From", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                        ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                        ("Call-ID", call_id),
                        ("CSeq", f"{client._invite_cseq} INVITE"),
                    ],
                    b"",
                ),
                ("127.0.0.2", 5060),
            )
        )

        self.assertEqual(await client.terminate(timeout=0.1), "cancelled")
        self.assertEqual([sip.parse_message(raw).method for raw, _addr in transport.sent], ["CANCEL", "ACK"])

    async def test_confirmed_terminate_propagates_unexpected_reader_failure(
        self,
    ) -> None:
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        client.dialog = types.SimpleNamespace()  # type: ignore[assignment]
        client.bye = lambda: True  # type: ignore[method-assign]

        async def read_response(_timeout: float):
            raise RuntimeError("reader invariant")

        client._read_response = read_response  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "reader invariant"):
            await client.terminate(timeout=0.1)

    async def test_cancel_terminate_propagates_unexpected_reader_failure(
        self,
    ) -> None:
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        client.cancel = lambda: True  # type: ignore[method-assign]

        async def read_response(_timeout: float):
            raise RuntimeError("reader invariant")

        client._read_response = read_response  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "reader invariant"):
            await client.terminate(timeout=0.1)

    async def test_cancelled_invite_final_response_does_not_wait_for_cancel_ok(
        self,
    ) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client._pending_remote_host = "127.0.0.2"
        client._pending_remote_sip_port = 5060
        client._pending_request_uri = "sip:ESP@127.0.0.2:5060"
        client._pending_local_uri = "sip:HA@127.0.0.1:5060"
        client._pending_remote_uri = "sip:ESP@127.0.0.2:5060"
        client._invite_transaction_active = True
        client._received_provisional = True
        read_count = 0

        async def read_response(_timeout: float):
            nonlocal read_count
            read_count += 1
            if read_count > 1:
                await asyncio.Future()
            return (
                sip.parse_message(
                    sip.build_response(
                        487,
                        "Request Terminated",
                        [
                            (
                                "Via",
                                "SIP/2.0/UDP 127.0.0.1:5060;branch="
                                f"{client.dialog_ids.branch}",
                            ),
                            (
                                "From",
                                "<sip:HA@127.0.0.1>;tag="
                                f"{client.dialog_ids.local_tag}",
                            ),
                            ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                            ("Call-ID", client.dialog_ids.call_id),
                            ("CSeq", f"{client._invite_cseq} INVITE"),
                        ],
                    )
                ),
                ("127.0.0.2", 5060),
            )

        client._read_response = read_response  # type: ignore[method-assign]

        self.assertEqual(
            await asyncio.wait_for(client.terminate(timeout=10), timeout=0.1),
            "cancelled",
        )
        self.assertEqual(read_count, 1)
        self.assertEqual(
            [sip.parse_message(raw).method for raw, _addr in transport.sent],
            ["CANCEL", "ACK"],
        )

    async def test_cancel_awaits_existing_final_response_owner(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client._pending_remote_host = "127.0.0.2"
        client._pending_remote_sip_port = 5060
        client._pending_request_uri = "sip:ESP@127.0.0.2:5060"
        client._pending_local_uri = "sip:HA@127.0.0.1:5060"
        client._pending_remote_uri = "sip:ESP@127.0.0.2:5060"
        client._invite_transaction_active = True
        client._received_provisional = True
        response_ready = asyncio.Event()
        response_release = asyncio.Event()
        read_count = 0

        async def read_response(_timeout: float):
            nonlocal read_count
            read_count += 1
            response_ready.set()
            await response_release.wait()
            return (
                sip.parse_message(
                    sip.build_response(
                        487,
                        "Request Terminated",
                        [
                            (
                                "Via",
                                "SIP/2.0/UDP 127.0.0.1:5060;branch="
                                f"{client.dialog_ids.branch}",
                            ),
                            (
                                "From",
                                "<sip:HA@127.0.0.1>;tag="
                                f"{client.dialog_ids.local_tag}",
                            ),
                            ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                            ("Call-ID", client.dialog_ids.call_id),
                            ("CSeq", f"{client._invite_cseq} INVITE"),
                        ],
                    )
                ),
                ("127.0.0.2", 5060),
            )

        client._read_response = read_response  # type: ignore[method-assign]

        waiter = asyncio.create_task(client.wait_for_final(timeout=10))
        await response_ready.wait()
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter

        async def release_response() -> None:
            await asyncio.sleep(0)
            response_release.set()

        release = asyncio.create_task(release_response())
        self.assertEqual(
            await asyncio.wait_for(client.terminate(timeout=10), timeout=0.1),
            "cancelled",
        )
        await release
        self.assertEqual(read_count, 1)
        self.assertEqual(
            [sip.parse_message(raw).method for raw, _addr in transport.sent],
            ["CANCEL", "ACK"],
        )

    async def test_cancel_race_accepts_the_separate_bye_transaction(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1", local_name="HA", local_sip_port=5060, local_rtp_port=41000
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client._pending_target = "ESP"
        client._pending_remote_host = "127.0.0.2"
        client._pending_remote_sip_port = 5060
        client._pending_request_uri = "sip:ESP@127.0.0.2:5060"
        client._pending_local_uri = "sip:HA@127.0.0.1:5060"
        client._pending_remote_uri = "sip:ESP@127.0.0.2:5060"
        client._invite_transaction_active = True
        client._received_provisional = True
        call_id = client.dialog_ids.call_id
        rtp_fmt = sdp.audio_format_to_rtp(audio_format.AudioFormat(16000, "s16le", 1, 20), 96)
        answer = sdp.build_answer_directional(
            "127.0.0.2", "127.0.0.2", 42000, rtp_fmt, rtp_fmt
        ).encode()

        def response(status: int, reason: str, method: str, cseq: int, branch: str, body: bytes = b"") -> bytes:
            headers = [
                ("Via", f"SIP/2.0/UDP 127.0.0.1:5060;branch={branch}"),
                ("From", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                ("Call-ID", call_id),
                ("CSeq", f"{cseq} {method}"),
            ]
            if body:
                headers.append(("Content-Type", "application/sdp"))
            return sip.build_response(status, reason, headers, body)

        client.queue.put_nowait((
            response(200, "OK", "INVITE", client._invite_cseq, client.dialog_ids.branch, answer),
            ("127.0.0.2", 5060),
        ))
        terminating = asyncio.create_task(client.terminate(timeout=0.5))
        while not any(sip.parse_message(raw).method == "BYE" for raw, _addr in transport.sent):
            await asyncio.sleep(0)
        client.queue.put_nowait((
            response(200, "OK", "BYE", client._bye_cseq, client._bye_branch),
            ("127.0.0.2", 5060),
        ))

        self.assertEqual(await terminating, "cancelled")
        self.assertEqual([sip.parse_message(raw).method for raw, _addr in transport.sent], ["CANCEL", "ACK", "BYE"])

    async def test_trunk_old_tcp_reader_cannot_clear_replacement(self) -> None:
        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="tcp",
            server="127.0.0.1",
            port=5060,
            domain="127.0.0.1",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
        )
        trunk = sip_trunk.SipTrunkClient(config=config, local_ip="127.0.0.1", local_sip_port=5060)
        old_reader = asyncio.StreamReader()
        new_reader = asyncio.StreamReader()

        class Writer:
            def is_closing(self) -> bool:
                return False

            def get_extra_info(self, _name: str):
                return ("127.0.0.1", 5060)

            def close(self) -> None:
                pass

        old_writer = Writer()
        new_writer = Writer()
        trunk.reader = old_reader
        trunk.writer = old_writer  # type: ignore[assignment]
        trunk._reader_ready.set()
        replacement_read = asyncio.Event()

        async def fake_read(reader):
            if reader is old_reader:
                trunk.reader = new_reader
                trunk.writer = new_writer  # type: ignore[assignment]
                return None
            replacement_read.set()
            await asyncio.Event().wait()

        original_read = sip_trunk._read_sip_stream_message
        sip_trunk._read_sip_stream_message = fake_read
        task = asyncio.create_task(trunk._receive_loop())
        try:
            await asyncio.wait_for(replacement_read.wait(), timeout=0.1)
            self.assertIs(trunk.reader, new_reader)
            self.assertIs(trunk.writer, new_writer)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            sip_trunk._read_sip_stream_message = original_read

    async def test_trunk_transport_loss_preserves_confirmed_dialog_for_new_flow(self) -> None:
        """A confirmed trunk dialog must outlive the TCP flow that carried it."""

        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="tcp",
            server="pbx.example",
            port=5060,
            domain="pbx.example",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
        )
        trunk = sip_trunk.SipTrunkClient(
            config=config,
            local_ip="127.0.0.1",
            local_sip_port=5060,
        )
        audio = audio_format.AudioFormat(16000, "s16le", 1, 20)
        terminated: list[tuple[str, str]] = []
        media_updates: list[tuple[int, str]] = []
        routed_invites: list[str] = []

        def answer(invite):
            return sdp.build_answer_directional(
                "127.0.0.1",
                "127.0.0.1",
                41000,
                invite.send_format,
                invite.recv_format,
                remote_sdp=invite.remote_sdp,
                video_port=41002,
                video_format=invite.answer_video_format,
                video_direction=sdp.local_direction_for_offer(
                    invite.video_format.direction
                    if invite.video_format is not None
                    else "inactive"
                ),
            )

        async def on_invite(invite):
            routed_invites.append(invite.call_id)
            return sip_listener.SipInviteResult(
                200,
                "OK",
                answer_sdp=answer(invite),
            )

        async def on_media_update(_previous, updated, _method):
            media_updates.append(
                (
                    updated.remote_rtp_port,
                    updated.video_format.direction
                    if updated.video_format is not None
                    else "",
                )
            )
            return sip_listener.SipInviteResult(
                200,
                "OK",
                answer_sdp=answer(updated),
            )

        async def on_terminated(call_id: str, reason: str) -> None:
            terminated.append((call_id, reason))

        manager = types.SimpleNamespace(
            local_ip="127.0.0.1",
            port=5060,
            local_rtp_port=41000,
            supported_formats=[audio],
            supported_send_formats=[audio],
            supported_recv_formats=[audio],
            on_invite=on_invite,
            on_terminated=on_terminated,
            on_register=None,
            on_info=None,
            on_media_update=on_media_update,
            enable_video=True,
            enable_video_transcoding=False,
            prefer_browser_video_send=True,
        )
        trunk.attach_endpoint_manager(manager)
        endpoint = trunk.inbound_endpoint
        assert endpoint is not None

        call_id = "trunk-flow-replacement"
        remote_tag = "remote-dialog-tag"

        def invite(
            cseq: int,
            branch: str,
            remote_rtp_port: int,
            *,
            to_tag: str = "",
            source_ip: str = "192.0.2.10",
            video: bool = False,
        ) -> bytes:
            headers = [
                (
                    "Via",
                    f"SIP/2.0/TCP {source_ip}:5060;branch={branch};rport",
                ),
                ("From", f"<sip:caller@pbx.example>;tag={remote_tag}"),
                (
                    "To",
                    "<sip:ha@pbx.example>" + (f";tag={to_tag}" if to_tag else ""),
                ),
                ("Call-ID", call_id),
                ("CSeq", f"{cseq} INVITE"),
                ("Contact", f"<sip:caller@{source_ip}:5060;transport=tcp>"),
                ("Content-Type", "application/sdp"),
            ]
            body = sdp.build_offer_directional(
                source_ip,
                source_ip,
                remote_rtp_port,
                [audio],
                [audio],
                video_port=remote_rtp_port + 2 if video else None,
                video_formats=sdp.DEFAULT_VIDEO_FORMATS if video else (),
                video_direction="sendrecv" if video else "inactive",
            ).encode()
            return sip.build_request(
                "INVITE",
                "sip:ha@pbx.example",
                headers,
                body,
            )

        class StreamWriter:
            def __init__(self, peer: tuple[str, int]) -> None:
                self.peer = peer
                self.closed = False

            def is_closing(self) -> bool:
                return self.closed

            def get_extra_info(self, name: str):
                return self.peer if name == "peername" else None

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                return None

        final_response = asyncio.Event()

        class TrunkWriter:
            def __init__(self) -> None:
                self.sent: list[bytes] = []
                self.closed = False

            def send_nowait(self, raw: bytes) -> bool:
                self.sent.append(raw)
                message = sip.parse_message(raw)
                if message.status_code == 200 and message.header("CSeq") == "1 INVITE":
                    final_response.set()
                return True

            async def close(self) -> None:
                self.closed = True

        first_reader = asyncio.StreamReader()
        first_stream_writer = StreamWriter(("192.0.2.10", 5060))
        first_tx = TrunkWriter()
        trunk.reader = first_reader
        trunk.writer = first_stream_writer  # type: ignore[assignment]
        trunk._tcp_writer = first_tx  # type: ignore[assignment]
        trunk._reader_ready.set()
        first_invite = invite(1, "z9hG4bKinitial", 42000)
        read_count = 0

        async def read_first_flow(_reader):
            nonlocal read_count
            read_count += 1
            if read_count == 1:
                return first_invite
            if read_count == 2:
                await asyncio.wait_for(final_response.wait(), timeout=1)
                local_tag = sip.extract_tag(
                    next(
                        sip.parse_message(raw)
                        for raw in first_tx.sent
                        if sip.parse_message(raw).status_code == 200
                    ).header("To")
                )
                return sip.build_request(
                    "ACK",
                    "sip:ha@pbx.example",
                    [
                        (
                            "Via",
                            "SIP/2.0/TCP 192.0.2.10:5060;branch=z9hG4bKack;rport",
                        ),
                        ("From", f"<sip:caller@pbx.example>;tag={remote_tag}"),
                        ("To", f"<sip:ha@pbx.example>;tag={local_tag}"),
                        ("Call-ID", call_id),
                        ("CSeq", "1 ACK"),
                    ],
                    b"",
                )
            for _ in range(100):
                dialog = endpoint.active_dialogs.get(call_id)
                if dialog is not None and dialog.pending_ack_cseq == 0:
                    break
                await asyncio.sleep(0)
            return None

        original_read = sip_trunk._read_sip_stream_message
        sip_trunk._read_sip_stream_message = read_first_flow
        receive_task = asyncio.create_task(trunk._receive_loop())
        try:
            for _ in range(200):
                if trunk.reader is None:
                    break
                await asyncio.sleep(0)
            self.assertIsNone(trunk.reader)
            self.assertTrue(trunk._refresh_wakeup.is_set())
        finally:
            receive_task.cancel()
            await asyncio.gather(receive_task, return_exceptions=True)
            sip_trunk._read_sip_stream_message = original_read

        self.assertIn(call_id, endpoint.active_dialogs)
        self.assertEqual(terminated, [])
        local_tag = endpoint.active_dialogs[call_id].to_tag

        replacement_tx = TrunkWriter()
        trunk._tcp_writer = replacement_tx  # type: ignore[assignment]
        reinvite = invite(
            2,
            "z9hG4bKvideo",
            43000,
            to_tag=local_tag,
            source_ip="198.51.100.20",
            video=True,
        )
        await endpoint._handle_datagram(reinvite, ("198.51.100.20", 5090))
        self.assertEqual(media_updates, [(43000, "sendrecv")])
        self.assertIn(
            200,
            [sip.parse_message(raw).status_code for raw in replacement_tx.sent],
        )

        bye = sip.build_request(
            "BYE",
            "sip:ha@pbx.example",
            [
                (
                    "Via",
                    "SIP/2.0/TCP 198.51.100.20:5090;branch=z9hG4bKbye;rport",
                ),
                ("From", f"<sip:caller@pbx.example>;tag={remote_tag}"),
                ("To", f"<sip:ha@pbx.example>;tag={local_tag}"),
                ("Call-ID", call_id),
                ("CSeq", "3 BYE"),
            ],
            b"",
        )
        await endpoint._handle_datagram(bye, ("198.51.100.20", 5090))
        self.assertNotIn(call_id, endpoint.active_dialogs)
        self.assertEqual(terminated, [(call_id, "remote_hangup")])

        # A delayed retransmission of the original INVITE must not recreate a
        # routed call after BYE has already released its endpoint ownership.
        await endpoint._handle_datagram(first_invite, ("192.0.2.10", 5060))
        self.assertEqual(routed_invites, [call_id])
        self.assertEqual(
            sip.parse_message(replacement_tx.sent[-1]).status_code,
            481,
        )

    async def test_udp_message_dispatches_once_and_enforces_safe_size(self) -> None:
        sent: list[bytes] = []
        received: list[tuple[str, tuple[str, int], str]] = []

        async def on_invite(_invite):
            raise AssertionError("not used")

        async def on_request(request, addr, transport, _send_follow_up):
            received.append((request.body.decode(), addr, transport))
            return sip_listener.SipRequestResult()

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="127.0.0.1",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=on_invite,
            on_request=on_request,
            send_override=lambda raw, _addr: not sent.append(raw),
        )
        headers = [
            ("Via", "SIP/2.0/UDP 127.0.0.2:5060;branch=z9hG4bKmessage"),
            ("From", "<sip:alice@localhost>;tag=sender"),
            ("To", "<sip:ha@localhost>"),
            ("Call-ID", "message-call"),
            ("CSeq", "1 MESSAGE"),
            ("Content-Type", "text/plain"),
        ]
        request = sip.build_request("MESSAGE", "sip:ha@localhost", headers, b"hello")

        await endpoint._handle_datagram(request, ("127.0.0.2", 5060))
        await endpoint._handle_datagram(request, ("127.0.0.2", 5060))

        self.assertEqual(received, [("hello", ("127.0.0.2", 5060), "UDP")])
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)

        oversized = sip.build_request(
            "MESSAGE",
            "sip:ha@localhost",
            headers,
            b"x" * 1300,
        )
        await endpoint._handle_datagram(oversized, ("127.0.0.2", 5060))
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 513)
        self.assertEqual(len(received), 1)

    async def test_subscribe_response_precedes_owned_notify_transaction(self) -> None:
        sent: list[bytes] = []

        async def on_invite(_invite):
            raise AssertionError("not used")

        async def on_request(request, _addr, _transport, _send_follow_up):
            self.assertEqual(request.method, "SUBSCRIBE")
            return sip_listener.SipRequestResult(
                headers=(("Expires", "300"),),
                to_tag="server-tag",
                follow_up=sip_listener.SipFollowUpRequest(
                    method="NOTIFY",
                    headers=(
                        ("Event", "presence"),
                        ("Subscription-State", "active;expires=300"),
                    ),
                    body=b"<presence/>",
                    content_type="application/pidf+xml",
                ),
            )

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="127.0.0.1",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=on_invite,
            on_request=on_request,
            send_override=lambda raw, _addr: not sent.append(raw),
        )
        subscribe = sip.build_request(
            "SUBSCRIBE",
            "sip:alice@localhost",
            [
                ("Via", "SIP/2.0/UDP 127.0.0.2:5060;branch=z9hG4bKsubscribe"),
                ("From", "<sip:bob@localhost>;tag=subscriber"),
                ("To", "<sip:alice@localhost>"),
                ("Contact", "<sip:bob@127.0.0.2:5060>"),
                ("Call-ID", "subscribe-call"),
                ("CSeq", "1 SUBSCRIBE"),
                ("Event", "presence"),
                ("Expires", "300"),
            ],
        )

        await endpoint._handle_datagram(subscribe, ("127.0.0.2", 5060))
        for _ in range(100):
            parsed = [sip.parse_message(raw) for raw in sent]
            notify = next((item for item in parsed if item.method == "NOTIFY"), None)
            if notify is not None:
                break
            await asyncio.sleep(0)
        else:
            self.fail("NOTIFY was not sent")

        self.assertEqual(parsed[0].status_code, 200)
        self.assertEqual(sip.extract_tag(parsed[0].header("To")), "server-tag")
        self.assertEqual(notify.header("Subscription-State"), "active;expires=300")
        response_headers = [
            *(('Via', value) for value in notify.header_values("Via")),
            ("From", notify.header("From")),
            ("To", notify.header("To")),
            ("Call-ID", notify.header("Call-ID")),
            ("CSeq", notify.header("CSeq")),
        ]
        await endpoint._handle_datagram(
            sip.build_response(200, "OK", response_headers),
            ("127.0.0.2", 5060),
        )
        await endpoint.wait_closed()
        self.assertFalse(endpoint._maintenance_tasks)

    async def test_inbound_dialog_commits_offerless_reinvite_answer_from_ack(self) -> None:
        sent: list[bytes] = []
        committed: list[int] = []
        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)

        def answer(invite):
            return sdp.build_answer_directional(
                "127.0.0.1",
                "127.0.0.1",
                41000,
                invite.send_format,
                invite.recv_format,
                remote_sdp=invite.remote_sdp,
            )

        async def on_invite(invite):
            return sip_listener.SipInviteResult(
                200, "OK", answer_sdp=answer(invite)
            )

        async def on_media_update(_previous, updated, method):
            self.assertEqual(method, "INVITE")

            async def commit() -> None:
                committed.append(updated.remote_rtp_port)

            return sip_listener.SipInviteResult(200, "OK", commit=commit)

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="127.0.0.1",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[pcm],
            on_invite=on_invite,
            on_media_update=on_media_update,
            send_override=lambda raw, _addr: not sent.append(raw),
        )
        call_id = "offerless-inbound"
        remote_tag = "remote"

        def request(
            method: str,
            cseq: int,
            branch: str,
            *,
            to_tag: str = "",
            body: bytes = b"",
        ) -> bytes:
            headers = [
                ("Via", f"SIP/2.0/UDP 127.0.0.2:5060;branch={branch}"),
                ("From", f"<sip:peer@127.0.0.2>;tag={remote_tag}"),
                ("To", "<sip:HA@127.0.0.1>" + (f";tag={to_tag}" if to_tag else "")),
                ("Contact", "<sip:peer@127.0.0.2:5060>"),
                ("Call-ID", call_id),
                ("CSeq", f"{cseq} {method}"),
            ]
            if body:
                headers.append(("Content-Type", "application/sdp"))
            return sip.build_request(
                method,
                "sip:HA@127.0.0.1:5060",
                headers,
                body,
            )

        initial_offer = sdp.build_offer_directional(
            "127.0.0.2", "127.0.0.2", 42000, [pcm], [pcm]
        ).encode()
        await endpoint._handle_datagram(
            request("INVITE", 1, "z9hG4bKinitial", body=initial_offer),
            ("127.0.0.2", 5060),
        )
        initial_200 = next(
            message
            for raw in sent
            if (message := sip.parse_message(raw)).status_code == 200
            and message.header("CSeq") == "1 INVITE"
        )
        local_tag = sip.extract_tag(initial_200.header("To"))
        await endpoint._handle_datagram(
            request("ACK", 1, "z9hG4bKack1", to_tag=local_tag),
            ("127.0.0.2", 5060),
        )

        await endpoint._handle_datagram(
            request("INVITE", 2, "z9hG4bKofferless", to_tag=local_tag),
            ("127.0.0.2", 5060),
        )
        delayed_offer = next(
            message.body
            for raw in reversed(sent)
            if (message := sip.parse_message(raw)).status_code == 200
            and message.header("CSeq") == "2 INVITE"
        )
        negotiated = sdp.audio_format_to_rtp(pcm, 96)
        delayed_answer = sdp.build_answer_directional(
            "127.0.0.2",
            "127.0.0.2",
            43000,
            negotiated,
            negotiated,
            remote_sdp=delayed_offer,
        ).encode()
        await endpoint._handle_datagram(
            request(
                "ACK",
                2,
                "z9hG4bKack2",
                to_tag=local_tag,
                body=delayed_answer,
            ),
            ("127.0.0.2", 5060),
        )

        self.assertEqual(committed, [43000])
        self.assertIsNone(endpoint.active_dialogs[call_id].delayed_offer)
        await endpoint._handle_datagram(
            request("BYE", 3, "z9hG4bKbye", to_tag=local_tag),
            ("127.0.0.2", 5060),
        )
        self.assertNotIn(call_id, endpoint.active_dialogs)

    async def test_udp_endpoint_caps_concurrent_handler_tasks(self) -> None:
        async def on_invite(_invite):
            raise AssertionError("not used")

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="127.0.0.1",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=on_invite,
        )
        release = asyncio.Event()

        async def blocked_handler(_data, _addr):
            await release.wait()

        endpoint._handle_datagram = blocked_handler  # type: ignore[method-assign]
        for index in range(100):
            endpoint.datagram_received(b"test", ("127.0.0.1", index))
        await asyncio.sleep(0)

        self.assertEqual(len(endpoint._request_tasks), sip_listener._MAX_SIP_INVITE_TASKS)
        self.assertEqual(endpoint.dropped_datagrams, 100 - sip_listener._MAX_SIP_INVITE_TASKS)
        release.set()
        await asyncio.gather(*tuple(endpoint._request_tasks))

    async def test_udp_endpoint_reserves_control_capacity_under_invite_load(self) -> None:
        async def on_invite(_invite):
            raise AssertionError("not used")

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="127.0.0.1",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=on_invite,
        )
        release = asyncio.Event()

        async def blocked_handler(_data, _addr):
            await release.wait()

        endpoint._handle_datagram = blocked_handler  # type: ignore[method-assign]
        for index in range(100):
            endpoint.datagram_received(b"INVITE sip:test SIP/2.0\r\n\r\n", ("127.0.0.1", index))
        for index in range(8):
            endpoint.datagram_received(b"CANCEL sip:test SIP/2.0\r\n\r\n", ("127.0.0.1", index))
        await asyncio.sleep(0)

        self.assertEqual(len(endpoint._invite_tasks), sip_listener._MAX_SIP_INVITE_TASKS)
        self.assertEqual(len(endpoint._request_tasks), sip_listener._MAX_SIP_UDP_TASKS)
        release.set()
        await asyncio.gather(*tuple(endpoint._request_tasks))

    async def test_trunk_reserves_control_capacity_under_invite_load(self) -> None:
        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="udp",
            server="127.0.0.1",
            port=5060,
            domain="127.0.0.1",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
        )
        trunk = sip_trunk.SipTrunkClient(config=config, local_ip="127.0.0.1", local_sip_port=5060)
        release = asyncio.Event()

        async def blocked_handler(_data, _addr):
            await release.wait()

        trunk.request_handler = blocked_handler
        for index in range(100):
            trunk._submit_request(b"invite", ("127.0.0.1", index), "INVITE")
        for index in range(8):
            trunk._submit_request(b"cancel", ("127.0.0.1", index), "CANCEL")
        await asyncio.sleep(0)

        self.assertEqual(len(trunk._invite_tasks), sip_trunk._MAX_TRUNK_INVITE_TASKS)
        self.assertEqual(len(trunk._request_tasks), sip_trunk._MAX_TRUNK_REQUEST_TASKS)
        release.set()
        await asyncio.gather(*tuple(trunk._request_tasks))

    async def test_udp_read_response_timeout_returns_none(self) -> None:
        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            signaling_transport="UDP",
        )

        self.assertIsNone(await client._read_response(0.001))

    async def test_final_response_waiter_stops_on_transport_failure(self) -> None:
        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        client._pending_target = "P4"
        client._pending_remote_host = "192.0.2.10"
        client._pending_remote_sip_port = 5060
        client._invite_transaction_active = True
        reads = 0

        async def read_response(_timeout: float):
            nonlocal reads
            reads += 1
            raise OSError("socket closed")

        client._read_response = read_response  # type: ignore[method-assign]

        self.assertEqual(
            await client.wait_for_final(timeout=1),
            "transport_unreachable",
        )
        self.assertEqual(reads, 1)
        self.assertFalse(client._invite_transaction_active)
        self.assertEqual(client.last_sip_event, "TRANSPORT_ERROR")

    async def test_final_response_waiter_skips_one_malformed_message(self) -> None:
        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        reads = 0

        async def read_response(_timeout: float):
            nonlocal reads
            reads += 1
            if reads == 1:
                raise sip.SipError("malformed packet")
            return None

        client._read_response = read_response  # type: ignore[method-assign]

        self.assertEqual(await client.wait_for_final(timeout=1), "timeout")
        self.assertEqual(reads, 2)

    async def test_invite_honors_retry_after_once_as_a_new_transaction(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        response_count = 0

        async def read_response(_timeout: float):
            nonlocal response_count
            response_count += 1
            status, reason = (
                (503, "Service Unavailable")
                if response_count == 1
                else (180, "Ringing")
            )
            headers = [
                (
                    "Via",
                    "SIP/2.0/UDP 192.168.1.10:5060;"
                    f"branch={client.dialog_ids.branch}",
                ),
                (
                    "From",
                    "<sip:HA@192.168.1.10:5060>;"
                    f"tag={client.dialog_ids.local_tag}",
                ),
                ("To", "<sip:P4@192.0.2.10>;tag=remote"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", f"{client._invite_cseq} INVITE"),
            ]
            if status == 503:
                headers.append(("Retry-After", "0"))
            return (
                sip.parse_message(sip.build_response(status, reason, headers)),
                ("192.0.2.10", 5060),
            )

        client._read_response = read_response  # type: ignore[method-assign]
        with patch.object(sip_client, "_MIN_INVITE_RETRY_AFTER", 0.001):
            result = await client.invite(
                target="P4",
                remote_host="192.0.2.10",
                remote_sip_port=5060,
                request_uri="sip:P4@192.0.2.10:5060;transport=udp",
            )

        self.assertEqual(result, "ringing")
        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        self.assertEqual(
            [message.method for message in messages],
            ["INVITE", "ACK", "INVITE"],
        )
        first, _ack, second = messages
        self.assertEqual(first.header("Call-ID"), second.header("Call-ID"))
        self.assertEqual(first.header("From"), second.header("From"))
        self.assertEqual(first.header("To"), second.header("To"))
        self.assertEqual(
            sip.parse_cseq(second.header("CSeq")).number,
            sip.parse_cseq(first.header("CSeq")).number + 1,
        )
        self.assertNotEqual(
            sip.parse_via(first.header("Via")).branch,
            sip.parse_via(second.header("Via")).branch,
        )

    async def test_authenticated_retry_after_advances_digest_nonce_count(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="17770000000",
            local_sip_port=5060,
            local_rtp_port=41000,
            username="17770000000",
            auth_username="17770000000",
            password="secret",
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        response_count = 0

        async def read_response(_timeout: float):
            nonlocal response_count
            response_count += 1
            if response_count == 1:
                status, reason = 407, "Proxy Authentication Required"
            elif response_count == 2:
                status, reason = 503, "Service Unavailable"
            else:
                status, reason = 180, "Ringing"
            headers = [
                (
                    "Via",
                    "SIP/2.0/UDP 192.168.1.10:5060;"
                    f"branch={client.dialog_ids.branch}",
                ),
                (
                    "From",
                    "<sip:17770000000@192.168.1.10:5060>;"
                    f"tag={client.dialog_ids.local_tag}",
                ),
                ("To", "<sip:P4@sip.example>;tag=provider"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", f"{client._invite_cseq} INVITE"),
            ]
            if status == 407:
                headers.append(
                    (
                        "Proxy-Authenticate",
                        'Digest realm="sip.example", nonce="nonce", qop="auth"',
                    )
                )
            if status == 503:
                headers.append(("Retry-After", "0"))
            return (
                sip.parse_message(sip.build_response(status, reason, headers)),
                ("192.0.2.10", 5060),
            )

        client._read_response = read_response  # type: ignore[method-assign]
        with patch.object(sip_client, "_MIN_INVITE_RETRY_AFTER", 0.001):
            result = await client.invite(
                target="P4",
                remote_host="192.0.2.10",
                remote_sip_port=5060,
                request_uri="sip:P4@sip.example:5060;transport=udp",
            )

        self.assertEqual(result, "ringing")
        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        invites = [message for message in messages if message.method == "INVITE"]
        self.assertEqual(len(invites), 3)
        first_auth = sip_auth.parse_digest_challenge(
            invites[1].header("Proxy-Authorization")
        )
        retry_auth = sip_auth.parse_digest_challenge(
            invites[2].header("Proxy-Authorization")
        )
        self.assertEqual(first_auth["nc"], "00000001")
        self.assertEqual(retry_auth["nc"], "00000002")
        self.assertNotEqual(first_auth["cnonce"], retry_auth["cnonce"])

    async def test_initial_delayed_offer_is_answered_in_ack(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        remote_offer = sdp.build_offer_directional(
            "192.0.2.10",
            "192.0.2.10",
            42000,
            client.supported_recv_formats,
            client.supported_send_formats,
        ).encode()

        async def read_response(_timeout: float):
            return (
                sip.parse_message(
                    sip.build_response(
                        200,
                        "OK",
                        [
                            (
                                "Via",
                                "SIP/2.0/UDP 192.168.1.10:5060;"
                                f"branch={client.dialog_ids.branch}",
                            ),
                            (
                                "From",
                                "<sip:HA@192.168.1.10:5060>;"
                                f"tag={client.dialog_ids.local_tag}",
                            ),
                            ("To", "<sip:P4@192.0.2.10>;tag=remote"),
                            ("Contact", "<sip:P4@192.0.2.10:5060>"),
                            ("Call-ID", client.dialog_ids.call_id),
                            ("CSeq", f"{client._invite_cseq} INVITE"),
                            ("Content-Type", "application/sdp"),
                        ],
                        remote_offer,
                    )
                ),
                ("192.0.2.10", 5060),
            )

        client._read_response = read_response  # type: ignore[method-assign]
        result = await client.invite(
            target="P4",
            remote_host="192.0.2.10",
            remote_sip_port=5060,
            request_uri="sip:P4@192.0.2.10:5060;transport=udp",
            delayed_offer=True,
        )

        self.assertEqual(result, "in_call")
        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        self.assertEqual([message.method for message in messages], ["INVITE", "ACK"])
        invite, ack = messages
        self.assertFalse(invite.body)
        self.assertFalse(invite.header("Content-Type"))
        self.assertEqual(ack.header("Content-Type"), "application/sdp")
        sdp.validate_sdp_answer(remote_offer, ack.body)
        self.assertIsNotNone(client.dialog)
        assert client.dialog is not None
        self.assertEqual(client.dialog.remote_rtp_port, 42000)

    async def test_invite_retry_after_is_limited_to_one_retry(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]

        async def read_response(_timeout: float):
            return (
                sip.parse_message(
                    sip.build_response(
                        503,
                        "Service Unavailable",
                        [
                            (
                                "Via",
                                "SIP/2.0/UDP 192.168.1.10:5060;"
                                f"branch={client.dialog_ids.branch}",
                            ),
                            (
                                "From",
                                "<sip:HA@192.168.1.10:5060>;"
                                f"tag={client.dialog_ids.local_tag}",
                            ),
                            ("To", "<sip:P4@192.0.2.10>;tag=remote"),
                            ("Call-ID", client.dialog_ids.call_id),
                            ("CSeq", f"{client._invite_cseq} INVITE"),
                            ("Retry-After", "0"),
                        ],
                    )
                ),
                ("192.0.2.10", 5060),
            )

        client._read_response = read_response  # type: ignore[method-assign]
        with patch.object(sip_client, "_MIN_INVITE_RETRY_AFTER", 0.001):
            result = await client.invite(
                target="P4",
                remote_host="192.0.2.10",
                remote_sip_port=5060,
                request_uri="sip:P4@192.0.2.10:5060;transport=udp",
            )

        self.assertEqual(result, "sip_503")
        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        self.assertEqual(
            [message.method for message in messages],
            ["INVITE", "ACK", "INVITE", "ACK"],
        )

    async def test_retry_after_also_replaces_a_proceeding_invite(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        audio_rtp = sdp.audio_format_to_rtp(pcm, 96)
        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[pcm],
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        response_count = 0

        async def read_response(_timeout: float):
            nonlocal response_count
            response_count += 1
            invite = next(
                sip.parse_message(raw)
                for raw, _addr in reversed(transport.sent)
                if sip.parse_message(raw).method == "INVITE"
            )
            status, reason = (
                (180, "Ringing")
                if response_count == 1
                else (503, "Service Unavailable")
                if response_count == 2
                else (200, "OK")
            )
            headers = [
                ("Via", invite.header("Via")),
                ("From", invite.header("From")),
                ("To", "<sip:P4@192.0.2.10>;tag=remote"),
                ("Call-ID", invite.header("Call-ID")),
                ("CSeq", invite.header("CSeq")),
            ]
            body = b""
            if status == 503:
                headers.append(("Retry-After", "0"))
            elif status == 200:
                headers.extend(
                    [
                        ("Contact", "<sip:P4@192.0.2.10:5060>"),
                        ("Content-Type", "application/sdp"),
                    ]
                )
                body = sdp.build_answer_directional(
                    "192.0.2.10",
                    "192.0.2.10",
                    42000,
                    audio_rtp,
                    audio_rtp,
                    remote_sdp=invite.body,
                ).encode()
            return (
                sip.parse_message(
                    sip.build_response(status, reason, headers, body)
                ),
                ("192.0.2.10", 5060),
            )

        client._read_response = read_response  # type: ignore[method-assign]
        self.assertEqual(
            await client.invite(
                target="P4",
                remote_host="192.0.2.10",
                remote_sip_port=5060,
                request_uri="sip:P4@192.0.2.10:5060;transport=udp",
            ),
            "ringing",
        )
        with patch.object(sip_client, "_MIN_INVITE_RETRY_AFTER", 0.001):
            self.assertEqual(await client.wait_for_final(), "in_call")

        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        self.assertEqual(
            [message.method for message in messages],
            ["INVITE", "ACK", "INVITE", "ACK"],
        )

    async def test_direct_200_ok_prefers_remote_display_identity(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        audio_rtp = sdp.audio_format_to_rtp(pcm, 96)
        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[pcm],
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]

        async def read_response(_timeout: float):
            invite = sip.parse_message(transport.sent[-1][0])
            answer = sdp.build_answer_directional(
                "192.0.2.10",
                "192.0.2.10",
                42000,
                audio_rtp,
                audio_rtp,
                remote_sdp=invite.body,
            ).encode()
            return (
                sip.parse_message(
                    sip.build_response(
                        200,
                        "OK",
                        [
                            ("Via", invite.header("Via")),
                            ("From", invite.header("From")),
                            (
                                "To",
                                '"Portineria" <sip:1000@sip.example>;tag=remote',
                            ),
                            ("Call-ID", invite.header("Call-ID")),
                            ("CSeq", invite.header("CSeq")),
                            (
                                "Contact",
                                '"Waveshare_P4_Touch" <sip:dialog@192.0.2.10:5060>',
                            ),
                            ("Content-Type", "application/sdp"),
                        ],
                        answer,
                    )
                ),
                ("192.0.2.10", 5060),
            )

        client._read_response = read_response  # type: ignore[method-assign]
        result = await client.invite(
            target="1000",
            remote_host="192.0.2.10",
            remote_sip_port=5060,
            request_uri="sip:1000@sip.example:5060;transport=udp",
        )

        self.assertEqual(result, "in_call")
        self.assertEqual(client.connected_party, "Portineria")
        self.assertEqual(
            sip.name_addr_identity("<sip:1000@sip.example>"),
            "1000",
        )

    async def test_proxy_auth_retry_uses_trunk_identity(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="17770000000",
            local_sip_port=5060,
            local_rtp_port=41000,
            local_video_rtp_port=41002,
            video_formats=(sdp.DEFAULT_H264_FORMAT,),
            video_direction="sendrecv",
            username="17770000000",
            auth_username="17770000000",
            password="secret",
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        response_count = 0

        async def read_response(_timeout: float):
            nonlocal response_count
            response_count += 1
            status, reason = (
                (401, "Unauthorized")
                if response_count == 1
                else (407, "Proxy Authentication Required")
                if response_count == 2
                else (180, "Ringing")
            )
            headers = [
                ("Via", f"SIP/2.0/UDP 192.168.1.10:5060;branch={client.dialog_ids.branch}"),
                ("From", f"<sip:17770000000@192.168.1.10:5060>;tag={client.dialog_ids.local_tag}"),
                ("To", "<sip:+15551234567@sip.example>;tag=provider"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", f"{client._invite_cseq} INVITE"),
            ]
            if status == 401:
                headers.extend(
                    (
                        (
                            "WWW-Authenticate",
                            'Digest realm="sip.example", nonce="md5", '
                            'algorithm=MD5, qop="auth"',
                        ),
                        (
                            "WWW-Authenticate",
                            'Digest realm="sip.example", nonce="sha", '
                            'algorithm=SHA-256, qop="auth"',
                        ),
                    )
                )
            if status == 407:
                headers.append(
                    (
                        "Proxy-Authenticate",
                        'Digest realm="sip.example", nonce="proxy", '
                        'algorithm=SHA-512-256, qop="auth"',
                    )
                )
            return sip.parse_message(sip.build_response(status, reason, headers)), ("192.0.2.10", 5060)

        client._read_response = read_response  # type: ignore[method-assign]
        result = await client.invite(
            target="+15551234567",
            remote_host="192.0.2.10",
            remote_sip_port=5060,
            request_uri="sip:+15551234567@sip.example:5060;transport=udp",
        )

        self.assertEqual(result, "ringing")
        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        invites = [message for message in messages if message.method == "INVITE"]
        self.assertEqual(len(invites), 3)
        self.assertEqual(invites[0].header("From"), invites[2].header("From"))
        self.assertTrue(
            invites[0]
            .header("From")
            .startswith(
                '"17770000000" <sip:17770000000@192.168.1.10:5060'
            )
        )
        self.assertTrue(invites[0].header("Contact").startswith("<sip:17770000000@192.168.1.10:5060"))
        self.assertEqual(invites[0].header("X-Voip-Stack-Caller-Name"), "17770000000")
        self.assertEqual(invites[2].header("X-Voip-Stack-Caller-Name"), "17770000000")
        self.assertEqual(invites[0].body, invites[2].body)
        video_payload = sdp.DEFAULT_H264_FORMAT.payload_type
        self.assertIn(
            f"m=video 41002 RTP/AVP {video_payload}".encode(),
            invites[0].body,
        )
        self.assertIn(
            f"a=rtpmap:{video_payload} H264/90000".encode(),
            invites[0].body,
        )
        self.assertIn(b"a=sendrecv", invites[0].body)
        self.assertFalse(invites[0].header("Proxy-Authorization"))
        self.assertEqual(
            sip_auth.parse_digest_challenge(invites[1].header("Authorization"))[
                "algorithm"
            ],
            "SHA-256",
        )
        self.assertTrue(invites[2].header("Authorization"))
        self.assertIn(
            'username="17770000000"',
            invites[2].header("Proxy-Authorization"),
        )
        self.assertIn(
            'uri="sip:+15551234567@sip.example:5060;transport=udp"',
            invites[2].header("Proxy-Authorization"),
        )
        self.assertNotEqual(invites[0].header("Via"), invites[2].header("Via"))
        self.assertEqual(
            sip.parse_cseq(invites[2].header("CSeq")).number,
            sip.parse_cseq(invites[0].header("CSeq")).number + 2,
        )

    async def test_pending_cancel_waits_for_provisional_then_terminates_invite(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="420",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        response_count = 0

        async def read_response(_timeout: float):
            nonlocal response_count
            response_count += 1
            if response_count == 1:
                self.assertTrue(client.request_cancel())
                status, reason, method = 100, "Trying", "INVITE"
            elif response_count == 2:
                status, reason, method = 200, "OK", "CANCEL"
            else:
                status, reason, method = 487, "Request Terminated", "INVITE"
            headers = [
                ("Via", f"SIP/2.0/UDP 192.168.1.10:5060;branch={client.dialog_ids.branch}"),
                ("From", f"<sip:420@192.168.1.10:5060>;tag={client.dialog_ids.local_tag}"),
                ("To", "<sip:3519968203@sip.example>;tag=provider"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", f"{client._invite_cseq} {method}"),
            ]
            return sip.parse_message(sip.build_response(status, reason, headers)), ("192.0.2.10", 5060)

        client._read_response = read_response  # type: ignore[method-assign]
        result = await client.invite(
            target="3519968203",
            remote_host="192.0.2.10",
            remote_sip_port=5060,
            request_uri="sip:3519968203@sip.example:5060;transport=udp",
        )

        self.assertEqual(result, "cancelled")
        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        self.assertEqual([message.method for message in messages], ["INVITE", "CANCEL", "ACK"])
        invite, cancel, ack = messages
        self.assertEqual(sip.parse_cseq(cancel.header("CSeq")).number, sip.parse_cseq(invite.header("CSeq")).number)
        self.assertEqual(sip.parse_cseq(ack.header("CSeq")).number, sip.parse_cseq(invite.header("CSeq")).number)
        self.assertEqual(sip.parse_via(cancel.header("Via")).branch, sip.parse_via(invite.header("Via")).branch)
        self.assertEqual(sip.parse_via(ack.header("Via")).branch, sip.parse_via(invite.header("Via")).branch)

    async def test_invite_transaction_survives_owner_task_cancellation(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        responses: asyncio.Queue[tuple[int, str, str]] = asyncio.Queue()

        async def read_response(_timeout: float):
            status, reason, method = await responses.get()
            headers = [
                ("Via", f"SIP/2.0/UDP 192.168.1.10:5060;branch={client.dialog_ids.branch}"),
                ("From", f"<sip:HA@192.168.1.10:5060>;tag={client.dialog_ids.local_tag}"),
                ("To", "<sip:ESP@192.0.2.10>;tag=remote"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", f"{client._invite_cseq} {method}"),
            ]
            return sip.parse_message(sip.build_response(status, reason, headers)), ("192.0.2.10", 5060)

        client._read_response = read_response  # type: ignore[method-assign]
        owner = asyncio.create_task(
            client.invite(
                target="ESP",
                remote_host="192.0.2.10",
                remote_sip_port=5060,
                request_uri="sip:ESP@192.0.2.10:5060;transport=udp",
            )
        )
        while not transport.sent:
            await asyncio.sleep(0)
        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner
        self.assertIsNotNone(client._invite_task)
        assert client._invite_task is not None
        self.assertFalse(client._invite_task.done())

        responses.put_nowait((100, "Trying", "INVITE"))
        responses.put_nowait((200, "OK", "CANCEL"))
        responses.put_nowait((487, "Request Terminated", "INVITE"))
        self.assertEqual(await client._invite_task, "cancelled")
        self.assertEqual(
            [sip.parse_message(raw).method for raw, _addr in transport.sent],
            ["INVITE", "CANCEL", "ACK"],
        )

    async def test_invite_treats_183_session_progress_as_ringing(self) -> None:
        sent: list[bytes] = []
        responses: asyncio.Queue[bytes] = asyncio.Queue()
        responses.put_nowait(
            sip.build_response(
                183,
                "Session Progress",
                [
                    ("Via", "SIP/2.0/TCP 192.168.1.10:5060;branch=z9hG4bKorig"),
                    ("From", "<sip:420@192.168.1.10>;tag=ltag"),
                    ("To", "<sip:3519968203@provider.example>;tag=rtag"),
                    ("Call-ID", "progress-call"),
                    ("CSeq", "1 INVITE"),
                ],
                b"",
            )
        )
        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="420",
            local_sip_port=5060,
            local_rtp_port=41000,
            signaling_transport="TCP",
        )
        client.dialog_ids.call_id = "progress-call"
        client.dialog_ids.branch = "z9hG4bKorig"
        client.use_reused_tcp_connection(
            send=sent.append,
            responses=responses,
            close=lambda: None,
        )
        result = await client.invite(
            target="3519968203",
            remote_host="provider.example",
            remote_sip_port=5060,
            timeout=0.2,
        )
        self.assertEqual(result, "ringing")
        self.assertEqual(client.last_sip_status_code, 183)
        self.assertTrue(sent)
