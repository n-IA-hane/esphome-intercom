"""Transactional camera-intent tests against the HA runtime boundary."""

from __future__ import annotations

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
    return module, store


async def test_enabling_video_commits_reserved_media_only_after_reinvite(
    intent, monkeypatch
) -> None:
    module, store = intent
    previous = SimpleNamespace(
        video_format=None,
        local_video_rtp_port=0,
        local_video_direction="inactive",
    )
    candidate = SimpleNamespace(
        video_format=object(),
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
