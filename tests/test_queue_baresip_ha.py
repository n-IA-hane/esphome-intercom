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
