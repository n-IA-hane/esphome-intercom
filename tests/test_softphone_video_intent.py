"""Transactional camera-intent tests against the HA runtime boundary."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


pytestmark = pytest.mark.ha


class _Reservation:
    ports = (42000, 42002)

    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


class _Socket:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Registry:
    def __init__(self, client) -> None:
        self.client = client
        self.session = SimpleNamespace(generation=7)

    def get_session(self, _call_id):
        return self.session

    def sip_client_for(self, _call_id):
        return self.client

    def is_generation_current(self, _call_id, generation):
        return generation == 7

    def resource_for(self, _call_id, _kind):
        return None


@pytest.fixture
def intent(monkeypatch):
    from custom_components.voip_stack import local_softphone_runtime
    from custom_components.voip_stack import softphone_video_intent as module

    store = {
        "call_id": "call-1",
        "state": "in_call",
        "send_video": True,
    }
    monkeypatch.setattr(module, "_ha_softphone_store", lambda _hass, _endpoint: store)
    monkeypatch.setattr(module, "_fire_call_event", Mock())
    monkeypatch.setattr(
        module,
        "transport_config",
        lambda _hass: {
            module.CONF_SIP_VIDEO: True,
            module.CONF_VIDEO_CAMERA_SEND: True,
        },
    )
    monkeypatch.setattr(local_softphone_runtime, "local_softphone_bridge", lambda _hass: None)
    monkeypatch.setattr(module, "require_runtime_data", lambda _hass: SimpleNamespace(
        media=SimpleNamespace(sessions_for=lambda _kind: {}),
    ))
    return module, store


async def test_enabling_video_commits_reserved_media_only_after_reinvite(
    intent, monkeypatch
) -> None:
    module, store = intent
    video = module.sdp.RtpVideoFormat(payload_type=104, encoding="VP8")
    previous = SimpleNamespace(
        video_format=None,
        local_video_rtp_port=0,
        local_video_direction="inactive",
    )
    candidate = SimpleNamespace(
        video_format=video, send_video_format=video, recv_video_format=video,
        local_video_rtp_port=42002,
        local_video_direction="sendrecv",
    )
    client = SimpleNamespace(
        dialog=previous,
        video_formats=(),
        last_sip_status_code=200,
        async_prepare_video_reinvite=AsyncMock(return_value=candidate),
        commit_prepared_reinvite=Mock(return_value=True),
        media_reservation=None,
        video_rtp_socket=None,
        video_rtcp_socket=None,
    )
    registry = _Registry(client)
    reservation = _Reservation()
    rtp_socket = _Socket()
    rtcp_socket = _Socket()
    monkeypatch.setattr(module, "call_registry", lambda _hass: registry)
    monkeypatch.setattr(
        module,
        "reserve_sip_video_media",
        lambda _hass: (reservation, rtp_socket, rtcp_socket),
    )

    assert await module.async_apply_send_video_intent(object(), "phone", True)
    client.commit_prepared_reinvite.assert_called_once_with(previous, candidate)
    assert client.media_reservation is reservation
    assert client.video_rtp_socket is rtp_socket
    assert client.video_rtcp_socket is rtcp_socket
    assert store["video_direction"] == "sendrecv"
    assert not reservation.released


@pytest.mark.parametrize("outcome", ["accepted", "rejected", "superseded"])
async def test_enabling_video_keeps_audio_reservation_owned_until_client_cleanup(
    intent, monkeypatch, outcome,
) -> None:
    from custom_components.voip_stack import media_ports
    from custom_components.voip_stack.sip_client import SipCallClient

    module, _store = intent
    video = module.sdp.RtpVideoFormat(payload_type=104, encoding="VP8")
    hass = object()
    runtime = SimpleNamespace(rtp_port_pool={"used": {40022, 40024, 40026, 40028}})
    monkeypatch.setattr(media_ports, "require_runtime_data", lambda _hass: runtime)
    original = media_ports.RtpPortReservation(hass, (40022, 40024))
    staged = media_ports.RtpPortReservation(hass, (40026, 40028))
    audio_socket, previous_rtp, previous_rtcp = _Socket(), _Socket(), _Socket()
    new_rtp, new_rtcp = _Socket(), _Socket()
    previous = SimpleNamespace(video_format=None, local_video_rtp_port=0, local_video_direction="inactive")
    candidate = SimpleNamespace(video_format=video, send_video_format=video, recv_video_format=video,
                                local_video_rtp_port=40028, local_video_direction="sendrecv")
    client = SipCallClient(
        local_ip="127.0.0.1", local_name="browser", local_sip_port=5060,
        local_rtp_port=40022, local_video_rtp_port=40024,
        media_reservation=original, video_rtp_socket=previous_rtp,
        video_rtcp_socket=previous_rtcp,
    )
    client.dialog = previous
    client.audio_rtp_socket = audio_socket
    client.async_prepare_video_reinvite = AsyncMock(return_value=None if outcome == "rejected" else candidate)
    client.commit_prepared_reinvite = Mock(return_value=True)
    registry = _Registry(client)
    if outcome == "superseded":
        registry.is_generation_current = lambda *_args: False
    monkeypatch.setattr(module, "call_registry", lambda _hass: registry)
    monkeypatch.setattr(module, "reserve_sip_video_media", lambda _hass: (staged, new_rtp, new_rtcp))

    if outcome != "accepted":
        assert not await module.async_apply_send_video_intent(hass, "phone", True)
        assert client.media_reservation is original
        assert runtime.rtp_port_pool["used"] == {40022, 40024}
        assert not audio_socket.closed and not previous_rtp.closed and not previous_rtcp.closed
        assert new_rtp.closed and new_rtcp.closed
        runtime.rtp_port_pool["used"].update({40026, 40028})
        await client.close()
        assert audio_socket.closed and previous_rtp.closed and previous_rtcp.closed
        assert runtime.rtp_port_pool["used"] == {40026, 40028}
        return

    assert await module.async_apply_send_video_intent(hass, "phone", True)
    assert runtime.rtp_port_pool["used"] == {40022, 40028}
    assert client.media_reservation.ports == (40022, 40028)
    assert previous_rtp.closed and previous_rtcp.closed
    assert not audio_socket.closed and not new_rtp.closed and not new_rtcp.closed
    # Another call can immediately reuse the discarded video and staging ports.
    runtime.rtp_port_pool["used"].update({40024, 40026})
    original.release()
    staged.release()
    assert runtime.rtp_port_pool["used"] == {40022, 40024, 40026, 40028}

    await client.close()
    assert audio_socket.closed and new_rtp.closed and new_rtcp.closed
    assert runtime.rtp_port_pool["used"] == {40024, 40026}


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("outgoing", [False, True])
@pytest.mark.parametrize("committed", [False, True])
async def test_camera_intent_wakes_existing_media_owner(intent, monkeypatch, enabled, outgoing, committed):
    module, store = intent
    store["send_video"] = enabled
    video = module.sdp.RtpVideoFormat(payload_type=26, encoding="JPEG")
    direction = "sendrecv" if enabled else "recvonly"
    candidate = SimpleNamespace(
        video_format=video, send_video_format=video, recv_video_format=video,
        local_video_rtp_port=42002, local_video_direction=direction,
        remote_video_rtp_host="192.0.2.5", remote_video_rtp_port=44002,
        remote_video_rtcp_host="192.0.2.5", remote_video_rtcp_port=44003,
        remote_video_rtcp_mux=False, remote_video_payload_types=(26,),
        remote_video_connection_held=False,
        remote_sdp="v=0\r\nc=IN IP4 192.0.2.5\r\nm=video 44002 RTP/AVP 26\r\na="
        + ("sendrecv" if enabled else "sendonly") + "\r\n",
    )
    media = {"invite": candidate, "local_video_rtp_port": 42002}
    session = SimpleNamespace(
        media_generation=3, update_event=asyncio.Event(), camera_send_enabled=not enabled,
    )
    other_phone = SimpleNamespace(media_generation=9, update_event=asyncio.Event(), camera_send_enabled=not enabled)
    client = SimpleNamespace(
        dialog=candidate, video_formats=(video,), last_sip_status_code=200,
        async_prepare_video_reinvite=AsyncMock(return_value=candidate),
        commit_prepared_reinvite=Mock(return_value=committed),
        abort_prepared_reinvite=Mock(),
    ) if outgoing else None
    registry = _Registry(client)
    registry.resource_for = lambda *_args: media
    prepared = SimpleNamespace(candidate=candidate, commit=Mock(return_value=committed))
    manager = SimpleNamespace(
        async_prepare_video_reinvite=AsyncMock(return_value=prepared),
        video_reinvite_result=lambda _id: (200, "OK"),
    )
    monkeypatch.setattr(module, "call_registry", lambda _hass: registry)
    monkeypatch.setattr(module, "sip_endpoint_manager", lambda _hass: manager)
    monkeypatch.setattr(module, "require_runtime_data", lambda _hass: SimpleNamespace(
        media=SimpleNamespace(sessions_for=lambda _kind: {
            ("call-1", "phone"): session, ("call-1", "other-phone"): other_phone,
        }),
    ))
    result = await module.async_apply_send_video_intent(object(), "phone", enabled)
    if not committed:
        assert not result
        assert session.camera_send_enabled is not enabled
        assert session.media_generation == 3
        assert not session.update_event.is_set()
        assert other_phone.media_generation == 9 and not other_phone.update_event.is_set()
        return
    assert result
    assert session.camera_send_enabled is enabled
    assert session.local_direction == direction
    assert session.remote_rtp_port == 44002
    assert session.media_generation == 4
    assert session.update_event.is_set()
    assert other_phone.media_generation == 9 and not other_phone.update_event.is_set()


async def test_rejected_video_addition_releases_staged_media_and_keeps_audio(
    intent, monkeypatch
) -> None:
    module, store = intent
    previous = SimpleNamespace(
        video_format=None,
        local_video_rtp_port=0,
        local_video_direction="inactive",
    )
    client = SimpleNamespace(
        dialog=previous,
        video_formats=(),
        last_sip_status_code=488,
        async_prepare_video_reinvite=AsyncMock(return_value=None),
        commit_prepared_reinvite=Mock(),
    )
    reservation = _Reservation()
    rtp_socket = _Socket()
    rtcp_socket = _Socket()
    monkeypatch.setattr(module, "call_registry", lambda _hass: _Registry(client))
    monkeypatch.setattr(
        module,
        "reserve_sip_video_media",
        lambda _hass: (reservation, rtp_socket, rtcp_socket),
    )

    assert not await module.async_apply_send_video_intent(object(), "phone", True)
    client.commit_prepared_reinvite.assert_not_called()
    assert reservation.released
    assert rtp_socket.closed and rtcp_socket.closed
    assert store["video_failure_reason"] == "sip_488"


async def test_successful_sdp_answer_rejecting_only_video_keeps_audio_dialog(
    intent, monkeypatch
) -> None:
    module, store = intent
    previous = SimpleNamespace(
        video_format=None,
        local_video_rtp_port=0,
        local_video_direction="inactive",
    )
    candidate = SimpleNamespace(
        video_format=None,
        send_video_format=None,
        recv_video_format=None,
        local_video_rtp_port=0,
        local_video_direction="inactive",
    )
    client = SimpleNamespace(
        dialog=previous,
        video_formats=(),
        last_sip_status_code=200,
        async_prepare_video_reinvite=AsyncMock(return_value=candidate),
        commit_prepared_reinvite=Mock(return_value=True),
        media_reservation=None,
        video_rtp_socket=None,
        video_rtcp_socket=None,
    )
    reservation = _Reservation()
    rtp_socket = _Socket()
    rtcp_socket = _Socket()
    monkeypatch.setattr(module, "call_registry", lambda _hass: _Registry(client))
    monkeypatch.setattr(
        module,
        "reserve_sip_video_media",
        lambda _hass: (reservation, rtp_socket, rtcp_socket),
    )

    assert not await module.async_apply_send_video_intent(object(), "phone", True)
    client.commit_prepared_reinvite.assert_called_once_with(previous, candidate)
    assert reservation.released
    assert rtp_socket.closed and rtcp_socket.closed
    assert client.media_reservation is None
    assert store["video_status"] == "rejected"
    assert store["video_failure_reason"] == "video_rejected"


def _dialog(video=None, *, receive=None, direction="inactive"):
    from custom_components.voip_stack.sip_client import SipDialog
    from custom_components.voip_stack.core import sdp

    return SipDialog(
        target="peer", remote_host="192.0.2.5", remote_sip_port=5060,
        remote_rtp_host="192.0.2.5", remote_rtp_port=44000,
        local_rtp_port=42000, call_id="call-1",
        local_uri="sip:phone@example.test", remote_uri="sip:peer@example.test",
        send_format=sdp.RtpPcmFormat(0, "PCMU", 8000, 1),
        recv_format=sdp.RtpPcmFormat(0, "PCMU", 8000, 1),
        video_format=video, local_video_format=receive,
        local_video_rtp_port=42002 if video is not None else 0,
        local_video_direction=direction,
    )


def _invite(dialog):
    from custom_components.voip_stack.sip_listener import SipInvite
    from custom_components.voip_stack.core.sip import parse_sip_uri

    remote_direction = {
        "sendrecv": "sendrecv", "recvonly": "sendonly",
        "sendonly": "recvonly", "inactive": "inactive",
    }[dialog.local_video_direction]
    video_sdp = (
        f"m=video 44002 RTP/AVP {dialog.video_format.payload_type}\r\n"
        f"a={remote_direction}\r\n"
        if dialog.video_format is not None else ""
    )
    return SipInvite(
        source_host=dialog.remote_host, source_port=5060,
        request_uri=parse_sip_uri(dialog.local_uri),
        caller_uri=parse_sip_uri(dialog.remote_uri), target="phone", caller="peer",
        call_id=dialog.call_id, cseq="1 INVITE",
        remote_sdp=("v=0\r\nc=IN IP4 192.0.2.5\r\n" + video_sdp).encode(),
        send_format=dialog.send_format, recv_format=dialog.recv_format,
        remote_rtp_host=dialog.remote_rtp_host, remote_rtp_port=dialog.remote_rtp_port,
        video_format=dialog.video_format, local_video_format=dialog.recv_video_format,
    )


def _metadata_context(intent, monkeypatch, *, outgoing, previous):
    """Use real negotiated contracts and the existing commit entry points."""
    from custom_components.voip_stack.sip_client import SipCallClient
    from custom_components.voip_stack.sip_endpoint import PreparedEndpointVideoReinvite

    module, store = intent
    reservation = _Reservation()
    monkeypatch.setattr(module, "reserve_sip_video_media", lambda _hass: (
        reservation, _Socket(), _Socket(),
    ))
    client = SipCallClient(
        local_ip="127.0.0.1", local_name="phone", local_sip_port=5060,
        local_rtp_port=42000, local_video_rtp_port=previous.local_video_rtp_port,
    ) if outgoing else None
    if client is not None:
        client.dialog = previous
    media = {"invite": _invite(previous), "local_video_rtp_port": previous.local_video_rtp_port,
             "video_direction": previous.local_video_direction}
    registry = _Registry(client)
    registry.resource_for = lambda *_args: media
    monkeypatch.setattr(module, "call_registry", lambda _hass: registry)
    endpoint = SimpleNamespace(active=media["invite"])

    def commit_endpoint(_call_id, old, candidate):
        if endpoint.active is not old:
            return False
        endpoint.active = candidate
        return True

    endpoint.commit_prepared_reinvite = commit_endpoint
    endpoint.async_prepare_video_reinvite = AsyncMock(return_value=media["invite"])
    manager = SimpleNamespace(video_reinvite_result=lambda _id: (488, "Rejected"))
    monkeypatch.setattr(module, "sip_endpoint_manager", lambda _hass: manager)

    def stage(candidate, *, prepare_effect=None, commit=True):
        if outgoing:
            async def prepare(**_kwargs):
                if prepare_effect is not None:
                    await prepare_effect()
                if candidate is not None:
                    client._prepared_reinvite = (
                        client.dialog, candidate, (candidate.video_format,),
                        candidate.local_video_direction,
                    )
                return candidate
            client.async_prepare_video_reinvite = prepare
            # Rejected/stale accepted offers are aborted by the production owner.
            client.abort_prepared_reinvite = Mock()
            if not commit:
                client.commit_prepared_reinvite = Mock(return_value=False)
        else:
            async def prepare(_call_id, **_kwargs):
                if prepare_effect is not None:
                    await prepare_effect()
                if candidate is None:
                    return None
                prepared = PreparedEndpointVideoReinvite(
                    endpoint, "call-1", endpoint.active, _invite(candidate),
                )
                if not commit:
                    endpoint.commit_prepared_reinvite = Mock(return_value=False)
                return prepared
            manager.async_prepare_video_reinvite = prepare
        return candidate

    return SimpleNamespace(
        module=module, store=store, registry=registry, client=client,
        media=media, endpoint=endpoint, stage=stage,
    )


@pytest.mark.parametrize("outgoing", [False, True])
async def test_committed_camera_cycle_publishes_actual_directional_formats(
    intent, monkeypatch, outgoing,
):
    module, store = intent
    vp8 = module.sdp.RtpVideoFormat(payload_type=104, encoding="VP8")
    receive = replace(vp8, payload_type=105)
    jpeg = module.sdp.RtpVideoFormat(payload_type=26, encoding="JPEG")
    context = _metadata_context(intent, monkeypatch, outgoing=outgoing, previous=_dialog())
    store.update(video_format="", video_send_format="", video_receive_format="")

    for enabled, send, recv in ((True, vp8, receive), (False, vp8, receive), (True, jpeg, jpeg)):
        direction = "sendrecv" if enabled else "recvonly"
        context.stage(_dialog(send, receive=recv, direction=direction))
        store["send_video"] = enabled
        assert await module.async_apply_send_video_intent(object(), "phone", enabled)
        assert store["video_format"] == send.wire_token()
        assert store["video_send_format"] == send.wire_token()
        assert store["video_receive_format"] == recv.wire_token()
        assert store["video_direction"] == direction
        assert store["video_requested"] is enabled
        assert store["video_negotiated"] is True
    assert store["media_renegotiations"] == 3


@pytest.mark.parametrize("outgoing", [False, True])
@pytest.mark.parametrize("failure", ["rejected", "commit", "cancelled"])
async def test_uncommitted_camera_intent_preserves_format_metadata(
    intent, monkeypatch, outgoing, failure,
):
    module, store = intent
    jpeg = module.sdp.RtpVideoFormat(payload_type=26, encoding="JPEG")
    vp8 = module.sdp.RtpVideoFormat(payload_type=104, encoding="VP8")
    context = _metadata_context(
        intent, monkeypatch, outgoing=outgoing,
        previous=_dialog(jpeg, direction="recvonly"),
    )
    expected = dict(video_format=jpeg.wire_token(), video_send_format=jpeg.wire_token(),
                    video_receive_format=jpeg.wire_token())
    store.update(expected)

    async def cancel():
        raise asyncio.CancelledError

    context.stage(
        None if failure == "rejected" else _dialog(vp8, direction="sendrecv"),
        prepare_effect=cancel if failure == "cancelled" else None,
        commit=failure != "commit",
    )
    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await module.async_apply_send_video_intent(object(), "phone", True)
    else:
        assert not await module.async_apply_send_video_intent(object(), "phone", True)
    assert {key: store[key] for key in expected} == expected


@pytest.mark.parametrize("outgoing", [False, True])
@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("stale", ["generation", "replacement_call", "hangup"])
async def test_late_camera_intent_never_publishes_to_stale_call(
    intent, monkeypatch, outgoing, accepted, stale,
):
    module, store = intent
    jpeg = module.sdp.RtpVideoFormat(payload_type=26, encoding="JPEG")
    vp8 = module.sdp.RtpVideoFormat(payload_type=104, encoding="VP8")
    context = _metadata_context(
        intent, monkeypatch, outgoing=outgoing,
        previous=_dialog(jpeg, direction="recvonly"),
    )
    store.update(video_format="replacement", video_send_format="replacement-send",
                 video_receive_format="replacement-receive")

    async def supersede():
        if stale == "generation":
            context.registry.is_generation_current = lambda *_args: False
        elif stale == "replacement_call":
            store["call_id"] = "call-2"
        else:
            store["state"] = "idle"

    context.stage(_dialog(vp8, direction="sendrecv") if accepted else None,
                  prepare_effect=supersede)
    assert not await module.async_apply_send_video_intent(object(), "phone", True)
    assert store["video_format"] == "replacement"
    assert store["video_send_format"] == "replacement-send"
    assert store["video_receive_format"] == "replacement-receive"
    module._fire_call_event.assert_not_called()


async def test_local_and_remote_reoffers_publish_same_committed_formats(intent, monkeypatch):
    from custom_components.voip_stack import browser_dialog_updates, call_scope

    module, store = intent
    vp8 = module.sdp.RtpVideoFormat(payload_type=104, encoding="VP8")
    receive = replace(vp8, payload_type=105)
    previous = _dialog()
    updated = _dialog(vp8, receive=receive, direction="recvonly")
    context = _metadata_context(intent, monkeypatch, outgoing=True, previous=previous)
    store["send_video"] = False
    context.stage(updated)
    assert await module.async_apply_send_video_intent(object(), "phone", False)
    keys = ("video_format", "video_send_format", "video_receive_format", "video_direction")
    local = {key: store[key] for key in keys}
    registry = context.registry
    registry.sessions = {"call-1": registry.session}
    registry.resolve_session_id = lambda call_id: call_id
    registry.resource_for = lambda *_args: None
    registry.clear_video_parameter_sets = Mock()
    context.client.dialog_ids.call_id = "call-1"
    monkeypatch.setattr(browser_dialog_updates, "call_registry", lambda _hass: registry)
    monkeypatch.setattr(browser_dialog_updates, "require_runtime_data", module.require_runtime_data)
    monkeypatch.setattr(browser_dialog_updates, "_ha_softphone_store", module._ha_softphone_store)
    monkeypatch.setattr(browser_dialog_updates, "_fire_call_event", Mock())
    monkeypatch.setattr(call_scope, "call_belongs_to_endpoint", lambda *_args: True)
    browser_dialog_updates.attach_browser_dialog_updates(object(), context.client, "phone", "device")
    for key in keys:
        store[key] = "uncommitted"
    prepared = await context.client.on_media_update(previous, updated, "INVITE")
    assert prepared is not None
    assert all(store[key] == "uncommitted" for key in keys)
    await prepared.commit()
    context.client.dialog = updated
    prepared.on_committed(updated)
    assert {key: store[key] for key in keys} == local
