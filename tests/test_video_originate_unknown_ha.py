"""A compliant independent UDP peer proves unknown-video offer policy publicly."""

import asyncio
from dataclasses import replace

import pytest

from custom_components.voip_stack.const import CONF_SIP_VIDEO, CONF_VIDEO_CAMERA_SEND
from custom_components.voip_stack.core import sdp
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
from tests.test_automation_originate_ha import lab as lab

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("policy", ["unknown_compliant", "known_audio_only", "unknown_bad_profile"])
async def test_public_unknown_video_negotiates_without_relaxing_profile_validation(lab, monkeypatch, policy):
    lab.runtime.transport_config.update({CONF_SIP_VIDEO: True, CONF_VIDEO_CAMERA_SEND: False})
    source = PhoneEndpoint(endpoint_id="browser:unknown-video", device_id="unknown-video-browser",
        name="Unknown video browser", kind=EndpointKind.BROWSER,
        capabilities=frozenset({"audio", "dtmf", "video"}))
    lab.runtime.endpoints.register(source)
    capabilities = ["audio", "dtmf"] if policy == "known_audio_only" else []
    lab.roster[1] = replace(lab.roster[1], metadata={**lab.roster[1].metadata, "capabilities": capabilities})
    from custom_components.voip_stack.roster import dump_roster_json
    lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
    lab.runtime.endpoints.update("sip:bedroom0", availability="available")
    phone = lab.phones[0]
    answers, offered = [], []

    def answer():
        request = phone.last_request
        audio = next(fmt for fmt in sdp.offered_pcm_formats(request.body)
            if fmt.encoding == "L16" and fmt.sample_rate == 16000)
        formats = sdp.offered_video_formats(request.body)
        offered.append(formats)
        video = formats[0] if formats else None
        if video is not None and policy == "unknown_bad_profile":
            assert video.profile_level_id == "42801f"
            video = replace(video, profile_level_id="42e01f")
        body = sdp.build_answer_directional("127.0.0.1", "127.0.0.1",
            phone.rtp_socket.getsockname()[1], audio, audio,
            remote_sdp=request.body, video_port=35002 if video is not None else 0,
            video_format=video, video_direction="sendonly")
        answers.append(body)
        phone.respond(request, phone.remote, 200, body.encode())
        phone.answered.set()

    monkeypatch.setattr(phone, "answer", answer)
    call_id = ""
    try:
        await lab.hass.services.async_call("voip_stack", "call", {
            "device_id": source.device_id, "destination": "bedroom0", "send_video": False,
        }, blocking=True)
        async with asyncio.timeout(3):
            while not (call_id := lab.runtime.endpoints.get(source.endpoint_id).active_call_id):
                await asyncio.sleep(0)
            while (client := lab.calls.sip_client_for(call_id)).dialog is None:
                await asyncio.sleep(0)
        dialog = client.dialog
        assert bool(offered[0]) is (policy != "known_audio_only")
        assert dialog.send_format.sample_rate == dialog.recv_format.sample_rate == 16000
        assert dialog.send_format.frame_ms == dialog.recv_format.frame_ms
        if policy == "unknown_compliant":
            assert dialog.video_format is not None
            assert dialog.video_format.profile_level_id == "42801f"
            assert dialog.local_video_direction == "recvonly"
        else:
            assert dialog.video_format is None
            if policy == "unknown_bad_profile":
                assert sdp.offered_video_formats(answers[0])[0].profile_level_id == "42e01f"
    finally:
        if call_id:
            await lab.hass.services.async_call("voip_stack", "hangup", {
                "device_id": source.device_id, "call_id": call_id,
            }, blocking=True)
    assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")
