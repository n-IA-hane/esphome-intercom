"""Native account diagnostics read real relay operations and keep call ownership."""

import logging
from datetime import timedelta
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
import pytest

from custom_components.voip_stack import call_projection
from custom_components.voip_stack import pbx_media_diagnostics as diagnostics
from custom_components.voip_stack.core import rtp, sdp
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.sip_rtp_bridge import RtpPeer, SipRtpRelay
from custom_components.voip_stack.sip_video_relay import SipVideoRtpRelay, VideoRtpPeer
from tests.test_managed_phones_ha import custom_integration_path as custom_integration_path
from tests.test_phone_state_ha import phones as phones

pytestmark = pytest.mark.ha

def fmt(rate=16000, *, codec="L16", ms=20, pt=96):
    return sdp.RtpPcmFormat(pt, codec, rate, 1, ms)


def peer(wire, host, port, **kwargs):
    return RtpPeer(host, port, wire.payload_type, wire.audio_format,
                   rtp_format=wire, send_rtp_format=wire, **kwargs)


def relay(left=None, right=None, **kwargs):
    return SipRtpRelay(left=left or peer(fmt(), "192.0.2.1", 40000),
                       right=right or peer(fmt(pt=110), "192.0.2.2", 41000),
                       left_port=42000, right_port=42002, **kwargs)


@pytest.mark.parametrize("source,target,operation", [
    (fmt(), fmt(pt=110), "forwarded"),
    (fmt(ms=10), fmt(ms=20), "repacketized"),
    (fmt(), fmt(48000), "resampled"),
    (fmt(8000, codec="PCMA", pt=8), fmt(8000, codec="PCMU", pt=0), "codec_converted"),
    (fmt(8000, codec="PCMA", pt=8), fmt(), "resampled_codec_converted"),
    (fmt(8000, codec="G722", pt=9), fmt(), "codec_converted"),
])
async def test_operations_come_from_observed_wire_and_current_encoder(source, target, operation):
    media = relay(left=peer(source, "192.0.2.1", 40000), right=peer(target, "192.0.2.2", 41000))
    try:
        assert media.audio_operations == ("unknown", "unknown")
        media.left_to_right_clock.observe_format(source)
        assert media.audio_operations == (operation, "unknown")
        media.right.can_receive = False
        assert media.audio_operations[0] == "inactive"
        media.right.can_receive = True
        media.right.connection_held = True
        assert media.audio_operations[0] == "inactive"
    finally:
        await media.stop()


async def test_debug_decoder_does_not_make_identical_wire_a_conversion(monkeypatch, tmp_path):
    from custom_components.voip_stack import sip_rtp_bridge

    monkeypatch.setattr(sip_rtp_bridge, "DEBUG_CAPTURE_DIR", tmp_path)
    media = relay(debug_capture=True)
    try:
        assert media.left_decoder is not None and media.right_encoder is not None
        media.left_to_right_clock.observe_format(media.left.inbound_rtp_format)
        assert media.audio_operations == ("forwarded", "unknown")
    finally:
        await media.stop()


async def test_video_uses_committed_transcoder_direction_and_hold(monkeypatch):
    media = relay()
    video_fmt = sdp.RtpVideoFormat(payload_type=98, encoding="JPEG")
    video = SipVideoRtpRelay(left=VideoRtpPeer("192.0.2.1", 40004, 40005, video_fmt),
                             right=VideoRtpPeer("192.0.2.2", 41004, 41005, video_fmt),
                             left_port=42004, right_port=42006)
    media.video_relay = video
    try:
        # This is the active generation, including same-codec JPEG geometry work.
        video._transcode.directions.add("left")
        video._transcode.transcoders["left"] = SimpleNamespace(
            ready=True, async_close=AsyncMock())
        assert diagnostics._video_operations(media) == ("converted", "forwarded")
        video.right.video_format = replace(video_fmt, direction="sendonly")
        assert diagnostics._video_operations(media) == ("inactive", "forwarded")
        video.right.video_format = video_fmt
        video.left.connection_held = True
        assert diagnostics._video_operations(media) == ("converted", "inactive")
    finally:
        await media.stop()



def attach(phones, media, call_id="one", *, source="office", target="kitchen", source_role="caller"):
    calls = phones.calls
    session = calls.upsert(call_id, state="in_call", source_endpoint_id=source, dest_endpoint_id=target)
    calls.observe_leg(call_id, call_id + "-source", role=source_role, state="in_call", endpoint_id=source,
                      sip_call_id=call_id + "-source")
    calls.observe_leg(call_id, call_id + "-dest", role="callee", state="in_call", endpoint_id=target,
                      sip_call_id=call_id + "-dest")
    calls.attach_relay(call_id, media)
    return session


async def add_sensor(hass, phones, endpoint_id):
    from custom_components.voip_stack.sensor import PhoneEndpointMediaProcessingSensor

    entity = PhoneEndpointMediaProcessingSensor(hass, phones.endpoints.require(endpoint_id), phones.endpoints)
    platform = EntityPlatform(hass=hass, logger=logging.getLogger(__name__), domain="sensor",
                              platform_name="voip_stack", platform=None, scan_interval=timedelta(seconds=30),
                              entity_namespace=None)
    platform.config_entry = hass.config_entries.async_entries("voip_stack")[0]
    await platform.async_add_entities([entity])
    return entity, platform


