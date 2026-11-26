"""Real fork/route owners for browser-only group pickup and rollback."""

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.core import ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError

from custom_components.voip_stack.dial_fork import DialDisposition, ForkStrategy
from custom_components.voip_stack.endpoint_session import EndpointCallSession
from custom_components.voip_stack.outbound_attempts import BrowserLeg, OutboundLeg
from custom_components.voip_stack.ring_group_fork import (
    build_ring_group_fork,
    run_owned_ring_group_fork,
)
from custom_components.voip_stack.call_pickup import _pickup_group_phone
from custom_components.voip_stack.route_decisions import set_pending_route_decision
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
from custom_components.voip_stack.call_projection import observe_phone_leg_projection
from custom_components.voip_stack.phone_state import phone_states
from .test_trunk_calls_ha import trunks as trunks, lab as lab

pytestmark = pytest.mark.ha


async def until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.parametrize("strategy", [ForkStrategy.PARALLEL, ForkStrategy.SEQUENTIAL])
@pytest.mark.parametrize("ticket_state", ["pending", "committed", "withdrawn"])
async def test_browser_only_owned_fork_confirms_before_retiring_route(
    hass, strategy, ticket_state
):
    session = EndpointCallSession("browser-group", 1)
    route = hass.loop.create_future()
    browsers = [BrowserLeg("A", "A", "A", "a")]
    candidates, payloads, _ = build_ring_group_fork(
        sip_port=5060,
        route_future=route,
        attempts=[],
        browser_legs=browsers,
        preflight_failures=[],
    )
    assert all(c.control for c in candidates)
    commits = []
    task = asyncio.create_task(
        run_owned_ring_group_fork(
            session,
            candidates,
            payloads,
            invite=None,
            attempts=[],
            browser_legs=browsers,
            on_leg_state=None,
            commit_winner=lambda winner, _: commits.append(winner.candidate_id) or True,
            strategy=strategy,
            overall_timeout=1,
            step_timeout=0.1,
        )
    )
    try:
        await until(lambda: bool(session.resources))
        gate = session.resources[0].value
        ticket = gate.submit("picker")
        if ticket_state == "committed":
            ticket.payload = OutboundLeg("picker", None, None, None)
            ticket.finish(True)
        elif ticket_state == "withdrawn":
            ticket.finish(False)
        if ticket_state == "withdrawn":
            await until(lambda: gate.current is ticket)
            await asyncio.sleep(0)
            assert not task.done(), "withdrawn pickup must not retire the browser group"
            assert not route.done(), "original browser answer must remain available"
            route.set_result({"action": "answer_ha", "endpoint_id": "A"})
        else:
            assert await asyncio.wait_for(asyncio.shield(ticket.selected), 0.5), (
                "pickup must be selected before fork commit"
            )
            if ticket_state == "pending":
                assert not route.done() and not commits
                ticket.finish(True)
        result, _ = await task
        assert result.outcome.disposition is DialDisposition.ANSWERED
        assert result.winner.candidate_id == (
            "browser:route-control"
            if ticket_state == "withdrawn"
            else "external:pickup"
        )
        assert ticket.accepted == (ticket_state != "withdrawn")
        assert len(commits) == 1 and not session.resources
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def prepare_browser_group(runtime):
    endpoints = []
    for name in ("A", "B", "C"):
        endpoint = PhoneEndpoint(
            endpoint_id="browser-" + name,
            name=name,
            device_id="device-" + name,
            kind="browser",
            availability="available",
            capabilities=frozenset({"audio", "dtmf"}),
        )
        runtime.runtime.endpoints.register(endpoint)
        endpoints.append(endpoint)
    session = runtime.calls.upsert(
        "pickup-group", state="ringing", caller="Caller", callee="Group"
    )
    route = {
        "future": runtime.hass.loop.create_future(),
        "ring_group_endpoint_ids": tuple(e.endpoint_id for e in endpoints[:2]),
        "defer_answer_projection": True,
        "invite": SimpleNamespace(caller="Caller"),
    }
    session.artifacts.pending_route = route
    session.update_metadata(ring_endpoint_ids=route["ring_group_endpoint_ids"])
    from custom_components.voip_stack.dial_fork import ForkAnswerGate

    gate = ForkAnswerGate(
        browser_legs=[
            BrowserLeg(e.name, e.endpoint_id, e.name, e.device_id)
            for e in endpoints[:2]
        ]
    )
    session.add_resource("fork_answer:" + session.call_id, gate, gate.close)
    for endpoint in endpoints[:2]:
        runtime.calls.claim_endpoint(
            session.call_id, endpoint.endpoint_id, role="group_candidate"
        )
        observe_phone_leg_projection(
            runtime.hass,
            runtime.calls,
            session,
            endpoint.endpoint_id,
            "ringing",
            leg_id="browser:" + endpoint.endpoint_id,
        )
    return session, route, gate, endpoints


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_declined_browser_pickup_retries_preserve_canonical_state(trunks):
    runtime = trunks
    session, route, gate, (a, b, _) = prepare_browser_group(runtime)
    set_pending_route_decision(
        runtime.hass,
        {"call_id": session.call_id, "endpoint_id": a.endpoint_id, "action": "decline"},
    )
    old_leg = session.legs["browser:" + a.endpoint_id]
    assert old_leg.state == "declined" and not a.active_call_id
    before = (
        route["ring_group_endpoint_ids"],
        session.metadata["ring_endpoint_ids"],
        tuple(gate.browser_legs),
    )
    await runtime.hass.async_block_till_done()
    from custom_components.voip_stack.websocket_api import CALL_EVENT

    events = []
    unsubscribe = runtime.hass.bus.async_listen(
        CALL_EVENT, callback(lambda event: events.append(event.data))
    )
    revision = session.revision
    for _ in range(3):
        call = ServiceCall(
            runtime.hass,
            "voip_stack",
            "pickup",
            {
                "device_id": a.device_id,
                "media_client_id": "owner",
                "answer_timeout": 0.1,
            },
        )
        with pytest.raises(ServiceValidationError):
            await _pickup_group_phone(call, a, session)
        await runtime.hass.async_block_till_done()
        assert session.revision == revision
        assert not [
            event for event in events if event.get("endpoint_id") == a.endpoint_id
        ]
        assert old_leg.state == "declined", (
            "failed pickup must restore the declined leg"
        )
        assert (
            route["ring_group_endpoint_ids"],
            session.metadata["ring_endpoint_ids"],
            tuple(gate.browser_legs),
        ) == before
        assert (
            a.endpoint_id not in session.endpoint_claims
            and not runtime.runtime.endpoints.get(a.endpoint_id).active_call_id
        )
        assert phone_states(runtime.hass)[a.endpoint_id]["state"] == "idle"
        assert phone_states(runtime.hass)[b.endpoint_id]["state"] == "ringing"
        assert not route["future"].done()
    unsubscribe()
    set_pending_route_decision(
        runtime.hass,
        {
            "call_id": session.call_id,
            "endpoint_id": b.endpoint_id,
            "action": "answer_ha",
        },
    )
    assert route["future"].result()["endpoint_id"] == b.endpoint_id
    fresh = runtime.calls.upsert("next-call", state="ringing")
    assert runtime.calls.claim_endpoint(fresh.call_id, a.endpoint_id)
    assert runtime.runtime.endpoints.get(a.endpoint_id).active_call_id == fresh.call_id


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize(
    "failure",
    [
        "resolve",
        "answer",
        "cancel",
        "other_winner",
        "new_generation",
        "projection",
        "concurrent_member",
    ],
)
@pytest.mark.parametrize("existing_leg", [False, True])
async def test_browser_candidate_rollback_is_attempt_and_generation_scoped(
    trunks, monkeypatch, failure, existing_leg
):
    from custom_components.voip_stack import (
        softphone_commands,
        softphone_answer,
        call_projection,
    )
    from custom_components.voip_stack.endpoint_session import LegPhase

    runtime = trunks
    session, route, gate, (_, b, c) = prepare_browser_group(runtime)
    prior_leg = None
    if existing_leg:
        observe_phone_leg_projection(
            runtime.hass,
            runtime.calls,
            session,
            c.endpoint_id,
            "cancelled",
            leg_id="browser:" + c.endpoint_id,
        )
        prior_leg = session.legs["browser:" + c.endpoint_id]
        previous_candidate = BrowserLeg(c.name, c.endpoint_id, c.name, c.device_id)
        gate.browser_legs.append(previous_candidate)
        route["ring_group_endpoint_ids"] = (
            *route["ring_group_endpoint_ids"],
            c.endpoint_id,
        )
        session.update_metadata(
            ring_endpoint_ids=(*session.metadata["ring_endpoint_ids"], c.endpoint_id)
        )
    events = []
    from custom_components.voip_stack.websocket_api import CALL_EVENT

    # Observe publication order on HA's loop, just like the WebSocket consumer.
    # An unmarked lambda is dispatched to the executor, whose threads can append
    # the terminal event before the earlier ringing event even after draining.
    unsubscribe = runtime.hass.bus.async_listen(
        CALL_EVENT, callback(lambda event: events.append(event.data))
    )
    before_members = route["ring_group_endpoint_ids"]
    before_metadata = session.metadata["ring_endpoint_ids"]
    resolve = softphone_commands.async_resolve_browser_call_command
    fresh = None

    async def fail_resolve(*args, **kwargs):
        nonlocal fresh
        # Fault injection at an existing async authorization boundary, not a
        # reproduction of the unrelated historical double-discovery hypothesis.
        if failure == "concurrent_member":
            d = PhoneEndpoint(
                endpoint_id="browser-D",
                name="D",
                device_id="device-D",
                kind="browser",
                availability="available",
            )
            runtime.runtime.endpoints.register(d)
            runtime.calls.claim_endpoint(
                session.call_id, d.endpoint_id, role="group_candidate"
            )
            gate.browser_legs.append(BrowserLeg("D", d.endpoint_id, "D", d.device_id))
            route["ring_group_endpoint_ids"] = (
                *route["ring_group_endpoint_ids"],
                d.endpoint_id,
            )
            session.update_metadata(
                ring_endpoint_ids=(
                    *session.metadata["ring_endpoint_ids"],
                    d.endpoint_id,
                )
            )
        if failure == "new_generation":
            await runtime.calls.terminate_call_wait(session.call_id, reason="cancelled")
            fresh = runtime.calls.upsert(session.call_id, state="ringing")
            runtime.calls.claim_endpoint(fresh.call_id, c.endpoint_id)
        elif failure == "other_winner":
            set_pending_route_decision(
                runtime.hass,
                {
                    "call_id": session.call_id,
                    "endpoint_id": b.endpoint_id,
                    "action": "answer_ha",
                },
            )
            runtime.calls.transition(session.call_id, state="in_call")
            session.update_metadata(dest_endpoint_id=b.endpoint_id)
        elif failure == "answer":
            return await resolve(*args, **kwargs)
        if failure == "cancel":
            raise asyncio.CancelledError
        raise ServiceValidationError("injected preparation failure")

    async def fail_answer(hass, call, command):
        softphone_commands.bind_service_call_controller(
            runtime.calls, session.call_id, call, endpoint_id=c.endpoint_id
        )
        raise ServiceValidationError("injected answer failure")

    monkeypatch.setattr(
        softphone_commands, "async_resolve_browser_call_command", fail_resolve
    )
    if failure == "answer":
        monkeypatch.setattr(softphone_answer, "async_answer_browser_call", fail_answer)
    if failure == "projection":
        publish = call_projection.publish_phone_projection
        first = True

        def fail_projection(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                raise ServiceValidationError("injected projection failure")
            return publish(*args, **kwargs)

        monkeypatch.setattr(
            call_projection, "publish_phone_projection", fail_projection
        )
    call = ServiceCall(
        runtime.hass,
        "voip_stack",
        "pickup",
        {"device_id": c.device_id, "media_client_id": "owner", "answer_timeout": 0.2},
    )
    with pytest.raises(
        asyncio.CancelledError if failure == "cancel" else ServiceValidationError
    ):
        await _pickup_group_phone(call, c, session)
    await runtime.hass.async_block_till_done()
    unsubscribe()
    if fresh is not None:
        assert fresh.generation != session.generation
        assert c.endpoint_id in fresh.endpoint_claims
        assert (
            runtime.runtime.endpoints.get(c.endpoint_id).active_call_id == fresh.call_id
        )
        return
    suffix = ("browser-D",) if failure == "concurrent_member" else ()
    assert route["ring_group_endpoint_ids"] == before_members + suffix
    assert session.metadata["ring_endpoint_ids"] == before_metadata + suffix
    if existing_leg:
        assert [
            leg for leg in gate.browser_legs if leg.endpoint_id == c.endpoint_id
        ] == [previous_candidate]
        assert (
            next(leg for leg in gate.browser_legs if leg.endpoint_id == c.endpoint_id)
            is previous_candidate
        )
    else:
        assert all(leg.endpoint_id != c.endpoint_id for leg in gate.browser_legs)
    assert c.endpoint_id not in session.endpoint_claims
    if prior_leg is None:
        assert "browser:" + c.endpoint_id not in session.legs
    else:
        assert session.legs["browser:" + c.endpoint_id] is prior_leg
        assert prior_leg.state == "cancelled"
    projected = [e for e in events if e.get("endpoint_id") == c.endpoint_id]
    assert projected and projected[-1]["state"] == (
        "cancelled" if existing_leg else "idle"
    )
    assert phone_states(runtime.hass)[c.endpoint_id]["state"] == "idle"
    assert session.legs["browser:" + b.endpoint_id].phase is not LegPhase.CLOSED
    assert "ha_context" not in session.metadata
    if failure != "other_winner":
        assert not route["future"].done()
        set_pending_route_decision(
            runtime.hass,
            {
                "call_id": session.call_id,
                "endpoint_id": b.endpoint_id,
                "action": "answer_ha",
            },
        )
        assert route["future"].result()["endpoint_id"] == b.endpoint_id


@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize(
    "ending", ["negative", "timeout", "caller_cancel", "source_terminated", "normal"]
)
async def test_failed_confirmation_keeps_answerers_and_allows_retry(
    hass, mixed, ending
):
    from custom_components.voip_stack.dial_fork import DialCandidate
    from custom_components.voip_stack.endpoint_session import TerminationIntent

    session = EndpointCallSession("group", 1)
    route = hass.loop.create_future()
    browsers = [BrowserLeg("A", "A", "A", "device-a")]
    candidates, payloads, _ = build_ring_group_fork(
        sip_port=5060,
        route_future=route,
        attempts=[],
        browser_legs=browsers,
        preflight_failures=[],
    )
    closed = []

    async def sip_wait():
        await asyncio.Future()

    async def sip_close(_mode):
        closed.append("sip")

    if mixed:
        candidates.append(DialCandidate("sip:ordinary", sip_wait, sip_close, tier=1))
    commits = []
    task = asyncio.create_task(
        run_owned_ring_group_fork(
            session,
            candidates,
            payloads,
            invite=None,
            attempts=[],
            browser_legs=browsers,
            on_leg_state=None,
            commit_winner=lambda c, _: commits.append(c.candidate_id) or True,
            overall_timeout=0.5,
            step_timeout=0.03,
        )
    )
    try:
        await until(lambda: bool(session.resources))
        gate = session.resources[0].value
        if ending == "normal":
            route.set_result({"action": "answer_ha", "endpoint_id": "A"})
            result, _ = await task
            assert result.winner.candidate_id == "browser:route-control"
            return
        ticket = gate.submit("picker")
        assert await asyncio.wait_for(asyncio.shield(ticket.selected), 0.2)
        assert not closed and not route.done() and not commits
        if ending == "caller_cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not ticket.accepted and not commits
            return
        if ending == "source_terminated":
            await session.terminate(TerminationIntent("cancelled"))
            result, _ = await asyncio.wait_for(task, 0.2)
            assert (
                result.outcome.disposition is DialDisposition.CANCELLED and not commits
            )
            return
        if ending == "timeout":
            # The preparation owner reports its timeout before the fork's
            # overall deadline; confirmation must not retire other answers.
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.02):
                    await asyncio.Future()
            ticket.finish(False)
        if ending == "negative":
            ticket.finish(False)
        await until(lambda: ticket.committed.done())
        assert not ticket.accepted and not route.done() and not closed
        next_ticket = gate.submit("second-picker")
        assert await asyncio.wait_for(asyncio.shield(next_ticket.selected), 0.2)
        next_ticket.finish(True)
        result, _ = await task
        assert next_ticket.accepted and result.winner.candidate_id == "external:pickup"
        assert len(commits) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("strategy", [ForkStrategy.PARALLEL, ForkStrategy.SEQUENTIAL])
