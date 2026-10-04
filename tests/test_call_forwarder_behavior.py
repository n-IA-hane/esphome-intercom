"""Executable guards for the canonical call-forwarding boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

from homeassistant.exceptions import ServiceValidationError
import pytest

from custom_components.voip_stack import call_forwarder


pytestmark = pytest.mark.ha


def _runtime() -> call_forwarder.ForwardRuntime:
    return call_forwarder.ForwardRuntime(
        hass=SimpleNamespace(),
        config={},
        local_ip="127.0.0.1",
        route_resolver=Mock(),
        browser_leg_for_member=Mock(),
        defer_invite_to_softphone=Mock(),
        prepare_outbound_leg=Mock(),
        publish_pending_ringing=Mock(),
        start_local_assist_bridge=Mock(),
    )


def _registry(
    *,
    invite: object | None = None,
    state: str = "ringing",
    sequence: int = 4,
    route_history: tuple[str, ...] = (),
) -> SimpleNamespace:
    context = SimpleNamespace(
        state=state,
        sequence=sequence,
        route_history=route_history,
    )
    return SimpleNamespace(
        pending_invites={"call-1": invite} if invite is not None else {},
        artifact_for=lambda call_id, name: (
            invite if call_id == "call-1" and name == "pending_invite" else None
        ),
        event_context=Mock(return_value=context),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call_id", "destination", "on_failure", "message"),
    [
        ("", "Test", "resume", "call_id and destination are required"),
        ("call-1", "", "resume", "call_id and destination are required"),
        ("call-1", "Test", "retry", "on_failure must be"),
    ],
)
async def test_forward_rejects_invalid_public_request(
    call_id: str,
    destination: str,
    on_failure: str,
    message: str,
) -> None:
    with pytest.raises(ServiceValidationError, match=message):
        await call_forwarder.async_forward_existing_call(
            _runtime(),
            call_id=call_id,
            destination=destination,
            on_failure=on_failure,
        )


@pytest.mark.asyncio
async def test_forward_validation_error_exposes_translation_contract() -> None:
    with pytest.raises(ServiceValidationError) as captured:
        await call_forwarder.async_forward_existing_call(
            _runtime(),
            call_id="",
            destination="Test",
        )

    error = captured.value
    assert error.translation_domain == "voip_stack"
    assert error.translation_key == "call_id_destination_required"
    assert error.translation_placeholders is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expected_state", "expected_sequence", "message"),
    [
        ("in_call", 0, "is ringing, expected in_call"),
        ("", 9, "sequence is 4, expected 9"),
    ],
)
async def test_forward_rejects_stale_snapshot_before_route_mutation(
    monkeypatch: pytest.MonkeyPatch,
    expected_state: str,
    expected_sequence: int,
    message: str,
) -> None:
    registry = _registry(invite=object())
    monkeypatch.setattr(call_forwarder, "_call_registry", lambda _hass: registry)

    with pytest.raises(ServiceValidationError, match=message):
        await call_forwarder.async_forward_existing_call(
            _runtime(),
            call_id="call-1",
            destination="Test",
            expected_state=expected_state,
            expected_sequence=expected_sequence,
        )


@pytest.mark.asyncio
async def test_forward_rejects_route_loop_and_missing_invite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(invite=object(), route_history=("hop",) * 8)
    monkeypatch.setattr(call_forwarder, "_call_registry", lambda _hass: registry)
    with pytest.raises(ServiceValidationError, match="exceeded 8 routing hops"):
        await call_forwarder.async_forward_existing_call(
            _runtime(), call_id="call-1", destination="Test"
        )

    registry = _registry()
    with pytest.raises(ServiceValidationError, match="not a forwardable"):
        await call_forwarder.async_forward_existing_call(
            _runtime(), call_id="call-1", destination="Test"
        )


@pytest.mark.asyncio
async def test_forward_rejects_concurrent_owner_without_replacing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(invite=object(), state="ringing")
    owner = asyncio.create_task(asyncio.sleep(10))
    stored = {"forward": owner}
    artifacts = SimpleNamespace(
        task_for=lambda call_id, name: stored.get(name),
        artifacts_for=lambda call_id: SimpleNamespace(forward_claim=False),
    )
    monkeypatch.setattr(call_forwarder, "_call_registry", lambda _hass: registry)
    monkeypatch.setattr(
        call_forwarder,
        "call_runtime_artifacts",
        lambda _hass: artifacts,
    )

    try:
        with pytest.raises(ServiceValidationError, match="already being forwarded"):
            await call_forwarder.async_forward_existing_call(
                _runtime(), call_id="call-1", destination="Test"
            )
        assert not owner.cancelled()
        assert stored["forward"] is owner
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.parametrize("direct_uri", [False, True])
async def test_forbidden_external_forward_preserves_current_call(monkeypatch, direct_uri):
    from unittest.mock import AsyncMock
    from custom_components.voip_stack import runtime_data
    from custom_components.voip_stack.router import RouteAction, RouteDecision

    invite = object()
    registry = _registry(invite=invite)
    registry.get_session = lambda _: SimpleNamespace(metadata={"source_endpoint_id": "sip:restricted"})
    runtime = _runtime()
    runtime.config["external_call_blocked_endpoints"] = ["sip:restricted"]
    runtime.route_resolver.route.return_value = RouteDecision(
        RouteAction.DIRECT if direct_uri else RouteAction.TRUNK,
        sip_uri="sip:441234567890@provider.example" if direct_uri else "",
    )
    access_runtime = SimpleNamespace(
        transport_config=runtime.config, trunk_config={"trunk_server": "provider.example"}, sip=registry,
    )
    monkeypatch.setattr(call_forwarder, "_call_registry", lambda _: registry)
    monkeypatch.setattr(call_forwarder, "_async_build_peer_snapshot", AsyncMock(return_value=[]))
    monkeypatch.setattr(call_forwarder, "_roster_from_peers", lambda *_: [])
    monkeypatch.setattr(call_forwarder, "_registered_roster_entries", lambda _: [])
    monkeypatch.setattr(runtime_data, "require_runtime_data", lambda _: access_runtime)
    monkeypatch.setattr(runtime_data, "sip_trunk", lambda _: None)
    artifacts = Mock(side_effect=AssertionError("must reject before claiming or cancelling route"))
    monkeypatch.setattr(call_forwarder, "call_runtime_artifacts", artifacts)
    with pytest.raises(ServiceValidationError) as caught:
        await call_forwarder.async_forward_existing_call(
            runtime, call_id="call-1", destination="441234567890",
        )
    assert caught.value.translation_key == "external_calls_disabled"
    artifacts.assert_not_called()
    runtime.prepare_outbound_leg.assert_not_called()
    assert registry.artifact_for("call-1", "pending_invite") is invite
