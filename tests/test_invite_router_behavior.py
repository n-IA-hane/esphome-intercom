"""Executable capacity and retransmission guards for inbound routing."""

from __future__ import annotations

import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.voip_stack import invite_router
from custom_components.voip_stack.core.sdp import RtpPcmFormat
from custom_components.voip_stack.core.sip import parse_sip_uri
from custom_components.voip_stack.router import RouteAction, RouteDecision
from custom_components.voip_stack.sip_listener import SipInvite
from custom_components.voip_stack.endpoint_session import LegPhase


pytestmark = pytest.mark.ha


def _invite() -> SipInvite:
    """One parsed INVITE after admission has identified its account."""
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
        identified_source_endpoint_id="source",
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
    from custom_components.voip_stack import trunk_selection

    # This routing unit has no configured trunks; public trunk-name parsing
    # has its own native configuration tests.
    monkeypatch.setattr(trunk_selection, "parse_public_trunk_destination",
                        lambda _hass, target: ("", target))
    monkeypatch.setattr(
        invite_router,
        "_registered_roster_entries",
        Mock(return_value=[]),
    )
    monkeypatch.setattr(invite_router, "_roster_from_peers", Mock(return_value=[]))
    invites = pending_invites or {}
    registry = SimpleNamespace(
        component=lambda _name: None,
        sip_client_for=lambda _call_id: None,
        get_session=lambda _call_id: None,
        artifact_items=lambda name: (
            iter(invites.items()) if name == "pending_invite" else iter(())
        )
    )
    monkeypatch.setattr(invite_router, "_call_registry", lambda _hass: registry)
    monkeypatch.setattr(
        invite_router,
        "endpoint_directory",
        lambda _hass: SimpleNamespace(get=Mock(return_value=SimpleNamespace(endpoint_id="source"))),
    )
    monkeypatch.setattr(
        invite_router,
        "_pending_routes",
        lambda _hass: pending_routes or {},
    )


@pytest.mark.parametrize("invalid", [None, "trunk", "ended", "generation", "claim", "account", "leg", "flow"])
async def test_local_browser_source_requires_current_account_generation_and_owned_client(monkeypatch, invalid):
    from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
    from custom_components.voip_stack.sip_ingress import owned_local_call_source

    runtime = _runtime()
    runtime.config["sip_port"] = 5060
    client = SimpleNamespace(matches_local_invite_source=Mock(return_value=invalid != "flow"))
    endpoint = PhoneEndpoint(endpoint_id="source", name="Alice", username="alice", kind=EndpointKind.BROWSER,
        active_call_id="another" if invalid == "account" else "call-1")
    session = SimpleNamespace(call_id="call-1", live=invalid != "ended", generation=7,
        metadata={"source_endpoint_id": "source"},
        endpoint_claims={"source": "destination" if invalid == "claim" else "source"},
        legs={"call-1": SimpleNamespace(dialog=client, role="ha_softphone",
            phase=LegPhase.CLOSING if invalid == "leg" else LegPhase.INVITING)})
    registry = SimpleNamespace(component=lambda _name: None, sip_client_for=lambda _id: client,
        get_session=lambda _id, generation=None: None if generation is not None and invalid == "generation" else session)
    invite = replace(_invite(), received_via_trunk=invalid == "trunk")
    correlated = owned_local_call_source(invite, registry, SimpleNamespace(get=lambda _id: endpoint),
        listener_hosts={runtime.local_ip, "127.0.0.1", "::1"}, listener_port=5060)
    assert correlated is (endpoint if invalid is None else None)
    if invalid is None:
        client.matches_local_invite_source.assert_called_once_with(invite,
            listener_hosts={runtime.local_ip, "127.0.0.1", "::1"}, listener_port=5060)


@pytest.mark.asyncio
async def test_route_retransmit_keeps_pending_decision(
    monkeypatch: pytest.MonkeyPatch, caplog,
) -> None:
    caplog.set_level(logging.DEBUG, logger=invite_router._LOGGER.name)
    _patch_route_prefix(monkeypatch, pending_routes={"call-1": object()})

    result = await invite_router.route_invite(_runtime(), _invite())

    assert result.status == 100
    assert result.reason == "Trying"
    assert not any(record.getMessage().startswith("PBX |") for record in caplog.records)


@pytest.mark.asyncio
async def test_discovery_does_not_reselect_listener_authoritative_audio(monkeypatch):
    from custom_components.voip_stack.peer import Peer

    _patch_route_prefix(monkeypatch, pending_routes={"call-1": object()})
    known = Peer(name="Alice", host="192.0.2.10",
        device={"sip_audio_tx_formats": ("L16/16000/1/10",),
                "sip_audio_rx_formats": ("L16/48000/1/10",)})
    runtime = _runtime()
    runtime.build_peer_snapshot.return_value = [known]
    authoritative = _invite()
    reject_reselection = Mock(side_effect=AssertionError("routing cannot renegotiate audio"))
    monkeypatch.setattr(invite_router.sip_sdp, "negotiate_directional", reject_reselection)

    result = await invite_router.route_invite(runtime, authoritative)

    assert result.status == 100
    reject_reselection.assert_not_called()
    assert authoritative.send_format.encoding == authoritative.recv_format.encoding == "PCMA"


@pytest.mark.asyncio
async def test_route_retransmit_keeps_existing_ringing_invite(
    monkeypatch: pytest.MonkeyPatch, caplog,
) -> None:
    caplog.set_level(logging.DEBUG, logger=invite_router._LOGGER.name)
    _patch_route_prefix(monkeypatch, pending_invites={"call-1": object()})

    result = await invite_router.route_invite(_runtime(), _invite())

    assert result.status == 180
    assert result.defer_final is True
    assert not any(record.getMessage().startswith("PBX |") for record in caplog.records)


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