async def test_media_candidate_confirmation_precedes_loser_cleanup(strategy):
    from custom_components.voip_stack.dial_fork import (
        DialCandidate,
        DialOutcome,
        DialForkController,
    )

    session = EndpointCallSession("confirm", 1)
    closed = []

    async def answer():
        return DialOutcome(DialDisposition.ANSWERED)

    async def close(mode):
        closed.append(mode)

    async def reject():
        assert not closed
        return False

    candidates = [
        DialCandidate("first", answer, close, confirm=reject),
        DialCandidate("second", answer, close, order=1),
    ]
    result = await DialForkController(session, candidates, strategy=strategy, step_timeout=.1).run(
        lambda *_: True
    )
    assert result.winner.candidate_id == "second"


async def test_browser_only_group_obeys_overall_deadline(hass):
    session = EndpointCallSession("timeout", 1)
    route = hass.loop.create_future()
    browsers = [BrowserLeg("A", "A", "A", "a")]
    candidates, payloads, _ = build_ring_group_fork(
        sip_port=5060,
        route_future=route,
        attempts=[],
        browser_legs=browsers,
        preflight_failures=[],
    )
    result, gate = await run_owned_ring_group_fork(
        session,
        candidates,
        payloads,
        invite=None,
        attempts=[],
        browser_legs=browsers,
        on_leg_state=None,
        commit_winner=lambda *_: pytest.fail("deadline cannot commit a winner"),
        overall_timeout=0.03,
    )
    assert result.outcome.disposition is DialDisposition.TIMEOUT
    assert gate.closed and route.cancelled() and not session.resources


