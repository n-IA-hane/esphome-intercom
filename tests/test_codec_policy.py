"""A leg's codec policy filters encodings without renumbering RTP payloads."""

import pytest

from .voip_phase1_support import sdp
from custom_components.voip_stack.core.audio_format import HA_TRUNK_AUDIO_FORMATS
from custom_components.voip_stack.core.codec_policy import CodecPolicy


def test_default_policy_preserves_existing_offer_byte_for_byte():
    kwargs=dict(include_common_codecs=True,video_port=40002,video_formats=sdp.DEFAULT_VIDEO_FORMATS)
    original=sdp.build_offer_directional('192.0.2.1','192.0.2.1',40000,list(HA_TRUNK_AUDIO_FORMATS),list(HA_TRUNK_AUDIO_FORMATS),**kwargs)
    filtered=sdp.build_offer_directional('192.0.2.1','192.0.2.1',40000,list(HA_TRUNK_AUDIO_FORMATS),list(HA_TRUNK_AUDIO_FORMATS),codec_policy=CodecPolicy.from_config({}),**kwargs)
    assert filtered==original


def test_offer_has_only_selected_audio_and_video_but_retains_dtmf():
    policy=CodecPolicy(('OPUS','PCMU'),('H264',))
    offer=sdp.build_offer_directional('192.0.2.1','192.0.2.1',40000,list(HA_TRUNK_AUDIO_FORMATS),list(HA_TRUNK_AUDIO_FORMATS),
        include_common_codecs=True,video_port=40002,video_formats=sdp.DEFAULT_VIDEO_FORMATS,codec_policy=policy)
    assert {f.encoding for f in sdp.offered_pcm_formats(offer)}=={'OPUS','PCMU'}
    assert {f.encoding for f in sdp.offered_video_formats(offer)}=={'H264'}
    assert sdp.offered_dtmf_formats(offer)
    assert len({f.payload_type for f in sdp.offered_pcm_formats(offer)})==2


def test_inbound_offer_rejects_disallowed_audio_and_selects_allowed_alternative():
    offer=sdp.build_offer_directional('192.0.2.1','192.0.2.1',40000,list(HA_TRUNK_AUDIO_FORMATS),list(HA_TRUNK_AUDIO_FORMATS),include_common_codecs=True)
    selected=sdp.negotiate_directional(offer,list(HA_TRUNK_AUDIO_FORMATS),list(HA_TRUNK_AUDIO_FORMATS),codec_policy=CodecPolicy(('PCMU',),()))
    assert selected.send.encoding==selected.recv.encoding=='PCMU'
    assert sdp.negotiate_directional(offer,list(HA_TRUNK_AUDIO_FORMATS),list(HA_TRUNK_AUDIO_FORMATS),codec_policy=CodecPolicy(('L24',),())) is None


def test_empty_audio_rejected_empty_video_explicitly_supported():
    with pytest.raises(ValueError):
        CodecPolicy.from_config({'trunk_audio_codecs':[]})
    with pytest.raises(ValueError):
        CodecPolicy.from_config({'trunk_audio_codecs':['G729']})
    policy=CodecPolicy.from_config({'trunk_video_codecs':[]})
    offer=sdp.build_offer_directional('192.0.2.1','192.0.2.1',40000,list(HA_TRUNK_AUDIO_FORMATS),list(HA_TRUNK_AUDIO_FORMATS),
        include_common_codecs=True,video_port=40002,video_formats=sdp.DEFAULT_VIDEO_FORMATS,codec_policy=policy)
    assert 'm=video' not in offer


@pytest.mark.asyncio
async def test_inbound_dialog_keeps_its_codec_policy_for_reinvite():
    from .voip_phase1_support import sip,sip_listener
    policy=CodecPolicy(('PCMU',),())
    endpoint=sip_listener.SipUdpEndpoint(local_ip='127.0.0.1',local_sip_port=5060,local_rtp_port=40000,
        supported_formats=list(HA_TRUNK_AUDIO_FORMATS),on_invite=None,enable_video=True,
        codec_policy_resolver=lambda _initial:policy)
    body=sdp.build_offer_directional('127.0.0.1','127.0.0.1',41000,list(HA_TRUNK_AUDIO_FORMATS),list(HA_TRUNK_AUDIO_FORMATS),
        include_common_codecs=True,video_port=41002,video_formats=sdp.DEFAULT_VIDEO_FORMATS).encode()
    raw=sip.build_request('INVITE','sip:HA@127.0.0.1:5060',[
        ('Via','SIP/2.0/UDP 127.0.0.1:5099;branch=z9hG4bK-codecs'),
        ('From','<sip:caller@127.0.0.1:5099>;tag=caller'),('To','<sip:HA@127.0.0.1:5060>'),
        ('Contact','<sip:caller@127.0.0.1:5099>'),('Call-ID','codec-policy'),('CSeq','1 INVITE'),
        ('Content-Type','application/sdp')],body)
    request=sip.parse_message(raw)
    initial=endpoint._parse_invite(request,('127.0.0.1',5099))
    assert initial.send_format.encoding==initial.recv_format.encoding=='PCMU'
    assert initial.video_format is None
    pending=sip_listener._PendingInvite(request,('127.0.0.1',5099),'local','UDP',
        codec_policy=initial.codec_policy,identity=initial,media_contract=initial)
    dialog=endpoint._activate_dialog('codec-policy',pending,request,('127.0.0.1',5099))
    assert dialog.codec_policy==policy
    policy=CodecPolicy(('OPUS',),('H264',))
    updated=endpoint._parse_invite(request,('127.0.0.1',5099))
    assert updated.send_format.encoding=='PCMU' and updated.video_format is None
