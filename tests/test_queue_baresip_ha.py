"""Independent registered baresip callers qualify per-call queue membership."""

import asyncio
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest
from homeassistant.config_entries import ConfigSubentry

from custom_components.voip_stack.call_queues import QueuePolicy
from custom_components.voip_stack.core import sip
from custom_components.voip_stack.sip_capture import SipCapture
from .test_queue_call_ha import queue_lab as queue_lab
from .test_sip_capabilities_ha import wait_until
from .test_sip_phone_transfers_ha import (
    registered_phones as registered_phones,
    assert_peer_audio_tones,
)
from .test_trunk_calls_ha import lab as lab, trunks as trunks

pytestmark = [pytest.mark.ha, pytest.mark.skipif(shutil.which("baresip") is None, reason="baresip is not installed")]


@pytest.fixture
def isolated_baresip_output(tmp_path, monkeypatch):
    from tools import ha_softphone_matrix
    monkeypatch.setattr(ha_softphone_matrix, "TEST_CAPTURE_DIR", tmp_path / "baresip-runtime")


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("mode", ["other_available", "caller_only", "all_logged_out"])
async def test_real_baresip_queue_excludes_its_caller_and_obeys_no_agent_wait(
    isolated_baresip_output, registered_phones, queue_lab, tmp_path, monkeypatch, mode,
):
    lab = queue_lab
    caller = registered_phones[0]
    expected = "transfer_b" if mode == "other_available" else "transfer_c" if mode == "caller_only" else ""
    evidence = Path(os.environ.get("VOIP_QUEUE_BARESIP_EVIDENCE_DIR", str(tmp_path / "evidence"))) / mode
    evidence.mkdir(parents=True, exist_ok=True)
    lab.manager.definitions[lab.identity] = replace(lab.manager.definitions[lab.identity],
        members=("transfer_a", "transfer_b"))
    lab.manager.policies[lab.identity] = QueuePolicy(fallback_no_agents="transfer_c" if mode == "caller_only" else "")
    lab.manager.agent_states[lab.identity] = {
        "transfer_a": "logged_out" if mode == "all_logged_out" else "available",
        "transfer_b": "available" if mode == "other_available" else "logged_out",
    }
    for name in ("transfer_a", "transfer_b"):
        lab.runtime.endpoints.update(name, queues=("Reception",))
    # Real REGISTER shutdown refreshes the directory and needs its real owner.
    from custom_components.voip_stack.group_config import normalize_group_data
    entry = lab.hass.config_entries.async_entries("voip_stack")[0]
    lab.manager.entry = entry
    group = normalize_group_data({"kind": "queue", "name": "Reception", "extension": "401",
        "origin": "manual", "policy": asdict(lab.manager.policies[lab.identity])}, identity=lab.identity)
    lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(subentry_type="group",
        title="Reception", unique_id="group:" + lab.identity, data=group))
    capture = SipCapture()
    observed = []
    record = capture.record

    def observe(data, local, remote, tcp, outgoing):
        try:
            message = sip.parse_message(data)
        except sip.SipError:
            pass
        else:
            observed.append((lab.manager.clock(), outgoing, message))
        record(data, local, remote, tcp, outgoing)

    monkeypatch.setattr(capture, "record", observe)
    capture.start(duration=20)
    session = None
    result = {"mode": mode, "binary": shutil.which("baresip"),
        "binary_sha256": hashlib.sha256(Path(shutil.which("baresip")).read_bytes()).hexdigest(),
        "policy": {"max_wait": 60, "no_agent_wait": 1}, "passed": False}
    try:
        started = lab.manager.clock()
        await asyncio.to_thread(caller.command, "/dial 401")
        await asyncio.to_thread(caller.wait_for, "Call established", 3)
        call_id = lab.runtime.endpoints.require("transfer_a").active_call_id
        session = lab.calls.get_session(call_id)
        assert session is not None and session.metadata["source_endpoint_id"] == "transfer_a"
        result["call_id"] = call_id
        result["source_pid"] = caller.proc.pid
        if expected:
            target = registered_phones[1 if expected == "transfer_b" else 2]
            await asyncio.to_thread(target.wait_for, "Call established", 3)
            elapsed = lab.manager.clock() - started
            await wait_until(lambda: session.owner == "bridge")
            forward = lab.calls.task_for(call_id, "forward")
            if forward is not None:
                await asyncio.wait_for(asyncio.shield(forward), 2)
            relay = lab.calls.resource_for(call_id, "relay")
            await wait_until(lambda: relay.left_tx_packets >= 15 and relay.right_tx_packets >= 15)
            result["media"] = {"left_tx_packets": relay.left_tx_packets, "right_tx_packets": relay.right_tx_packets}
            assert elapsed < 3
            if mode == "caller_only":
                assert .8 <= elapsed
            await asyncio.to_thread(caller.hangup)
            await asyncio.wait_for(session.terminated.wait(), 3)
        else:
            await asyncio.wait_for(session.terminated.wait(), 3)
            elapsed = lab.manager.clock() - started
            assert .8 <= elapsed < 3
            await asyncio.to_thread(caller.wait_for, "session closed", 2)
        offers = [sip.parse_sip_uri(message.uri).user for _time, outgoing, message in observed
                  if outgoing and message.method == "INVITE"]
        assert "transfer_a" not in offers
        assert set(offers) == ({expected} if expected else set())
        if mode != "other_available":
            exit_event, = [event for event in lab.queue_events
                           if event["event_type"] == "exited" and event.get("call_id") == call_id]
            assert exit_event["reason"] == "queue_no_agents"
            assert exit_event["outcome"] == "no_agents"
            result["exit"] = exit_event
        result.update(elapsed_seconds=elapsed, offers=offers)
        for peer in registered_phones:
            await asyncio.to_thread(peer.close)
        if expected:
            assert_peer_audio_tones(tmp_path, (("transfer_a", 550 if mode == "other_available" else 650),))
            result["independent_decoded_tone_hz"] = 550 if mode == "other_available" else 650
        listener = lab.calls.component("udp_listener").udp_server.endpoint
        assert not lab.calls.sessions and not lab.calls.endpoint_claims_snapshot() and not lab.calls.relays_snapshot()
        assert not lab.manager.waiting and not lab.manager.reservations
        assert not listener._dialog_queues and not listener._reused_dialog_keys
        assert not lab.runtime.rtp_port_pool.get("used")
        assert listener.transport is not None and not listener.transport.is_closing()
        result["resources_zero"] = result["passed"] = True
    finally:
        if session is not None and session.live:
            from custom_components.voip_stack.endpoint_session import TerminationIntent
            await lab.calls.request_termination(session.call_id, TerminationIntent("test_cleanup"))
        for peer in registered_phones:
            if peer.proc.poll() is None:
                await asyncio.to_thread(peer.close)
        result["cleanup_resources_zero"] = not (lab.calls.sessions or lab.calls.endpoint_claims_snapshot()
            or lab.calls.relays_snapshot() or lab.manager.waiting or lab.manager.reservations
            or lab.runtime.rtp_port_pool.get("used"))
        assert result["cleanup_resources_zero"]
        capture.stop()
        (evidence / "sip-application-io.pcap").write_bytes(capture.buffer)
        capture.clear()
        for index, peer in enumerate(registered_phones):
            (evidence / f"baresip-{index}.log").write_text(peer.read())
        for wav in tmp_path.glob("transfer_*/*-dec.wav"):
            shutil.copyfile(wav, evidence / f"{wav.parent.name}-{wav.name}")
        (evidence / "RESULT.json").write_text(json.dumps(result, indent=2, default=str) + "\n")


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": ("transfer_b",)}], indirect=True)
@pytest.mark.parametrize("boundary", [
    "caller_bye_during_offer", "caller_bye_before_offer", "queue_to_queue",
    "agent_logged_out_during_offer", "agent_paused_during_offer", "shared_operator_fifo",
])
async def test_baresip_queue_boundary_and_immediate_endpoint_reuse(
    isolated_baresip_output, registered_phones, queue_lab, tmp_path, monkeypatch, boundary,
):
    """Exercise independent peers at queue ownership changes, then redial at idle."""
    from custom_components.voip_stack.endpoint_session import TerminationIntent
    from custom_components.voip_stack.group_config import normalize_group_data
    from .test_queue_call_ha import wait_for_bridge

    lab = queue_lab
    peers = registered_phones
    names = ("transfer_a", "transfer_b", "transfer_c")
    entry = lab.hass.config_entries.async_entries("voip_stack")[0]
    lab.manager.entry = entry
    evidence = Path(os.environ.get("VOIP_QUEUE_BARESIP_EVIDENCE_DIR", str(tmp_path / "evidence"))) / boundary
    evidence.mkdir(parents=True, exist_ok=True)
    policy = QueuePolicy(max_wait=6, ring_timeout=2, no_agent_wait=.25,
        retry_delay=.1, auto_pause_after=1,
        fallback_no_agents="Support" if boundary == "queue_to_queue" else
        "transfer_c" if boundary.startswith("agent_") else "")
    members = ("transfer_a", "transfer_c") if boundary == "queue_to_queue" else names
    lab.manager.definitions[lab.identity] = replace(lab.manager.definitions[lab.identity], members=members)
    lab.manager.policies[lab.identity] = policy
    lab.manager.agent_states[lab.identity] = {name: "available" if name in {"transfer_a", "transfer_b"}
        else "logged_out" for name in members}
    if boundary == "caller_bye_before_offer":
        lab.manager.agent_states[lab.identity]["transfer_b"] = "paused"
    if boundary == "shared_operator_fifo":
        lab.manager.agent_states[lab.identity]["transfer_a"] = "logged_out"
        lab.manager.policies[lab.identity] = replace(policy, ring_timeout=5)
    if boundary == "queue_to_queue":
        second_id = "c2bdd71d87f340b3b79c36e25ce99035"
        lab.manager.definitions[second_id] = replace(lab.manager.definitions[lab.identity],
            queue_id=second_id, name="Support", members=("transfer_a", "transfer_b"), extension="402")
        lab.manager.policies[second_id] = replace(policy, fallback_no_agents="")
        lab.manager.agent_states[second_id] = {"transfer_a": "available", "transfer_b": "available"}
    for name in names:
        memberships = tuple(definition.name for definition in lab.manager.definitions.values() if name in definition.members)
        lab.runtime.endpoints.update(name, queues=memberships)
    for queue_id, definition in lab.manager.definitions.items():
        group = normalize_group_data({"kind": "queue", "name": definition.name,
            "extension": definition.extension, "origin": "manual",
            "policy": asdict(lab.manager.policies[queue_id])}, identity=queue_id)
        lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(subentry_type="group",
            title=definition.name, unique_id="group:" + queue_id, data=group))
    known = {row.id for row in lab.roster}
    lab.roster.extend(row for row in lab.manager.roster_entries() if row.id not in known)
    capture = SipCapture()
    observed = []
    offers_changed = asyncio.Event()
    timeline = []
    record = capture.record

    def observe(data, local, remote, tcp, outgoing):
        try:
            message = sip.parse_message(data)
        except sip.SipError:
            pass
        else:
            observed.append((outgoing, message))
            timeline.append({"at": lab.manager.clock(), "outgoing": outgoing,
                "method": message.method, "status": message.status_code,
                "call_id": message.header("Call-ID"), "cseq": message.header("CSeq")})
            if outgoing and message.method == "INVITE":
                offers_changed.set()
        record(data, local, remote, tcp, outgoing)

    monkeypatch.setattr(capture, "record", observe)
    capture.start(duration=40)
    result = {"boundary": boundary, "passed": False, "calls": [], "media": [],
        "timeline": timeline, "snapshots": []}
    tones = []

    def offers(name):
        return [message for outgoing, message in observed
                if outgoing and message.method == "INVITE" and sip.parse_sip_uri(message.uri).user == name]

    def received(call_id, *, method="", status=None):
        return [message for outgoing, message in observed if not outgoing and message.header("Call-ID") == call_id
                and (not method or message.method == method) and (status is None or message.status_code == status)]

    async def start(index=0, target="401"):
        previous = set(lab.calls.sessions)
        await asyncio.to_thread(peers[index].command, "/dial " + target)
        await wait_until(lambda: lab.runtime.endpoints.require(names[index]).active_call_id
                         and lab.runtime.endpoints.require(names[index]).active_call_id not in previous)
        session = lab.calls.get_session(lab.runtime.endpoints.require(names[index]).active_call_id)
        result["calls"].append(session.call_id)
        return session

    async def offered(name="transfer_b", count=1, *, timeout=2):
        async def wait_offer():
            while len(offers(name)) < count:
                offers_changed.clear()
                await offers_changed.wait()
        await asyncio.wait_for(wait_offer(), timeout)
        message = offers(name)[count - 1]
        await wait_until(lambda: bool(received(message.header("Call-ID"), status=180)))
        return message

    def snapshot(label, session):
        no_agent_since = None
        for task in session.tasks:
            for frame in task.get_stack():
                if frame.f_code.co_name == "run" and "no_agent_since" in frame.f_locals:
                    no_agent_since = frame.f_locals["no_agent_since"]
        forward = lab.calls.task_for(session.call_id, "forward")
        result["snapshots"].append({"label": label, "at": lab.manager.clock(),
            "call_id": session.call_id, "owner": session.owner,
            "source_endpoint_id": session.metadata.get("source_endpoint_id"),
            "agent_states": dict(lab.manager.agent_states[lab.identity]),
            "no_agent_since": no_agent_since,
            "forward_pending": forward is not None and not forward.done(),
            "pending_source_invite": session.artifacts.pending_invite is not None,
            "tickets": [{"call_id": ticket.call_id, "reserved": ticket.reserved,
                         "closed": ticket.closed} for ticket in lab.manager.waiting.get(lab.identity, ())]})

    async def answered(session, *, accept=False):
        if accept:
            await asyncio.to_thread(peers[1].command, "/accept")
        await wait_until(lambda: bool(received(session.call_id, method="ACK")))
        await wait_for_bridge(lab, session.call_id)
        relay = lab.calls.resource_for(session.call_id, "relay")
        await wait_until(lambda: relay.left_tx_packets >= 15 and relay.right_tx_packets >= 15)
        result["media"].append({"call_id": session.call_id,
            "left_tx_packets": relay.left_tx_packets, "right_tx_packets": relay.right_tx_packets})

    async def hangup(index, session):
        await asyncio.to_thread(peers[index].hangup)
        await asyncio.wait_for(session.terminated.wait(), 2)
        assert received(session.call_id, method="BYE")

    def clean():
        listener = lab.calls.component("udp_listener").udp_server.endpoint
        assert not lab.calls.sessions and not lab.calls.endpoint_claims_snapshot() and not lab.calls.relays_snapshot()
        assert not lab.manager.waiting and not lab.manager.reservations
        assert not listener._dialog_queues and not listener._reused_dialog_keys
        assert not lab.runtime.rtp_port_pool.get("used")
        assert listener.transport is not None and not listener.transport.is_closing()

    try:
        first = await start()
        await wait_until(lambda: bool(received(first.call_id, method="ACK")))
        assert first.metadata["source_endpoint_id"] == "transfer_a"
        if boundary == "caller_bye_before_offer":
            assert not any(outgoing and message.method == "INVITE" for outgoing, message in observed)
            await hangup(0, first)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(offers_changed.wait(), .35)
        elif boundary == "queue_to_queue":
            offer = await offered()
            assert not offers("transfer_a") and not offers("transfer_c")
            await answered(first, accept=True)
            departure, = [event for event in lab.queue_events if event["event_type"] == "exited"
                           and event["queue_id"] == lab.identity and event.get("call_id") == first.call_id]
            assert departure["reason"] == "queue_no_agents" and departure["destination"] == "Support"
            assert departure["disposition"] == "forwarded"
            assert any(event["event_type"] == "answered" and event["queue_id"] == second_id for event in lab.queue_events)
            tones.append(("transfer_a", "401", 550))
            await hangup(0, first)
        else:
            offer = await offered()
            assert not offers("transfer_a") and not offers("transfer_c")
            if boundary == "caller_bye_during_offer":
                await hangup(0, first)
                await wait_until(lambda: bool(received(offer.header("Call-ID"), status=487)))
                assert any(outgoing and message.method == "CANCEL" and message.header("Call-ID") == offer.header("Call-ID")
                           for outgoing, message in observed)
                assert any(outgoing and message.method == "ACK" and message.header("Call-ID") == offer.header("Call-ID")
                           for outgoing, message in observed)
                assert lab.manager.agent_states[lab.identity]["transfer_b"] == "available"
                assert lab.manager.agent_stats[lab.identity]["transfer_b"]["consecutive_misses"] == 0
            elif boundary.startswith("agent_"):
                state = "logged_out" if "logged_out" in boundary else "paused"
                snapshot("agent_180", first)
                forward = lab.calls.task_for(first.call_id, "forward")
                assert forward is not None
                lab.manager.set_agent_state(lab.identity, "transfer_b", state)
                snapshot("agent_intent_changed", first)
                budget = policy.ring_timeout + policy.retry_delay + policy.no_agent_wait + .75
                result["fallback_budget"] = {"ring_timeout": policy.ring_timeout,
                    "retry_delay": policy.retry_delay, "no_agent_wait": policy.no_agent_wait,
                    "scheduling_margin": .75, "total": budget}
                deadline = asyncio.get_running_loop().time() + budget
                await asyncio.to_thread(peers[1].hangup)
                await wait_until(lambda: bool(received(offer.header("Call-ID"), status=486)))
                assert any(outgoing and message.method == "ACK" and message.header("Call-ID") == offer.header("Call-ID")
                           for outgoing, message in observed)
                snapshot("agent_486_ack", first)
                await asyncio.wait_for(asyncio.shield(forward), budget)
                snapshot("offer_forward_released", first)
                assert first.live and first.metadata["source_endpoint_id"] == "transfer_a"
                await offered("transfer_c", timeout=max(.01, deadline - asyncio.get_running_loop().time()))
                snapshot("fallback_invite", first)
                await answered(first)
                snapshot("fallback_committed", first)
                assert len(offers("transfer_b")) == 1
                assert lab.manager.agent_states[lab.identity]["transfer_b"] == state
                tones.append(("transfer_a", "401", 650))
                await hangup(0, first)
            else:
                second = await start(index=2)
                await wait_until(lambda: bool(received(second.call_id, method="ACK")))
                assert [ticket.call_id for ticket in lab.manager.waiting[lab.identity]] == [first.call_id, second.call_id]
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(second.terminated.wait(), .4)
                assert len(offers("transfer_b")) == 1 and second.live
                await answered(first, accept=True)
                tones.append(("transfer_a", "401", 550))
                await hangup(0, first)
                await offered(count=2)
                await answered(second, accept=True)
                tones.append(("transfer_c", "401", 550))
                await hangup(2, second)
        clean()
        # No grace sleep: reuse the same caller and operator after the barrier.
        count = len(offers("transfer_b")) + 1
        recovery = await start(target="transfer_b")
        await offered(count=count)
        await answered(recovery, accept=True)
        tones.append(("transfer_a", "transfer_b", 550))
        await hangup(0, recovery)
        clean()
        for peer in peers:
            await asyncio.to_thread(peer.close)
        for index, (name, target, frequency) in enumerate(tones):
            selection = tmp_path / ("tone-proof-" + str(index))
            selected = selection / name
            selected.mkdir(parents=True)
            for wav in (tmp_path / name).glob("*-dec.wav"):
                if "=>sip:" + target + "@" in wav.name:
                    shutil.copyfile(wav, selected / wav.name)
            assert_peer_audio_tones(selection, ((name, frequency),))
        result["passed"] = True
        result["tone_proofs"] = tones
    finally:
        for session in tuple(lab.calls.sessions.values()):
            await lab.calls.request_termination(session.call_id, TerminationIntent("test_cleanup"))
        for peer in peers:
            if peer.proc.poll() is None:
                await asyncio.to_thread(peer.close)
        clean()
        capture.stop()
        (evidence / "sip-application-io.pcap").write_bytes(capture.buffer)
        capture.clear()
        result["observed"] = [{"outgoing": outgoing, "method": message.method,
            "status": message.status_code, "call_id": message.header("Call-ID"),
            "uri": message.uri} for outgoing, message in observed]
        for index, peer in enumerate(peers):
            (evidence / f"baresip-{index}.log").write_text(peer.read())
        for wav in tmp_path.glob("transfer_*/*-dec.wav"):
            shutil.copyfile(wav, evidence / f"{wav.parent.name}-{wav.name}")
        (evidence / "RESULT.json").write_text(json.dumps(result, indent=2) + "\n")
