"""JPEG preferences follow the committed media generation to the browser."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.ha


def test_image_preferences_survive_committed_ws_media_update():
    from custom_components.voip_stack import video_ws_view as view
    from custom_components.voip_stack.core import sdp
    from custom_components.voip_stack.media_session_updates import commit_video_session_update

    def fmt(width, height):
        _, attributes = sdp.parse_imageattr(f"26 recv [x=[8:8:{width}],y=[8:8:{height}]]")
        return sdp.RtpVideoFormat(payload_type=26, encoding="JPEG", image_attributes=attributes)

    tx, rx = fmt(320,240), fmt(640,480)
    session = view._VideoMediaSession(call_id="size-update",local_rtp_port=40002,
        remote_rtp_host="192.0.2.20",remote_rtp_port=41002,
        remote_rtcp_host="192.0.2.20",remote_rtcp_port=41003,remote_rtcp_mux=False,
        video_format=tx,local_video_format=rx,local_direction="sendrecv",camera_send_enabled=True)
    initial=view._video_negotiation_payload(session)
    assert initial["send"]["image_attributes"]["recv"]["sets"][0]["x"]["max"]==320
    assert initial["receive"]["image_attributes"]["recv"]["sets"][0]["x"]["max"]==640
    signature=view._video_pipeline_signature(session)
    committed=SimpleNamespace(video_format=fmt(160,120),recv_video_format=rx,
        remote_video_rtp_host="192.0.2.20",remote_video_rtp_port=41002,
        remote_video_rtcp_host="192.0.2.20",remote_video_rtcp_port=41003,
        remote_video_rtcp_mux=False,remote_video_payload_types=(26,),remote_video_connection_held=False)
    commit_video_session_update(session,committed,local_direction="sendrecv")
    update=view._video_negotiation_payload(session,message_type="media_update")
    assert update["send"]["image_attributes"]["recv"]["sets"][0]["x"]["max"]==160
    assert update["receive"]==initial["receive"]
    assert update["media_generation"]==initial["media_generation"]+1
    assert session.update_event.is_set()
    assert view._video_pipeline_signature(session)==signature


@pytest.mark.parametrize("encoding,payload", [("JPEG",26),("H264",103),("VP8",104)])
def test_legacy_or_other_codec_payload_has_no_image_preferences(encoding,payload):
    from custom_components.voip_stack import video_ws_view as view
    from custom_components.voip_stack.core import sdp
    fmt=sdp.RtpVideoFormat(encoding=encoding,payload_type=payload)
    assert "image_attributes" not in view._video_format_payload(fmt)
    if encoding!="JPEG":
        assert "image_attributes" not in view._video_format_payload(replace(fmt,image_attributes=sdp.ANY_IMAGE_ATTRIBUTES))
