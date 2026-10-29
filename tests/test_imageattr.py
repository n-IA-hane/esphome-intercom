"""RFC 6236 preferences through the existing directional JPEG SDP contract."""
from dataclasses import replace
from pathlib import Path
import json
import importlib
import subprocess

import pytest

from tests.test_video_rtp import _load

sdp = _load("sdp")
constraints_payload = importlib.import_module("custom_components.voip_stack.core.imageattr").constraints_payload


def video_sdp(attribute=""):
    return "v=0\r\nc=IN IP4 192.0.2.10\r\nt=0 0\r\nm=video 40002 RTP/AVP 26\r\na=rtpmap:26 JPEG/90000\r\n" + attribute


def parsed(value):
    result = sdp.parse_imageattr(value)
    assert result is not None
    return result[1]


@pytest.mark.parametrize("value", [
    "26 send * recv *", "* recv [x=320,y=240]",
    "26 recv [x=[8:8:320],y=[8:8:240],sar=1.0,q=1.0]",
    "26 recv [x=[160,320],y=[120,240],sar=[0.9-1.1],par=[1.3-1.4],q=0.75]",
    "26 recv [x=320,y=240,unknown=ignored] send [x=640,y=480]",
])
def test_valid_round_trip(value):
    attributes = parsed(value)
    again = sdp.parse_imageattr(sdp.serialize_imageattr(26, attributes).partition(":")[2])
    assert again is not None and again[1] == attributes
    assert "unknown" not in sdp.serialize_imageattr(26, attributes)


@pytest.mark.parametrize("value", [
    "26 recv [x=0,y=240]", "26 recv [x=320,y=240,q=NaN]",
    "26 recv [x=[8:0:320],y=240]", "26 recv [x=[320:8],y=240]",
    "128 send * recv *", "26 recv * recv *", "26 recv [x=320,y=240,x=160]",
    "26 recv [x=320,y=240,sar=1.2]", "26 recv [x=320,y=240,par=1.3]",
    "26 recv " + " ".join("[x=320,y=240]" for _ in range(9)),
    "26 recv [x=320,y=240,unknown=" + "x" * 2048 + "]",
    "26 recv [x=1000000,y=240]", "26 recv [x=[8,16,24],y=240",
])
def test_unsupported_attribute_is_optional(value):
    assert sdp.parse_imageattr(value) is None
    formats = sdp.offered_video_formats(video_sdp("a=imageattr:" + value + "\r\n"))
    assert len(formats) == 1 and formats[0].image_attributes is None


def test_pt_and_wildcard_matching_does_not_touch_other_codecs():
    body = video_sdp("a=imageattr:* recv [x=320,y=240]\r\na=imageattr:26 recv [x=160,y=120]\r\n")
    selected = sdp.offered_video_formats(body)[0]
    assert sdp.jpeg_image_plan(selected, 640, 480).width == 160
    body = body.replace("RTP/AVP 26", "RTP/AVP 26 103") + "a=rtpmap:103 H264/90000\r\n"
    assert next(f for f in sdp.offered_video_formats(body) if f.encoding == "H264").image_attributes is None


def test_default_offer_and_legacy_answer_are_independent():
    jpeg = next(f for f in sdp.DEFAULT_VIDEO_FORMATS if f.encoding == "JPEG")
    lines = sdp._video_media_lines_many(40002, (jpeg,), direction="sendrecv")
    assert "a=imageattr:26 send * recv *" in lines
    legacy = sdp.negotiate_video_offer_directional(video_sdp())
    assert legacy is not None and legacy.answer.image_attributes is None
    assert not any("imageattr" in x for x in sdp._video_media_lines(40002, legacy.answer, direction="sendrecv"))
    outgoing = sdp.negotiate_video_answer_directional(video_sdp(), jpeg)
    assert outgoing.send.image_attributes is None and outgoing.recv.image_attributes is None
    unsolicited = sdp.negotiate_video_answer_directional(video_sdp("a=imageattr:26 recv [x=160,y=120]\r\n"),
                                                         replace(jpeg, image_attributes=None))
    assert unsolicited.send.image_attributes is None


def test_answer_receive_limits_constrain_local_sender_not_local_receiver():
    jpeg = next(f for f in sdp.DEFAULT_VIDEO_FORMATS if f.encoding == "JPEG")
    response = video_sdp("a=imageattr:26 send [x=640,y=480] recv [x=[8:8:320],y=[8:8:240],sar=1.0,q=1.0]\r\n")
    contract = sdp.negotiate_video_answer_directional(response, jpeg)
    assert sdp.jpeg_image_plan(contract.send, 640, 480).width == 320
    assert sdp.jpeg_image_plan(contract.recv, 640, 480).width == 640
    incoming = sdp.negotiate_video_offer_directional(response)
    assert sdp.jpeg_image_plan(incoming.send, 640, 480).width == 320
    assert sdp.jpeg_image_plan(incoming.recv, 640, 480).width == 640


