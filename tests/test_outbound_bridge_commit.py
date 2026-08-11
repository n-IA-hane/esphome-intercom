from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.ha


def _format(token: str):
    return SimpleNamespace(
        audio_format=SimpleNamespace(wire_token=lambda: token),
        wire_token=lambda: token,
    )


def _fixture():
    from custom_components.voip_stack.outbound_attempts import OutboundLeg

    client = SimpleNamespace(
        dialog_ids=SimpleNamespace(call_id="dest-call"),
        dialog=object(),
        wait_for_dialog_termination=AsyncMock(return_value="remote_hangup"),
    )
    ports = SimpleNamespace(
        ports=(40000, 40002), detach=MagicMock(), release=MagicMock()
    )
    winner = OutboundLeg("desk", "sip:desk@pbx", client, ports)
    invite = SimpleNamespace(
        call_id="source-call",
        send_format=_format("PCMA/8000"),
        recv_format=_format("PCMU/8000"),
        remote_sdp=(
            "v=0\r\nc=IN IP4 127.0.0.1\r\n"
            "m=audio 5000 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\n"
        ),
    )
    session = SimpleNamespace(create_task=asyncio.create_task)
    registry = SimpleNamespace(
        register_bridge=MagicMock(return_value=session),
        forget_bridge_link=MagicMock(),
        close_leg=AsyncMock(return_value=True),
        attach_relay=MagicMock(),
        attach_media=MagicMock(),
        take_pending_invite=MagicMock(),
        take_media=MagicMock(return_value=None),
    )
    relay = SimpleNamespace(
        video_relay=None,
        start=AsyncMock(),
        stop=AsyncMock(),
        attach_video_relay=MagicMock(),
    )
    return invite, winner, registry, relay


def _pending_watchers() -> list[asyncio.Task]:
    return [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and task.get_name().startswith("voip-bridge-destination-")
        and not task.done()
    ]


@pytest.mark.asyncio
async def test_commit_transfers_bridge_and_owns_destination_watcher(monkeypatch):
    from custom_components.voip_stack import outbound_bridge_commit as module
    from custom_components.voip_stack.inbound_answer import AnswerCommitResult

    invite, winner, registry, relay = _fixture()
    binder = SimpleNamespace(attach=MagicMock())
    monkeypatch.setattr(module, "build_invite_client_relay", lambda **_: relay)
    monkeypatch.setattr(module, "attach_dtmf_event_bridge", MagicMock())
    monkeypatch.setattr(module, "build_answer_directional", lambda *_a, **_k: "sdp")
    monkeypatch.setattr(module, "BridgeMediaUpdateBinder", lambda _hass: binder)
    monkeypatch.setattr(
        module,
        "async_commit_runtime_answer",
        AsyncMock(return_value=AnswerCommitResult(True, True)),
    )
    monkeypatch.setattr(
        module, "async_watch_sip_bridge_destination", AsyncMock()
    )

    result = await module.async_commit_outbound_bridge(
        MagicMock(),
        registry,
        module.BridgeCommitData(
            invite=invite,
            winner=winner,
            source_relay_port=40000,
            dest_relay_port=40002,
            local_ip="127.0.0.1",
            release_port_pairs=(winner.ports.ports,),
            detach_reservations=(winner.ports,),
        ),
        module.BridgeCommitPolicy(
            route_kind="direct",
            caller="door",
            callee="desk",
            connected_party="desk",
        ),
    )
    await asyncio.sleep(0)

    assert result is not None
    relay.start.assert_awaited_once()
    winner.ports.detach.assert_called_once()
    registry.attach_relay.assert_called_once_with("source-call", relay)
    binder.attach.assert_called_once()
    assert not _pending_watchers()


