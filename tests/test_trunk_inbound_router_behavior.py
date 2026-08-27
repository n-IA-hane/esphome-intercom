"""Executable source-termination guard for inbound trunk routing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.voip_stack import endpoint_runtime, trunk_inbound_router


pytestmark = pytest.mark.ha


@dataclass(frozen=True)
class _TestInvite:
    received_via_trunk: bool
    source_host: str
    source_port: int
    signaling_transport: str


def _invite(*, received_via_trunk: bool = False) -> _TestInvite:
    return _TestInvite(
        received_via_trunk=received_via_trunk,
        source_host="198.51.100.20",
        source_port=49152,
        signaling_transport="UDP",
    )


def test_registered_trunk_classifies_trusted_main_listener_invite() -> None:
    trunk = SimpleNamespace(
        registered=True,
        accepts_inbound_source=Mock(return_value=True),
    )

    classified = endpoint_runtime._classify_trunk_invite(
        _invite(), enabled=True, trunk=trunk
    )

    assert classified.received_via_trunk
    trunk.accepts_inbound_source.assert_called_once_with(
        "198.51.100.20", 49152, "UDP"
    )


def test_untrusted_or_unregistered_invite_is_not_classified_as_trunk() -> None:
    untrusted = SimpleNamespace(
        registered=True,
        accepts_inbound_source=Mock(return_value=False),
    )
    unregistered = SimpleNamespace(
        registered=False,
        accepts_inbound_source=Mock(return_value=True),
    )

    assert not endpoint_runtime._classify_trunk_invite(
        _invite(), enabled=True, trunk=untrusted
    ).received_via_trunk
    assert not endpoint_runtime._classify_trunk_invite(
        _invite(received_via_trunk=True), enabled=True, trunk=unregistered
    ).received_via_trunk
    unregistered.accepts_inbound_source.assert_not_called()


def test_dtmf_route_keeps_provisional_owner_until_bridge_commit() -> None:
    source = Path(trunk_inbound_router.__file__).read_text()
    direct_route = source.rsplit(
        'preanswered = registry.resource_for(invite.call_id, "preanswered")', 1
    )[1]
    before_commit = direct_route.split("async_commit_outbound_bridge(", 1)[0]
    assert "take_media(invite.call_id, provisional=True)" not in before_commit
    assert "consume_pending_source=True" in direct_route


def _runtime() -> trunk_inbound_router.TrunkInboundRuntime:
    return trunk_inbound_router.TrunkInboundRuntime(
        hass=SimpleNamespace(),
        config={},
        local_ip="192.0.2.1",
        ha_peer_name="Casa",
        route_resolver=Mock(),
        forward_existing_call=AsyncMock(),
        defer_invite_to_softphone=Mock(),
        start_local_assist_bridge=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_source_bye_before_routing_leaves_cleanup_to_session_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_artifacts = SimpleNamespace(
        trunk_closed=True,
        trunk_info_queue=object(),
    )
    artifacts = SimpleNamespace(
        artifacts_for=lambda call_id: call_artifacts,
    )
    registry = Mock()
    ports = SimpleNamespace(ports=(40000, 40002), release=Mock())
    invite = SimpleNamespace(call_id="call-1", caller="Alice", target="Casa")
    route = Mock()
    monkeypatch.setattr(
        trunk_inbound_router,
        "call_runtime_artifacts",
        lambda _hass: artifacts,
    )
    monkeypatch.setattr(
        trunk_inbound_router,
        "call_registry",
        lambda _hass: registry,
    )
    monkeypatch.setattr(
        trunk_inbound_router,
        "trunk_config",
        lambda _hass: {},
    )
    monkeypatch.setattr(
        trunk_inbound_router,
        "async_build_peer_snapshot",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        trunk_inbound_router,
        "registered_roster_entries",
        Mock(return_value=[]),
    )
    monkeypatch.setattr(
        trunk_inbound_router,
        "roster_from_peers",
        Mock(return_value=[]),
    )
    monkeypatch.setattr(
        trunk_inbound_router,
        "dtmf_extension_routes",
        Mock(return_value={}),
    )
    monkeypatch.setattr(trunk_inbound_router, "route_inbound_trunk", route)

    await trunk_inbound_router.async_route_trunk_invite(
        _runtime(), invite, bridge_ports=ports
    )

    ports.release.assert_not_called()
    assert call_artifacts.trunk_closed
    assert call_artifacts.trunk_info_queue is not None
    route.assert_not_called()
    registry.take_pending_invite.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_route_terminates_answered_source_through_session_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.voip_stack.router import RouteAction

    call_artifacts = SimpleNamespace(
        trunk_closed=False,
        trunk_info_queue=None,
    )
    artifacts = SimpleNamespace(artifacts_for=lambda _call_id: call_artifacts)
    registry = Mock()
    registry.resource_for.return_value = {"final_response_sent": True}
    terminate = AsyncMock(return_value=True)
    ports = SimpleNamespace(ports=(40000, 40002), release=Mock())
    invite = SimpleNamespace(
        call_id="call-reject",
        caller="Wildix caller",
        target="Casa",
        source_host="192.0.2.10",
    )
    decision = SimpleNamespace(action=RouteAction.REJECT, target="")
    monkeypatch.setattr(
        trunk_inbound_router,
        "call_runtime_artifacts",
        lambda _hass: artifacts,
    )
    monkeypatch.setattr(trunk_inbound_router, "call_registry", lambda _hass: registry)
    monkeypatch.setattr(
        trunk_inbound_router,
        "EndpointTerminationHandler",
        lambda _hass: SimpleNamespace(terminate=terminate),
    )
    monkeypatch.setattr(trunk_inbound_router, "trunk_config", lambda _hass: {})
    monkeypatch.setattr(
        trunk_inbound_router,
        "async_build_peer_snapshot",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        trunk_inbound_router,
        "registered_roster_entries",
        Mock(return_value=[]),
    )
    monkeypatch.setattr(
        trunk_inbound_router,
        "roster_from_peers",
        Mock(return_value=[]),
    )
    monkeypatch.setattr(
        trunk_inbound_router,
        "dtmf_extension_routes",
        Mock(return_value={}),
    )
    monkeypatch.setattr(
        trunk_inbound_router,
        "route_inbound_trunk",
        Mock(return_value=decision),
    )
    monkeypatch.setattr(trunk_inbound_router, "trunk_default_target", lambda _cfg: "Casa")
    await trunk_inbound_router.async_route_trunk_invite(
        _runtime(), invite, bridge_ports=ports
    )

    registry.take_pending_invite.assert_not_called()
    registry.take_media.assert_not_called()
    ports.release.assert_not_called()
    terminate.assert_awaited_once()
    call_id, intent = terminate.await_args.args
    assert call_id == invite.call_id
    assert intent.sip_disposition.value == "bye"
    assert intent.reason == "route_not_found"


def test_dtmf_preanswer_creates_call_owner_before_attaching_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.voip_stack import pbx_runtime
    from custom_components.voip_stack.const import (
        CONF_TRUNK_DTMF_ENABLED,
        CONF_TRUNK_DTMF_TIMEOUT_MS,
        CONF_TRUNK_INBOUND_DEFAULT_TARGET,
        CONF_TRUNK_INBOUND_MODE,
        TRUNK_INBOUND_MODE_DTMF,
    )
    from custom_components.voip_stack.inbound_routing import trunk

    owner = pbx_runtime.SipEndpointRuntime(allow_dark_sessions=True)
    ports = SimpleNamespace(ports=(40000, 40002), release=Mock())
    audio_format = SimpleNamespace(wire_token=lambda: "PCMA/8000/1")
    rtp_format = SimpleNamespace(
        audio_format=audio_format,
        wire_token=lambda: "pt=8:PCMA/8000/1",
    )
    invite = SimpleNamespace(
        call_id="trunk-call-1",
        caller="Wildix caller",
        source_host="192.0.2.10",
        send_format=rtp_format,
        recv_format=rtp_format,
        remote_sdp="v=0\r\n",
        video_format=None,
        answer_video_format=None,
    )
    config = {
        CONF_TRUNK_INBOUND_MODE: TRUNK_INBOUND_MODE_DTMF,
        CONF_TRUNK_DTMF_ENABLED: True,
        CONF_TRUNK_DTMF_TIMEOUT_MS: 1000,
        CONF_TRUNK_INBOUND_DEFAULT_TARGET: "Casa",
    }
    runtime = SimpleNamespace(
        hass=SimpleNamespace(),
        config={},
        local_ip="192.0.2.1",
        run_trunk_inbound_route_guarded=AsyncMock(),
    )

    monkeypatch.setattr(trunk, "call_runtime_artifacts", lambda _hass: owner)
    monkeypatch.setattr(
        trunk.RtpPortReservation,
        "allocate",
        classmethod(lambda cls, _hass: ports),
    )
    monkeypatch.setattr(trunk, "build_answer_directional", Mock(return_value="sdp"))
    monkeypatch.setattr(trunk, "publish_bridge_projection", Mock())
    monkeypatch.setattr(trunk.sip_sdp, "offered_dtmf_formats", Mock(return_value=[]))

    owned_task = Mock()

    def capture_task(_hass, coroutine):
        coroutine.close()
        return owned_task

    monkeypatch.setattr(trunk, "create_runtime_task", capture_task)

    result = trunk.prepare_trunk_preanswer(
        runtime=runtime,
        invite=invite,
        trunk_config=config,
        direct_route_preprocessed=False,
        registry=owner,
    )

    assert result is not None
    assert result.status == 200
    session = owner.get_session(invite.call_id)
    assert session is not None
    assert session.artifacts.pending_invite is invite
    assert any(
        resource.name == f"preanswered:{invite.call_id}"
        for resource in session.resources
    )
    assert session.named_tasks[f"trunk_route:{invite.call_id}"] is owned_task
