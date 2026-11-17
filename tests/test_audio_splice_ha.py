"""Audio handoff preserves transport addresses, RTP clocks and ownership."""

import asyncio
from dataclasses import replace
import socket
import struct
import unittest
import pytest

from custom_components.voip_stack.core import audio_format, sdp, rtp
from custom_components.voip_stack import sip_rtp_bridge
from custom_components.voip_stack.local_media_bridge import LocalMediaBridge, adopt_relay_for_application
from .test_automation_call import application as application

pytestmark = pytest.mark.ha


async def test_application_pickup_keeps_bound_socket_and_bidirectional_audio(application):
    app, registry, caller, released = application
    picker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    picker.bind(("127.0.0.1", 0))
    picker.setblocking(False)
    handoff = None
    try:
        await app.answer()
        media = app.media
        transport = media.transport
        pickup = replace(app.invite, call_id="pickup", remote_rtp_port=picker.getsockname()[1])
        abandoned = await LocalMediaBridge.prepare(media, pickup)
        await abandoned.rollback()
        assert media.transport is transport and not media.closed.is_set()
        assert released == []
        handoff = await LocalMediaBridge.prepare(media, pickup)
        source_ssrc = media.rtp_source.ssrc
        relay = handoff.commit()
        registry.release_resource(app.session.call_id, f"relay:{app.session.call_id}", value=media)
        registry.attach_relay(app.session.call_id, relay)
        app.media = None
        await media.stop()
        assert relay.left_transport is transport and not transport.is_closing()
        assert released == []

        async def exchange(sender, target, port, sample, source_ssrc):
            payload = struct.pack("!320h", *([sample] * 320))
            packet = rtp.build_packet(rtp.RtpPacket(96, 1, 320, source_ssrc, payload))
            await asyncio.get_running_loop().sock_sendto(sender, packet, ("127.0.0.1", port))
            async with asyncio.timeout(2):
                while True:
                    received = rtp.parse_packet(await asyncio.get_running_loop().sock_recv(target, 2048))
                    if received.payload == payload:
                        return received
        recorded, errors = [], []
        assert relay.left_to_right_passthrough and relay.right_to_left_passthrough
        detach_recording = relay.observe_audio(lambda side, pcm, fmt: recorded.append((side, pcm, fmt)), errors.append)
        await exchange(caller, picker, relay.left_port, 1700, 123)
        received = await exchange(picker, caller, relay.right_port, -2100, 456)
        assert received.ssrc == source_ssrc
        assert [(side, struct.unpack('<320h', pcm)[0]) for side, pcm, _fmt in recorded] == [('left', 1700), ('right', -2100)]
        assert relay.left_to_right_passthrough and relay.right_to_left_passthrough
        decode = relay.left_decoder.decode
        def failed_decode(*_args):
            raise ValueError('injected recording decode failure')
        relay.left_decoder.decode = failed_decode
        await exchange(caller, picker, relay.left_port, 1900, 123)
        relay.left_decoder.decode = decode
        assert errors == ['decode_error']
        detach_recording()
        await exchange(caller, picker, relay.left_port, 2000, 123)
        assert len(recorded) == 2
        async def complete(_reason):
            raise AssertionError("Retaining the caller must not terminate it")
        parked_again = adopt_relay_for_application(app.hass, relay, "left", media.invite, media.reservation, complete)
        parked_again.release_reservation_on_stop = False
        registry.release_resource(app.session.call_id, f"relay:{app.session.call_id}", value=relay)
        registry.attach_relay(app.session.call_id, parked_again)
        app.media = parked_again
        await relay.stop()
        assert parked_again.transport is transport and not transport.is_closing()
        assert parked_again.rtp_source.ssrc == source_ssrc and not released
        await registry.terminate_call_wait(app.session.call_id, reason="test_complete")
        assert len(released) == 1 and not registry.sessions
        assert transport.is_closing()
    finally:
        if handoff is not None:
            await handoff.rollback()
        await registry.shutdown()
        picker.close()


async def test_application_pickup_rejects_changed_contract_without_detaching(application):
    app, registry, _caller, released = application
    await app.answer()
    media = app.media
    handoff = await LocalMediaBridge.prepare(media, app.invite)
    try:
        media.invite = replace(media.invite, remote_rtp_port=49999)
        with pytest.raises(RuntimeError, match="changed"):
            handoff.commit()
        await handoff.rollback()
        assert media.transport is handoff.transport and not media.closed.is_set()
        assert not handoff.transport.is_closing() and released == []
    finally:
        await handoff.rollback()
        await registry.shutdown()


