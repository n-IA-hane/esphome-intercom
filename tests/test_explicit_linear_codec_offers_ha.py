"""Configured standard PCM widths use dynamic HA capabilities, not preset depth."""

import hashlib

import pytest

from custom_components.voip_stack.core import sdp
from custom_components.voip_stack.core.audio_capabilities import HA_AUDIO_CAPABILITIES
from custom_components.voip_stack.core.audio_format import (
    HA_SIP_PCM_TX_FORMATS,
    HA_SIP_PCM_RX_FORMATS,
)
from custom_components.voip_stack.core.codec_policy import CodecPolicy
from custom_components.voip_stack.core.media import negotiator, offers
from custom_components.voip_stack.core.media.capability import PeerEvidence

pytestmark = pytest.mark.ha


def body(policy, *, common=True, **kwargs):
    return sdp.build_offer_directional(
        "127.0.0.1",
        "127.0.0.1",
        40000,
        list(HA_SIP_PCM_TX_FORMATS),
        list(HA_SIP_PCM_RX_FORMATS),
        include_common_codecs=common,
        codec_policy=policy,
        **kwargs,
    )


def assert_frozen_audio_wire(wire, expected):
    """Allow only the explicit disabled-RTCP bandwidth declaration."""

    lines = wire.splitlines(keepends=True)
    audio = [index for index, line in enumerate(lines) if line.startswith("m=audio ")]
    assert len(audio) == 1, wire
    position = audio[0] + 1
    assert lines[position:position + 2] == ["b=RS:0\r\n", "b=RR:0\r\n"], wire
    assert sum(line.startswith(("b=RS:", "b=RR:")) for line in lines) == 2, wire
    preserved = "".join(lines[:position] + lines[position + 2:])
    assert hashlib.sha256(preserved.encode()).hexdigest() == expected, wire


@pytest.mark.parametrize("configured_absent", [False, True])
@pytest.mark.parametrize(
    "common,expected",
    [
        (False, "3da2d069658471731ccc0dcb0a3a6d41886f2db91a828f3103f0bdee363ce1ee"),
        (True, "35d909a935c5fe78a946b644f92a7ad50aefb53aa908b59390e4f9848f288ab5"),
    ],
)
def test_absent_policy_and_default_catalogue_retain_frozen_wire_bytes(
    configured_absent, common, expected
):
    from custom_components.voip_stack.core.codec_capabilities import codec_available

    if common and not all(
        codec_available(name, receive=receive)
        for name in ("G722", "OPUS")
        for receive in (False, True)
    ):
        pytest.skip("The frozen common-codec oracle requires actual G722/Opus backends")
    policy = CodecPolicy.from_config({}, prefix="") if configured_absent else None
    assert_frozen_audio_wire(body(policy, common=common), expected)


@pytest.mark.parametrize("enabled", [("L24",), ("PCMU", "L24"), ("L16", "L24")])
def test_explicit_l24_is_generated_from_dynamic_backend_with_16bit_preferences(enabled):
    wire = body(CodecPolicy(audio=enabled), local_capabilities=HA_AUDIO_CAPABILITIES)
    formats = sdp.offered_pcm_formats(wire)
    assert "L24" in {fmt.encoding for fmt in formats}
    assert all(fmt.encoding in enabled for fmt in formats)
    assert any(
        fmt.encoding == "L24" and fmt.sample_rate == 16000 and fmt.channels == 1
        for fmt in formats
    )
    assert all(HA_AUDIO_CAPABILITIES.supports_rtp(fmt, receive=True) for fmt in formats)
    assert "m=video" not in wire


@pytest.mark.parametrize("unavailable_receive", [False, True])
def test_configured_l24_never_overrides_directional_backend_unavailability(
    monkeypatch, unavailable_receive
):
    actual = offers.codec_available

    def available(name, *, receive=False):
        return (
            False
            if name == "L24" and receive == unavailable_receive
            else actual(name, receive=receive)
        )

    monkeypatch.setattr(offers, "codec_available", available)
    with pytest.raises(ValueError, match="No enabled audio codec"):
        body(CodecPolicy(audio=("L24",)), local_capabilities=HA_AUDIO_CAPABILITIES)


def test_fixed_16bit_local_capabilities_do_not_claim_dynamic_l24():
    with pytest.raises(ValueError, match="No enabled audio codec"):
        negotiator.create_offer(
            HA_SIP_PCM_TX_FORMATS,
            HA_SIP_PCM_RX_FORMATS,
            codec_policy=CodecPolicy(audio=("L24",)),
            capabilities=None,
        )


