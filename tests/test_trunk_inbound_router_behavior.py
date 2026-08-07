"""Executable source-termination guard for inbound trunk routing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.voip_stack import trunk_inbound_router


pytestmark = pytest.mark.ha


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
        attach_client_media_update=Mock(),
        attach_dtmf_event_bridge=Mock(),
        terminate_sip_bridge=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_source_bye_before_routing_releases_only_reserved_media(
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

    ports.release.assert_called_once_with()
    assert not call_artifacts.trunk_closed
    assert call_artifacts.trunk_info_queue is None
    route.assert_not_called()
    registry.take_pending_invite.assert_not_called()
