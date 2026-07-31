#!/usr/bin/env python3
"""SDP and PCM offer-answer contracts."""

from __future__ import annotations

from .voip_phase1_support import (
    asyncio,
    audio_format,
    audio_pcm,
    codec_capabilities,
    sdp,
    sip,
    sip_client,
    sip_listener,
    unittest,
)


class SdpPcmProfileTest(unittest.TestCase):
    def test_dahua_pcm_is_profile_gated_and_preserves_little_endian_samples(
        self,
    ) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.0.2.10\r\n"
            "s=Dahua\r\n"
            "c=IN IP4 192.0.2.10\r\n"
            "t=0 0\r\n"
            "m=audio 20000 RTP/AVP 97 0\r\n"
            "a=rtpmap:97 PCM/16000\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=ptime:20\r\n"
            "a=sendrecv\r\n"
        )
        local = [audio_format.AudioFormat(16000, "s16le", 1, 20)]

        self.assertIsNone(sdp.negotiate_directional(offer, local, local))
        selected = sdp.negotiate_directional(
            offer,
            local,
            local,
            allow_dahua_pcm=True,
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.send.encoding, "PCM")
        self.assertEqual(selected.send.audio_format, local[0])
        samples = b"\xf7\xff\x0c\x00\x0e\x00\xf3\xff"
        self.assertEqual(sip_client.pcm_to_rtp_payload(samples, selected.send), samples)
        self.assertEqual(sip_client.rtp_payload_to_pcm(samples, selected.recv), samples)

    def test_dahua_pcm_offer_is_explicit_and_does_not_replace_standard_l16(
        self,
    ) -> None:
        local = [audio_format.AudioFormat(16000, "s16le", 1, 20)]

        standard = sdp.build_offer_directional(
            "192.0.2.20",
            "192.0.2.20",
            40000,
            local,
            local,
        )
        dahua = sdp.build_offer_directional(
            "192.0.2.20",
            "192.0.2.20",
            40000,
            local,
            local,
            include_dahua_pcm=True,
        )

        self.assertNotIn(" PCM/16000", standard)
        self.assertIn(" L16/16000/1", standard)
        self.assertIn(" PCM/16000/1", dahua)
        self.assertIn(" L16/16000/1", dahua)
        self.assertEqual(
            [
                item.encoding
                for item in sdp.offered_pcm_formats(
                    dahua,
                    allow_dahua_pcm=True,
                )
            ],
            ["PCM", "L16"],
        )

    def test_dahua_pcm_rejects_wrong_rate_channels_and_static_payload(self) -> None:
        template = (
            "v=0\r\n"
            "c=IN IP4 192.0.2.10\r\n"
            "t=0 0\r\n"
            "m=audio 20000 RTP/AVP {payload}\r\n"
            "a=rtpmap:{payload} PCM/{rate}/{channels}\r\n"
            "a=ptime:20\r\n"
        )
        for payload, rate, channels in (
            (97, 8000, 1),
            (97, 16000, 2),
            (0, 16000, 1),
        ):
            with self.subTest(payload=payload, rate=rate, channels=channels):
                self.assertEqual(
                    sdp.offered_pcm_formats(
                        template.format(
                            payload=payload,
                            rate=rate,
                            channels=channels,
                        ),
                        allow_dahua_pcm=True,
                    ),
                    [],
                )

    def test_dahua_user_agent_detection_is_narrow(self) -> None:
        self.assertTrue(
            codec_capabilities.supports_dahua_pcm("Dahua UAC/3.0")
        )
        self.assertTrue(
            codec_capabilities.supports_dahua_pcm("  dahua uac/4.1 ")
        )
        self.assertFalse(codec_capabilities.supports_dahua_pcm("baresip v4.6.0"))
        self.assertFalse(codec_capabilities.supports_dahua_pcm("Dahua Camera/1.0"))

    def test_listener_accepts_dahua_pcm_only_for_the_dahua_uac_profile(self) -> None:
        local = audio_format.AudioFormat(16000, "s16le", 1, 20)
        body = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.0.2.85\r\n"
            "s=Dahua\r\n"
            "c=IN IP4 192.0.2.85\r\n"
            "t=0 0\r\n"
            "m=audio 20000 RTP/AVP 97\r\n"
            "a=rtpmap:97 PCM/16000\r\n"
            "a=ptime:20\r\n"
            "a=sendrecv\r\n"
        ).encode()

        def request(user_agent: str) -> sip.SipMessage:
            return sip.parse_message(
                sip.build_request(
                    "INVITE",
                    "sip:HA@192.0.2.10",
                    [
                        ("Via", "SIP/2.0/UDP 192.0.2.85:5060;branch=z9hG4bKdahua"),
                        ("From", "<sip:100@VDP>;tag=dahua"),
                        ("To", "<sip:HA@192.0.2.10>"),
                        ("Call-ID", f"dahua-{user_agent}"),
                        ("CSeq", "1 INVITE"),
                        ("Contact", "<sip:100@192.0.2.85:5060>"),
                        ("User-Agent", user_agent),
                        ("Content-Type", "application/sdp"),
                    ],
                    body,
                )
            )

        async def on_invite(_invite):
            return sip_listener.SipInviteResult(486, "Busy Here")

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.0.2.10",
            local_sip_port=5060,
            local_rtp_port=40000,
            supported_formats=[local],
            on_invite=on_invite,
        )

        accepted = endpoint._parse_invite(
            request("Dahua UAC/3.0"),
            ("192.0.2.85", 5060),
        )
        rejected = endpoint._parse_invite(
            request("Generic SIP Phone/1.0"),
            ("192.0.2.85", 5060),
        )

        self.assertIsNotNone(accepted)
        assert accepted is not None
        self.assertEqual(accepted.peer_profile, "dahua")
        self.assertEqual(accepted.send_format.encoding, "PCM")
        self.assertIsNone(rejected)

    def test_answer_cannot_remap_a_selected_dynamic_payload_type(self) -> None:
        offer = (
            "v=0\r\no=- 1 1 IN IP4 192.0.2.10\r\n"
            "s=offer\r\nc=IN IP4 192.0.2.10\r\nt=0 0\r\n"
            "m=audio 40000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\na=sendrecv\r\n"
        )
        answer = (
            "v=0\r\no=- 2 1 IN IP4 192.0.2.20\r\n"
            "s=answer\r\nc=IN IP4 192.0.2.20\r\nt=0 0\r\n"
            "m=audio 41000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/48000/1\r\na=sendrecv\r\n"
        )

        with self.assertRaisesRegex(sdp.SdpError, "remapped payload type 96"):
            sdp.validate_sdp_answer(offer, answer)

    def test_answer_may_use_a_different_payload_for_the_same_audio_codec(self) -> None:
        audio = audio_format.AudioFormat(16000, "s16le", 1, 20)
        offer = sdp.build_offer_directional(
            "192.0.2.10",
            "192.0.2.10",
            40000,
            [audio],
            [audio],
        )
        offered = sdp.offered_pcm_formats(offer)[0]
        answer = (
            "v=0\r\no=- 2 1 IN IP4 192.0.2.20\r\n"
            "s=answer\r\nc=IN IP4 192.0.2.20\r\nt=0 0\r\n"
            "m=audio 41000 RTP/AVP 120 121\r\n"
            "a=rtpmap:120 L16/16000/1\r\n"
            "a=rtpmap:121 telephone-event/8000\r\n"
            "a=fmtp:121 0-16\r\na=sendrecv\r\n"
        )

        sdp.validate_sdp_answer(offer, answer)
        selected = sdp.negotiate_answer_directional(
            answer,
            [audio],
            [audio],
            local_offer_sdp=offer,
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.send.payload_type, 120)
        self.assertEqual(selected.recv.payload_type, offered.payload_type)
        offered_dtmf = sdp.offered_dtmf_formats(offer)[0]
        selected_dtmf = sdp.negotiate_dtmf_answer(answer, offer)
        self.assertIsNotNone(selected_dtmf)
        assert selected_dtmf is not None
        self.assertEqual(selected_dtmf.payload_type, offered_dtmf.payload_type)
        self.assertEqual(selected_dtmf.events, frozenset(range(16)))

    def test_dtmf_negotiation_restricts_events_to_remote_fmtp(self) -> None:
        audio = audio_format.AudioFormat(8000, "s16le", 1, 20)
        offer = sdp.build_offer("192.0.2.10", "192.0.2.10", 40000, [audio])
        answer = (
            "v=0\r\no=- 2 1 IN IP4 192.0.2.20\r\n"
            "s=answer\r\nc=IN IP4 192.0.2.20\r\nt=0 0\r\n"
            "m=audio 41000 RTP/AVP 96 97\r\n"
            "a=rtpmap:96 L16/8000/1\r\n"
            "a=rtpmap:97 telephone-event/8000\r\n"
            "a=fmtp:97 1,3-4\r\na=sendrecv\r\n"
        )
        negotiated = sdp.negotiate_dtmf_answer(answer, offer)
        self.assertIsNotNone(negotiated)
        assert negotiated is not None
        self.assertEqual(negotiated.events, frozenset({1, 3, 4}))

    def test_first_offered_dtmf_format_preserves_remote_order(self) -> None:
        offer = (
            "v=0\r\no=- 1 1 IN IP4 192.0.2.10\r\n"
            "s=offer\r\nc=IN IP4 192.0.2.10\r\nt=0 0\r\n"
            "m=audio 40000 RTP/AVP 96 121 101\r\n"
            "a=rtpmap:96 L16/8000/1\r\n"
            "a=rtpmap:121 telephone-event/16000\r\n"
            "a=rtpmap:101 telephone-event/8000\r\n"
        )

        selected = sdp.first_offered_dtmf_format(offer)

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.payload_type, 121)
        self.assertEqual(selected.sample_rate, 16000)
        audio_only = (
            "v=0\r\no=- 1 1 IN IP4 192.0.2.10\r\n"
            "s=offer\r\nc=IN IP4 192.0.2.10\r\nt=0 0\r\n"
            "m=audio 40000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/8000/1\r\n"
        )
        self.assertIsNone(sdp.first_offered_dtmf_format(audio_only))

    def test_answer_cannot_add_rtcp_mux_or_feedback_capabilities(self) -> None:
        offer = (
            "v=0\r\no=- 1 1 IN IP4 192.0.2.10\r\n"
            "s=offer\r\nc=IN IP4 192.0.2.10\r\nt=0 0\r\n"
            "m=video 40002 RTP/AVPF 103\r\n"
            "a=rtpmap:103 H264/90000\r\n"
            "a=fmtp:103 packetization-mode=1;profile-level-id=42801f\r\n"
            "a=rtcp-fb:103 nack pli\r\na=sendrecv\r\n"
        )
        base_answer = offer.replace("192.0.2.10", "192.0.2.20").replace(
            "m=video 40002", "m=video 41002"
        )

        with self.subTest("rtcp-mux"), self.assertRaisesRegex(
            sdp.SdpError, "unoffered rtcp-mux"
        ):
            sdp.validate_sdp_answer(
                offer,
                base_answer.replace("a=sendrecv", "a=rtcp-mux\r\na=sendrecv"),
            )
        with self.subTest("rtcp-feedback"), self.assertRaisesRegex(
            sdp.SdpError, "unoffered RTCP feedback"
        ):
            sdp.validate_sdp_answer(
                offer,
                base_answer.replace(
                    "a=rtcp-fb:103 nack pli",
                    "a=rtcp-fb:103 nack pli\r\na=rtcp-fb:103 ccm fir",
                ),
            )

    def test_answer_feedback_can_use_an_offered_wildcard(self) -> None:
        offer = (
            "v=0\r\no=- 1 1 IN IP4 192.0.2.10\r\n"
            "s=offer\r\nc=IN IP4 192.0.2.10\r\nt=0 0\r\n"
            "m=video 40002 RTP/AVPF 103\r\n"
            "a=rtpmap:103 H264/90000\r\n"
            "a=rtcp-fb:* nack pli\r\na=sendrecv\r\n"
        )
        answer = offer.replace("192.0.2.10", "192.0.2.20").replace(
            "m=video 40002", "m=video 41002"
        ).replace("a=rtcp-fb:* nack pli", "a=rtcp-fb:103 nack pli")

        sdp.validate_sdp_answer(offer, answer)

    def test_avp_answer_feedback_is_ignored_as_inapplicable(self) -> None:
        offer = (
            "v=0\r\no=- 1 1 IN IP4 192.0.2.10\r\n"
            "s=offer\r\nc=IN IP4 192.0.2.10\r\nt=0 0\r\n"
            "m=video 40002 RTP/AVP 103\r\n"
            "a=rtpmap:103 H264/90000\r\n"
            "a=fmtp:103 packetization-mode=1;profile-level-id=42801f\r\n"
            "a=sendrecv\r\n"
        )
        answer = (
            "v=0\r\no=- 2 1 IN IP4 192.0.2.20\r\n"
            "s=answer\r\nc=IN IP4 192.0.2.20\r\nt=0 0\r\n"
            "m=video 41002 RTP/AVP 103\r\n"
            "a=rtpmap:103 H264/90000\r\n"
            "a=fmtp:103 packetization-mode=1;profile-level-id=42801f\r\n"
            "a=rtcp-fb:103 nack pli\r\n"
            "a=sendrecv\r\n"
        )

        sdp.validate_sdp_answer(offer, answer)
        selected = sdp.offered_video_formats(answer)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].transport_profile, "RTP/AVP")
        self.assertEqual(selected[0].rtcp_feedback, ())

    def test_answer_must_preserve_media_count_order_transport_and_formats(self) -> None:
        audio = audio_format.AudioFormat(16000, "s16le", 1, 20)
        offer = sdp.build_offer_directional(
            "192.0.2.10",
            "192.0.2.10",
            40000,
            [audio],
            [audio],
            video_port=40002,
            video_formats=(sdp.DEFAULT_H264_FORMAT,),
        )
        answer = sdp.build_answer_directional(
            "192.0.2.20",
            "192.0.2.20",
            41000,
            sdp.audio_format_to_rtp(audio, 96),
            sdp.audio_format_to_rtp(audio, 96),
            remote_sdp=offer,
            video_port=41002,
            video_format=sdp.DEFAULT_H264_FORMAT,
        )
        sdp.validate_sdp_answer(offer, answer)

        _session, _direction, sections = sdp._parse_media_sections(answer)
        self.assertEqual([item["media"] for item in sections], ["audio", "video"])
        cases = {
            "missing": "\r\n".join(
                line
                for line in answer.split("\r\n")
                if not line.startswith(("m=video", "a=rtpmap:103", "a=fmtp:103", "a=rtcp:41003"))
            ),
            "reordered": answer[answer.index("m=video") :] + answer[: answer.index("m=video")],
            "media-type": answer.replace("m=video ", "m=application ", 1),
            "transport": answer.replace("m=video 41002 RTP/AVP", "m=video 41002 RTP/AVPF", 1),
            "payload": answer.replace("m=video 41002 RTP/AVP 103", "m=video 41002 RTP/AVP 120", 1),
        }
        for name, invalid in cases.items():
            with self.subTest(name=name), self.assertRaises(sdp.SdpError):
                sdp.validate_sdp_answer(offer, invalid)

    def test_uac_interop_may_treat_omitted_trailing_video_as_rejected(self) -> None:
        audio = audio_format.AudioFormat(8000, "s16le", 1, 20)
        offer = sdp.build_offer_directional(
            "192.0.2.10",
            "192.0.2.10",
            40000,
            [audio],
            [audio],
            video_port=40002,
            video_formats=(sdp.DEFAULT_H264_FORMAT,),
        )
        payload = sdp.offered_pcm_formats(offer)[0]
        audio_only_answer = sdp.build_answer_directional(
            "192.0.2.20",
            "192.0.2.20",
            41000,
            payload,
            payload,
            remote_sdp=sdp.build_offer_directional(
                "192.0.2.10",
                "192.0.2.10",
                40000,
                [audio],
                [audio],
            ),
        )

        with self.assertRaisesRegex(sdp.SdpError, "count and order"):
            sdp.validate_sdp_answer(offer, audio_only_answer)
        sdp.validate_sdp_answer(
            offer,
            audio_only_answer,
            allow_omitted_trailing_media=True,
        )

    def test_answer_direction_matrix_matches_rfc3264(self) -> None:
        audio = audio_format.AudioFormat(16000, "s16le", 1, 20)
        inverse = {
            "sendrecv": {"sendrecv", "sendonly", "recvonly", "inactive"},
            "sendonly": {"recvonly", "inactive"},
            "recvonly": {"sendonly", "inactive"},
            "inactive": {"inactive"},
        }
        for offer_direction, allowed in inverse.items():
            offer = sdp.build_offer_directional(
                "192.0.2.10",
                "192.0.2.10",
                40000,
                [audio],
                [audio],
                audio_direction=offer_direction,
            )
            payload = sdp.offered_pcm_formats(offer)[0]
            for answer_direction in ("sendrecv", "sendonly", "recvonly", "inactive"):
                answer = sdp.build_answer_directional(
                    "192.0.2.20",
                    "192.0.2.20",
                    41000,
                    payload,
                    payload,
                    remote_sdp=offer,
                    audio_direction=answer_direction,
                )
                if answer_direction in allowed:
                    sdp.validate_sdp_answer(offer, answer)
                else:
                    with self.assertRaises(sdp.SdpError):
                        sdp.validate_sdp_answer(offer, answer)

    def test_sdp_origin_rewrite_preserves_identity_and_detects_real_changes(self) -> None:
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        initial = sdp.rewrite_sdp_origin(
            sdp.build_offer("192.0.2.10", "192.0.2.10", 40000, [fmt]),
            123456,
            0,
        )
        refresh = sdp.rewrite_sdp_origin(initial, 123456, 1)
        held = refresh.replace("a=sendrecv", "a=inactive")

        self.assertIn("o=- 123456 0 IN IP4 192.0.2.10", initial)
        self.assertIn("o=- 123456 1 IN IP4 192.0.2.10", refresh)
        self.assertFalse(sdp.sdp_description_changed(initial, refresh))
        self.assertTrue(sdp.sdp_description_changed(refresh, held))

    def test_static_audio_payload_type_cannot_be_remapped(self) -> None:
        invalid = (
            "v=0\r\nc=IN IP4 192.0.2.10\r\nt=0 0\r\n"
            "m=audio 40000 RTP/AVP 0\r\n"
            "a=rtpmap:0 L16/16000/1\r\na=ptime:20\r\n"
        )
        self.assertEqual(sdp.offered_pcm_formats(invalid), [])

        canonical = invalid.replace("L16/16000/1", "PCMU/8000/1")
        offered = sdp.offered_pcm_formats(canonical)
        self.assertEqual(
            [(item.payload_type, item.encoding, item.sample_rate) for item in offered],
            [(0, "PCMU", 8000)],
        )

    def test_audio_direction_is_parsed_and_answered_per_rfc3264(self) -> None:
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        selected = sdp.audio_format_to_rtp(fmt, 96)

        for remote_direction, local_direction in (
            ("sendrecv", "sendrecv"),
            ("sendonly", "recvonly"),
            ("recvonly", "sendonly"),
            ("inactive", "inactive"),
        ):
            with self.subTest(remote_direction=remote_direction):
                offer = sdp.build_offer_directional(
                    "192.0.2.10",
                    "192.0.2.10",
                    40000,
                    [fmt],
                    [fmt],
                    audio_direction=remote_direction,
                )
                self.assertEqual(sdp.parse_sdp(offer)["direction"], remote_direction)
                answer = sdp.build_answer_directional(
                    "192.0.2.20",
                    "192.0.2.20",
                    41000,
                    selected,
                    selected,
                    remote_sdp=offer,
                )
                audio = sdp.parse_sdp(answer)
                self.assertEqual(audio["direction"], local_direction)

    def test_audio_direction_defaults_to_sendrecv_and_rejects_bad_values(self) -> None:
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        offer = sdp.build_offer("192.0.2.10", "192.0.2.10", 40000, [fmt])
        self.assertEqual(sdp.parse_sdp(offer)["direction"], "sendrecv")
        with self.assertRaises(sdp.SdpError):
            sdp.build_offer_directional(
                "192.0.2.10",
                "192.0.2.10",
                40000,
                [fmt],
                [fmt],
                audio_direction="sideways",
            )

    def test_legacy_zero_connection_hold_suppresses_only_local_send(self) -> None:
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        selected = sdp.audio_format_to_rtp(fmt, 96)

        for remote_direction, local_direction in (
            ("sendrecv", "recvonly"),
            ("sendonly", "recvonly"),
            ("recvonly", "inactive"),
            ("inactive", "inactive"),
        ):
            with self.subTest(remote_direction=remote_direction):
                offer = sdp.build_offer_directional(
                    "192.0.2.10",
                    "192.0.2.10",
                    40000,
                    [fmt],
                    [fmt],
                    audio_direction=remote_direction,
                ).replace("c=IN IP4 192.0.2.10", "c=IN IP4 0.0.0.0")
                parsed = sdp.parse_sdp(offer)
                self.assertTrue(parsed["connection_held"])
                self.assertEqual(parsed["media_port"], 40000)
                self.assertEqual(parsed["direction"], remote_direction)

                answer = sdp.build_answer_directional(
                    "192.0.2.20",
                    "192.0.2.20",
                    41000,
                    selected,
                    selected,
                    remote_sdp=offer,
                    # Exercise the explicit-direction fail-safe as well.
                    audio_direction=sdp.local_direction_for_remote(remote_direction),
                )
                self.assertEqual(sdp.parse_sdp(answer)["direction"], local_direction)
                self.assertIn("m=audio 41000", answer)

    def test_media_connection_override_can_resume_one_held_stream(self) -> None:
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        offer = sdp.build_offer_directional(
            "192.0.2.10",
            "192.0.2.10",
            40000,
            [fmt],
            [fmt],
            video_port=42000,
            video_formats=sdp.DEFAULT_VIDEO_FORMATS[:1],
        ).replace("c=IN IP4 192.0.2.10", "c=IN IP4 0.0.0.0", 1)
        offer = offer.replace(
            "m=video 42000 RTP/AVP 103\r\n",
            "m=video 42000 RTP/AVP 103\r\nc=IN IP4 192.0.2.30\r\n",
        )

        self.assertTrue(sdp.parse_sdp(offer)["connection_held"])
        video = sdp.parse_video_sdp(offer)
        self.assertIsNotNone(video)
        assert video is not None
        self.assertFalse(video["connection_held"])
        self.assertEqual(video["connection_ip"], "192.0.2.30")

    def test_negotiate_l16_48k(self) -> None:
        offer = sdp.build_offer(
            "192.168.1.20",
            "192.168.1.20",
            40000,
            [
                audio_format.AudioFormat(48000, "s16le", 1, 10),
                audio_format.AudioFormat(16000, "s16le", 1, 20),
            ],
        )
        self.assertNotIn("a=fmtp:96", offer)
        self.assertIn("telephone-event/8000", offer)
        self.assertIn("a=fmtp:97 0-15", offer)
        self.assertIn("a=maxptime:10", offer)
        selected_direction = sdp.negotiate_directional(
            offer,
            [
                audio_format.AudioFormat(16000, "s16le", 1, 20),
                audio_format.AudioFormat(48000, "s16le", 1, 10),
            ],
            [
                audio_format.AudioFormat(16000, "s16le", 1, 20),
                audio_format.AudioFormat(48000, "s16le", 1, 10),
            ],
        )
        self.assertIsNotNone(selected_direction)
        assert selected_direction is not None
        selected = selected_direction.send
        self.assertEqual(selected.encoding, "L16")
        self.assertEqual(selected.sample_rate, 48000)
        self.assertEqual(selected.payload_type, 96)

    def test_negotiate_missing_ptime_prefers_best_local_pcm(self) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.48\r\n"
            "s=pjmedia\r\n"
            "c=IN IP4 192.168.1.48\r\n"
            "t=0 0\r\n"
            "m=audio 40760 RTP/AVP 96 97 98\r\n"
            "a=rtpmap:96 L16/16000\r\n"
            "a=rtpmap:97 L16/48000\r\n"
            "a=rtpmap:98 opus/48000/2\r\n"
            "a=sendrecv\r\n"
        )
        selected_direction = sdp.negotiate_directional(
            offer,
            [
                audio_format.AudioFormat(48000, "s16le", 1, 10),
                audio_format.AudioFormat(16000, "s16le", 1, 20),
            ],
            [
                audio_format.AudioFormat(48000, "s16le", 1, 10),
                audio_format.AudioFormat(16000, "s16le", 1, 20),
            ],
        )
        self.assertIsNotNone(selected_direction)
        assert selected_direction is not None
        selected = selected_direction.send
        self.assertEqual(selected.payload_type, 97)
        self.assertEqual(selected.audio_format, audio_format.AudioFormat(48000, "s16le", 1, 10))

        answer = sdp.build_answer_directional(
            "192.168.1.10", "192.168.1.10", 40000, selected, selected
        )
        self.assertIn("m=audio 40000 RTP/AVP 97", answer)
        self.assertIn("a=rtpmap:97 L16/48000/1", answer)
        self.assertIn("a=ptime:10", answer)

    def test_negotiate_l24_from_s24(self) -> None:
        offer = sdp.build_offer(
            "192.168.1.20",
            "192.168.1.20",
            40000,
            [audio_format.AudioFormat(16000, "s24le", 1, 20)],
        )
        offered = sdp.offered_pcm_formats(offer)
        self.assertEqual(offered[0].encoding, "L24")
        self.assertEqual(offered[0].sample_rate, 16000)

    def test_sendrecv_offer_and_negotiation_use_one_common_wire_format(self) -> None:
        tx_preferred = audio_format.AudioFormat(48000, "s16le", 1, 10)
        rx_preferred = audio_format.AudioFormat(32000, "s16le", 1, 10)
        common = audio_format.AudioFormat(16000, "s16le", 1, 10)
        offer = sdp.build_offer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40020,
            [tx_preferred, common],
            [rx_preferred, common],
        )
        offered = sdp.offered_pcm_formats(offer)
        self.assertEqual([fmt.audio_format for fmt in offered], [common])

        selected = sdp.negotiate_directional(
            offer,
            [common],
            [common],
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.send, selected.recv)
        self.assertEqual(selected.send.audio_format, common)

        answer = sdp.build_answer_directional(
            "192.168.1.47",
            "192.168.1.47",
            40000,
            selected.send,
            selected.recv,
            remote_sdp=offer,
        )
        self.assertIn("L16/16000/1", answer)
        self.assertEqual(
            sdp.parse_sdp(answer)["payload_order"],
            [selected.send.payload_type],
        )
        self.assertNotIn("a=fmtp:", answer)
        self.assertIn("a=maxptime:10", answer)

    def test_directional_offer_requires_common_ptime(self) -> None:
        with self.assertRaises(sdp.SdpError):
            sdp.build_offer_directional(
                "192.168.1.10",
                "192.168.1.10",
                40020,
                [audio_format.AudioFormat(16000, "s16le", 1, 16)],
                [audio_format.AudioFormat(48000, "s16le", 1, 10)],
            )

    def test_sendrecv_negotiation_rejects_disjoint_directional_formats(self) -> None:
        ha_to_esp = audio_format.AudioFormat(48000, "s16le", 1, 10)
        esp_to_ha = audio_format.AudioFormat(16000, "s16le", 1, 10)
        answer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.47\r\n"
            "s=VoIP Stack\r\n"
            "c=IN IP4 192.168.1.47\r\n"
            "t=0 0\r\n"
            "m=audio 40000 RTP/AVP 96 97\r\n"
            "a=rtpmap:96 L16/48000/1\r\n"
            "a=rtpmap:97 L16/16000/1\r\n"
            "a=ptime:10\r\n"
            "a=maxptime:10\r\n"
            "a=sendrecv\r\n"
        )
        selected = sdp.negotiate_answer_directional(
            answer,
            [ha_to_esp],
            [esp_to_ha],
        )
        self.assertIsNone(selected)

        with self.assertRaisesRegex(sdp.SdpError, "one RTP payload"):
            sdp.build_answer_directional(
                "192.168.1.47",
                "192.168.1.47",
                40000,
                sdp.audio_format_to_rtp(ha_to_esp, 96),
                sdp.audio_format_to_rtp(esp_to_ha, 97),
            )

    def test_one_way_audio_uses_only_the_active_local_capability(self) -> None:
        local_send = audio_format.AudioFormat(48000, "s16le", 1, 10)
        local_recv = audio_format.AudioFormat(16000, "s16le", 1, 20)

        send_offer = sdp.build_offer_directional(
            "192.0.2.10",
            "192.0.2.10",
            40000,
            [local_send],
            [local_recv],
            audio_direction="sendonly",
        )
        recv_offer = sdp.build_offer_directional(
            "192.0.2.10",
            "192.0.2.10",
            40000,
            [local_send],
            [local_recv],
            audio_direction="recvonly",
        )
        self.assertEqual(
            sdp.offered_pcm_formats(send_offer)[0].audio_format,
            local_send,
        )
        self.assertEqual(
            sdp.offered_pcm_formats(recv_offer)[0].audio_format,
            local_recv,
        )

        recv_selected = sdp.negotiate_directional(
            send_offer,
            [local_recv],
            [local_send],
        )
        send_selected = sdp.negotiate_directional(
            recv_offer,
            [local_recv],
            [local_send],
        )
        self.assertIsNotNone(recv_selected)
        self.assertIsNotNone(send_selected)
        assert recv_selected is not None and send_selected is not None
        self.assertEqual(recv_selected.recv.audio_format, local_send)
        self.assertEqual(send_selected.send.audio_format, local_recv)

        recv_answer = sdp.build_answer_directional(
            "192.0.2.20",
            "192.0.2.20",
            41000,
            recv_selected.send,
            recv_selected.recv,
            remote_sdp=send_offer,
        )
        send_answer = sdp.build_answer_directional(
            "192.0.2.20",
            "192.0.2.20",
            41000,
            send_selected.send,
            send_selected.recv,
            remote_sdp=recv_offer,
        )
        self.assertEqual(sdp.parse_sdp(recv_answer)["direction"], "recvonly")
        self.assertEqual(sdp.parse_sdp(send_answer)["direction"], "sendonly")
        self.assertEqual(len(sdp.offered_pcm_formats(recv_answer)), 1)
        self.assertEqual(len(sdp.offered_pcm_formats(send_answer)), 1)

    def test_inactive_answer_format_list_follows_original_offer_direction(self) -> None:
        send = sdp.RtpPcmFormat(96, "L16", 48000, 1, 10)
        recv = sdp.RtpPcmFormat(97, "L16", 16000, 1, 10)

        def remote_offer(direction: str) -> str:
            return (
                "v=0\r\n"
                "o=- 0 0 IN IP4 192.0.2.10\r\n"
                "s=-\r\n"
                "c=IN IP4 192.0.2.10\r\n"
                "t=0 0\r\n"
                "m=audio 40000 RTP/AVP 96 97\r\n"
                "a=rtpmap:96 L16/48000/1\r\n"
                "a=rtpmap:97 L16/16000/1\r\n"
                "a=ptime:10\r\n"
                f"a={direction}\r\n"
            )

        for direction, expected_payload in (("sendonly", 97), ("recvonly", 96)):
            with self.subTest(direction=direction):
                answer = sdp.build_answer_directional(
                    "192.0.2.20",
                    "192.0.2.20",
                    41000,
                    send,
                    recv,
                    remote_sdp=remote_offer(direction),
                    audio_direction="inactive",
                )
                parsed = sdp.parse_sdp(answer)
                self.assertEqual(parsed["direction"], "inactive")
                self.assertEqual(parsed["payload_order"], [expected_payload])

        for direction in ("sendrecv", "inactive"):
            with self.subTest(direction=direction):
                with self.assertRaisesRegex(sdp.SdpError, "one RTP payload"):
                    sdp.build_answer_directional(
                        "192.0.2.20",
                        "192.0.2.20",
                        41000,
                        send,
                        recv,
                        remote_sdp=remote_offer(direction),
                        audio_direction="inactive",
                    )

    def test_inactive_answer_negotiation_uses_local_offer_capability_direction(self) -> None:
        local_send = audio_format.AudioFormat(48000, "s16le", 1, 10)
        local_recv = audio_format.AudioFormat(16000, "s16le", 1, 10)
        inactive_answer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.0.2.20\r\n"
            "s=-\r\n"
            "c=IN IP4 192.0.2.20\r\n"
            "t=0 0\r\n"
            "m=audio 41000 RTP/AVP 96 97\r\n"
            "a=rtpmap:96 L16/48000/1\r\n"
            "a=rtpmap:97 L16/16000/1\r\n"
            "a=ptime:10\r\n"
            "a=inactive\r\n"
        )
        sendonly = sdp.negotiate_answer_directional(
            inactive_answer,
            [local_send],
            [local_recv],
            local_offer_direction="sendonly",
        )
        recvonly = sdp.negotiate_answer_directional(
            inactive_answer,
            [local_send],
            [local_recv],
            local_offer_direction="recvonly",
        )
        sendrecv = sdp.negotiate_answer_directional(
            inactive_answer,
            [local_send],
            [local_recv],
        )
        self.assertIsNotNone(sendonly)
        self.assertIsNotNone(recvonly)
        assert sendonly is not None and recvonly is not None
        self.assertEqual(sendonly.send.audio_format, local_send)
        self.assertEqual(recvonly.recv.audio_format, local_recv)
        self.assertIsNone(sendrecv)

    def test_standard_softphone_answer_uses_one_common_payload_when_profiles_are_symmetric(self) -> None:
        answer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.48\r\n"
            "s=baresip\r\n"
            "c=IN IP4 192.168.1.48\r\n"
            "t=0 0\r\n"
            "m=audio 45686 RTP/AVP 96 98\r\n"
            "a=rtpmap:96 L16/48000\r\n"
            "a=rtpmap:98 L16/16000\r\n"
            "a=minptime:10\r\n"
            "a=ptime:10\r\n"
            "a=sendrecv\r\n"
        )
        selected = sdp.negotiate_answer_directional(
            answer,
            list(audio_format.HA_SIP_PCM_FORMATS),
            list(audio_format.HA_SIP_PCM_FORMATS),
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.send.payload_type, 96)
        self.assertEqual(selected.recv.payload_type, 96)
        self.assertEqual(selected.send.audio_format, audio_format.AudioFormat(48000, "s16le", 1, 10))
        self.assertEqual(selected.recv.audio_format, audio_format.AudioFormat(48000, "s16le", 1, 10))

    def test_rejects_oversized_pcm_rtp_frame(self) -> None:
        with self.assertRaises(sdp.SdpError):
            sdp.build_offer(
                "192.168.1.20",
                "192.168.1.20",
                40000,
                [audio_format.AudioFormat(48000, "s16le", 1, 20)],
            )

    def test_accepts_g711_trunk_offer_as_pcm_edge_codec(self) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.20\r\n"
            "s=Phone\r\n"
            "c=IN IP4 192.168.1.20\r\n"
            "t=0 0\r\n"
            "m=audio 40000 RTP/AVP 0 8 96\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=rtpmap:8 PCMA/8000\r\n"
            "a=rtpmap:96 opus/48000/2\r\n"
            "a=ptime:20\r\n"
        )
        preferred = [audio_format.AudioFormat(8000, "s16le", 1, 20)]
        selected_direction = sdp.negotiate_directional(offer, preferred, preferred)
        self.assertIsNotNone(selected_direction)
        assert selected_direction is not None
        selected = selected_direction.send
        self.assertEqual(selected.encoding, "PCMU")
        self.assertEqual(selected.payload_type, 0)
        self.assertEqual(selected.audio_format, audio_format.AudioFormat(8000, "s16le", 1, 20))

    def test_prefers_l16_48k_over_g711_when_both_are_offered(self) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.20\r\n"
            "s=Phone\r\n"
            "c=IN IP4 192.168.1.20\r\n"
            "t=0 0\r\n"
            "m=audio 40000 RTP/AVP 0 96 8\r\n"
            "a=rtpmap:96 L16/48000/1\r\n"
            "a=ptime:10\r\n"
        )
        preferred = [
            audio_format.AudioFormat(48000, "s16le", 1, 10),
            audio_format.AudioFormat(8000, "s16le", 1, 20),
        ]
        selected_direction = sdp.negotiate_directional(
            offer,
            preferred,
            preferred,
        )
        self.assertIsNotNone(selected_direction)
        assert selected_direction is not None
        selected = selected_direction.send
        self.assertEqual(selected.encoding, "L16")
        self.assertEqual(selected.sample_rate, 48000)

    def test_negotiate_48k_10ms_when_softphone_offers_20ms_with_minptime_10(self) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.48\r\n"
            "s=baresip\r\n"
            "c=IN IP4 192.168.1.48\r\n"
            "t=0 0\r\n"
            "m=audio 12456 RTP/AVP 96 97 8 0 101\r\n"
            "a=rtpmap:96 L16/48000\r\n"
            "a=rtpmap:97 L16/16000\r\n"
            "a=rtpmap:8 PCMA/8000\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=rtpmap:101 telephone-event/8000\r\n"
            "a=fmtp:101 0-15\r\n"
            "a=sendrecv\r\n"
            "a=minptime:10\r\n"
            "a=ptime:20\r\n"
        )
        selected = sdp.negotiate_directional(
            offer,
            list(audio_format.HA_SIP_PCM_FORMATS),
            list(audio_format.HA_SIP_PCM_FORMATS),
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.send.audio_format, audio_format.AudioFormat(48000, "s16le", 1, 10))
        self.assertEqual(selected.recv.audio_format, audio_format.AudioFormat(48000, "s16le", 1, 10))
        answer = sdp.build_answer_directional("192.168.1.10", "192.168.1.10", 40000, selected.send, selected.recv)
        self.assertIn("a=rtpmap:96 L16/48000/1", answer)
        self.assertIn("a=ptime:10", answer)

    def test_g711_rtp_payload_converts_to_internal_s16le(self) -> None:
        pcm = b"\x00\x00\x00\x10\x00\xf0"
        alaw = sip_client.pcm_to_rtp_payload(pcm, sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20))
        ulaw = sip_client.pcm_to_rtp_payload(pcm, sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20))
        self.assertEqual(len(alaw), 3)
        self.assertEqual(len(ulaw), 3)
        self.assertEqual(len(sip_client.rtp_payload_to_pcm(alaw, sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20))), len(pcm))
        self.assertEqual(len(sip_client.rtp_payload_to_pcm(ulaw, sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20))), len(pcm))

    def test_linear_pcm_rtp_endianness_round_trips_exactly(self) -> None:
        vectors = (
            (audio_format.AudioFormat(16000, "s16le", 1, 20), b"\x01\x02\xfe\xff", b"\x02\x01\xff\xfe"),
            (
                audio_format.AudioFormat(16000, "s24le", 1, 20),
                b"\x01\x02\x03\xfe\xfd\xfc",
                b"\x03\x02\x01\xfc\xfd\xfe",
            ),
            (
                audio_format.AudioFormat(16000, "s24le_in_s32", 1, 20),
                b"\x56\x34\x12\x00\x01\x00\x80\xff",
                b"\x12\x34\x56\x80\x00\x01",
            ),
        )
        for fmt, pcm, wire in vectors:
            with self.subTest(fmt=fmt.pcm_format):
                self.assertEqual(sip_client.pcm_to_rtp_payload(pcm, fmt), wire)
                self.assertEqual(sip_client.rtp_payload_to_pcm(wire, fmt), pcm)

    def test_s24_in_s32_bridge_conversion_is_right_aligned(self) -> None:
        s24 = audio_format.AudioFormat(16000, "s24le_in_s32", 1, 10)
        s16 = audio_format.AudioFormat(16000, "s16le", 1, 10)
        s24_pair = b"\x00\x00\x40\x00\x00\x00\xc0\xff"
        source = s24_pair * (s24.nominal_frame_samples // 2)

        converted = audio_pcm.PcmFrameConverter(s24, s16).convert(source)
        self.assertEqual(len(converted), 1)
        self.assertEqual(converted[0][:4], b"\x00\x40\x00\xc0")

        restored = audio_pcm.PcmFrameConverter(s16, s24).convert(converted[0])
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0][:8], s24_pair)

    def test_bounded_sip_udp_queue_keeps_freshest_datagram(self) -> None:
        queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue(maxsize=2)
        protocol = sip_client._SipClientProtocol(queue)
        protocol.datagram_received(b"one", ("127.0.0.1", 1))
        protocol.datagram_received(b"two", ("127.0.0.1", 2))
        protocol.datagram_received(b"three", ("127.0.0.1", 3))

        self.assertEqual(protocol.dropped_packets, 1)
        self.assertEqual(queue.get_nowait()[0], b"two")
        self.assertEqual(queue.get_nowait()[0], b"three")

    def test_rejects_s32_wire_mapping(self) -> None:
        with self.assertRaises(sdp.SdpError):
            sdp.audio_format_to_rtp(audio_format.AudioFormat(48000, "s32le", 1, 20), 96)

    def test_offer_filters_non_rtp_mappable_pcm_formats(self) -> None:
        offer = sdp.build_offer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40020,
            [
                audio_format.AudioFormat(48000, "s32le", 1, 10),
                audio_format.AudioFormat(48000, "s16le", 1, 10),
                audio_format.AudioFormat(16000, "s16le", 1, 10),
            ],
            [
                audio_format.AudioFormat(48000, "s16le", 1, 10),
                audio_format.AudioFormat(48000, "s16le", 2, 10),
                audio_format.AudioFormat(16000, "s16le", 1, 10),
            ],
        )
        offered = sdp.offered_pcm_formats(offer)
        self.assertEqual(
            [(fmt.encoding, fmt.sample_rate, fmt.channels, fmt.frame_ms) for fmt in offered],
            [("L16", 48000, 1, 10), ("L16", 16000, 1, 10)],
        )

    def test_offer_caps_payloads_to_compact_udp_safe_profile_without_losing_esp_baseline(self) -> None:
        browser_tx_formats = [
            audio_format.AudioFormat(rate, fmt, 1, frame_ms)
            for rate in sorted(audio_format.SUPPORTED_SAMPLE_RATES)
            for frame_ms in sorted(audio_format.SUPPORTED_FRAME_MS)
            if (rate * frame_ms) % 1000 == 0
            for fmt in audio_format.PcmFormat
        ]
        browser_rx_formats = [
            audio_format.AudioFormat(rate, fmt, channels, frame_ms)
            for rate in sorted(audio_format.SUPPORTED_SAMPLE_RATES)
            for frame_ms in sorted(audio_format.SUPPORTED_FRAME_MS)
            if (rate * frame_ms) % 1000 == 0
            for fmt in audio_format.PcmFormat
            for channels in (1, 2)
        ]
        offer = sdp.build_offer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40020,
            browser_tx_formats,
            browser_rx_formats,
        )
        offered = sdp.offered_pcm_formats(offer)
        self.assertLessEqual(len(offered), 12)
        self.assertLess(len(offer.encode()), 900)
        self.assertEqual(offered[0].audio_format, audio_format.AudioFormat(48000, "s16le", 1, 10))
        self.assertIn(
            audio_format.AudioFormat(16000, "s16le", 1, 10),
            [fmt.audio_format for fmt in offered],
        )

    def test_ha_sip_profile_rejects_browser_only_sample_rates(self) -> None:
        offer = sdp.build_offer_directional(
            "192.168.1.48",
            "192.168.1.48",
            40020,
            [audio_format.AudioFormat(44100, "s16le", 1, 10)],
            [audio_format.AudioFormat(44100, "s16le", 1, 10)],
        )
        selected = sdp.negotiate_directional(
            offer,
            list(audio_format.HA_SIP_PCM_TX_FORMATS),
            list(audio_format.HA_SIP_PCM_RX_FORMATS),
        )
        self.assertIsNone(selected)

    def test_ha_sip_profile_keeps_esp_baseline_16k_16ms(self) -> None:
        baseline = audio_format.AudioFormat(16000, "s16le", 1, 16)
        offer = sdp.build_offer_directional(
            "192.168.1.48",
            "192.168.1.48",
            40020,
            [baseline],
            [baseline],
        )
        selected = sdp.negotiate_directional(
            offer,
            list(audio_format.HA_SIP_PCM_TX_FORMATS),
            list(audio_format.HA_SIP_PCM_RX_FORMATS),
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.send.audio_format, baseline)
