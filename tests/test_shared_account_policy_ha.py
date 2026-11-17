"""One account's configured no-answer policy owns its mixed transport offer."""
import asyncio
import json
import math
import time

import pytest

from .test_shared_account_fork_ha import (
    shared_account as shared_account, canonical_browser as canonical_browser,
    logical_browser as logical_browser, registered_phones as registered_phones,
    trunks as trunks, lab as lab, assert_no_active_calls,
)
from .test_browser_logical_availability_ha import assert_no_browser_media_owner, end_call, resources

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("logical_browser", [{"ringing_timeout": 1}], indirect=True)
@pytest.mark.parametrize("single_timeout_owner", [False, True], ids=["wire-control", "single-owner-invariant"])
async def test_shared_noanswer_retains_caller_for_real_fallback(
    shared_account, registered_phones, monkeypatch, single_timeout_owner, tmp_path,
):
    from custom_components.voip_stack import ring_group_orchestrator
    from custom_components.voip_stack.call_deadlines import CallDeadline
    from custom_components.voip_stack.websocket_api import CALL_EVENT

    runtime, caller, contacts = shared_account
    fallback = registered_phones[2]
    budgets, deadlines, events = [], [], []
    original_fork, original_arm = ring_group_orchestrator.run_owned_ring_group_fork, CallDeadline.arm

    async def observe_fork(*args, **kwargs):
        budgets.append(kwargs.get("overall_timeout"))
        return await original_fork(*args, **kwargs)

    def observe_arm(deadline, loop, delay):
        if deadline.name.startswith("deadline:phone_policy:"):
            deadlines.append({"name": deadline.name, "delay": delay})
        return original_arm(deadline, loop, delay)

    monkeypatch.setattr(ring_group_orchestrator, "run_owned_ring_group_fork", observe_fork)
    monkeypatch.setattr(CallDeadline, "arm", observe_arm)
    unsubscribe = runtime.hass.bus.async_listen(CALL_EVENT, lambda event: events.append({
        key: event.data.get(key) for key in ("call_id", "generation", "scope", "event_type", "state", "reason", "terminal_reason")}))
    evidence = {"mode": "single-owner-invariant" if single_timeout_owner else "wire-control",
                "configured_timeout": 1, "fallback": "transfer_c", "home_or_physical_device": False}
    caller_offset, fallback_offset = len(caller.read()), len(fallback.read())
    identity = None
    try:
        target = f'sip:221@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}'
        await asyncio.to_thread(caller.command, f"/dial {target}")
        for contact in contacts:
            await asyncio.to_thread(contact.wait_for, "Incoming call", 3)
        current = next(session for session in runtime.calls.sessions.values() if "casa" in session.endpoint_claims)
        identity, generation = current.call_id, current.generation
        evidence.update(call_id=identity, generation=generation)
        async with asyncio.timeout(5):
            while ("Call established" not in fallback.read()[fallback_offset:]
                   or "Call established" not in caller.read()[caller_offset:]):
                if not current.live:
                    break
                await asyncio.sleep(.01)
        # Automatic bareSIP answering can omit the interactive Incoming line;
        # its established dialog independently proves INVITE/200/ACK receipt.
        incoming_line = "Incoming call" in fallback.read()[fallback_offset:]
        fallback_established = "Call established" in fallback.read()[fallback_offset:]
        evidence.update(fallback_incoming_console_line=incoming_line,
            fallback_received_INVITE=incoming_line or fallback_established,
            fallback_INVITE_evidence="UA established dialog" if fallback_established else "incoming console line",
            fallback_established=fallback_established,
            caller_established_once=caller.read()[caller_offset:].count("Call established") == 1,
            caller_session_retained=runtime.calls.get_session(identity, generation=generation) is current and current.live,
            termination_reason=current.terminal_reason,
            fork_overall_timeouts=["infinite" if value is not None and math.isinf(value) else value for value in budgets],
            owned_phone_policy_deadlines=deadlines, events=events)
        assert evidence["fallback_received_INVITE"] and evidence["fallback_established"], evidence
        assert evidence["caller_established_once"] and evidence["caller_session_retained"], evidence
        assert all("Call established" not in contact.read() for contact in contacts)
        assert_no_browser_media_owner(runtime)
        await assert_no_active_calls(contacts)
        if single_timeout_owner:
            assert len(budgets) == 1 and math.isinf(budgets[0]), evidence
            assert any(deadline["delay"] == 1 for deadline in deadlines), evidence
        evidence["result"] = "PASS"
    finally:
        unsubscribe()
        await end_call(runtime, caller)
        evidence["final_call_scoped_quiescent"] = not runtime.calls.sessions and not runtime.calls.endpoint_claims_snapshot()
        output = tmp_path
        output.mkdir(parents=True, exist_ok=True)
        (output / f'shared-noanswer-{evidence["mode"]}-{time.time_ns()}.json').write_text(json.dumps(evidence, indent=2) + '\n')


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
@pytest.mark.parametrize("logical_browser", [{"ringing_timeout": 1}], indirect=True)
@pytest.mark.parametrize("forward_source", ["public", "no_answer"])
async def test_public_forward_to_shared_account_uses_its_noanswer_policy(shared_account, registered_phones, tmp_path, forward_source):
    from custom_components.voip_stack.phone_policy import PhonePolicy
    from custom_components.voip_stack.websocket_api import CALL_EVENT
    from .test_sip_phone_transfers_ha import assert_peer_audio_tones

    runtime, caller, contacts = shared_account
    original = runtime.runtime.endpoints.require("transfer_b")
    runtime.runtime.endpoints.update(original.endpoint_id, device_id="original-phone")
    if forward_source == "no_answer":
        runtime.runtime.endpoints.update(original.endpoint_id, policy=PhonePolicy.from_data({
            "ringing_timeout": 1, "no_answer": {"kind": "contact", "target": "casa"},
        }))
    fallback = runtime.runtime.endpoints.require("transfer_c")
    session = None
    events = []
    unsubscribe = runtime.hass.bus.async_listen(CALL_EVENT, lambda event: events.append(dict(event.data)))
    try:
        await asyncio.to_thread(caller.command, "/dial transfer_b")
        async with asyncio.timeout(3):
            while not any(session.state == "remote_ringing" and session.callee == original.name
                          for session in runtime.calls.sessions.values()):
                await asyncio.sleep(.01)
        identity = runtime.runtime.endpoints.require(original.endpoint_id).active_call_id
        session = runtime.calls.get_session(identity)
        token = session.token
        if forward_source == "public":
            await runtime.hass.services.async_call("voip_stack", "forward", {
                "device_id": "original-phone", "call_id": identity, "destination": "Casa",
            }, blocking=True)
        for contact in contacts:
            await asyncio.to_thread(contact.wait_for, "Incoming call", 3)
        async with asyncio.timeout(4):
            while runtime.runtime.endpoints.require(fallback.endpoint_id).active_call_id != identity or session.state != "in_call":
                await asyncio.sleep(.01)
        assert session.owns(token)
        assert caller.read().count("Call established") == 1
        assert all("Call established" not in contact.read() for contact in contacts)
        await assert_no_active_calls(contacts)
        relay = runtime.calls.resource_for(identity, "relay")
        async with asyncio.timeout(3):
            while min(relay.left_tx_packets, relay.right_tx_packets) < 30:
                await asyncio.sleep(.01)
        forwarding = [event for event in events if event.get("event_type") == "forwarding"
                      and event.get("scope") == "sip_bridge" and event.get("call_id") == identity]
        assert sum(event.get("callee") == "Casa" for event in forwarding) == 1, forwarding
        assert all(event.get("generation") == token.generation for event in forwarding), forwarding
        # The later orchestrator handoff is already claimed, not this migrated
        # forward-claim presentation family.
        assert all(event.get("route_source") == "automation" for event in forwarding
                   if event.get("callee") == "Casa"), forwarding
    finally:
        unsubscribe()
        await asyncio.to_thread(caller.hangup)
        if session is not None:
            await asyncio.wait_for(session.terminated.wait(), 3)
        snapshot = resources(runtime)
        assert snapshot["call_scoped_quiescent"], {
            key: value for key, value in snapshot["resource_counts"].items() if value
        }
    # The independent sndfile filters finalize their WAV headers on UA close.
    for peer in (caller, registered_phones[2]):
        await asyncio.to_thread(peer.close)
    assert_peer_audio_tones(tmp_path, (("transfer_a", 650), ("transfer_c", 450)))


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("logical_browser", [{"ringing_timeout": 60}], indirect=True)
@pytest.mark.timeout(45)
async def test_shared_account_is_still_answerable_after_thirty_seconds(shared_account):
    """Use elapsed wall time, not a patched timeout or synthetic fork result."""
    runtime, caller, contacts = shared_account
    started = asyncio.get_running_loop().time()
    try:
        target = f'sip:221@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}'
        await asyncio.to_thread(caller.command, f"/dial {target}")
        for contact in contacts:
            await asyncio.to_thread(contact.wait_for, "Incoming call", 3)
        session = next(session for session in runtime.calls.sessions.values() if "casa" in session.endpoint_claims)
        token = session.token
        await asyncio.sleep(max(0, started + 31 - asyncio.get_running_loop().time()))
        route = runtime.calls.artifact_for(session.call_id, "pending_route")
        assert session.owns(token) and route and not route["future"].done()
        assert all("Call established" not in contact.read() for contact in contacts)
        await runtime.hass.services.async_call("voip_stack", "answer", {
            "device_id": "browser-casa", "call_id": session.call_id, "media_client_id": "after-thirty",
        }, blocking=True)
        await asyncio.to_thread(caller.wait_for, "Call established", 3)
        assert session.owns(token) and caller.read().count("Call established") == 1
        await assert_no_active_calls(contacts)
    finally:
        await end_call(runtime, caller)