async def test_browser_answer_ready_before_confirmation_deadline_is_not_discarded(hass):
    session = EndpointCallSession("ready-answer", 1)
    route = hass.loop.create_future()
    browsers = [BrowserLeg("A", "A", "A", "a")]
    candidates, payloads, _ = build_ring_group_fork(
        sip_port=5060,
        route_future=route,
        attempts=[],
        browser_legs=browsers,
        preflight_failures=[],
    )
    task = asyncio.create_task(
        run_owned_ring_group_fork(
            session,
            candidates,
            payloads,
            invite=None,
            attempts=[],
            browser_legs=browsers,
            on_leg_state=None,
            commit_winner=lambda *_: True,
            overall_timeout=0.05,
        )
    )
    try:
        await until(lambda: bool(session.resources))
        ticket = session.resources[0].value.submit("picker")
        assert await asyncio.shield(ticket.selected)
        route.set_result({"action": "answer_ha", "endpoint_id": "A"})
        result, _ = await task
        assert (
            result.winner is not None
            and result.winner.candidate_id == "browser:route-control"
        )
        assert not ticket.accepted
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("strategy", [ForkStrategy.PARALLEL, ForkStrategy.SEQUENTIAL])
@pytest.mark.parametrize("composition,ordinary_answers", [("mixed", True), ("mixed", False), ("browser", False)])
@pytest.mark.parametrize("action", ["forward", "bridge", "cancel"])
@pytest.mark.parametrize("failure", ["rejected", "deadline"])
async def test_ready_route_control_precedes_answer_after_failed_pickup(
    trunks, strategy, composition, ordinary_answers, action, failure, monkeypatch
):
    from unittest.mock import AsyncMock, Mock
    from custom_components.voip_stack.core.sip import parse_sip_uri

    runtime = trunks
    session, route, old_gate, _ = prepare_browser_group(runtime)
    await old_gate.close()
    session.release_resource("fork_answer:" + session.call_id, value=old_gate)
    # No media projection is needed to exercise the actual route decision.
    route["invite"] = None
    ready = asyncio.Event()
    async def invite(**kwargs):
        await ready.wait()
        return "in_call"
    client = SimpleNamespace(invite=AsyncMock(side_effect=invite), dialog=object(),
        last_sip_status_code=200, dialog_ids=SimpleNamespace(call_id="ordinary"),
        terminate=AsyncMock(), close=AsyncMock())
    from custom_components.voip_stack import media_ports
    release_pair = Mock(wraps=media_ports.release_sip_rtp_port_pair)
    monkeypatch.setattr(media_ports, "release_sip_rtp_port_pair", release_pair)
    ports = media_ports.RtpPortReservation.allocate(runtime.hass) if composition == "mixed" else None
    attempts = [OutboundLeg("ordinary", parse_sip_uri("sip:ordinary@127.0.0.1:5090"), client, ports,
                            candidate_id="sip:ordinary", endpoint_id="ordinary")] if composition == "mixed" else []
    candidates, payloads, decision = build_ring_group_fork(
        sip_port=5060, route_future=route["future"], attempts=attempts,
        browser_legs=old_gate.browser_legs, preflight_failures=[])
    commits = []
    task = asyncio.create_task(run_owned_ring_group_fork(
        session, candidates, payloads, invite=None, attempts=attempts,
        browser_legs=old_gate.browser_legs, on_leg_state=None,
        commit_winner=lambda candidate, _: commits.append(candidate.candidate_id) or True,
        strategy=strategy, step_timeout=.1 if failure == "deadline" else 1,
        overall_timeout=.1 if failure == "deadline" else 1))
    try:
        await until(lambda: runtime.calls.resource_for(session.call_id, "fork_answer") is not None)
        gate = runtime.calls.resource_for(session.call_id, "fork_answer")
        branch_task = next(t for t in asyncio.all_tasks() if t.get_name() == "voip-dial-fork-branches")
        ticket = gate.submit("picker")
        assert await asyncio.shield(ticket.selected)
        assert not ticket.committed.done()
        set_pending_route_decision(runtime.hass, {"call_id": session.call_id, "action": action,
            "destination": "Alternate" if action != "cancel" else "", "endpoint_id": "caller"})
        await until(lambda: bool(decision))
        assert decision["action"] == action
        if ordinary_answers:
            ready.set()
            await until(branch_task.done)
            assert branch_task.result().winner.candidate_id == "sip:ordinary"
        if failure == "rejected":
            ticket.finish(False)
        result, _ = await task
        expected = DialDisposition.SOURCE_CANCELLED if action == "cancel" else DialDisposition.REROUTE
        assert result.outcome.disposition is expected, "accepted route control must precede the prepared SIP winner"
        assert not commits and result.winner is None and not ticket.accepted
        if attempts:
            # Pending-leg cancellation may request idempotent transport cleanup
            # again. The actual reservation must nevertheless be released once.
            assert client.close.await_count >= 1
            if ordinary_answers:
                client.terminate.assert_awaited_once()
                client.close.assert_awaited_once()
            assert ports.released
            release_pair.assert_called_once()
            assert not runtime.runtime.rtp_port_pool.get("used")
        assert runtime.calls.resource_for(session.call_id, "fork_answer") is None
        assert not any(
            pending.get_name().startswith(("voip-dial-fork", "voip-fork-confirm"))
            for pending in asyncio.all_tasks()
            if not pending.done()
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("ending", ["cancel_task", "terminate", "normal"])
async def test_prepared_sip_branch_is_owned_during_external_confirmation(ending):
    from custom_components.voip_stack.dial_fork import DialForkController, DialCandidate, DialOutcome, ForkAnswerGate
    from custom_components.voip_stack.endpoint_session import TerminationIntent
    session = EndpointCallSession("prepared", 1)
    ready = asyncio.Event()
    closed = []
    async def dial():
        await ready.wait()
        return DialOutcome(DialDisposition.ANSWERED)
    async def close(mode):
        closed.append(mode)
    gate = ForkAnswerGate()
    commits = []
    task = asyncio.create_task(DialForkController(session, [
        DialCandidate("sip:ordinary", dial, close),
        DialCandidate("external", gate.dial, gate.reject, control=True, confirm=gate.confirm),
    ]).run(lambda candidate, _: commits.append(candidate.candidate_id) or True))
    try:
        ticket = gate.submit("picker")
        assert await asyncio.shield(ticket.selected)
        branch_task = next(t for t in asyncio.all_tasks() if t.get_name() == "voip-dial-fork-branches")
        ready.set()
        await until(branch_task.done)
        if ending == "cancel_task":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif ending == "terminate":
            await session.terminate(TerminationIntent("cancelled"))
            result = await task
            assert result.outcome.disposition is DialDisposition.CANCELLED
        else:
            ticket.finish(False)
            result = await asyncio.wait_for(task, .5)
            assert result.winner.candidate_id == "sip:ordinary"
        assert commits == (["sip:ordinary"] if ending == "normal" else [])
        assert len(closed) == (0 if ending == "normal" else 1)
        assert not ticket.accepted
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await gate.close()