@pytest.mark.asyncio
async def test_commit_reuses_registered_session_without_second_watcher(monkeypatch):
    from custom_components.voip_stack import outbound_bridge_commit as module
    from custom_components.voip_stack.inbound_answer import AnswerCommitResult

    invite, winner, registry, relay = _fixture()
    session = SimpleNamespace(generation=7, create_task=asyncio.create_task)
    registry.is_generation_current = MagicMock(return_value=True)
    registry.register_bridge.side_effect = AssertionError("duplicate bridge owner")
    monkeypatch.setattr(module, "build_invite_client_relay", lambda **_: relay)
    monkeypatch.setattr(module, "attach_dtmf_event_bridge", MagicMock())
    monkeypatch.setattr(module, "build_answer_directional", lambda *_a, **_k: "sdp")
    monkeypatch.setattr(
        module,
        "BridgeMediaUpdateBinder",
        lambda _hass: SimpleNamespace(attach=MagicMock()),
    )
    monkeypatch.setattr(
        module,
        "async_commit_runtime_answer",
        AsyncMock(return_value=AnswerCommitResult(True, True)),
    )

    result = await module.async_commit_outbound_bridge(
        MagicMock(),
        registry,
        module.BridgeCommitData(
            invite=invite,
            winner=winner,
            source_relay_port=40000,
            dest_relay_port=40002,
            local_ip="127.0.0.1",
            release_port_pairs=(winner.ports.ports,),
            detach_reservations=(winner.ports,),
        ),
        module.BridgeCommitPolicy(
            route_kind="direct",
            caller="door",
            callee="desk",
            connected_party="desk",
        ),
        session=session,
    )
    await asyncio.sleep(0)

    assert result is not None
    registry.is_generation_current.assert_called_once_with("source-call", 7)
    registry.register_bridge.assert_not_called()
    assert not _pending_watchers()


@pytest.mark.asyncio
async def test_commit_atomically_transfers_provisional_video_owner(monkeypatch):
    from custom_components.voip_stack import outbound_bridge_commit as module
    from custom_components.voip_stack.inbound_answer import AnswerCommitResult

    invite, winner, registry, relay = _fixture()
    reservation = SimpleNamespace(release=MagicMock())
    rtp_socket = SimpleNamespace(close=MagicMock())
    rtcp_socket = SimpleNamespace(close=MagicMock())
    pending_source = {
        "video_rtp_reservation": reservation,
        "video_rtp_socket": rtp_socket,
        "video_rtcp_socket": rtcp_socket,
    }
    registry.take_media.return_value = pending_source
    video_relay = SimpleNamespace(
        left=SimpleNamespace(send_format=object(), recv_format=object()),
        left_port=40008,
    )
    winner.video_relay = video_relay
    relay.attach_video_relay.side_effect = lambda value: setattr(
        relay, "video_relay", value
    )
    binder = SimpleNamespace(attach=MagicMock())
    monkeypatch.setattr(module, "build_invite_client_relay", lambda **_: relay)
    monkeypatch.setattr(
        module,
        "configure_answered_invite_video_relay",
        lambda *_a, **_k: SimpleNamespace(direction="sendrecv"),
    )
    monkeypatch.setattr(module, "attach_dtmf_event_bridge", MagicMock())
    monkeypatch.setattr(
        module, "async_start_sip_bridge_media", AsyncMock(return_value=False)
    )
    activate_source = AsyncMock(return_value=True)
    monkeypatch.setattr(
        module,
        "sip_endpoint_manager",
        lambda _hass: SimpleNamespace(
            async_activate_video_reinvite=activate_source
        ),
    )
    monkeypatch.setattr(module, "build_answer_directional", lambda *_a, **_k: "")
    monkeypatch.setattr(module, "BridgeMediaUpdateBinder", lambda _hass: binder)
    monkeypatch.setattr(
        module,
        "async_commit_runtime_answer",
        AsyncMock(return_value=AnswerCommitResult(True, True)),
    )
    monkeypatch.setattr(module, "async_watch_sip_bridge_destination", AsyncMock())

    await module.async_commit_outbound_bridge(
        MagicMock(),
        registry,
        module.BridgeCommitData(
            invite=invite,
            winner=winner,
            source_relay_port=40000,
            dest_relay_port=40002,
            local_ip="127.0.0.1",
            release_port_pairs=(winner.ports.ports,),
            detach_reservations=(winner.ports,),
        ),
        module.BridgeCommitPolicy(
            route_kind="trunk",
            caller="provider",
            callee="desk",
            connected_party="desk",
            response_already_sent=True,
            consume_pending_source=True,
        ),
    )
    await asyncio.sleep(0)

    registry.attach_relay.assert_called_once_with("source-call", relay)
    registry.take_media.assert_called_once_with("source-call", provisional=True)
    assert pending_source == {}
    reservation.release.assert_not_called()
    rtp_socket.close.assert_not_called()
    rtcp_socket.close.assert_not_called()
    assert relay.video_relay is video_relay
    activate_source.assert_awaited_once_with(
        "source-call",
        local_video_rtp_port=relay.video_relay.left_port,
        video_formats=(
            relay.video_relay.left.recv_format,
            relay.video_relay.left.send_format,
        ),
        video_direction="sendrecv",
    )
    assert not _pending_watchers()


