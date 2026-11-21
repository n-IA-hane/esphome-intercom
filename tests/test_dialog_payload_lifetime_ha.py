"""Production SIP candidates retain both receivers' retired RTP meanings."""

from custom_components.voip_stack.core import sdp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.sip_client import SipCallClient, SipDialog
from dataclasses import replace
import pytest

pytestmark = pytest.mark.ha


def description(*mappings, video=""):
    payloads = " ".join(str(number) for number, _ in mappings)
    maps = "".join(f"a=rtpmap:{number} {mapping}\r\n" for number, mapping in mappings)
    return ("v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=Independent peer\r\n"
            "c=IN IP4 192.0.2.2\r\nt=0 0\r\n"
            f"m=audio 40000 RTP/AVP {payloads}\r\n{maps}a=ptime:20\r\na=sendrecv\r\n" + video)


def dialog(send, receive, **extra):
    return SipDialog(target="Door", remote_host="192.0.2.2", remote_sip_port=5060,
        remote_rtp_host="192.0.2.2", remote_rtp_port=40000, local_rtp_port=40002,
        call_id="payload-lifetime", local_uri="sip:HA@192.0.2.1", remote_uri="sip:Door@192.0.2.2",
        remote_target_uri="sip:Door@192.0.2.2", send_format=send, recv_format=receive,
        recv_formats=(receive,), send_formats=(send,), **extra)


def answer_candidate(client, current, offer, body, *, video=()):
    answer = sip.parse_message(sip.build_response(200, "OK", [
        ("Content-Type", "application/sdp"), ("Contact", "<sip:Door@192.0.2.2>")], body.encode()))
    return client._dialog_candidate_from_answer(current, offer, answer,
        local_video_rtp_port=40004 if video else 0, offered_video_formats=video,
        video_direction="sendrecv" if video else "inactive", session_version=2)


@pytest.mark.parametrize("offerer", ["local", "remote"])
def test_retired_peer_receive_number_cannot_change_codec_after_offerer_changes(offerer):
    client = SipCallClient(local_ip="192.0.2.1", local_name="HA", local_sip_port=5060,
        local_rtp_port=40002, supported_formats=[AudioFormat(rate, frame_ms=20) for rate in (8000, 16000)],
        peer_user_agent="Dahua UAC/1.0")
    original = dialog(sdp.RtpPcmFormat(120, "PCM", 16000, 1, 20),
                      sdp.RtpPcmFormat(96, "PCM", 16000, 1, 20))
    client.dialog = original
    retired = answer_candidate(client, original, description((96, "PCM/16000"), (0, "PCMU/8000")),
                               description((0, "PCMU/8000")))
    assert retired is not None and retired.send_format.payload_type == retired.recv_format.payload_type == 0
    client.dialog = retired
    if offerer == "local":
        changed = answer_candidate(client, retired, description((97, "L16/16000")),
                                   description((120, "L16/16000")))
    else:
        request = sip.parse_message(sip.build_request("INVITE", "sip:HA@192.0.2.1",
            [("Content-Type", "application/sdp"), ("Contact", "<sip:Door@192.0.2.2>")],
            description((120, "L16/16000")).encode()))
        changed = client._answer_remote_offer(request)
    assert changed is None
    assert client.dialog is retired and retired.send_format.encoding == "PCMU"
    restored = answer_candidate(client, retired, description((96, "PCM/16000")),
                                description((120, "PCM/16000")))
    assert restored is not None and (restored.send_format.payload_type, restored.recv_format.payload_type) == (120, 96)


def video_section(payload, codec, *, fps=10, port=42000):
    parameters = (f"a=fmtp:{payload} packetization-mode=1;profile-level-id=42c01e;level-asymmetry-allowed=1\r\n"
                  if codec == "H264" else "")
    return (f"m=video {port} RTP/AVP {payload}\r\na=rtpmap:{payload} {codec}/90000\r\n"
            f"{parameters}a=framerate:{fps}\r\na=sendrecv\r\n")


