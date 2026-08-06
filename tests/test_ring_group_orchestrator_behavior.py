"""Executable startup guards for ring-group orchestration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.voip_stack import ring_group_orchestrator
from custom_components.voip_stack.core.sdp import RtpPcmFormat
from custom_components.voip_stack.core.sip import parse_sip_uri
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.sip_listener import SipInvite


pytestmark = pytest.mark.ha


def _invite() -> SipInvite:
    audio = RtpPcmFormat(8, "PCMA", 8000, 1)
    return SipInvite(
        source_host="192.0.2.10",
        source_port=5060,
        request_uri=parse_sip_uri("sip:group@192.0.2.1"),
        caller_uri=parse_sip_uri("sip:alice@192.0.2.10"),
        target="group",
        caller="Alice",
        call_id="call-1",
        cseq="1 INVITE",
        remote_sdp=b"",
        send_format=audio,
        recv_format=audio,
        remote_rtp_host="192.0.2.10",
        remote_rtp_port=40000,
    )


def _runtime() -> ring_group_orchestrator.RingGroupRuntime:
    return ring_group_orchestrator.RingGroupRuntime(
        hass=SimpleNamespace(),
        config={},
        local_ip="192.0.2.1",
        ha_peer_name=Mock(return_value="Casa"),
        browser_leg_for_member=Mock(),
        logical_endpoint_for_member=Mock(),
        prepare_outbound_leg=Mock(),
        attach_client_media_update=Mock(),
        terminate_sip_bridge=Mock(),
    )


@pytest.mark.asyncio
async def test_invalid_ring_policy_terminates_source_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = SimpleNamespace(finish_and_pop=Mock())
    final_response = Mock()
    monkeypatch.setattr(
        ring_group_orchestrator,
        "_call_registry",
        lambda _hass: registry,
    )
    monkeypatch.setattr(
        ring_group_orchestrator,
        "endpoint_directory",
        lambda _hass: SimpleNamespace(get=Mock(return_value=None)),
    )
    monkeypatch.setattr(
        ring_group_orchestrator,
        "_sip_send_final_response",
        final_response,
    )
    entry = RosterEntry(
        id="group",
        name="Group",
        metadata={"members": ["Casa"], "ring_timeout": -1},
    )

    await ring_group_orchestrator.run_ring_group_call(
        _runtime(), _invite(), entry, [], []
    )

    final_response.assert_called_once()
    assert final_response.call_args.args[2:4] == (500, "Server Internal Error")
    registry.finish_and_pop.assert_called_once()


@pytest.mark.asyncio
async def test_terminated_source_never_starts_group_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = SimpleNamespace(
        sessions={},
        resolve_session_id=Mock(return_value="missing"),
        is_generation_current=Mock(),
    )
    prepare = Mock()
    monkeypatch.setattr(
        ring_group_orchestrator,
        "_call_registry",
        lambda _hass: registry,
    )
    monkeypatch.setattr(
        ring_group_orchestrator,
        "endpoint_directory",
        lambda _hass: SimpleNamespace(get=Mock(return_value=None)),
    )
    monkeypatch.setattr(
        ring_group_orchestrator,
        "async_prepare_ring_group_candidates",
        prepare,
    )
    entry = RosterEntry(
        id="group",
        name="Group",
        metadata={
            "members": ["Casa"],
            "ring_timeout": 10,
            "step_timeout": 5,
        },
    )

    await ring_group_orchestrator.run_ring_group_call(
        _runtime(), _invite(), entry, [], []
    )

    prepare.assert_not_called()
    registry.is_generation_current.assert_not_called()
