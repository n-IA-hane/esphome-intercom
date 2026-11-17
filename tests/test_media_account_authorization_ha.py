"""Account access applies to the real HTTP media views and their live leases."""

import asyncio
from types import SimpleNamespace

from aiohttp import ClientError, WSMsgType, WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer
import pytest
from homeassistant.auth import EVENT_USER_REMOVED, EVENT_USER_UPDATED
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import EVENT_ENTITY_REGISTRY_UPDATED

from .test_managed_phones_ha import custom_integration_path as custom_integration_path
from .test_phone_state_ha import phones as phones

pytestmark = pytest.mark.ha


@pytest.fixture
async def media_account(hass, phones, socket_enabled):
    from custom_components.voip_stack.audio_ws_view import VoipAudioWebSocketView
    from custom_components.voip_stack.entity_identity import shared_entity_ids
    from custom_components.voip_stack.local_softphone_bridge import LocalSoftphoneBridge
    from custom_components.voip_stack.video_ws_view import VoipVideoWebSocketView

    for identity in ("kitchen", "office"):
        phones.endpoints.update(identity, entity_ids={f"sensor.{identity}_access"},
            capabilities={"audio", "video"})
    er.async_get(hass).async_get_or_create("event", "voip_stack", "voip_stack_call_event")
    allowed = {shared_entity_ids(hass)["call_event"], "sensor.kitchen_access"}
    revoked_permissions, handlers = set(), []
    user = SimpleNamespace(id="media-user", is_active=True, is_admin=True,
        permissions=SimpleNamespace(check_entity=lambda entity, policy: entity in allowed and (entity, policy) not in revoked_permissions))
    bridge = phones.runtime.local_bridge = LocalSoftphoneBridge(phones.endpoints)
    bridge.start_call("office", "kitchen", call_id="account-call", request_video=True,
        enable_caller_video_send=True, caller_owner_id="office-document-1234")
    bridge.answer("account-call", "kitchen", "kitchen-document-1234", enable_video_send=True)
    peer = bridge.acquire_media("account-call", "office", "office-document-1234")
    call = phones.calls.upsert("account-call", state="in_call", owner="local_bridge",
        source_endpoint_id="office", dest_endpoint_id="kitchen", local_bridge=True)
    phones.calls.bind_controller(call.call_id, user_id=user.id, endpoint_id="kitchen")

    @web.middleware
    async def authenticated(request, handler):
        # HA supplies this identity at its HTTP boundary. All account/call/media
        # authorization and PCM handling below run through production code.
        handlers.append(asyncio.current_task())
        request["hass_user"] = user
        try:
            return await handler(request)
        except Unauthorized as error:
            raise web.HTTPForbidden() from error

    app = web.Application(middlewares=[authenticated])
    app["hass"] = hass
    app.router.add_get("/audio", VoipAudioWebSocketView().get)
    app.router.add_get("/video", VoipVideoWebSocketView().get)
    client = TestClient(TestServer(app))
    await client.start_server()
    query = "?endpoint_id=kitchen&call_id=account-call&client_id=kitchen-document-1234"
    try:
        yield SimpleNamespace(hass=hass, phones=phones, bridge=bridge, call=call, peer=peer,
            user=user, allowed=allowed, revoked_permissions=revoked_permissions, handlers=handlers,
            client=client, query=query)
    finally:
        await client.close()
        bridge.hangup("account-call", "office")


