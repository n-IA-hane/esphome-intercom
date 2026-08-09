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
    registry = SimpleNamespace(
        register_bridge=MagicMock(return_value=object()),
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