async def test_native_sensor_tracks_both_directions_packets_and_cleanup(hass, phones):
    source, target = fmt(16000, ms=10), fmt(48000, ms=10, pt=110)
    media = relay(left=peer(source, "192.0.2.1", 40000), right=peer(target, "192.0.2.2", 41000))
    media.left_transport, media.right_transport = Mock(), Mock()
    from custom_components.voip_stack.phone_state import PHONE_STATE_CHANGED

    events = []
    unsubscribe = hass.bus.async_listen(PHONE_STATE_CHANGED, lambda event: events.append(event))
    first, platform = await add_sensor(hass, phones, "office")
    second, other_platform = await add_sensor(hass, phones, "kitchen")
    try:
        assert hass.states.get(first.entity_id).state == "idle"
        session = attach(phones, media)
        call_projection.publish_media_route(hass, session.call_id, media.media_route)
        await hass.async_block_till_done()
        assert hass.states.get(first.entity_id).state == "unknown"
        events.clear()
        media.handle_packet("left", rtp.build_packet(rtp.RtpPacket(source.payload_type,
                            1, 0, 123, b"\0" * 320)), ("192.0.2.1", 40000))
        await hass.async_block_till_done()
        left = hass.states.get(first.entity_id).attributes["calls"][0]
        right = hass.states.get(second.entity_id).attributes["calls"][0]
        assert (left["audio_tx"], left["audio_rx"]) == ("resampled", "unknown")
        assert (right["audio_tx"], right["audio_rx"]) == ("unknown", "resampled")
        assert left["leg_id"] == "one-source" and left["generation"] == session.generation
        assert not events, "A media-only update must not emit call-state/BLF activity"
        media.handle_packet("right", rtp.build_packet(rtp.RtpPacket(target.payload_type,
                            1, 0, 456, b"\0" * 960)), ("192.0.2.2", 41000))
        await hass.async_block_till_done()
        assert hass.states.get(first.entity_id).state == "processed"
        before = hass.states.get(first.entity_id)
        media.handle_packet("left", rtp.build_packet(rtp.RtpPacket(source.payload_type,
                            2, 160, 123, b"\0" * 320)), ("192.0.2.1", 40000))
        await hass.async_block_till_done()
        assert hass.states.get(first.entity_id) is before
        # Native entity rename preserves the same account and listener.
        old_id, unique = first.entity_id, first.unique_id
        er.async_get(hass).async_update_entity(old_id, new_entity_id="sensor.office_processing_renamed")
        await hass.async_block_till_done()
        assert first.unique_id == unique
        assert phones.endpoints.by_entity_id(first.entity_id).endpoint_id == "office"
        assert phones.endpoints.by_entity_id(old_id) is None
        media.right.can_receive = False
        diagnostics.publish_media_processing(hass, phones.runtime, session)
        await hass.async_block_till_done()
        assert hass.states.get(first.entity_id).attributes["calls"][0]["audio_tx"] == "inactive"
        assert hass.states.get(first.entity_id).attributes["calls"][0]["audio_rx"] == "resampled"
        await phones.calls.request_termination(session.call_id, TerminationIntent("done"))
        await hass.async_block_till_done()
        assert hass.states.get(first.entity_id).state == "idle"
        assert hass.states.get(first.entity_id).attributes["calls"] == []
    finally:
        await platform.async_reset()
        await other_platform.async_reset()
        unsubscribe()


async def test_multiple_calls_stay_distinct_and_unsupported_adapters_stay_unknown(hass, phones):
    from custom_components.voip_stack.phone_state import phone_states

    first = relay()
    first.left_to_right_clock.observe_format(first.left.inbound_rtp_format)
    first.right_to_left_clock.observe_format(first.right.inbound_rtp_format)
    one = attach(phones, first)
    second = relay(left=peer(fmt(8000, codec="PCMA", pt=8), "192.0.2.1", 43000))
    second.left_to_right_clock.observe_format(second.left.inbound_rtp_format)
    second.right_to_left_clock.observe_format(second.right.inbound_rtp_format)
    two = attach(phones, second, "two")
    state, attributes = diagnostics.account_media_processing(phones.runtime, "office", phone_states(hass)["office"]["calls"])
    assert state == "mixed"
    assert {item["call_id"] for item in attributes["calls"]} == {one.call_id, two.call_id}
    assert attributes["call_count"] == 2
    await phones.calls.request_termination(one.call_id, TerminationIntent("done"))
    await phones.calls.request_termination(two.call_id, TerminationIntent("done"))
    browser = attach(phones, relay(), "browser", source_role="ha_softphone")
    state, attributes = diagnostics.account_media_processing(phones.runtime, "office", phone_states(hass)["office"]["calls"])
    assert state == "unknown"
    call = phone_states(hass)["office"]["calls"][0]
    assert set(diagnostics.call_media_processing(phones.runtime, "office", {**call, "generation": browser.generation + 1}).values()) == {"unknown"}


async def test_relay_handoff_rejects_old_processing_callback(hass, phones):
    entity, platform = await add_sensor(hass, phones, "office")
    media = relay()
    first = attach(phones, media)
    call_projection.publish_media_route(hass, first.call_id, media.media_route)
    retired = media.on_media_processing_changed
    first.release_resource(f"relay:{first.call_id}", value=media)
    second = attach(phones, media, "two")
    call_projection.publish_media_route(hass, second.call_id, media.media_route)
    active = media.on_media_processing_changed
    try:
        assert active is not retired
        await phones.calls.request_termination(first.call_id, TerminationIntent("replaced"))
        media.left_to_right_clock.observe_format(media.left.inbound_rtp_format)
        media.right_to_left_clock.observe_format(media.right.inbound_rtp_format)
        retired()
        await hass.async_block_till_done()
        assert hass.states.get(entity.entity_id).state == "unknown"
        active()
        await hass.async_block_till_done()
        state = hass.states.get(entity.entity_id)
        assert state.state == "forwarded" and state.attributes["call_count"] == 1
        assert state.attributes["calls"][0]["call_id"] == "two"
    finally:
        await platform.async_reset()