@pytest.mark.asyncio
async def test_preanswered_video_activation_uses_source_dialog(monkeypatch):
    from custom_components.voip_stack.sip_endpoint import SipEndpointManager

    send = object()
    receive = object()
    prepared = SimpleNamespace(commit=MagicMock(return_value=True))
    endpoint = SimpleNamespace(
        async_prepare_video_reinvite=AsyncMock(return_value=prepared)
    )

    assert await SipEndpointManager.async_activate_video_reinvite(
        endpoint,
        "source-call",
        local_video_rtp_port=40008,
        video_formats=(receive, send),
        video_direction="sendrecv",
    )
    endpoint.async_prepare_video_reinvite.assert_awaited_once_with(
        "source-call",
        local_video_rtp_port=40008,
        video_formats=(receive, send),
        video_direction="sendrecv",
    )
    prepared.commit.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["relay", "answer"])
async def test_commit_failure_leaves_no_destination_watcher(monkeypatch, failure):
    from custom_components.voip_stack import outbound_bridge_commit as module
    from custom_components.voip_stack.inbound_answer import AnswerCommitResult

    invite, winner, registry, relay = _fixture()
    binder = SimpleNamespace(attach=MagicMock())
    monkeypatch.setattr(module, "build_invite_client_relay", lambda **_: relay)
    monkeypatch.setattr(module, "attach_dtmf_event_bridge", MagicMock())
    monkeypatch.setattr(module, "build_answer_directional", lambda *_a, **_k: "sdp")
    monkeypatch.setattr(module, "BridgeMediaUpdateBinder", lambda _hass: binder)
    monkeypatch.setattr(
        module,
        "EndpointTerminationHandler",
        lambda _hass: SimpleNamespace(terminate=AsyncMock(return_value=True)),
    )
    monkeypatch.setattr(
        module,
        "async_commit_runtime_answer",
        AsyncMock(
            return_value=AnswerCommitResult(
                failure != "answer", failure != "answer", "claim_failed"
            )
        ),
    )
    if failure == "relay":
        relay.start.side_effect = RuntimeError("media failed")

    with pytest.raises(RuntimeError):
        await module.async_commit_outbound_bridge(
            MagicMock(),
            registry,
            module.BridgeCommitData(
                invite=invite,
                winner=winner,
                source_relay_port=40000,
                dest_relay_port=40002,
                local_ip="127.0.0.1",
                release_port_pairs=(winner.ports.ports,),
                detach_reservations=(winner.ports,),
            ),
            module.BridgeCommitPolicy(
                route_kind="direct",
                caller="door",
                callee="desk",
                connected_party="desk",
            ),
        )
    await asyncio.sleep(0)

    assert not _pending_watchers()