def test_valid_disjoint_answer_requests_one_owner_retry():
    jpeg = sdp.RtpVideoFormat(payload_type=26, encoding="JPEG", image_attributes=parsed("26 send [x=320,y=240] recv *"))
    with pytest.raises(sdp.ImageAttributeFallbackRequired) as failure:
        sdp.negotiate_video_answer_directional(video_sdp("a=imageattr:26 recv [x=160,y=120]\r\n"), jpeg)
    assert failure.value.payload_type == 26
    retry = sdp.remove_image_attributes((jpeg,))
    assert retry[0].image_attributes is None
    assert sdp.negotiate_video_answer_directional(video_sdp(), retry).send.image_attributes is None


@pytest.mark.parametrize("source,attribute,expected", [
    ((640,480),"26 recv [x=[8:8:320],y=[8:8:240]]",(320,240,320,240)),
    ((640,360),"26 recv [x=[8:8:320],y=[8:8:240]]",(320,184,320,180)),
    ((480,640),"26 recv [x=[8:8:320],y=[8:8:240]]",(184,240,180,240)),
    ((320,240),"26 recv [x=[8:8:640],y=[8:8:480]]",(320,240,320,240)),
    ((640,480),"26 recv [x=320,y=240,q=0.5] [x=160,y=120,q=1.0]",(160,120,160,120)),
    ((640,480),"26 recv [x=[160,320],y=[120,240]]",(320,240,320,240)),
])
def test_jpeg_plan_preserves_picture_and_uses_minimum_padding(source, attribute, expected):
    attributes = parsed(attribute)
    fmt = sdp.RtpVideoFormat(payload_type=26, encoding="JPEG", image_attributes=attributes)
    plan = sdp.jpeg_image_plan(fmt, *source)
    assert (plan.width,plan.height,plan.content_width,plan.content_height) == expected
    # Run the actual browser pure helper as an independent cross-language check.
    model = Path(__file__).resolve().parents[1] / "custom_components/voip_stack/frontend/voip-stack-video-model.js"
    data = json.dumps(constraints_payload(attributes))
    script = f'''import fs from 'fs';const m=await import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync({json.dumps(str(model))})).toString('base64'));
console.log(JSON.stringify(m.jpegImagePlan({source[0]},{source[1]},{data})));'''
    result = subprocess.run(["node","--input-type=module","-e",script],capture_output=True,text=True,check=True)
    js = json.loads(result.stdout)
    assert (js["width"],js["height"],js["contentWidth"],js["contentHeight"]) == expected


def test_picture_aspect_ratio_is_enforced_without_stretch():
    fmt=sdp.RtpVideoFormat(payload_type=26,encoding="JPEG",image_attributes=parsed("26 recv [x=[8:8:320],y=[8:8:240],par=[1.0-1.1]]"))
    plan=sdp.jpeg_image_plan(fmt,640,360)
    assert 1.0 <= plan.width/plan.height <= 1.1
    assert abs(plan.content_width/plan.content_height - 640/360) < .02


@pytest.mark.parametrize("direction,attribute,dimension", [
    ("sendonly", "send [x=640,y=480] recv [x=999992,y=999992]", 640),
    ("recvonly", "send [x=999992,y=999992] recv [x=320,y=240]", 320),
    ("inactive", "send [x=999992,y=999992] recv [x=999992,y=999992]", None),
])
def test_unused_direction_cannot_force_a_legacy_retry(direction, attribute, dimension):
    offer=next(f for f in sdp.DEFAULT_VIDEO_FORMATS if f.encoding == "JPEG")
    contract=sdp.negotiate_video_answer_directional(video_sdp("a="+direction+"\r\na=imageattr:26 "+attribute+"\r\n"),offer)
    assert contract is not None
    if dimension is not None:
        selected=contract.recv if direction=="sendonly" else contract.send
        assert sdp.jpeg_image_plan(selected,640,480).width==dimension


def test_split_lines_keep_both_directions_and_preferences():
    response=video_sdp("a=imageattr:26 send [x=640,y=480]\r\na=imageattr:26 recv [x=320,y=240,q=0.5]\r\na=imageattr:26 recv [x=160,y=120,q=1.0]\r\n")
    fmt=sdp.offered_video_formats(response)[0]
    assert fmt.image_attributes.send is not None and len(fmt.image_attributes.recv.sets)==2
    assert sdp.jpeg_image_plan(fmt,640,480).width==160
