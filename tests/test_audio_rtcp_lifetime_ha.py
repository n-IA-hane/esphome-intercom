"""A browser SIP leg reports RTCP before any card connects and retains its owner."""
import asyncio
import socket
import struct
from unittest.mock import AsyncMock

from aiohttp import web
import pytest
from homeassistant.core import Context

from custom_components.voip_stack import audio_ws_view, softphone_originate
from custom_components.voip_stack.core import sip
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
from custom_components.voip_stack.websocket_api import _ha_softphone_state
from .test_automation_originate_ha import lab as lab, assert_call_resources_released

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("provisional", [True, False], ids=["early-noWS", "answered-noWS"])
async def test_rtcp_precedes_browser_connection_and_reuses_same_call_resource(
        lab, monkeypatch, aiohttp_client, hass_admin_user, provisional):
    from homeassistant.helpers import entity_registry as er
    entity = er.async_get(lab.hass).async_get_or_create("event", "voip_stack", "rtcp-no-ws-event")
    endpoint = PhoneEndpoint(entity_ids={entity.entity_id}, endpoint_id="rtcp-no-ws", name="RTCP no WS", username="rtcp-no-ws",
        device_id="rtcp-no-ws-device", kind="browser", availability="available", capabilities={"audio", "dtmf"})
    lab.runtime.endpoints.register(endpoint)
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    phone = lab.phones[0]
    phone.auto_answer = False
    control = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    control.bind(("127.0.0.1", 0))
    control.setblocking(False)
    call_id = ""
    ws = None

    @web.middleware
    async def authenticated(request, handler):
        request["hass_user"] = hass_admin_user
        return await handler(request)
    app = web.Application(middlewares=[authenticated])
    app["hass"] = lab.hass
    app.router.add_get("/api/voip_stack/ws", audio_ws_view.VoipAudioWebSocketView().get)
    http = await aiohttp_client(app)

    def answer(status):
        request = phone.last_request
        headers = [(name, request.header(name)) for name in ("Via", "From", "Call-ID", "CSeq")]
        headers += [("To", request.header("To") + ";tag=bedroom"), ("Content-Type", "application/sdp"),
            ("Contact", f"<sip:bedroom@127.0.0.1:{phone.transport.get_extra_info('sockname')[1]}>")]
        body = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=RTCP no card\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
            f"m=audio {phone.rtp_socket.getsockname()[1]} RTP/AVP 8\r\n"
            f"a=rtcp:{control.getsockname()[1]} IN IP4 127.0.0.1\r\na=rtpmap:8 PCMA/8000\r\n"
            "a=ptime:20\r\na=sendrecv\r\n").encode()
        phone.transport.sendto(sip.build_response(status, "Session Progress" if status == 183 else "OK", headers, body), phone.remote)

    async def wait_for(predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(.005)

    try:
        await lab.hass.services.async_call("voip_stack", "call", {"device_id": endpoint.device_id,
            "destination": "bedroom0", "media_client_id": "rtcp-no-ws-browser-owner"}, blocking=True,
            context=Context(user_id=hass_admin_user.id))
        call_id = lab.runtime.endpoints.require(endpoint.endpoint_id).active_call_id
        client = lab.calls.sip_client_for(call_id)
        await asyncio.wait_for(phone.invited.wait(), 2)
        answer(183 if provisional else 200)
        await wait_for(lambda: _ha_softphone_state(lab.hass, endpoint.endpoint_id).get("early_media")
            if provisional else _ha_softphone_state(lab.hass, endpoint.endpoint_id).get("state") == "in_call")
        session = lab.calls.get_session(call_id)
        await wait_for(lambda: any(item.name == f"audio_rtcp:{client.local_rtp_port}" for item in session.resources))
        owner = next(item.value for item in session.resources if item.name == f"audio_rtcp:{client.local_rtp_port}")
        timer = owner.task
        rtp_source = client.audio_rtp_source
        assert owner.transport is not None and timer is not None
        assert client.audio_rtcp_socket is None
        assert (call_id, endpoint.endpoint_id) not in lab.runtime.media.sessions_for("audio")
        report, address = await asyncio.wait_for(lab.hass.loop.sock_recvfrom(control, 4096), 4)
        assert struct.unpack_from("!BBH", report) == (0x80, 201, 1)
        assert struct.unpack_from("!I", report, 4)[0] == rtp_source.ssrc
        assert address[1] == client.local_rtp_port + 1
        assert owner.sent_packets == 0
        ws = await http.ws_connect("/api/voip_stack/ws?endpoint_id=" + endpoint.endpoint_id
            + "&call_id=" + call_id + "&client_id=rtcp-no-ws-browser-owner")
        await ws.send_json({"type": "audio_ready"})
        await wait_for(lambda: (call_id, endpoint.endpoint_id) in lab.runtime.media.sessions_for("audio"))
        media = lab.runtime.media.sessions_for("audio")[(call_id, endpoint.endpoint_id)]
        await wait_for(lambda: media.audio_rtcp is not None)
        assert media.audio_rtcp is owner and owner.task is timer
        assert media.rtp_source is rtp_source
    finally:
        if call_id:
            await lab.hass.services.async_call("voip_stack", "hangup", {"device_id": endpoint.device_id,
                "call_id": call_id}, blocking=True, context=Context(user_id=hass_admin_user.id))
        if ws is not None:
            await ws.close()
        await http.close()
        control.close()
        await lab.hass.async_block_till_done()
    assert owner.closed and owner.transport is owner.task is None
    assert_call_resources_released(lab)
