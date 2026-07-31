#!/usr/bin/env python3
"""SIP media profile and transport contracts."""

from __future__ import annotations

from .voip_phase1_support import (
    _load_sip_transport_with_homeassistant_stubs,
    asyncio,
    audio_format,
    codec_capabilities,
    g722_codec,
    patch,
    rtp,
    sdp,
    sip,
    sip_client,
    sip_rtp_bridge,
    sys,
    types,
    unittest,
)


class SipProfileTest(unittest.TestCase):
    def test_explicit_empty_client_profile_never_falls_back_to_defaults(self) -> None:
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=40000,
            supported_send_formats=[],
            supported_recv_formats=[],
        )

        self.assertEqual(client.supported_send_formats, [])
        self.assertEqual(client.supported_recv_formats, [])
        with self.assertRaises(sdp.SdpError):
            sdp.build_offer_directional(
                client.local_ip,
                client.local_ip,
                client.local_rtp_port,
                client.supported_send_formats,
                client.supported_recv_formats,
            )

    def test_build_and_parse_invite_with_l16_sdp(self) -> None:
        body = sdp.build_offer(
            "192.168.1.20",
            "192.168.1.20",
            40000,
            [audio_format.AudioFormat(48000, "s16le", 1, 10)],
        ).encode()
        msg = sip.build_request(
            "INVITE",
            "sip:Cucina@192.168.1.30",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.20:5060;branch=z9hG4bKtest"),
                ("Max-Forwards", "70"),
                ("From", "<sip:Spotpear@192.168.1.20>;tag=abc"),
                ("To", "<sip:Cucina@192.168.1.30>"),
                ("Call-ID", "call-1"),
                ("CSeq", "1 INVITE"),
                ("Contact", "<sip:Spotpear@192.168.1.20:5060>"),
                ("Content-Type", "application/sdp"),
            ],
            body,
        )
        parsed = sip.parse_message(msg)
        self.assertEqual(parsed.method, "INVITE")
        self.assertEqual(parsed.uri, "sip:Cucina@192.168.1.30")
        self.assertEqual(parsed.header("Call-ID"), "call-1")
        self.assertEqual(parsed.body, body)

    def test_standard_offer_does_not_include_trunk_codecs_by_default(self) -> None:
        body = sdp.build_offer_directional(
            "192.168.1.20",
            "192.168.1.20",
            40000,
            [
                audio_format.AudioFormat(48000, "s16le", 2, 20),
                audio_format.AudioFormat(8000, "s16le", 1, 20),
            ],
            [
                audio_format.AudioFormat(48000, "s16le", 2, 20),
                audio_format.AudioFormat(8000, "s16le", 1, 20),
            ],
        )
        self.assertNotIn("OPUS/48000/2", body)
        self.assertNotIn("PCMA/8000", body)
        self.assertNotIn("PCMU/8000", body)

    def test_trunk_offer_includes_available_common_codec_fallbacks(self) -> None:
        with patch.object(
            sdp,
            "common_sip_codecs",
            return_value=frozenset({"OPUS", "G722"}),
        ):
            body = sdp.build_offer_directional(
                "192.168.1.20",
                "192.168.1.20",
                40000,
                list(audio_format.HA_TRUNK_AUDIO_FORMATS),
                list(audio_format.HA_TRUNK_AUDIO_FORMATS),
                include_common_codecs=True,
            )
        self.assertIn("m=audio 40000 RTP/AVP 98 9 8 0", body)
        self.assertIn("a=rtpmap:98 OPUS/48000/2", body)
        self.assertIn("a=rtpmap:9 G722/8000/1", body)
        self.assertIn("a=rtpmap:8 PCMA/8000/1", body)
        self.assertIn("a=rtpmap:0 PCMU/8000/1", body)
        self.assertIn("a=ptime:20", body)

    def test_trunk_offer_does_not_advertise_unavailable_optional_codecs(self) -> None:
        with patch.object(
            sdp,
            "common_sip_codecs",
            return_value=frozenset(),
        ):
            body = sdp.build_offer_directional(
                "192.168.1.20",
                "192.168.1.20",
                40000,
                list(audio_format.HA_TRUNK_AUDIO_FORMATS),
                list(audio_format.HA_TRUNK_AUDIO_FORMATS),
                include_common_codecs=True,
            )
        self.assertNotIn(" OPUS/", body)
        self.assertNotIn(" G722/", body)
        self.assertIn("a=rtpmap:8 PCMA/8000/1", body)
        self.assertIn("a=rtpmap:0 PCMU/8000/1", body)

    def test_g722_uses_16khz_pcm_with_the_rfc3551_8khz_rtp_clock(self) -> None:
        fmt = sdp.RtpPcmFormat(9, "G722", 8000, 1, 20)

        self.assertEqual(fmt.audio_format.sample_rate, 16000)
        self.assertEqual(fmt.rtp_clock_rate, 8000)
        self.assertEqual(fmt.pcm_sample_rate, 16000)
        self.assertEqual(fmt.rtp_timestamp_step, 160)
        self.assertEqual(fmt.audio_format.nominal_frame_samples, 320)

    def test_g722_static_payload_negotiates_against_16khz_pcm(self) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.30\r\n"
            "s=-\r\n"
            "c=IN IP4 192.168.1.30\r\n"
            "t=0 0\r\n"
            "m=audio 41000 RTP/AVP 9\r\n"
            "a=ptime:20\r\n"
            "a=sendrecv\r\n"
        )
        selected = sdp.negotiate_directional(
            offer,
            [audio_format.AudioFormat(16000, "s16le", 1, 20)],
            [audio_format.AudioFormat(16000, "s16le", 1, 20)],
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.send.wire_token(), "pt=9:G722/8000/1/20ms")
        self.assertEqual(selected.recv.audio_format.sample_rate, 16000)

    def test_optional_codec_capabilities_require_both_encoder_and_decoder(self) -> None:
        class CodecContext:
            @staticmethod
            def create(name: str, mode: str):
                if (name, mode) == ("libopus", "w"):
                    raise RuntimeError("encoder unavailable")
                return object()

        fake_av = types.SimpleNamespace(CodecContext=CodecContext)
        codec_capabilities.common_sip_codecs.cache_clear()
        try:
            with patch.dict(sys.modules, {"av": fake_av}):
                self.assertEqual(
                    codec_capabilities.common_sip_codecs(),
                    frozenset({"G722"}),
                )
        finally:
            codec_capabilities.common_sip_codecs.cache_clear()

    def test_g722_pyav_adapter_keeps_codec_state_and_pcm_shape(self) -> None:
        contexts: list[object] = []

        class Frame:
            def __init__(self, samples=None) -> None:
                self.samples = samples
                self.sample_rate = 0

            def to_ndarray(self):
                return g722_codec.np.arange(320, dtype="<i2").reshape(1, -1)

        class Context:
            def __init__(self, mode: str) -> None:
                self.mode = mode
                self.sample_rate = 0
                self.layout = ""
                self.format = ""
                self.bit_rate = 0
                self.opened = False
                contexts.append(self)

            def open(self) -> None:
                self.opened = True

            def decode(self, _packet):
                return [Frame()]

            def encode(self, frame):
                self.encoded_frame = frame
                return [bytes(range(160))]

        class CodecContext:
            @staticmethod
            def create(name: str, mode: str):
                self.assertEqual(name, "g722")
                return Context(mode)

        class AudioFrame:
            @staticmethod
            def from_ndarray(samples, *, format: str, layout: str):
                self.assertEqual(samples.shape, (1, 320))
                self.assertEqual((format, layout), ("s16", "mono"))
                return Frame(samples)

        fake_av = types.SimpleNamespace(
            CodecContext=CodecContext,
            AudioFrame=AudioFrame,
            Packet=lambda payload: payload,
        )
        with patch.dict(sys.modules, {"av": fake_av}):
            encoder = g722_codec.G722Encoder()
            decoder = g722_codec.G722Decoder()
            encoded = encoder.encode(bytes(640))
            decoded = decoder.decode(encoded)

        self.assertEqual(len(contexts), 2)
        self.assertTrue(contexts[0].opened)
        self.assertEqual(encoded, bytes(range(160)))
        self.assertEqual(len(decoded), 640)
        self.assertEqual(encoder.audio_format.sample_rate, 16000)
        self.assertEqual(decoder.audio_format.frame_ms, 20)

    def test_g722_relay_advances_rtp_clock_by_160_not_pcm_samples(self) -> None:
        class PassthroughCodec:
            def __init__(self, _fmt) -> None:
                pass

            def decode(self, payload: bytes) -> bytes:
                return payload

            def encode(self, _pcm: bytes) -> bytes:
                return bytes(160)

        class Transport:
            def __init__(self) -> None:
                self.sent: list[bytes] = []

            def sendto(self, packet: bytes, _addr) -> None:
                self.sent.append(packet)

        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        l16 = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
        g722 = sdp.RtpPcmFormat(9, "G722", 8000, 1, 20)
        with (
            patch.object(sip_rtp_bridge, "RtpPayloadDecoder", PassthroughCodec),
            patch.object(sip_rtp_bridge, "RtpPayloadEncoder", PassthroughCodec),
        ):
            relay = sip_rtp_bridge.SipRtpRelay(
                left=sip_rtp_bridge.RtpPeer(
                    "127.0.0.1",
                    40000,
                    96,
                    pcm,
                    rtp_format=l16,
                    send_rtp_format=l16,
                ),
                right=sip_rtp_bridge.RtpPeer(
                    "127.0.0.2",
                    40002,
                    9,
                    pcm,
                    rtp_format=g722,
                    send_rtp_format=g722,
                    timestamp=1000,
                ),
                left_port=41000,
                right_port=41002,
            )
        transport = Transport()
        relay.right_transport = transport
        packet = rtp.build_packet(
            rtp.RtpPacket(
                payload_type=96,
                sequence=1,
                timestamp=0,
                ssrc=7,
                payload=bytes(pcm.nominal_frame_bytes),
            )
        )

        relay.handle_packet("left", packet, ("127.0.0.1", 40000))

        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(rtp.parse_packet(transport.sent[0]).timestamp, 1000)
        self.assertEqual(relay.right.timestamp, 1160)

    def test_trunk_opus_answer_negotiates_48k_stereo_20ms(self) -> None:
        answer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.30\r\n"
            "s=-\r\n"
            "c=IN IP4 192.168.1.30\r\n"
            "t=0 0\r\n"
            "m=audio 41000 RTP/AVP 98\r\n"
            "a=rtpmap:98 opus/48000/2\r\n"
            "a=ptime:20\r\n"
            "a=sendrecv\r\n"
        )
        selected = sdp.negotiate_answer_directional(
            answer,
            list(audio_format.HA_TRUNK_AUDIO_FORMATS),
            list(audio_format.HA_TRUNK_AUDIO_FORMATS),
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.send.wire_token(), "pt=98:OPUS/48000/2/20ms")
        self.assertEqual(selected.recv.wire_token(), "pt=98:OPUS/48000/2/20ms")

    def test_sdp_parser_scopes_connection_and_payloads_to_selected_audio(self) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.20\r\n"
            "s=-\r\n"
            "c=IN IP4 192.168.1.21\r\n"
            "t=0 0\r\n"
            "m=audio 41000 RTP/AVP 96\r\n"
            "c=IN IP4 192.168.1.22\r\n"
            "a=rtpmap:96 L16/16000/1\r\n"
            "a=ptime:20\r\n"
            "m=video 42000 RTP/AVP 97\r\n"
            "c=IN IP4 192.168.1.99\r\n"
            "a=rtpmap:97 H264/90000\r\n"
            "m=audio 43000 RTP/AVP 98\r\n"
            "c=IN IP4 192.168.1.98\r\n"
            "a=rtpmap:98 L16/48000/1\r\n"
        )

        parsed = sdp.parse_sdp(offer)
        self.assertEqual(parsed["connection_ip"], "192.168.1.22")
        self.assertEqual(parsed["media_port"], 41000)
        self.assertEqual(parsed["payload_order"], [96])
        self.assertEqual(parsed["rtpmap"], {96: ("L16", 16000, 1)})

    def test_sdp_parser_skips_rejected_audio_and_validates_transport_ranges(self) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.20\r\n"
            "s=-\r\n"
            "c=IN IP4 192.168.1.20\r\n"
            "t=0 0\r\n"
            "m=audio 0 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\n"
            "m=audio 41000 RTP/AVP 97\r\n"
            "a=rtpmap:97 L16/48000/1\r\n"
        )
        parsed = sdp.parse_sdp(offer)
        self.assertEqual(parsed["media_port"], 41000)
        self.assertEqual(parsed["payload_order"], [97])

        with self.assertRaises(sdp.SdpError):
            sdp.parse_sdp(offer.replace("41000", "70000"))
        with self.assertRaises(sdp.SdpError):
            sdp.parse_sdp(offer.replace("RTP/AVP 97", "RTP/AVP 128"))

    def test_parser_preserves_unsupported_method_for_sip_response(self) -> None:
        raw = (
            b"REGISTER sip:ha@192.168.1.10 SIP/2.0\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        parsed = sip.parse_message(raw)
        self.assertEqual(parsed.method, "REGISTER")

    def test_ignores_datagram_bytes_after_content_length(self) -> None:
        raw = (
            b"OPTIONS sip:ha@192.168.1.10 SIP/2.0\r\n"
            b"Content-Length: 0\r\n\r\nx"
        )
        parsed = sip.parse_message(raw)
        self.assertEqual(parsed.method, "OPTIONS")
        self.assertEqual(parsed.body, b"")

    def test_parser_unfolds_bounded_sip_header_continuations(self) -> None:
        raw = (
            b"OPTIONS sip:ha@192.168.1.10 SIP/2.0\r\n"
            b"Subject: standard SIP\r\n"
            b"  continuation\r\n"
            b"Content-Length: 0\r\n\r\n"
        )

        parsed = sip.parse_message(raw)

        self.assertEqual(parsed.header("Subject"), "standard SIP continuation")

    def test_compact_sip_headers_are_canonicalized(self) -> None:
        raw = (
            b"OPTIONS sip:ha@192.168.1.10 SIP/2.0\r\n"
            b"v: SIP/2.0/UDP 192.168.1.20:5060;branch=z9hG4bKcompact\r\n"
            b"f: <sip:test@192.168.1.20>;tag=remote\r\n"
            b"t: <sip:ha@192.168.1.10>\r\n"
            b"i: compact-call\r\n"
            b"CSeq: 1 OPTIONS\r\n"
            b"l: 0\r\n\r\n"
        )
        parsed = sip.parse_message(raw)
        self.assertEqual(parsed.header("Via"), "SIP/2.0/UDP 192.168.1.20:5060;branch=z9hG4bKcompact")
        self.assertEqual(parsed.header("Call-ID"), "compact-call")
        self.assertEqual(parsed.header("Content-Length"), "0")

    def test_sip_uri_parser_accepts_display_name_address(self) -> None:
        parsed = sip.parse_sip_uri('"Kitchen phone" <sip:kitchen@192.0.2.20:5090;transport=tcp>')
        self.assertEqual(str(parsed), "sip:kitchen@192.0.2.20:5090;transport=tcp")

    def test_parser_rejects_canonical_and_compact_content_length_together(self) -> None:
        raw = b"OPTIONS sip:ha@192.168.1.10 SIP/2.0\r\nContent-Length: 0\r\nl: 0\r\n\r\n"
        with self.assertRaises(sip.SipError):
            sip.parse_message(raw)

    def test_parser_rejects_duplicate_dialog_identity_headers(self) -> None:
        raw = (
            b"OPTIONS sip:ha@192.168.1.10 SIP/2.0\r\n"
            b"Call-ID: first\r\n"
            b"i: second\r\n"
            b"CSeq: 1 OPTIONS\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        with self.assertRaises(sip.SipError):
            sip.parse_message(raw)

    def test_ack_reuses_invite_cseq_with_fresh_branch(self) -> None:
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
        client.transport = FakeTransport()  # type: ignore[assignment]
        client.dialog_ids = sip.SipDialogIds(
            call_id="call-ack",
            local_tag="local",
            remote_tag="remote",
            cseq=7,
            branch="z9hG4bKinvite",
        )
        client._invite_cseq = 7
        client._send_ack(
            "192.168.1.30",
            5060,
            "sip:Cucina@192.168.1.30",
            "sip:HA@192.168.1.10:5060",
            "sip:Cucina@192.168.1.30:5060",
        )
        raw, addr = client.transport.sent[0]  # type: ignore[union-attr]
        parsed = sip.parse_message(raw)
        self.assertEqual(addr, ("192.168.1.30", 5060))
        self.assertEqual(parsed.method, "ACK")
        self.assertEqual(parsed.header("CSeq"), "7 ACK")
        self.assertNotIn("z9hG4bKinvite", parsed.header("Via"))
        self.assertIn("SIP/2.0/UDP 192.168.1.10:5060", parsed.header("Via"))
        self.assertIn(";rport", parsed.header("Via"))

    def test_cancel_reuses_invite_transaction(self) -> None:
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
        client.transport = FakeTransport()  # type: ignore[assignment]
        client.dialog_ids = sip.SipDialogIds(
            call_id="call-cancel",
            local_tag="local",
            cseq=9,
            branch="z9hG4bKinvite",
        )
        client._invite_cseq = 9
        client._pending_request_uri = "sip:Cucina@192.168.1.30:5060"
        client._pending_local_uri = "sip:HA@192.168.1.10:5060"
        client._pending_remote_uri = "sip:Cucina@192.168.1.30:5060"
        client._pending_remote_host = "192.168.1.30"
        client._pending_remote_sip_port = 5060
        client._invite_transaction_active = True
        client._received_provisional = True

        client.cancel()

        raw, addr = client.transport.sent[0]  # type: ignore[union-attr]
        parsed = sip.parse_message(raw)
        self.assertEqual(addr, ("192.168.1.30", 5060))
        self.assertEqual(parsed.method, "CANCEL")
        self.assertEqual(parsed.header("CSeq"), "9 CANCEL")
        self.assertIn("z9hG4bKinvite", parsed.header("Via"))

    def test_tcp_ack_and_bye_use_stream_writer(self) -> None:
        sent = bytearray()
        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="Casa",
            local_sip_port=43123,
            local_rtp_port=41000,
            signaling_transport="TCP",
        )
        client.use_reused_tcp_connection(send=sent.extend, responses=asyncio.Queue(), close=lambda: None)
        client.dialog_ids = sip.SipDialogIds(
            call_id="call-tcp-dialog",
            local_tag="local",
            remote_tag="remote",
            cseq=3,
            branch="z9hG4bKinvite",
        )
        client._invite_cseq = 3
        client.dialog = sip_client.SipDialog(
            target="ESP",
            remote_host="192.168.1.30",
            remote_sip_port=5060,
            remote_rtp_host="192.168.1.30",
            remote_rtp_port=40000,
            local_rtp_port=41000,
            call_id="call-tcp-dialog",
            local_uri="sip:Casa@192.168.1.10:43123",
            remote_uri="sip:ESP@192.168.1.30:5060",
            send_format=sdp.RtpPcmFormat(96, "L16", 16000, 1, 32),
            recv_format=sdp.RtpPcmFormat(96, "L16", 16000, 1, 32),
        )

        client._send_ack(
            "192.168.1.30",
            5060,
            "sip:ESP@192.168.1.30:5060",
            "sip:Casa@192.168.1.10:43123",
            "sip:ESP@192.168.1.30:5060",
        )
        ack = sip.parse_message(bytes(sent))
        self.assertEqual(ack.method, "ACK")
        self.assertIn("SIP/2.0/TCP 192.168.1.10:43123", ack.header("Via"))

        sent.clear()
        client.bye()
        bye = sip.parse_message(bytes(sent))
        self.assertEqual(bye.method, "BYE")
        self.assertIn("SIP/2.0/TCP 192.168.1.10:43123", bye.header("Via"))

    def test_invite_carries_intercom_display_identity_headers(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="Casa",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        client.transport = FakeTransport()  # type: ignore[assignment]
        asyncio.run(client.invite(target="Cucina", remote_host="192.168.1.30", remote_sip_port=5060, timeout=0))

        raw, _ = client.transport.sent[0]  # type: ignore[union-attr]
        parsed = sip.parse_message(raw)
        self.assertEqual(parsed.header("X-Voip-Stack-Caller-Name"), "Casa")
        self.assertEqual(parsed.header("X-Voip-Stack-Caller-Route"), "Casa")
        self.assertEqual(parsed.header("X-Voip-Stack-Dest-Name"), "Cucina")

    def test_invite_preserves_registered_contact_request_uri_params(self) -> None:
        sent: list[bytes] = []
        contact_uri = "sip:Zoiper@192.168.1.50:5062;transport=tcp;ob;line=abc123"
        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="Casa",
            local_sip_port=5060,
            local_rtp_port=41000,
            signaling_transport="TCP",
        )
        client.use_reused_tcp_connection(
            send=sent.append,
            responses=asyncio.Queue(),
            close=lambda: None,
        )

        asyncio.run(
            client.invite(
                target="Zoiper",
                remote_host="192.168.1.50",
                remote_sip_port=5062,
                request_uri=contact_uri,
                timeout=0,
            )
        )

        self.assertTrue(sent)
        raw = sent[0].decode()
        self.assertTrue(raw.startswith(f"INVITE {contact_uri} SIP/2.0\r\n"))
        parsed = sip.parse_message(sent[0])
        self.assertEqual(parsed.header("To"), f"<{contact_uri}>")

    def test_tcp_invite_connection_refused_returns_transport_unreachable(self) -> None:
        async def run() -> str:
            server = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            server.close()
            await server.wait_closed()
            client = sip_client.SipCallClient(
                local_ip="127.0.0.1",
                local_name="Casa",
                local_sip_port=5060,
                local_rtp_port=41000,
                signaling_transport="TCP",
            )
            return await client.invite(
                target="TestBaresip",
                remote_host="127.0.0.1",
                remote_sip_port=port,
                timeout=0.1,
            )

        result = asyncio.run(run())

        self.assertEqual(result, "transport_unreachable")

    def test_dialog_headers_preserve_transport_port_and_rport(self) -> None:
        headers = sip.dialog_headers(
            request_uri="sip:Cucina@192.168.1.30:5070",
            local_uri="sip:Casa@192.168.1.10:43123",
            remote_uri="sip:Cucina@192.168.1.30:5070",
            dialog=sip.SipDialogIds(call_id="call-via", local_tag="local"),
            method="INVITE",
            contact_uri="sip:Casa@192.168.1.10:43123",
            transport="TCP",
        )
        via = dict(headers)["Via"]
        self.assertEqual(via.split(";", 1)[0], "SIP/2.0/TCP 192.168.1.10:43123")
        self.assertIn(";rport", via)

    def test_parse_via_and_cseq_for_transaction_matching(self) -> None:
        via = sip.parse_via("SIP/2.0/TCP 192.168.1.10:43123;branch=z9hG4bKabc;rport=43123;received=192.168.1.10")
        self.assertEqual(via.transport, "TCP")
        self.assertEqual(via.host, "192.168.1.10")
        self.assertEqual(via.port, 43123)
        self.assertEqual(via.branch, "z9hG4bKabc")
        self.assertEqual(via.rport, 43123)
        self.assertEqual(via.received, "192.168.1.10")

        cseq = sip.parse_cseq("42 INVITE")
        self.assertEqual(cseq.number, 42)
        self.assertEqual(cseq.method, "INVITE")

    def test_auth_challenge_failures_have_explicit_reasons(self) -> None:
        self.assertEqual(sip.sip_failure_reason(401), "auth_required_unsupported")
        self.assertEqual(sip.sip_failure_reason(407), "proxy_auth_required_unsupported")
        self.assertEqual(sip.sip_failure_reason(488), "media_incompatible")

    def test_sip_transport_classifies_terminal_response_reasons(self) -> None:
        sip_transport = _load_sip_transport_with_homeassistant_stubs()
        self.assertEqual(sip_transport.sip_terminal_status("busy"), ("decline", 0, "busy"))
        self.assertEqual(sip_transport.sip_terminal_status("declined"), ("decline", 0, "declined"))
        self.assertEqual(sip_transport.sip_terminal_status("cancelled"), ("decline", 0, "cancelled"))
        self.assertEqual(
            sip_transport.sip_terminal_status("media_incompatible"),
            ("error", 488, "media_incompatible"),
        )
        self.assertEqual(
            sip_transport.sip_terminal_status("auth_required_unsupported"),
            ("error", 401, "auth_required_unsupported"),
        )
        self.assertEqual(
            sip_transport.sip_terminal_status("proxy_auth_required_unsupported"),
            ("error", 407, "proxy_auth_required_unsupported"),
        )
        self.assertEqual(sip_transport.sip_terminal_status("timeout"), ("error", 408, "timeout"))
        self.assertEqual(sip_transport.sip_terminal_status("sip_500"), ("error", 500, "sip_500"))
        self.assertEqual(sip_transport.sip_public_state("sip_500"), "transport_unreachable")
        self.assertEqual(sip_transport.sip_terminal_reason("sip_500"), "sip_500")
        self.assertEqual(
            sip_transport.sip_failure_response("sip_500"),
            (480, "Temporarily Unavailable", "sip_500", "transport_unreachable"),
        )
        self.assertEqual(
            sip_transport.sip_failure_response("dnd"),
            (486, "Busy Here", "dnd", "declined"),
        )