class Transport:
    def __init__(self, protocol):
        self.protocol = protocol
        self.sent = []
        self.closed = 0
        self.fail_next = False

    def get_protocol(self):
        return self.protocol

    def set_protocol(self, protocol):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("injected protocol handoff failure")
        self.protocol = protocol

    def is_closing(self):
        return bool(self.closed)

    def close(self):
        self.closed += 1

    def sendto(self, data, address):
        assert not self.closed
        self.sent.append((data, address))


class AudioSpliceTest(unittest.IsolatedAsyncioTestCase):
    def relay(self, index, releases):
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        wire = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
        result = sip_rtp_bridge.SipRtpRelay(
            left=sip_rtp_bridge.RtpPeer("127.0.0.1", 10000 + index * 10, 96, fmt, rtp_format=wire),
            right=sip_rtp_bridge.RtpPeer("127.0.0.1", 10002 + index * 10, 96, fmt, rtp_format=wire),
            left_port=20000 + index * 10, right_port=20002 + index * 10,
            on_release=lambda ports: releases.append((index, ports)),
        )
        for side in ("left", "right"):
            protocol = sip_rtp_bridge._RelayProtocol(result, side)
            transport = Transport(protocol)
            protocol.connection_made(transport)
            setattr(result, f"{side}_transport", transport)
        return result

    async def test_splice_keeps_sockets_and_duplex_samples_then_releases_both_reservations(self):
        released = []
        first, second = self.relay(0, released), self.relay(1, released)
        a, b1, b2, c = first.left_transport, first.right_transport, second.left_transport, second.right_transport
        a_clock, c_clock = first.left.ssrc, second.right.ssrc
        combined = sip_rtp_bridge.SipRtpRelay.splice_audio(first, "left", second, "right")
        await first.stop()
        await second.stop()
        self.assertEqual(released, [])
        self.assertEqual((a.closed, b1.closed, b2.closed, c.closed), (0, 1, 1, 0))
        for transport, peer, target, sample in (
            (a, combined.left, c, 1700), (c, combined.right, a, -2100),
        ):
            payload = struct.pack("!320h", *([sample] * 320))
            transport.protocol.datagram_received(
                rtp.build_packet(rtp.RtpPacket(96, 1, 320, 123, payload)), (peer.host, peer.port),
            )
            self.assertTrue(target.sent)
            packet = rtp.parse_packet(target.sent[-1][0])
            self.assertEqual(packet.payload, payload)
            self.assertEqual(packet.ssrc, c_clock if target is c else a_clock)
        self.assertEqual(b1.sent, [])
        self.assertEqual(b2.sent, [])
        await combined.stop()
        await combined.stop()
        self.assertEqual(released, [(0, (20000, 20002)), (1, (20010, 20012))])
        self.assertEqual((a.closed, c.closed), (1, 1))

    async def test_failed_protocol_handoff_restores_both_original_relays(self):
        released = []
        first, second = self.relay(0, released), self.relay(1, released)
        a, c = first.left_transport, second.right_transport
        old_a, old_c = a.protocol, c.protocol
        c.fail_next = True
        with self.assertRaisesRegex(RuntimeError, "handoff failure"):
            sip_rtp_bridge.SipRtpRelay.splice_audio(first, "left", second, "right")
        self.assertIs(first.left_transport, a)
        self.assertIs(second.right_transport, c)
        self.assertIs(a.protocol, old_a)
        self.assertIs(c.protocol, old_c)
        self.assertFalse(a.closed or c.closed)
        combined = sip_rtp_bridge.SipRtpRelay.splice_audio(first, "left", second, "right")
        await first.stop()
        await second.stop()
        await combined.stop()
        self.assertEqual(len(released), 2)

    async def test_reservation_merge_failure_preserves_original_owners_for_retry(self):
        released = []
        first, second = self.relay(0, released), self.relay(1, released)
        left, right = first.left_transport, second.right_transport
        protocols = left.protocol, right.protocol

        def failed_merge(previous, retained):
            raise RuntimeError("reservation unavailable")

        with self.assertRaisesRegex(RuntimeError, "reservation unavailable"):
            sip_rtp_bridge.SipRtpRelay.splice_audio(
                first, "left", second, "right", merge_reservations=failed_merge,
            )
        self.assertEqual((left.protocol, right.protocol), protocols)
        self.assertIs(first.left_transport, left)
        self.assertIs(second.right_transport, right)
        self.assertFalse(first._stop_requested or second._stop_requested)
        self.assertFalse(released)
        merged_releases = []

        def merge(previous, retained):
            self.assertEqual(previous, ((20000, 20002), (20010, 20012)))
            self.assertEqual(retained, (20000, 20012))
            return merged_releases.append

        combined = sip_rtp_bridge.SipRtpRelay.splice_audio(
            first, "left", second, "right", merge_reservations=merge,
        )
        await first.stop()
        await second.stop()
        self.assertFalse(released or merged_releases)
        await combined.stop()
        await combined.stop()
        self.assertEqual(merged_releases, [(20000, 20012)])
        self.assertFalse(released)
        self.assertEqual((left.closed, right.closed), (1, 1))

    async def test_owner_failure_does_not_detach_existing_conversations(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
        from custom_components.voip_stack.runtime_data import VoipStackRuntime
        from custom_components.voip_stack.endpoint_registry import EndpointRegistry
        from custom_components.voip_stack.local_transfer import try_local_audio_transfer

        for failure in ("upsert", "attach_relay", "register_bridge"):
            with self.subTest(failure=failure):
                calls = SipEndpointRuntime(allow_dark_sessions=True)
                original = calls.upsert("original", owner="bridge", state="in_call")
                consultation = calls.upsert("consult", owner="bridge", state="in_call")
                first, second = self.relay(0, []), self.relay(1, [])
                for session, wire, relay in ((original, "a", first), (consultation, "c", second)):
                    calls.add_leg(session.call_id, wire, role="callee", state="in_call")
                    session.legs[wire].dialog = SimpleNamespace(dialog=object(), close=AsyncMock(), terminate=AsyncMock(),
                        dialog_ids=SimpleNamespace(call_id=wire), on_refer=None, on_media_update=None, on_info_dtmf=None)
                    calls.set_bridge_link(session.call_id, wire)
                    calls.attach_relay(session.call_id, relay)
                runtime = VoipStackRuntime(transport_config={}, assist_config={}, trunk_config={},
                    endpoints=EndpointRegistry(), phones=None, sip=calls)
                runtime.rtp_port_pool = {"used": {20000, 20002, 20010, 20012}}
                hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda _domain: [SimpleNamespace(runtime_data=runtime)]))
                grant = SimpleNamespace(original=original, consultation=consultation, pending_call_id="",
                                        endpoint_id="", current=lambda: True)
                left, right = first.right_transport, second.right_transport
                protocols = left.get_protocol(), right.get_protocol()
                try:
                    with patch.object(calls, failure, side_effect=RuntimeError("injected handoff failure")):
                        with self.assertRaisesRegex(RuntimeError, "injected handoff failure"):
                            await try_local_audio_transfer(hass, runtime, grant, None)
                    self.assertIs(first.right_transport, left)
                    self.assertIs(second.right_transport, right)
                    self.assertEqual((left.get_protocol(), right.get_protocol()), protocols)
                    self.assertFalse(first._stop_requested or second._stop_requested)
                    self.assertEqual(runtime.rtp_port_pool["used"], {20000, 20002, 20010, 20012})
                    self.assertEqual(set(calls.sessions), {"original", "consult"})
                    self.assertIs(calls.resource_for("original", "relay"), first)
                    self.assertIs(calls.resource_for("consult", "relay"), second)
                finally:
                    escaped = left.get_protocol().relay
                    if escaped not in (first, second):
                        await escaped.stop()
                    await calls.shutdown()


async def test_accepted_source_video_without_owner_still_blocks_audio_handoff(application):
    """An accepted source video contract cannot disappear during pickup."""
    app, registry, _caller, released = application
    try:
        await app.answer()
        media = app.media
        media.invite = replace(media.invite,
            video_format=sdp.RtpVideoFormat(payload_type=26, encoding="JPEG"), remote_video_rtp_port=42002)
        transport = media.transport
        with pytest.raises(ValueError, match="owned video update"):
            await LocalMediaBridge.prepare(media, app.invite)
        assert media.transport is transport and not media.closed.is_set()
        assert not transport.is_closing() and not released
    finally:
        await registry.shutdown()