def test_complete_known_peer_excluding_l24_is_not_expanded():
    evidence = PeerEvidence.from_tokens(
        ["PCMU/8000/1/20"], ["PCMU/8000/1/20"], complete=True
    )
    with pytest.raises(ValueError, match="No enabled audio codec"):
        # Complete evidence narrows the enabled catalogue; it cannot add PCMU
        # when the caller explicitly leaves common-codec expansion disabled.
        negotiator.create_offer(HA_SIP_PCM_TX_FORMATS, HA_SIP_PCM_RX_FORMATS, evidence,
            codec_policy=CodecPolicy(audio=("PCMU", "L24")), capabilities=HA_AUDIO_CAPABILITIES,
            common=False)
    reduced = negotiator.create_offer(
        HA_SIP_PCM_TX_FORMATS,
        HA_SIP_PCM_RX_FORMATS,
        evidence,
        codec_policy=CodecPolicy(audio=("PCMU", "L24")),
        capabilities=HA_AUDIO_CAPABILITIES,
        common=True,
    )
    assert {mapping[0] for _, mapping in reduced.payloads} == {
        "PCMU",
        "TELEPHONE-EVENT",
    }
    with pytest.raises(ValueError, match="No enabled audio codec"):
        negotiator.create_offer(
            HA_SIP_PCM_TX_FORMATS,
            HA_SIP_PCM_RX_FORMATS,
            evidence,
            codec_policy=CodecPolicy(audio=("L24",)),
            capabilities=HA_AUDIO_CAPABILITIES,
            common=True,
        )


def test_explicit_rtp_mapping_and_fmtp_remain_exact_without_linear_expansion():
    fmt = sdp.RtpPcmFormat(112, "L24", 16000, 1, 20, fmtp="emphasis=50-15")
    wire = body(
        CodecPolicy(audio=("L24", "L16")),
        local_capabilities=HA_AUDIO_CAPABILITIES,
        send_rtp_formats=(fmt,),
        recv_rtp_formats=(fmt,),
    )
    formats = sdp.offered_pcm_formats(wire)
    assert [
        (item.encoding, item.payload_type, item.sample_rate, item.channels, item.fmtp)
        for item in formats
    ] == [("L24", 112, 16000, 1, fmt.fmtp)]
    excluded = sdp.RtpPcmFormat(110, "L16", 16000, 1, 20)
    with pytest.raises(ValueError, match="No enabled audio codec"):
        body(
            CodecPolicy(audio=("L24",)),
            send_rtp_formats=(excluded,),
            recv_rtp_formats=(excluded,),
        )


@pytest.mark.parametrize("ordered", [False, True])
def test_legacy_constructor_l24_override_does_not_depend_on_ordering(ordered):
    policy = CodecPolicy(audio=("L24",), audio_ordered=ordered)
    assert policy.audio_configured is True
    assert {
        fmt.encoding
        for fmt in sdp.offered_pcm_formats(
            body(policy, local_capabilities=HA_AUDIO_CAPABILITIES)
        )
    } == {"L24"}


def test_policy_provenance_normalizes_constructor_and_preserves_inheritance():
    from dataclasses import replace
    from custom_components.voip_stack.core.codec_policy import DEFAULT_AUDIO_CODECS

    absent = CodecPolicy.from_config({}, prefix="")
    assert absent.audio == DEFAULT_AUDIO_CODECS and absent.audio_configured is False
    assert CodecPolicy() == CodecPolicy(audio_configured=False)
    assert CodecPolicy(audio=("L24",)) == CodecPolicy(
        audio=("L24",), audio_configured=True
    )
    parent = CodecPolicy.from_config({"audio_codecs": ["L24"]}, prefix="")
    inherited = CodecPolicy.from_config({}, parent, prefix="")
    assert inherited.audio == ("L24",) and inherited.audio_configured is True
    assert CodecPolicy.from_config({}, absent, prefix="").audio_configured is False
    explicit_default = CodecPolicy(absent.audio, absent.video, False, False)
    assert explicit_default.audio_configured is True
    assert explicit_default != absent
    assert explicit_default == replace(absent, audio_configured=True)
    assert "L24" in {
        fmt.encoding
        for fmt in sdp.offered_pcm_formats(
            body(explicit_default, local_capabilities=HA_AUDIO_CAPABILITIES)
        )
    }


@pytest.mark.parametrize(
    "direction,peer_sends,peer_receives,expected",
    [
        ("recvonly", ["PCMU/8000/1/32"], [], ("PCMU", 8000, 32)),
        ("sendonly", [], ["PCMU/8000/1/32"], ("PCMU", 8000, 32)),
        ("recvonly", ["L24/11025/1/40"], [], ("L24", 11025, 40)),
    ],
)
def test_complete_simplex_peer_keeps_only_its_declared_mapping_and_time(
    direction, peer_sends, peer_receives, expected
):
    evidence = PeerEvidence.from_tokens(peer_sends, peer_receives, complete=True)
    plan = negotiator.create_offer(
        HA_SIP_PCM_TX_FORMATS,
        HA_SIP_PCM_RX_FORMATS,
        evidence,
        codec_policy=CodecPolicy(audio=("PCMU", "L24")),
        capabilities=HA_AUDIO_CAPABILITIES,
        common=True,
        direction=direction,
    )
    assert {(fmt.encoding, fmt.sample_rate, fmt.frame_ms) for fmt in plan.formats} == {
        expected
    }
    assert plan.packet_ms == expected[2]
    assert bool(plan.send_formats) is (direction == "sendonly")
    assert bool(plan.receive_formats) is (direction == "recvonly")


