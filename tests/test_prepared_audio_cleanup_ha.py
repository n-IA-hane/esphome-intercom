"""Prepared relay codecs are released without retiring the live generation."""

from dataclasses import replace

import pytest

from custom_components.voip_stack import bridge_media_updates, sip_rtp_bridge
from custom_components.voip_stack.core import sdp
from custom_components.voip_stack.sip_client import SipCallClient
from . import test_automation_originate_ha as automation_fixtures
from .test_dialog_payload_lifetime_ha import dialog

lab = automation_fixtures.lab
pytestmark = pytest.mark.ha


def peer(fmt):
    return sip_rtp_bridge.RtpPeer("127.0.0.1", 40000, fmt.payload_type,
        fmt.audio_format, rtp_format=fmt, send_rtp_format=fmt,
        inbound_rtp_formats=(fmt,), outbound_rtp_formats=(fmt,))


def retain_preparations(monkeypatch, relay):
    states = []
    build = relay._build_media_state

    def capture(left, right):
        state = build(left, right)
        states.append(state)
        return state

    monkeypatch.setattr(relay, "_build_media_state", capture)
    return states


def native_contexts(state):
    return [state[name]._codec for name in ("left_encoder", "right_encoder")
        if state[name] is not None and state[name]._codec is not None]


@pytest.mark.parametrize("outcome", ["discard", "stale", "opposite_changed", "commit"])
async def test_prepared_native_codec_lifetime_is_independent_of_live_audio(monkeypatch, outcome):
    original = sdp.RtpPcmFormat(110, "G726-32", 8000, 1, 20)
    linear = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
    proposed = sdp.RtpPcmFormat(111, "G726-24", 8000, 1, 20)
    relay = sip_rtp_bridge.SipRtpRelay(left=peer(original), right=peer(linear), left_port=0, right_port=0)
    live_encoder = relay.left_encoder
    def observer(*_args):
        pass
    relay.left_decoder._pcm_observers[observer] = None
    states = retain_preparations(monkeypatch, relay)
    update = relay.prepare_peer_reconfiguration("right", peer(proposed))
    candidate = states[0]
    native = native_contexts(candidate)
    assert len(native) == 2 and all(codec._context is not None for codec in native)
    try:
        if outcome == "discard":
            update.discard()
            update.discard()
            assert relay.left_encoder is live_encoder
        elif outcome == "stale":
            relay.reconfigure_peer("right", peer(replace(linear, payload_type=97, sample_rate=8000)))
            with pytest.raises(RuntimeError, match="stale"):
                update()
        elif outcome == "opposite_changed":
            relay.reconfigure_peer("left", peer(sdp.RtpPcmFormat(112, "G726-40", 8000, 1, 20)))
            update()
            update.discard()
            assert relay.right_encoder is not candidate["right_encoder"]
        else:
            update()
            update.discard()
            assert relay.right_encoder is candidate["right_encoder"]
        assert all((codec._context is not None) == (outcome == "commit") for codec in native)
        assert observer in relay.left_decoder._pcm_observers
        assert relay.left_encoder._codec._context is not None
        assert relay.left_encoder.encode(bytes(relay.left_encoder.input_format.nominal_frame_bytes))
    finally:
        update.discard()
        await relay.stop()
    assert all(codec._context is None for codec in native)


async def test_partial_codec_construction_failure_closes_already_prepared_native_state(monkeypatch):
    original = sdp.RtpPcmFormat(110, "G726-32", 8000, 1, 20)
    linear = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
    relay = sip_rtp_bridge.SipRtpRelay(left=peer(original), right=peer(linear), left_port=0, right_port=0)
    constructor = sip_rtp_bridge.RtpPayloadEncoder
    created = []

    def failing_encoder(fmt, **options):
        if created:
            raise ValueError("second candidate encoder failed")
        encoder = constructor(fmt, **options)
        created.append(encoder)
        return encoder

    monkeypatch.setattr(sip_rtp_bridge, "RtpPayloadEncoder", failing_encoder)
    try:
        with pytest.raises(ValueError, match="second candidate"):
            relay.prepare_peer_reconfiguration("right", peer(sdp.RtpPcmFormat(111, "G726-24", 8000, 1, 20)))
        assert created[0]._codec._context is None
        assert relay.left_encoder._codec._context is not None
        assert relay.left_encoder.encode(bytes(320))
    finally:
        await relay.stop()


@pytest.mark.parametrize("rejection", ["rollback", "video_disabled"])
async def test_preparing_dialog_owner_discards_audio_on_rejection(lab, monkeypatch, rejection):
    linear = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
    original = sdp.RtpPcmFormat(110, "G726-32", 8000, 1, 20)
    proposed = sdp.RtpPcmFormat(111, "G726-24", 8000, 1, 20)
    previous = dialog(original, original)
    candidate = dialog(proposed, proposed)
    relay = sip_rtp_bridge.SipRtpRelay(left=peer(linear), right=peer(original), left_port=0, right_port=0)
    lab.calls.upsert(previous.call_id, state="in_call", owner="sip_bridge")
    lab.calls.attach_relay(previous.call_id, relay)
    client = SipCallClient(local_ip="127.0.0.1", local_name="HA", local_sip_port=0, local_rtp_port=0)
    client.dialog = previous
    states = retain_preparations(monkeypatch, relay)
    binder = bridge_media_updates.BridgeMediaUpdateBinder(lab.hass)
    if rejection == "rollback":
        binder.attach_audio_side(client, relay, source_call_id=previous.call_id, side="right")
    else:
        candidate.video_format = sdp.RtpVideoFormat(96)
        binder.attach(client, relay, source_call_id=previous.call_id)
    try:
        result = await client.on_media_update(previous, candidate, "INVITE")
        native = native_contexts(states[0])
        assert len(native) == 1
        if rejection == "rollback":
            assert result is not None and result.rollback is not None
            assert native[0]._context is not None
            await result.rollback()
            await result.rollback()
        else:
            assert result is None
        assert native[0]._context is None
        assert relay.right_encoder._codec._context is not None
        assert relay.right_encoder.encode(bytes(320))
    finally:
        await client.close()