async def duplex(lab, socket):
    from custom_components.voip_stack.audio_ws import decode_audio_frame, encode_audio_frame
    from custom_components.voip_stack.core.audio_format import HA_SIP_PCM_FORMATS

    pcm = b"\x12\x34" * (int(HA_SIP_PCM_FORMATS[0].nominal_frame_bytes) // 2)
    lab.bridge.send_audio("account-call", "office", lab.peer.token, pcm)
    incoming = await socket.receive(timeout=1)
    assert incoming.type == WSMsgType.BINARY and decode_audio_frame(incoming.data) == pcm
    await socket.send_bytes(encode_audio_frame(pcm))
    assert await asyncio.wait_for(lab.bridge.receive_audio(
        "account-call", "office", lab.peer.token), 1) == pcm


@pytest.mark.parametrize("policy", ["read", "control"])
async def test_new_media_attachment_requires_current_account_control(media_account, policy):
    lab = media_account
    lab.revoked_permissions.add(("sensor.kitchen_access", policy))
    with pytest.raises(WSServerHandshakeError) as error:
        await lab.client.ws_connect("/audio" + lab.query)
    assert error.value.status == 403
    assert not lab.phones.runtime.media.owners_for("audio")


async def test_account_remapped_while_controller_lock_is_held_cannot_be_claimed(media_account, monkeypatch):
    from custom_components.voip_stack import authorization

    lab = media_account
    lab.call.metadata["controller_user_ids"] = {}
    lock = lab.phones.calls.media_controller_lock = asyncio.Lock()
    await lock.acquire()
    checked = asyncio.Event()
    native_permission = authorization.require_user_endpoint_permission

    def check(*args):
        result = native_permission(*args)
        checked.set()
        return result

    monkeypatch.setattr(authorization, "require_user_endpoint_permission", check)
    connecting = asyncio.ensure_future(lab.client.ws_connect("/audio" + lab.query))
    try:
        await asyncio.wait_for(checked.wait(), 1)
        lab.phones.endpoints.update("kitchen", entity_ids={"sensor.different_private_account"})
        lock.release()
        with pytest.raises(WSServerHandshakeError) as error:
            await asyncio.wait_for(connecting, 1)
        assert error.value.status == 403
        assert not lab.call.metadata["controller_user_ids"]
        assert not lab.phones.runtime.media.owners_for("audio")
    finally:
        if lock.locked():
            lock.release()
        if not connecting.done():
            connecting.cancel()
        await asyncio.gather(connecting, return_exceptions=True)


@pytest.mark.parametrize("phase", ["claim", "upgrade"])
async def test_revocation_during_media_setup_cannot_start_the_PCM_loop(media_account, monkeypatch, phase):
    from custom_components.voip_stack import audio_ws_view, media_ws_session

    lab = media_account
    entered, proceed = asyncio.Event(), asyncio.Event()
    started = []
    native_claim = media_ws_session.async_claim_call_media_owner
    native_prepare = web.WebSocketResponse.prepare
    native_audio = audio_ws_view._run_local_audio_session

    async def claim(*args, **kwargs):
        if phase == "claim":
            entered.set()
            await proceed.wait()
        return await native_claim(*args, **kwargs)

    async def prepare(socket, request):
        result = await native_prepare(socket, request)
        if phase == "upgrade":
            entered.set()
            await proceed.wait()
        return result

    async def audio(*args, **kwargs):
        started.append(True)
        await native_audio(*args, **kwargs)

    monkeypatch.setattr(media_ws_session, "async_claim_call_media_owner", claim)
    monkeypatch.setattr(web.WebSocketResponse, "prepare", prepare)
    monkeypatch.setattr(audio_ws_view, "_run_local_audio_session", audio)
    connecting = asyncio.ensure_future(lab.client.ws_connect("/audio" + lab.query))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        owner = lab.phones.runtime.media.owners_for("audio").get("kitchen|account-call")
        lab.allowed.remove("sensor.kitchen_access")
        lab.hass.bus.async_fire(EVENT_USER_UPDATED, {"user_id": lab.user.id})
        await lab.hass.async_block_till_done()
        proceed.set()
        try:
            socket = await asyncio.wait_for(connecting, 1)
            assert (await socket.receive(timeout=1)).type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}
        except ClientError:
            pass  # Revocation may close the transport before HTTP upgrade.
        if owner is not None:
            await asyncio.wait_for(owner.released.wait(), 1)
        assert not started
        assert not lab.phones.runtime.media.owners_for("audio")
        assert lab.bridge.validate_media_lease("account-call", "office", lab.peer.token)
    finally:
        proceed.set()
        if not connecting.done():
            connecting.cancel()
        await asyncio.gather(connecting, return_exceptions=True)
    assert not lab.phones.runtime.media.owners_for("audio")


@pytest.mark.parametrize("policy", ["read", "control"])
@pytest.mark.parametrize("notice", ["user", "entity", "removed"])
async def test_revocation_closes_audio_video_without_ending_another_owner(media_account, notice, policy):
    from custom_components.voip_stack.phone_state import phone_state_observer

    lab = media_account
    audio = await lab.client.ws_connect("/audio" + lab.query)
    assert (await audio.receive_json())["state"] == "in_call"
    video = await lab.client.ws_connect("/video" + lab.query)
    assert (await video.receive_json())["state"] == "in_call"
    await duplex(lab, audio)
    owners = [lab.phones.runtime.media.owners_for(channel)["kitchen|account-call"]
              for channel in ("audio", "video")]
    lab.revoked_permissions.add(("sensor.kitchen_access", policy))
    kind, data = (EVENT_ENTITY_REGISTRY_UPDATED, {"action": "update", "entity_id": "sensor.kitchen_access"}) if notice == "entity" else (
        EVENT_USER_REMOVED if notice == "removed" else EVENT_USER_UPDATED, {"user_id": lab.user.id})
    lab.hass.bus.async_fire(kind, data)
    assert (await audio.receive(timeout=1)).type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}
    assert (await video.receive(timeout=1)).type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}
    await asyncio.wait_for(asyncio.gather(*(owner.released.wait() for owner in owners)), 1)
    await asyncio.wait_for(asyncio.gather(*set(lab.handlers), return_exceptions=True), 1)
    await lab.hass.async_block_till_done()
    assert not lab.phones.runtime.media.owners_for("audio")
    assert not lab.phones.runtime.media.owners_for("video")
    assert lab.bridge.validate_media_lease("account-call", "office", lab.peer.token)
    assert lab.phones.calls.get_session("account-call") is lab.call
    assert lab.bridge.get_call("account-call") is not None
    assert not phone_state_observer(lab.hass).projection_listeners
    with pytest.raises(WSServerHandshakeError) as error:
        await lab.client.ws_connect("/audio" + lab.query)
    assert error.value.status == 403
