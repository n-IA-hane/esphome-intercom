#!/usr/bin/env python3
"""Direct SIP bridge media lifecycle contracts."""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock

from .voip_phase1_support import (
    _reserved_udp_ports,
    asyncio,
    audio_format,
    roster,
    router,
    rtp,
    sdp,
    sip,
    sip_bridge,
    sip_client,
    sip_listener,
    sip_rtp_bridge,
    types,
    unittest,
)


class SipBridgeTest(unittest.IsolatedAsyncioTestCase):
    def test_video_reoffer_preserves_established_audio_payload(self):
        """A video-only re-offer must retain its accepted audio mapping."""

        pcm_48k = sdp.RtpPcmFormat(96, "L16", 48000, 1, 20)
        pcm_16k = sdp.RtpPcmFormat(97, "L16", 16000, 1, 20)
        invite = sip_listener.SipInvite(
            source_host="192.0.2.10",
            source_port=5060,
            request_uri=sip.parse_sip_uri("sip:418@192.0.2.1"),
            caller_uri=sip.parse_sip_uri("sip:P4@192.0.2.10"),
            target="418",
            caller="P4",
            call_id="video-only-reoffer",
            cseq="2 INVITE",
            remote_sdp=(
                b"v=0\r\n"
                b"c=IN IP4 192.0.2.10\r\n"
                b"m=audio 40000 RTP/AVP 96 97\r\n"
                b"a=rtpmap:96 L16/48000/1\r\n"
                b"a=rtpmap:97 L16/16000/1\r\n"
                b"a=sendrecv\r\n"
                b"m=video 41000 RTP/AVP 105\r\n"
                b"a=rtpmap:105 H264/90000\r\n"
                b"a=recvonly\r\n"
            ),
            send_format=pcm_48k,
            recv_format=pcm_48k,
            remote_rtp_host="192.0.2.10",
            remote_rtp_port=40000,
        )
        established = sip_rtp_bridge.RtpPeer(
            host="192.0.2.10",
            port=40000,
            payload_type=97,
            audio_format=pcm_16k.audio_format,
            rtp_format=pcm_16k,
            send_payload_type=97,
            send_audio_format=pcm_16k.audio_format,
            send_rtp_format=pcm_16k,
        )

        updated = sip_bridge.invite_rtp_peer(invite, established=established)

        self.assertEqual(updated.inbound_rtp_format, pcm_16k)
        self.assertEqual(updated.outbound_rtp_format, pcm_16k)

    def test_audio_contract_is_renegotiated_when_removed_from_reoffer(self):
        pcm_48k = sdp.RtpPcmFormat(96, "L16", 48000, 1, 20)
        invite, _dialog, _relay = self._cross_codec_video_fixture()
        invite = dataclasses.replace(
            invite,
            remote_sdp=(
                b"v=0\r\nc=IN IP4 192.0.2.10\r\n"
                b"m=audio 40000 RTP/AVP 96\r\n"
                b"a=rtpmap:96 L16/48000/1\r\na=sendrecv\r\n"
            ),
            send_format=pcm_48k,
            recv_format=pcm_48k,
        )
        established = sip_rtp_bridge.RtpPeer(
            host="192.0.2.10",
            port=40000,
            payload_type=97,
            audio_format=sdp.RtpPcmFormat(97, "L16", 16000, 1, 20).audio_format,
            rtp_format=sdp.RtpPcmFormat(97, "L16", 16000, 1, 20),
        )

        updated = sip_bridge.invite_rtp_peer(invite, established=established)

        self.assertEqual(updated.inbound_rtp_format, pcm_48k)

    @staticmethod
    def _cross_codec_video_fixture():
        audio = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
        jpeg = sdp.RtpVideoFormat(payload_type=26, encoding="JPEG")
        h264 = sdp.RtpVideoFormat(
            payload_type=105,
            encoding="H264",
            profile_level_id="42c00c",
            packetization_mode=1,
            max_framerate=10,
        )
        invite = sip_listener.SipInvite(
            source_host="192.0.2.10",
            source_port=5060,
            request_uri=sip.parse_sip_uri("sip:Zoiper@192.0.2.1"),
            caller_uri=sip.parse_sip_uri("sip:P4@192.0.2.10"),
            target="Zoiper",
            caller="P4",
            call_id="cross-codec",
            cseq="1 INVITE",
            remote_sdp=b"",
            send_format=audio,
            recv_format=audio,
            remote_rtp_host="192.0.2.10",
            remote_rtp_port=40000,
            video_format=jpeg,
            local_video_format=jpeg,
            video_answer_format=jpeg,
            remote_video_rtp_host="192.0.2.10",
            remote_video_rtp_port=41000,
            remote_video_rtcp_host="192.0.2.10",
            remote_video_rtcp_port=41001,
        )
        dialog = sip_client.SipDialog(
            target="Zoiper",
            remote_host="192.0.2.20",
            remote_sip_port=5060,
            remote_rtp_host="192.0.2.20",
            remote_rtp_port=42000,
            local_rtp_port=42002,
            call_id="destination",
            local_uri="sip:HA@192.0.2.1",
            remote_uri="sip:Zoiper@192.0.2.20",
            send_format=audio,
            recv_format=audio,
            video_format=h264,
            local_video_format=h264,
            remote_video_rtp_host="192.0.2.20",
            remote_video_rtp_port=43000,
            remote_video_rtcp_host="192.0.2.20",
            remote_video_rtcp_port=43001,
        )
        relay = sip_bridge.build_pending_invite_video_relay(
            invite,
            remote_host=dialog.remote_host,
            left_port=44000,
            right_port=44002,
            sockets=(None, None, None, None),
        )
        return invite, dialog, relay

    def test_cross_codec_video_offer_keeps_direct_codec_first(self) -> None:
        invite, _dialog, _relay = self._cross_codec_video_fixture()

        offered = sip_bridge.video_bridge_offer_formats(
            invite.video_format,
            enable_transcoding=True,
        )

        self.assertEqual(offered[0], invite.video_format)
        self.assertEqual([item.encoding for item in offered], ["JPEG", "H264"])
        self.assertEqual(len({item.payload_type for item in offered}), len(offered))

    def test_directional_h264_offer_uses_source_receive_contract(self) -> None:
        source_send = sdp.RtpVideoFormat(
            payload_type=103,
            encoding="H264",
            profile_level_id="42c00c",
            packetization_mode=1,
        )
        source_receive = sdp.RtpVideoFormat(
            payload_type=103,
            encoding="H264",
            profile_level_id="42c01f",
            packetization_mode=1,
        )

        offered = sip_bridge.video_bridge_offer_formats(
            source_send,
            source_receive=source_receive,
            enable_transcoding=True,
        )

        self.assertEqual(offered[0].profile_level_id, "42c01f")

    def test_cross_codec_direction_requires_transcoding(self) -> None:
        offered = sip_bridge.video_bridge_offer_formats(
            sdp.RtpVideoFormat(payload_type=26, encoding="JPEG"),
            source_receive=sdp.CONSTRAINED_BASELINE_H264_FORMAT,
            enable_transcoding=False,
        )

        self.assertEqual(offered, ())

    def test_known_jpeg_destination_gets_one_unfragmented_codec_offer(self) -> None:
        invite, _dialog, _relay = self._cross_codec_video_fixture()
        source = sdp.RtpVideoFormat(
            payload_type=102,
            encoding="H264",
            profile_level_id="42e01f",
            direction=invite.video_format.direction,
            transport_profile=invite.video_format.transport_profile,
        )

        offered = sip_bridge.video_bridge_offer_formats(
            source,
            enable_transcoding=True,
            target_codec="jpeg",
        )

        self.assertEqual([item.encoding for item in offered], ["JPEG"])
        self.assertEqual(offered[0].payload_type, 26)
        self.assertEqual(offered[0].transport_profile, "RTP/AVP")
        self.assertEqual(offered[0].rtcp_feedback, ())

    def test_known_destination_uses_avp_even_for_direct_codec(self) -> None:
        source = sdp.RtpVideoFormat(
            payload_type=102,
            encoding="H264",
            profile_level_id="42e01f",
            transport_profile="RTP/AVPF",
            rtcp_feedback=("nack pli", "ccm fir"),
        )

        offered = sip_bridge.video_bridge_offer_formats(
            source,
            enable_transcoding=True,
            target_codec="h264",
        )

        self.assertEqual([item.encoding for item in offered], ["H264"])
        self.assertEqual(offered[0].transport_profile, "RTP/AVP")
        self.assertEqual(offered[0].rtcp_feedback, ())

    def test_cross_codec_video_answer_configures_bidirectional_ffmpeg(self) -> None:
        invite, dialog, relay = self._cross_codec_video_fixture()
        hass = types.SimpleNamespace(data={})

        answer = sip_bridge.configure_answered_invite_video_relay(
            invite,
            dialog,
            relay,
            hass=hass,
            enable_transcoding=True,
        )

        self.assertIsNotNone(answer)
        self.assertTrue(relay.transcoding)
        self.assertEqual(relay._transcode.directions, {"left", "right"})  # noqa: SLF001
        self.assertEqual(answer.video_format.encoding, "JPEG")

    def test_source_peer_retains_committed_inverse_answer_direction(self) -> None:
        invite, dialog, relay = self._cross_codec_video_fixture()
        dialog.video_format = dataclasses.replace(
            dialog.video_format,
            direction="recvonly",
        )

        answer = sip_bridge.configure_answered_invite_video_relay(
            invite,
            dialog,
            relay,
            hass=types.SimpleNamespace(data={}),
            enable_transcoding=True,
        )

        self.assertIsNotNone(answer)
        self.assertEqual(answer.direction, "recvonly")
        self.assertEqual(relay.left.video_format.direction, "sendonly")

    async def test_cross_codec_h264_start_requests_rfc5168_keyframe(self) -> None:
        invite, dialog, video_relay = self._cross_codec_video_fixture()
        sip_bridge.configure_answered_invite_video_relay(
            invite,
            dialog,
            video_relay,
            hass=types.SimpleNamespace(data={}),
            enable_transcoding=True,
        )
        media = types.SimpleNamespace(
            video_relay=video_relay,
            start=AsyncMock(),
        )
        client = types.SimpleNamespace(
            request_video_keyframe=AsyncMock(return_value=True),
            dialog_ids=types.SimpleNamespace(call_id="destination"),
        )

        needs_keyframe = await sip_bridge.async_start_sip_bridge_media(media)

        media.start.assert_awaited_once()
        self.assertTrue(needs_keyframe)
        client.request_video_keyframe.assert_not_awaited()

        await sip_bridge.async_request_sip_bridge_keyframe(client)
        client.request_video_keyframe.assert_awaited_once()

    async def test_passthrough_video_start_does_not_request_keyframe(self) -> None:
        _invite, _dialog, video_relay = self._cross_codec_video_fixture()
        media = types.SimpleNamespace(
            video_relay=video_relay,
            start=AsyncMock(),
        )
        client = types.SimpleNamespace(
            request_video_keyframe=AsyncMock(return_value=True),
            dialog_ids=types.SimpleNamespace(call_id="destination"),
        )

        needs_keyframe = await sip_bridge.async_start_sip_bridge_media(media)

        media.start.assert_awaited_once()
        self.assertFalse(needs_keyframe)
        client.request_video_keyframe.assert_not_awaited()

    def test_local_client_relay_does_not_require_a_synthetic_sdp_offer(self) -> None:
        local_to_relay = sdp.RtpPcmFormat(96, "L16", 16000, 1, 16)
        relay_to_local = sdp.RtpPcmFormat(97, "L16", 48000, 1, 10)
        dialog = sip_client.SipDialog(
            target="ESP",
            remote_host="192.0.2.20",
            remote_sip_port=5060,
            remote_rtp_host="192.0.2.20",
            remote_rtp_port=41000,
            local_rtp_port=42002,
            call_id="dest-call",
            local_uri="sip:HA@192.0.2.10",
            remote_uri="sip:ESP@192.0.2.20",
            send_format=local_to_relay,
            recv_format=local_to_relay,
        )
        client = types.SimpleNamespace(dialog=dialog)

        relay = sip_bridge.build_local_client_relay(
            client=client,
            local_host="127.0.0.1",
            local_to_relay_format=local_to_relay,
            relay_to_local_format=relay_to_local,
            source_relay_port=42000,
            dest_relay_port=42002,
            capture_name="source_dest",
        )

        self.assertEqual((relay.left.host, relay.left.port), ("127.0.0.1", 0))
        self.assertEqual(relay.left.inbound_rtp_format, local_to_relay)
        self.assertEqual(relay.left.outbound_rtp_format, relay_to_local)
        self.assertEqual((relay.right.host, relay.right.port), ("192.0.2.20", 41000))

    async def test_local_browser_loopback_latches_ephemeral_rtp_port_bidirectionally(self) -> None:
        local = "127.0.0.1"
        with _reserved_udp_ports(3) as ports:
            relay_left_port, relay_right_port, destination_port = ports
        audio = audio_format.AudioFormat(16000, "s16le", 1, 32)

        class Capture(asyncio.DatagramProtocol):
            def __init__(self) -> None:
                self.queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue()

            def datagram_received(self, data: bytes, addr) -> None:
                self.queue.put_nowait((data, addr))

        loop = asyncio.get_running_loop()
        browser = Capture()
        destination = Capture()
        browser_transport, _ = await loop.create_datagram_endpoint(
            lambda: browser,
            local_addr=(local, 0),
        )
        destination_transport, _ = await loop.create_datagram_endpoint(
            lambda: destination,
            local_addr=(local, destination_port),
        )
        dtmf_events: list[tuple[str, str, str]] = []
        dtmf_received = asyncio.Event()

        def on_dtmf(side: str, digit: str, transport: str) -> None:
            dtmf_events.append((side, digit, transport))
            dtmf_received.set()

        relay = sip_rtp_bridge.SipRtpRelay(
            left=sip_rtp_bridge.RtpPeer(local, 0, 96, audio, dtmf_payload_type=101),
            right=sip_rtp_bridge.RtpPeer(local, destination_port, 96, audio, dtmf_payload_type=101),
            left_port=relay_left_port,
            right_port=relay_right_port,
            on_dtmf=on_dtmf,
        )

        def frame(*, sequence: int, ssrc: int) -> bytes:
            return rtp.build_packet(
                rtp.RtpPacket(
                    payload_type=96,
                    sequence=sequence,
                    timestamp=sequence * audio.nominal_frame_samples,
                    ssrc=ssrc,
                    payload=bytes(audio.nominal_frame_bytes),
                )
            )

        try:
            await relay.start()
            browser_port = int(browser_transport.get_extra_info("sockname")[1])
            browser_transport.sendto(frame(sequence=1, ssrc=101), (local, relay_left_port))
            forwarded, _ = await asyncio.wait_for(destination.queue.get(), timeout=1.0)
            self.assertEqual(rtp.parse_packet(forwarded).payload_type, 96)
            self.assertEqual(relay.left.port, browser_port)

            dtmf_packet = rtp.build_packet(
                rtp.RtpPacket(
                    payload_type=101,
                    sequence=2,
                    timestamp=audio.nominal_frame_samples,
                    ssrc=101,
                    payload=bytes((1, 0x80, 0x01, 0x40)),
                )
            )
            browser_transport.sendto(dtmf_packet, (local, relay_left_port))
            browser_transport.sendto(dtmf_packet, (local, relay_left_port))
            await asyncio.wait_for(dtmf_received.wait(), timeout=1.0)
            self.assertEqual(dtmf_events, [("left", "1", "rtp_event")])
            relayed_dtmf: list[rtp.RtpPacket] = []
            while sum(bool(packet.payload[1] & 0x80) for packet in relayed_dtmf) < 3:
                raw, _ = await asyncio.wait_for(destination.queue.get(), timeout=1.0)
                relayed_dtmf.append(rtp.parse_packet(raw))
            self.assertEqual({packet.payload_type for packet in relayed_dtmf}, {101})
            self.assertEqual(
                {packet.timestamp for packet in relayed_dtmf},
                {relayed_dtmf[0].timestamp},
            )
            self.assertTrue(relayed_dtmf[0].marker)
            self.assertFalse(any(packet.marker for packet in relayed_dtmf[1:]))
            self.assertEqual([packet.payload[0] for packet in relayed_dtmf], [1] * 7)
            self.assertEqual(
                sum(bool(packet.payload[1] & 0x80) for packet in relayed_dtmf),
                3,
            )
            self.assertEqual(relay.right_dtmf_tx_events, 1)

            destination_transport.sendto(frame(sequence=3, ssrc=202), (local, relay_right_port))
            returned, _ = await asyncio.wait_for(browser.queue.get(), timeout=1.0)
            self.assertEqual(rtp.parse_packet(returned).payload_type, 96)
            self.assertGreaterEqual(relay.forwarded, 2)
        finally:
            await relay.stop()
            browser_transport.close()
            destination_transport.close()

    async def test_busy_bridge_target_returns_terminal_response_without_ringing(self) -> None:
        local = "127.0.0.1"
        with _reserved_udp_ports(4) as ports:
            ha_sip, caller_rtp, dest_sip, caller_sip = ports
        audio = audio_format.AudioFormat(16000, "s16le", 1, 32)
        stats = {"dest_invites": 0}

        async def dest_invite(invite):
            stats["dest_invites"] += 1
            return sip_listener.SipInviteResult(
                486,
                "Busy Here",
                decline_reason="busy",
            )

        async def ha_invite(invite):
            entries = [
                roster.RosterEntry(id="HA", address=local, metadata={"sip_port": ha_sip}),
                roster.RosterEntry(id="Cucina", address=local, metadata={"sip_port": dest_sip, "sip_transport": "udp"}),
            ]
            decision = router.resolve_ha_router(invite.target, entries, trunk_ready=False)
            self.assertIsNotNone(decision.entry)
            dest_client = sip_client.SipCallClient(
                local_ip=local,
                local_name=invite.caller or "HA",
                local_sip_port=ha_sip,
                local_rtp_port=caller_rtp + 2,
                supported_formats=[invite.selected_format.audio_format],
            )
            try:
                result = await dest_client.invite(
                    target=decision.entry.id,
                    remote_host=decision.entry.address,
                    remote_sip_port=decision.entry.metadata["sip_port"],
                )
            finally:
                await dest_client.close()
            self.assertNotEqual(result, "ringing")
            return sip_listener.SipInviteResult(
                486,
                "Busy Here",
                decline_reason=result if result != "sip_486" else "busy",
            )

        dest_server = sip_listener.SipUdpServer(
            host=local,
            port=dest_sip,
            local_ip=local,
            local_rtp_port=caller_rtp + 4,
            supported_formats=[audio],
            on_invite=dest_invite,
        )
        ha_server = sip_listener.SipUdpServer(
            host=local,
            port=ha_sip,
            local_ip=local,
            local_rtp_port=caller_rtp + 6,
            supported_formats=[audio],
            on_invite=ha_invite,
        )
        self.assertTrue(await dest_server.start())
        self.assertTrue(await ha_server.start())
        caller = sip_client.SipCallClient(
            local_ip=local,
            local_name="Spotpear",
            local_sip_port=caller_sip,
            local_rtp_port=caller_rtp,
            supported_formats=[audio],
        )
        try:
            self.assertEqual(
                await caller.invite(target="Cucina", remote_host=local, remote_sip_port=ha_sip),
                "busy",
            )
            self.assertIsNone(caller.dialog)
            self.assertEqual(stats["dest_invites"], 1)
        finally:
            await caller.close()
            await ha_server.stop()
            await dest_server.stop()

    async def test_symbolic_target_bridges_through_ha_with_rtp_relay(self) -> None:
        local = "127.0.0.1"
        with _reserved_udp_ports(7) as ports:
            ha_sip, caller_rtp, dest_sip, dest_rtp, ha_rtp_left, ha_rtp_right, caller_sip = ports
        audio = audio_format.AudioFormat(16000, "s16le", 1, 32)
        stats = {"dest_invites": 0, "caller_rtp_rx": 0, "dest_rtp_rx": 0}

        class DestRtp(asyncio.DatagramProtocol):
            def __init__(self) -> None:
                self.transport = None
                self.remote: tuple[str, int, int] | None = None
                self.sequence = 10
                self.timestamp = 0

            def connection_made(self, transport) -> None:
                self.transport = transport

            def datagram_received(self, data: bytes, addr) -> None:
                rtp.parse_packet(data)
                stats["dest_rtp_rx"] += 1

            async def send_loop(self) -> None:
                while True:
                    await asyncio.sleep(0.032)
                    if self.transport is None or self.remote is None:
                        continue
                    host, port, pt = self.remote
                    packet = rtp.build_packet(
                        rtp.RtpPacket(
                            payload_type=pt,
                            sequence=self.sequence,
                            timestamp=self.timestamp,
                            ssrc=0x2222,
                            payload=b"\0" * 1024,
                        )
                    )
                    self.transport.sendto(packet, (host, port))
                    self.sequence = rtp.next_sequence(self.sequence)
                    self.timestamp = rtp.next_timestamp(self.timestamp, 512)

        class CallerRtp(asyncio.DatagramProtocol):
            def __init__(self) -> None:
                self.transport = None
                self.remote: tuple[str, int, int] | None = None
                self.sequence = 500
                self.timestamp = 0

            def connection_made(self, transport) -> None:
                self.transport = transport

            def datagram_received(self, data: bytes, addr) -> None:
                rtp.parse_packet(data)
                stats["caller_rtp_rx"] += 1

            async def send_loop(self) -> None:
                while True:
                    await asyncio.sleep(0.032)
                    if self.transport is None or self.remote is None:
                        continue
                    host, port, pt = self.remote
                    packet = rtp.build_packet(
                        rtp.RtpPacket(
                            payload_type=pt,
                            sequence=self.sequence,
                            timestamp=self.timestamp,
                            ssrc=0x1111,
                            payload=b"\0" * 1024,
                        )
                    )
                    self.transport.sendto(packet, (host, port))
                    self.sequence = rtp.next_sequence(self.sequence)
                    self.timestamp = rtp.next_timestamp(self.timestamp, 512)

        dest_rtp_proto = DestRtp()
        caller_rtp_proto = CallerRtp()
        relay: sip_rtp_bridge.SipRtpRelay | None = None
        dest_client: sip_client.SipCallClient | None = None
        tasks: list[asyncio.Task] = []

        async def dest_invite(invite):
            stats["dest_invites"] += 1
            dest_rtp_proto.remote = (
                invite.remote_rtp_host,
                invite.remote_rtp_port,
                invite.selected_format.payload_type,
            )
            return sip_listener.SipInviteResult(
                200,
                "OK",
                answer_sdp=sdp.build_answer_directional(
                    local,
                    local,
                    dest_rtp,
                    invite.send_format,
                    invite.recv_format,
                ),
            )

        async def ha_invite(invite):
            nonlocal relay, dest_client
            entries = [
                roster.RosterEntry(id="HA", address=local, metadata={"sip_port": ha_sip}),
                roster.RosterEntry(id="Cucina", address=local, metadata={"sip_port": dest_sip, "sip_transport": "udp"}),
            ]
            decision = router.resolve_ha_router(invite.target, entries, trunk_ready=False)
            self.assertEqual(decision.sip_uri, f"sip:Cucina@{local}:{dest_sip};transport=udp")
            self.assertIsNotNone(decision.entry)
            dest_client = sip_client.SipCallClient(
                local_ip=local,
                local_name="HA",
                local_sip_port=ha_sip,
                local_rtp_port=ha_rtp_right,
                supported_formats=[invite.selected_format.audio_format],
            )
            result = await dest_client.invite(
                target=decision.entry.id,
                remote_host=decision.entry.address,
                remote_sip_port=decision.entry.metadata["sip_port"],
            )
            self.assertEqual(result, "in_call")
            assert dest_client.dialog is not None
            relay = sip_rtp_bridge.SipRtpRelay(
                left=sip_rtp_bridge.RtpPeer(
                    invite.remote_rtp_host,
                    invite.remote_rtp_port,
                    invite.selected_format.payload_type,
                    invite.selected_format.audio_format,
                ),
                right=sip_rtp_bridge.RtpPeer(
                    dest_client.dialog.remote_rtp_host,
                    dest_client.dialog.remote_rtp_port,
                    dest_client.dialog.selected_format.payload_type,
                    dest_client.dialog.selected_format.audio_format,
                ),
                left_port=ha_rtp_left,
                right_port=ha_rtp_right,
            )
            await relay.start()
            return sip_listener.SipInviteResult(
                200,
                "OK",
                answer_sdp=sdp.build_answer_directional(
                    local,
                    local,
                    ha_rtp_left,
                    invite.send_format,
                    invite.recv_format,
                ),
            )

        dest_server = sip_listener.SipUdpServer(
            host=local,
            port=dest_sip,
            local_ip=local,
            local_rtp_port=dest_rtp,
            supported_formats=[audio],
            on_invite=dest_invite,
        )
        ha_server = sip_listener.SipUdpServer(
            host=local,
            port=ha_sip,
            local_ip=local,
            local_rtp_port=ha_rtp_left,
            supported_formats=[audio],
            on_invite=ha_invite,
        )
        self.assertTrue(await dest_server.start())
        self.assertTrue(await ha_server.start())
        loop = asyncio.get_running_loop()
        dest_transport, _ = await loop.create_datagram_endpoint(lambda: dest_rtp_proto, local_addr=(local, dest_rtp))
        caller_transport, _ = await loop.create_datagram_endpoint(
            lambda: caller_rtp_proto,
            local_addr=(local, caller_rtp),
        )
        tasks.append(asyncio.create_task(dest_rtp_proto.send_loop()))
        tasks.append(asyncio.create_task(caller_rtp_proto.send_loop()))
        caller = sip_client.SipCallClient(
            local_ip=local,
            local_name="Spotpear",
            local_sip_port=caller_sip,
            local_rtp_port=caller_rtp,
            supported_formats=[audio],
        )
        try:
            self.assertEqual(await caller.invite(target="Cucina", remote_host=local, remote_sip_port=ha_sip), "in_call")
            assert caller.dialog is not None
            caller_rtp_proto.remote = (
                caller.dialog.remote_rtp_host,
                caller.dialog.remote_rtp_port,
                caller.dialog.selected_format.payload_type,
            )
            deadline = asyncio.get_running_loop().time() + 3.0
            while (
                asyncio.get_running_loop().time() < deadline
                and (stats["caller_rtp_rx"] == 0 or stats["dest_rtp_rx"] == 0)
            ):
                await asyncio.sleep(0.05)
            self.assertEqual(stats["dest_invites"], 1)
            assert relay is not None
            relay_snapshot = relay.snapshot()
            self.assertGreater(stats["caller_rtp_rx"], 0, relay_snapshot)
            self.assertGreater(stats["dest_rtp_rx"], 0, relay_snapshot)
            self.assertGreater(relay.forwarded, 0)
            self.assertEqual(relay.dropped, 0)
        finally:
            caller.bye()
            await caller.close()
            if dest_client is not None:
                dest_client.bye()
                await dest_client.close()
            if relay is not None:
                await relay.stop()
            await ha_server.stop()
            await dest_server.stop()
            dest_transport.close()
            caller_transport.close()
            for task in tasks:
                task.cancel()
