"""Independent SDP witnesses: receive budgets stay directional for every codec."""


import pytest

from tests.test_video_rtp import _load

sdp = _load("sdp")


def remote_video(fmt, *, framerate=None, imageattr=""):
    lines = ["v=0", "o=- 1 1 IN IP4 127.0.0.2", "s=peer", "c=IN IP4 127.0.0.2", "t=0 0",
             f"m=video 40002 RTP/AVP {fmt.payload_type}",
             f"a=rtpmap:{fmt.payload_type} {fmt.encoding}/90000", "a=sendrecv"]
    if fmt.encoding == "H264":
        lines.append(f"a=fmtp:{fmt.payload_type} profile-level-id={fmt.profile_level_id};packetization-mode=1;level-asymmetry-allowed={int(fmt.level_asymmetry_allowed)}")
    if framerate is not None:
        lines.append(f"a=framerate:{framerate}")
    if imageattr:
        lines.append("a=imageattr:" + imageattr)
    return "\r\n".join(lines) + "\r\n"


@pytest.mark.parametrize("encoding,asymmetric", [("JPEG", False), ("VP8", False), ("H264", False), ("H264", True)])
@pytest.mark.parametrize("local_rx", [2, 15])
@pytest.mark.parametrize("role", ["offerer", "answerer"])
def test_peer_receive_fps_constrains_local_tx_without_lowering_local_rx(encoding, asymmetric, local_rx, role):
    fmt = sdp.RtpVideoFormat(payload_type=26 if encoding == "JPEG" else 96,
        encoding=encoding, profile_level_id="42e01f", packetization_mode=1,
        level_asymmetry_allowed=asymmetric, max_framerate=local_rx)
    remote = remote_video(fmt, framerate=10)
    if role == "offerer":
        contract = sdp.negotiate_video_answer_directional(remote, fmt)
    else:
        contract = sdp.negotiate_video_offer_directional(remote, local_formats=(fmt,))
    assert contract is not None
    assert contract.send.max_framerate == 10
    assert contract.recv.max_framerate == local_rx
    if role == "answerer":
        wire = "\r\n".join(sdp._video_media_lines(40004, contract.answer, direction="sendrecv")) + "\r\n"
        assert f"a=framerate:{local_rx}\r\n" in wire


def test_optional_imageattr_survives_actual_sdp_parser_and_answer_wire():
    fmt = sdp.RtpVideoFormat(payload_type=26, encoding="JPEG")
    remote = remote_video(fmt, imageattr="26 send [x=640,y=480] recv [x=320,y=240]")
    selected, = sdp.offered_video_formats(remote)
    assert getattr(selected, "image_attributes", None) is not None
    contract = sdp.negotiate_video_offer_directional(remote)
    assert contract is not None
    wire = "\r\n".join(sdp._video_media_lines(40004, contract.answer, direction="sendrecv")) + "\r\n"
    assert "a=imageattr:26" in wire
    assert "send [x=320,y=240" in wire
    assert "recv [x=640,y=480" in wire