@pytest.mark.parametrize("known", [True, False])
@pytest.mark.asyncio
async def test_direct_trunk_preserves_called_target_and_uses_fallback_only_if_unknown(monkeypatch, known, caplog):
    caplog.set_level(logging.DEBUG, logger=invite_router._LOGGER.name)
    from dataclasses import replace
    from custom_components.voip_stack.inbound_routing.automation import AutomationRoute
    from custom_components.voip_stack.sip_listener import SipInviteResult

    _patch_route_prefix(monkeypatch)
    original = replace(_invite(), request_uri=parse_sip_uri("sip:601@192.0.2.1"),
        target="601", target_route="601", received_via_trunk=True,
        called_number="601", called_number_raw="601", called_number_source="request_uri")
    destination = SimpleNamespace(extension="601", display_name="Bedroom")
    fallback = SimpleNamespace(extension="777", display_name="Assistant")
    initial = RouteDecision(RouteAction.FORWARD if known else RouteAction.TRUNK,
                            target="Bedroom" if known else "601", entry=destination if known else None)
    selected_fallback = RouteDecision(RouteAction.ASSIST, target="Assistant", entry=fallback)
    runtime = _runtime()
    runtime.is_trunk_invite = Mock(return_value=True)
    runtime.is_ha_target = Mock(return_value=False)
    runtime.get_trunk_config = Mock(return_value={"name": "Office", "trunk_inbound_mode": "direct", "trunk_inbound_default_target": "Assistant", "automation_routing_enabled": True})
    def route(invite, *_, lookup_target=None):
        target = invite.routing_target if lookup_target is None else lookup_target
        return initial if target == "601" else selected_fallback
    runtime.inbound_route_decision = Mock(side_effect=route)
    runtime.ha_router_decision = Mock(return_value=selected_fallback)
    runtime.hass.data = {"voip_stack": {"registration": SimpleNamespace(route_trigger_filters={})}}
    request = AsyncMock(return_value=AutomationRoute(action="decline"))
    monkeypatch.setattr(invite_router, "request_route_override", request)
    monkeypatch.setattr(invite_router, "automation_rejection", lambda **kw: SipInviteResult(603, "Decline", to_tag=""))
    assist = AsyncMock(return_value=SipInviteResult(488, "Wrong route", to_tag=""))
    monkeypatch.setattr(invite_router, "route_local_assist", assist)

    result = await invite_router.route_invite(runtime, original)

    assert result.status == 603
    request.assert_awaited_once()
    assert request.await_args.kwargs["invite"] is original
    assert request.await_args.kwargs["invite"].routing_target == "601"
    assert request.await_args.kwargs["decision"] == (initial if known else selected_fallback)
    assist.assert_not_awaited()
    assert runtime.inbound_route_decision.call_args_list[0].kwargs == {"lookup_target": "601"}
    assert runtime.inbound_route_decision.call_count == 1
    if known:
        runtime.ha_router_decision.assert_not_called()
    else:
        runtime.ha_router_decision.assert_called_once_with("Assistant", [])
    assert "Trunk Office received number 601, normalized to 601 from request_uri" in caplog.text
    assert "routing mode=direct" in caplog.text
    assert ("configured fallback is Assistant" in caplog.text) is not known


@pytest.mark.parametrize("override", [False, True])
async def test_restricted_registered_caller_cannot_egress_after_route_override(monkeypatch, override):
    from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
    _patch_route_prefix(monkeypatch)
    runtime = _runtime()
    runtime.config["external_call_blocked_endpoints"] = ["sip:alice"]
    runtime.registrar.registration_matches_source.return_value = True
    route = RouteDecision(RouteAction.TRUNK, target="441234567890")
    runtime.inbound_route_decision.return_value = (
        RouteDecision(RouteAction.FORWARD, target="Desk", sip_uri="sip:desk@192.0.2.20")
        if override else route
    )
    runtime.ha_router_decision.return_value = route
    runtime.get_trunk_config.return_value = {"trunk_server": "provider.example"}
    runtime.trunk_enabled.return_value = True
    caller = PhoneEndpoint(endpoint_id="sip:alice", username="alice", name="Alice", kind=EndpointKind.SIP_ACCOUNT)
    monkeypatch.setattr(invite_router, "endpoint_directory",
                        lambda _: SimpleNamespace(get=lambda identity: caller if identity == caller.endpoint_id else None))
    registry = SimpleNamespace(component=lambda _: None, artifact_items=lambda _: iter(()), get_session=lambda _: None,
                               sip_client_for=lambda _: None)
    monkeypatch.setattr(invite_router, "_call_registry", lambda _: registry)
    runtime.hass.data = {"voip_stack": {"registration": SimpleNamespace(route_trigger_filters={})}}
    monkeypatch.setattr(invite_router, "request_route_override", AsyncMock(return_value=SimpleNamespace(
        action="forward" if override else "", destination="441234567890" if override else "",
        ringing_timeout=None,
    )))
    monkeypatch.setattr(invite_router, "automation_rejection", lambda **_: None)
    monkeypatch.setattr(invite_router, "resolve_inbound_target", lambda **kwargs: SimpleNamespace(
        failure=None, decision=kwargs["decision"], endpoint=None,
    ))
    monkeypatch.setattr(invite_router, "sip_trunk", lambda _: SimpleNamespace(ready=True))
    bridge = AsyncMock(side_effect=AssertionError("forbidden call must not start bridge"))
    monkeypatch.setattr(invite_router, "route_sip_bridge", bridge)
    result = await invite_router.route_invite(runtime, replace(_invite(), identified_source_endpoint_id=caller.endpoint_id))
    assert result.status == 403
    assert result.decline_reason == "external_calls_disabled"
    bridge.assert_not_awaited()