@pytest.mark.parametrize("direction", ["sendrecv", "sendonly", "recvonly"])
def test_complete_empty_peer_cannot_gain_unannounced_audio(direction):
    with pytest.raises(ValueError, match="No enabled audio codec"):
        negotiator.create_offer(
            HA_SIP_PCM_TX_FORMATS,
            HA_SIP_PCM_RX_FORMATS,
            PeerEvidence(complete=True),
            codec_policy=CodecPolicy(audio=("L24",)),
            capabilities=HA_AUDIO_CAPABILITIES,
            direction=direction,
        )


def test_complete_peer_cannot_replace_explicit_local_payload_or_fmtp():
    fmt = sdp.RtpPcmFormat(112, "L24", 16000, 1, 20, fmtp="emphasis=50-15")
    evidence = PeerEvidence.from_tokens(
        ["L24/16000/1/20"], ["L24/16000/1/20"], complete=True
    )
    plan = negotiator.create_offer(
        HA_SIP_PCM_TX_FORMATS,
        HA_SIP_PCM_RX_FORMATS,
        evidence,
        codec_policy=CodecPolicy(audio=("L24",)),
        capabilities=HA_AUDIO_CAPABILITIES,
        send_rtp_formats=(fmt,),
        recv_rtp_formats=(fmt,),
    )
    assert len(plan.formats) == 1
    assert plan.formats[0].payload_type == 112 and plan.formats[0].fmtp == fmt.fmtp
    incompatible = PeerEvidence.from_tokens(["L24/16000/1/10"], [], complete=True)
    with pytest.raises(ValueError, match="No enabled audio codec"):
        negotiator.create_offer(
            HA_SIP_PCM_TX_FORMATS,
            HA_SIP_PCM_RX_FORMATS,
            incompatible,
            codec_policy=CodecPolicy(audio=("L24",)),
            capabilities=HA_AUDIO_CAPABILITIES,
            recv_rtp_formats=(fmt,),
            direction="recvonly",
        )


def test_runtime_trunk_policy_keeps_absence_inheritance_and_unordered_constructor():
    from types import SimpleNamespace
    from custom_components.voip_stack.config import normalized_trunk_config
    from custom_components.voip_stack.sip_trunk import SipTrunkConfig
    from custom_components.voip_stack.trunk_runtime import trunk_client_config
    from custom_components.voip_stack.trunk_signaling import reuse_registered_trunk_flow
    from custom_components.voip_stack.sip_client import SipCallClient
    from dataclasses import fields

    absent = trunk_client_config(normalized_trunk_config({"trunk_options_interval": 30, }))
    assert absent.audio_configured is False
    parent = CodecPolicy.from_config({"audio_codecs": ["L24"]}, prefix="")
    inherited = trunk_client_config(normalized_trunk_config({"trunk_options_interval": 30, }), parent=parent)
    assert inherited.audio_codecs == ("L24",) and inherited.audio_configured is True
    raw = {
        field.name: getattr(absent, field.name)
        for field in fields(absent)
        if field.name not in {"audio_codecs", "audio_ordered", "audio_configured"}
    }
    direct_default = SipTrunkConfig(**raw)
    assert (
        direct_default.audio_codecs is None and direct_default.audio_configured is False
    )
    direct = SipTrunkConfig(**raw, audio_codecs=("L24",), audio_ordered=False)
    assert direct.audio_codecs == ("L24",) and direct.audio_configured is True
    for config, expected in (
        (absent, False),
        (inherited, True),
        (direct, True),
        (direct_default, False),
    ):
        client = SipCallClient(
            local_ip="127.0.0.1",
            local_name="Trunk",
            local_sip_port=5060,
            local_rtp_port=40000,
        )
        assert not reuse_registered_trunk_flow(SimpleNamespace(config=config), client)
        assert client.codec_policy.audio_configured is expected
        if expected:
            assert {
                fmt.encoding
                for fmt in sdp.offered_pcm_formats(
                    body(client.codec_policy, local_capabilities=HA_AUDIO_CAPABILITIES)
                )
            } == {"L24"}


@pytest.mark.parametrize("configured_absent", [False, True])
@pytest.mark.parametrize(
    "common,expected",
    [
        (False, "6f9b635d3544e590bc646e415c17f5592c38b988e93700f19db9089b7fbf69ac"),
        (True, "be4852304b6864e798f99f975dd27b2559234ef661ab41a9282bf87865001b37"),
    ],
)
def test_dynamic_default_offer_keeps_independent_baseline_wire_bytes(
    configured_absent, common, expected
):
    from custom_components.voip_stack.core.codec_capabilities import codec_available

    if common and not all(
        codec_available(name, receive=receive)
        for name in ("G722", "OPUS")
        for receive in (False, True)
    ):
        pytest.skip("The frozen baseline requires actual G722/Opus backends")
    policy = CodecPolicy.from_config({}, prefix="") if configured_absent else None
    assert_frozen_audio_wire(
        body(policy, common=common, local_capabilities=HA_AUDIO_CAPABILITIES),
        expected,
    )
