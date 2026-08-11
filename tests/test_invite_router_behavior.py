"""Executable capacity and retransmission guards for inbound routing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.voip_stack import invite_router
from custom_components.voip_stack.core.sdp import RtpPcmFormat
from custom_components.voip_stack.core.sip import parse_sip_uri
from custom_components.voip_stack.router import RouteAction, RouteDecision
from custom_components.voip_stack.sip_listener import SipInvite


pytestmark = pytest.mark.ha


def _invite() -> SipInvite:
    audio = RtpPcmFormat(8, "PCMA", 8000, 1)
    return SipInvite(
        source_host="192.0.2.10",
        source_port=5060,
        request_uri=parse_sip_uri("sip:Casa@192.0.2.1"),
        caller_uri=parse_sip_uri("sip:alice@192.0.2.10"),
        target="Casa",
        caller="Alice",
        call_id="call-1",
        cseq="1 INVITE",
        remote_sdp=b"",
        send_format=audio,
        recv_format=audio,
        remote_rtp_host="192.0.2.10",
        remote_rtp_port=40000,
    )


def _runtime() -> invite_router.InviteRuntime:
    return invite_router.InviteRuntime(
        hass=SimpleNamespace(),
        config={},
        local_ip="192.0.2.1",
        registrar=SimpleNamespace(registration_matches_source=Mock(return_value=False)),
        ha_peer_name=Mock(),
        get_trunk_config=Mock(return_value={}),
        trunk_enabled=Mock(return_value=False),
        is_trunk_invite=Mock(return_value=False),
        is_ha_target=Mock(return_value=False),
        ha_router_decision=Mock(),
        inbound_route_decision=Mock(
            return_value=RouteDecision(RouteAction.ANSWER_HA, target="Casa")
        ),
        build_peer_snapshot=AsyncMock(return_value=[]),
        browser_leg_for_member=Mock(),
        defer_invite_to_softphone=Mock(),
        enable_reused_sip_tcp_connection=Mock(),
        on_conference_inbound_timeout=Mock(),
        ring_conference_members=Mock(),
        run_ring_group_call=Mock(),
        run_trunk_inbound_route_guarded=Mock(),
        send_final_response=Mock(),
        sip_uri_transport=Mock(),
        start_local_assist_bridge=Mock(),
        terminate_sip_bridge=Mock(),
    )


def _patch_route_prefix(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pending_routes: dict[str, object] | None = None,
    pending_invites: dict[str, object] | None = None,
) -> None:
    monkeypatch.setattr(invite_router, "_peer_for_target", Mock(return_value=None))
    monkeypatch.setattr(
        invite_router,
        "_registered_roster_entries",
        Mock(return_value=[]),
    )
    monkeypatch.setattr(invite_router, "_roster_from_peers", Mock(return_value=[]))
    monkeypatch.setattr(
        invite_router,
        "_roster_entry_for_target",
        Mock(return_value=None),
    )
    invites = pending_invites or {}
    registry = SimpleNamespace(
        artifact_items=lambda name: (
            iter(invites.items()) if name == "pending_invite" else iter(())
        )
    )
    monkeypatch.setattr(invite_router, "_call_registry", lambda _hass: registry)
    monkeypatch.setattr(
        invite_router,
        "endpoint_directory",
        lambda _hass: SimpleNamespace(get=Mock(return_value=None)),
    )
    monkeypatch.setattr(
        invite_router,
        "_pending_routes",
        lambda _hass: pending_routes or {},
    )


@pytest.mark.asyncio
async def test_route_retransmit_keeps_pending_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_route_prefix(monkeypatch, pending_routes={"call-1": object()})

    result = await invite_router.route_invite(_runtime(), _invite())

    assert result.status == 100
    assert result.reason == "Trying"


@pytest.mark.asyncio
async def test_route_retransmit_keeps_existing_ringing_invite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_route_prefix(monkeypatch, pending_invites={"call-1": object()})

    result = await invite_router.route_invite(_runtime(), _invite())

    assert result.status == 180
    assert result.defer_final is True


@pytest.mark.asyncio
async def test_route_capacity_rejects_before_allocating_more_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = {f"other-{index}": object() for index in range(64)}
    _patch_route_prefix(monkeypatch, pending_invites=pending)

    result = await invite_router.route_invite(_runtime(), _invite())

    assert result.status == 503
    assert result.decline_reason == "capacity_exhausted"
    assert len(pending) == 64
