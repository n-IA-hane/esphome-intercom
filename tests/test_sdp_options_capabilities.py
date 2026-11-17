"""OPTIONS capability placeholders must not relax offer/answer endpoints."""

from pathlib import Path

import pytest

from .voip_phase1_support import sdp


# Native S3 OPTIONS captured on 2026-11-06, with only IP/session identity scrubbed.
# Keep the emitted media/RTCP zero ports, directional catalogs and JPEG metadata.
NATIVE_OPTIONS = (Path(__file__).parent / "fixtures/sdp/native_options_capabilities.sdp").read_bytes()
RTCP = b"a=rtcp:0 IN IP4 192.0.2.192\r\n"
TX = ["L16/16000/1/10", "L16/16000/1/20", "G722/8000/1/20", "G722/8000/1/10"]
RX = ["L16/48000/1/10", "L16/16000/1/20", "L16/16000/1/10",
      "G722/8000/1/20", "G722/8000/1/10"]


def test_native_options_retains_video_and_exact_directional_audio_catalogs():
    caps = sdp.options_capabilities(NATIVE_OPTIONS)
    assert caps["sip_video_codec"] == "jpeg"
    assert caps["sip_audio_tx_formats"] == TX
    assert caps["sip_audio_rx_formats"] == RX
    assert caps["audio_mode"] == "full_duplex"
    assert caps["sdp_features"] == ["directional_audio_v1", "independent_audio_ptime_v1"]
    video = sdp.parse_video_sdp(NATIVE_OPTIONS, capabilities=True)
    assert video["media_port"] == video["rtcp_port"] == 0
    assert video["direction"] == "recvonly" and video["framerate"] == 10
    assert video["image_attributes"]
    assert [fmt.encoding for fmt in sdp.offered_video_formats(
        NATIVE_OPTIONS, capabilities=True)] == ["JPEG"]


@pytest.mark.parametrize("attribute", [RTCP, b"a=rtcp:0\r\n"])
def test_zero_rtcp_requires_capability_context_and_zero_media_port(attribute):
    body = NATIVE_OPTIONS.replace(RTCP, attribute)
    assert sdp.parse_video_sdp(body, capabilities=True)["rtcp_port"] == 0
    with pytest.raises(sdp.SdpError):
        sdp.parse_video_sdp(body)
    active = body.replace(b"m=video 0 ", b"m=video 41002 ")
    for capabilities in (False, True):
        with pytest.raises(sdp.SdpError):
            sdp.parse_video_sdp(active, capabilities=capabilities)
    assert "sip_video_codec" not in sdp.options_capabilities(active)


@pytest.mark.parametrize("port", [1, 41003, 65535])
def test_positive_rtcp_endpoint_validation_is_unchanged(port):
    body = NATIVE_OPTIONS.replace(b"m=video 0 ", b"m=video 41002 ").replace(
        RTCP, f"a=rtcp:{port} IN IP4 192.0.2.192\r\n".encode())
    for capabilities in (False, True):
        parsed = sdp.parse_video_sdp(body, capabilities=capabilities)
        assert parsed["media_port"] == 41002 and parsed["rtcp_port"] == port
        assert [fmt.encoding for fmt in sdp.offered_video_formats(
            body, capabilities=capabilities)] == ["JPEG"]


@pytest.mark.parametrize("attribute", [
    b"a=rtcp:-1\r\n", b"a=rtcp:65536\r\n", b"a=rtcp:invalid\r\n",
    b"a=rtcp:\r\n", b"a=rtcp:0 IN IP4\r\n", b"a=rtcp:0 IN IP4 192.0.2.192 extra\r\n",
])
def test_capability_context_keeps_malformed_rtcp_strict_and_audio_usable(attribute):
    body = NATIVE_OPTIONS.replace(RTCP, attribute)
    for capabilities in (False, True):
        with pytest.raises(sdp.SdpError):
            sdp.parse_video_sdp(body, capabilities=capabilities)
    caps = sdp.options_capabilities(body)
    assert "sip_video_codec" not in caps
    assert caps["sip_audio_tx_formats"] == TX and caps["sip_audio_rx_formats"] == RX


@pytest.mark.parametrize("old,new", [
    (RTCP, b"a=rtcp:0 IN IP6 2001:db8::1\r\n"),
    (b"m=video 0 RTP/AVP 26", b"m=video 0 UDP/TLS/RTP/SAVPF 26"),
    (b"m=video 0 RTP/AVP 26", b"m=video 0 RTP/AVP invalid"),
    (b"a=rtpmap:26 JPEG/90000", b"a=rtpmap:26 JPEG/8000"),
    (b"a=rtpmap:26 JPEG/90000", b"a=rtpmap:26 H264/90000"),
])
def test_capability_placeholder_does_not_invent_unsupported_video(old, new):
    caps = sdp.options_capabilities(NATIVE_OPTIONS.replace(old, new))
    assert "sip_video_codec" not in caps
    assert caps["sip_audio_tx_formats"] == TX and caps["sip_audio_rx_formats"] == RX


def test_audio_only_options_stays_unknown_and_disabled_ordinary_video_stays_disabled():
    audio = NATIVE_OPTIONS.split(b"m=video", 1)[0]
    assert "sip_video_codec" not in sdp.options_capabilities(audio)
    assert sdp.offered_video_formats(NATIVE_OPTIONS.replace(RTCP, b"")) == []
