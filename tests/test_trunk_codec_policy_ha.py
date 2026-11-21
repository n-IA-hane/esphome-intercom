"""Configured trunk codecs reach actual SIP offers and encoded RTP."""

from dataclasses import replace
import struct

import pytest
from homeassistant.core import Context
from custom_components.voip_stack.core import sdp,rtp,g711
from custom_components.voip_stack.runtime_data import sip_trunk
from .test_trunk_calls_ha import trunks as trunks,lab as lab
from .test_automation_originate_ha import dial,tts_fixture

pytestmark=pytest.mark.ha


async def test_two_trunks_keep_independent_offers_and_real_pcmu_announcement(trunks,monkeypatch):
    runtime=trunks
    selected=sip_trunk(runtime.hass,'home')
    selected.config=replace(selected.config,audio_codecs=('PCMU',),video_codecs=())
    peer=runtime.phones[0]
    def answer_pcmu():
        request=peer.last_request
        assert request.header('From') and request.header('To')
        assert not request.header('X-Voip-Stack-Caller-Name')
        assert not request.header('X-Voip-Stack-Dest-Route')
        formats=sdp.offered_pcm_formats(request.body)
        assert {item.encoding for item in formats}=={'PCMU'}
        assert not sdp.offered_video_formats(request.body)
        selected_format=formats[0]
        body=sdp.build_answer_directional('127.0.0.1','127.0.0.1',peer.rtp_socket.getsockname()[1],
            selected_format,selected_format,remote_sdp=request.body).encode()
        peer.respond(request,peer.remote,200,body)
        peer.answered.set()
    monkeypatch.setattr(peer,'answer',answer_pcmu)
    tts_fixture(monkeypatch,runtime)
    context=Context()
    await dial(runtime,context,destination='External',trunk_id='home')
    await runtime.hass.services.async_call('voip_stack','tts_say',{'tts_entity_id':'tts.test','message':'Wake up'},blocking=True,context=context)
    await runtime.hass.services.async_call('voip_stack','hangup',{},blocking=True,context=context)
    voiced=[]
    while not peer.audio.empty():
        raw,_=peer.audio.get_nowait()
        packet=rtp.parse_packet(raw)
        assert packet.payload_type==0
        pcm=g711.ulaw_to_s16le(packet.payload)
        samples=struct.unpack(f'<{len(pcm)//2}h',pcm)
        voiced.extend(sample for sample in samples if abs(sample)>20)
    assert len(voiced)>=400
    # The anti-aliasing resampler has a short startup transient; check the
    # steady signal after G.711 quantization and bound the complete waveform.
    assert sum(abs(sample-384)<32 for sample in voiced)>450
    assert max(abs(sample) for sample in voiced)<448
    assert not runtime.calls.sessions and not runtime.runtime.rtp_port_pool['used']
    context=Context()
    await dial(runtime,context,destination='External',trunk_id='work')
    assert 'L16' in {item.encoding for item in sdp.offered_pcm_formats(runtime.phones[1].invites[0].body)}
    await runtime.hass.services.async_call('voip_stack','hangup',{},blocking=True,context=context)
    assert not runtime.calls.sessions


async def test_no_usable_selected_codec_releases_call_and_allows_next_attempt(trunks, monkeypatch):
    from homeassistant.exceptions import ServiceValidationError
    runtime=trunks
    selected=sip_trunk(runtime.hass,'home')
    from custom_components.voip_stack.core.media import offers
    available = offers.codec_available
    # Configured L24 is supported. Preserve this regression's unavailable
    # transmitter contract at the directional backend availability boundary.
    monkeypatch.setattr(offers, "codec_available", lambda name, *, receive=False:
        False if name == "L24" and not receive else available(name, receive=receive))
    selected.config=replace(selected.config,audio_codecs=('L24',),audio_configured=True)
    with pytest.raises(ServiceValidationError):
        await dial(runtime,Context(),destination='External',trunk_id='home')
    assert not runtime.phones[0].invites
    assert not runtime.calls.sessions and not runtime.runtime.rtp_port_pool['used']
    assert selected.ready and not selected._outbound_dialogs
    selected.config=replace(selected.config,audio_codecs=('L16',),audio_configured=True)
    context=Context()
    await dial(runtime,context,destination='External',trunk_id='home')
    await runtime.hass.services.async_call('voip_stack','hangup',{},blocking=True,context=context)
    assert not runtime.calls.sessions and selected.ready