def video_client():
    formats = (sdp.RtpVideoFormat(103, profile_level_id="42c01e", max_framerate=10),
               sdp.RtpVideoFormat(104, encoding="VP8", max_framerate=10),
               sdp.RtpVideoFormat(26, encoding="JPEG", max_framerate=10))
    client = SipCallClient(local_ip="192.0.2.1", local_name="HA", local_sip_port=5060, local_rtp_port=40002,
        supported_formats=[AudioFormat(8000, frame_ms=20)], local_video_rtp_port=40004,
        video_formats=formats, generic_video_relay=True)
    return client, formats


@pytest.mark.parametrize("index,remote_payload", [(0, 120), (1, 121), (2, 26)], ids=["h264", "vp8", "jpeg"])
def test_video_answer_numbers_belong_to_their_own_receive_namespace(index, remote_payload):
    client, formats = video_client()
    offered = formats[index]
    pcmu = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20)
    original = dialog(pcmu, pcmu)
    candidate = answer_candidate(client, original,
        description((0, "PCMU/8000"), video=video_section(offered.payload_type, offered.encoding)),
        description((0, "PCMU/8000"), video=video_section(remote_payload, offered.encoding)), video=(offered,))
    assert candidate is not None
    assert candidate.video_format.payload_type == remote_payload
    assert candidate.local_video_format.payload_type == offered.payload_type
    identity = (offered.encoding, 90000, 1)
    assert candidate.video_payloads.send == {remote_payload: identity}
    assert candidate.video_payloads.receive == {offered.payload_type: identity}
    if remote_payload != offered.payload_type:
        assert remote_payload not in candidate.video_payloads.receive


@pytest.mark.parametrize("remapped", [103, 120], ids=["local-receive-history", "peer-receive-history"])
def test_video_removal_keeps_both_histories_and_optional_remap_preserves_audio(remapped):
    client, formats = video_client()
    pcmu = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20)
    original = dialog(pcmu, pcmu, video_format=replace(formats[0], payload_type=120), local_video_format=formats[0])
    removed = answer_candidate(client, original,
        description((0, "PCMU/8000"), video="m=video 0 RTP/AVP 103\r\n"),
        description((0, "PCMU/8000"), video="m=video 0 RTP/AVP 103\r\n"))
    assert removed is not None and removed.video_format is None
    assert not removed.video_payloads.send and not removed.video_payloads.receive
    assert removed.video_payloads.send_history[120] == removed.video_payloads.receive_history[103] == ("H264", 90000, 1)
    client.dialog = removed
    request = sip.parse_message(sip.build_request("INVITE", "sip:HA@192.0.2.1", [
        ("Content-Type", "application/sdp"), ("Contact", "<sip:Door@192.0.2.2>")],
        description((0, "PCMU/8000"), video=video_section(remapped, "VP8")).encode()))
    prepared = client._answer_remote_offer(request)
    assert prepared is not None
    candidate, answer = prepared
    assert candidate.send_format.encoding == candidate.recv_format.encoding == "PCMU"
    assert candidate.video_format is None and f"m=video 0 RTP/AVP {remapped}" in answer
    assert candidate.video_payloads.send_history == removed.video_payloads.send_history
    assert candidate.video_payloads.receive_history == removed.video_payloads.receive_history
    assert client.dialog is removed


def test_incompatible_video_answer_cannot_commit_over_retired_payload_meaning():
    client, formats = video_client()
    pcmu = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20)
    original = dialog(pcmu, pcmu, video_format=replace(formats[0], payload_type=120), local_video_format=formats[0])
    removed = replace(original, video_format=None, local_video_format=None)
    client.dialog = removed
    assert answer_candidate(client, removed,
        description((0, "PCMU/8000"), video=video_section(104, "VP8")),
        description((0, "PCMU/8000"), video=video_section(120, "VP8")), video=(formats[1],)) is None
    assert client.dialog is removed


def test_video_rate_recommendation_does_not_change_the_payloads_codec_identity():
    client, formats = video_client()
    pcmu = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20)
    original = dialog(pcmu, pcmu, video_format=formats[0], local_video_format=formats[0])
    candidate = answer_candidate(client, original,
        description((0, "PCMU/8000"), video=video_section(103, "H264", fps=10)),
        description((0, "PCMU/8000"), video=video_section(103, "H264", fps=15)), video=(formats[0],))
    assert candidate is not None
    assert candidate.video_payloads is original.video_payloads
