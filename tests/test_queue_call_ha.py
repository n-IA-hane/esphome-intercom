"""Queue admission reaches the existing SIP ring-all bridge and teardown."""

import asyncio
from dataclasses import replace
import socket
import struct
from types import SimpleNamespace

import pytest

from custom_components.voip_stack.call_queues import QueueDefinition, QueueManager, QueuePolicy
from custom_components.voip_stack.core import rtp
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol
from .test_trunk_calls_ha import lab as lab, trunks as trunks
from .test_sip_capabilities_ha import wait_until
from .test_udp_outbound_flow_ha import wire_lab as wire_lab, register_phone

pytestmark = pytest.mark.ha


@pytest.fixture
async def queue_lab(trunks, monkeypatch):
    lab = trunks
    from custom_components.voip_stack import call_forwarder
    monkeypatch.setattr(call_forwarder, "_roster_from_peers", lambda *_args: lab.roster)
    lab.calls.bind_endpoint_registry(lab.runtime.endpoints)
    for endpoint in lab.runtime.endpoints.endpoints:
        lab.runtime.endpoints.update(endpoint.endpoint_id, availability="available")
    manager = QueueManager(lab.hass, SimpleNamespace(runtime_data=lab.runtime,
        entry_id=lab.hass.config_entries.async_entries("voip_stack")[0].entry_id))
    identity = "d5cf849db68449f49f03552eeab34511"
    manager.definitions[identity] = QueueDefinition(identity, "Reception", ("sip:bedroom0", "sip:bedroom1"), "401")
    manager.policies[identity] = QueuePolicy(max_wait=5, ring_timeout=1, retry_delay=0.1, no_agent_wait=1, wrap_up=1)
    manager.agent_states[identity] = {member: "available" for member in manager.definitions[identity].members}
    lab.runtime.queues = manager
    contact = manager.roster_entries()[0]
    lab.roster.append(contact)
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="source", name="Source", username="source", kind=EndpointKind.SIP_ACCOUNT,
                                                availability="available"))
    lab.roster.append(RosterEntry("source", address="127.0.0.2", metadata={"endpoint_id": "source"}))
    clients, sockets = [], []

    async def caller(name="Source", address="127.0.0.2"):
        audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        audio.bind((address, 0))
        audio.setblocking(False)
        sockets.append(audio)
        client = SipCallClient(local_ip=address, local_name=name, local_uri_user=name.lower(), local_sip_port=5060,
                               local_rtp_port=audio.getsockname()[1], supported_formats=[AudioFormat(16000, "s16le", 1, 20)])
        client.transport, client.protocol = await lab.hass.loop.create_datagram_endpoint(
            lambda: SipDatagramQueueProtocol(client.queue), local_addr=(address, 0))
        client.local_sip_port = client.transport.get_extra_info("sockname")[1]
        clients.append(client)
        return client, audio

    from custom_components.voip_stack.const import QUEUE_EVENT
    events = []
    unsubscribe = lab.hass.bus.async_listen(QUEUE_EVENT, lambda event: events.append(dict(event.data)))
    yield SimpleNamespace(**vars(lab), manager=manager, identity=identity, contact=contact, caller=caller, queue_events=events)
    unsubscribe()
    for client in clients:
        await client.terminate()
        await client.close()
    for audio in sockets:
        audio.close()
    await manager.close()


async def dial_queue(lab):
    client, audio = await lab.caller()
    result = await client.invite(target="401", remote_host="127.0.0.1", remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
    if result == "ringing":
        result = await client.wait_for_final(timeout=3)
    assert result == "in_call"
    return client, audio


async def wait_for_bridge(lab, call_id):
    await wait_until(lambda: lab.calls.get_session(call_id).owner == "bridge")
    # register_bridge assigns the media owner before its async media/start and
    # answer commit. Assertions about the final peer need the forward barrier.
    task = lab.calls.task_for(call_id, "forward")
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), 3)
    session = lab.calls.get_session(call_id)
    assert session is not None and session.owner == "bridge"
    return session


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("inject_unmatched", [False, True], ids=["ordinary-registrar", "ignore-foreign-branch-and-source"])
async def test_public_browser_queue_call_uses_main_udp_for_provisional_and_complete_cancel(queue_lab, wire_lab, monkeypatch, inject_unmatched):
    from dataclasses import asdict
    from homeassistant.config_entries import ConfigSubentry
    from unittest.mock import AsyncMock
    from custom_components.voip_stack import softphone_originate
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.endpoint_session import TerminationIntent
    from custom_components.voip_stack.phone_state import phone_states, phone_state_dto
    from custom_components.voip_stack.roster import dump_roster_json
    from custom_components.voip_stack.group_config import normalize_group_data
    lab, wire = queue_lab, wire_lab
    assert wire.lab is not None and wire.lab.runtime is lab.runtime
    entry = lab.hass.config_entries.async_entries("voip_stack")[0]
    lab.manager.entry = entry
    group = normalize_group_data({"kind": "queue", "name": "Reception", "extension": "401",
        "policy": asdict(lab.manager.policies[lab.identity])}, identity=lab.identity)
    lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(subentry_type="group",
        title=group["name"], unique_id=f"group:{lab.identity}", data=group))
    peer = PhoneEndpoint(endpoint_id="outbound-phone", name="Wire phone", username="outbound-phone",
        kind=EndpointKind.SIP_ACCOUNT, availability="available", queues=("Reception",))
    lab.runtime.endpoints.register(peer)
    lab.manager.refresh()
    await register_phone(wire, outbound=False)
    lab.manager.definitions[lab.identity] = replace(lab.manager.definitions[lab.identity], members=(peer.endpoint_id,))
    lab.manager.agent_states[lab.identity] = {peer.endpoint_id: "available"}
    source = PhoneEndpoint(endpoint_id="browser-queue-wire", name="Test", username="Test",
        device_id="browser-queue-wire-device", kind=EndpointKind.BROWSER, availability="available")
    lab.runtime.endpoints.register(source)
    lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    original_receive = wire.a.datagram_received
    main_transport = wire.manager.udp_server.transport
    requests = []
    pending = None

    def respond(request, status, reason, *, sender=None, tag="queue-wire-peer", wrong_branch=False):
        headers = [(name, request.header(name)) for name in ("Via", "From", "Call-ID", "CSeq")]
        if wrong_branch:
            branch = sip.parse_via(headers[0][1]).branch
            headers[0] = ("Via", headers[0][1].replace(branch, "z9hG4bK-unrelated"))
        headers.append(("To", request.header("To") + ";tag=" + tag))
        # An ordinary independent phone sends responses to its configured PBX,
        # regardless of the source port on the received INVITE.
        (sender or wire.a).transport.sendto(sip.build_response(status, reason, headers), wire.listener)

    def receive(raw, address):
        nonlocal pending
        request = sip.parse_message(raw)
        if request.method not in {"INVITE", "CANCEL", "ACK"}:
            return original_receive(raw, address)
        requests.append(request)
        wire.a.requests.append(request)
        wire.a.messages.put_nowait((request, address))
        if request.method == "INVITE":
            pending = request
            if inject_unmatched:
                respond(request, 180, "Ringing", tag="wrong-branch", wrong_branch=True)
                respond(request, 180, "Ringing", sender=wire.b, tag="foreign-source")
            respond(request, 100, "Trying")
            respond(request, 180, "Ringing")
        elif request.method == "CANCEL":
            respond(request, 200, "OK")
            respond(pending, 487, "Request Terminated")

    monkeypatch.setattr(wire.a, "datagram_received", receive)
    session = None
    try:
        await lab.hass.services.async_call("voip_stack", "call", {
            "device_id": source.device_id, "destination": "Reception",
        }, blocking=True)
        call_id = lab.runtime.endpoints.get(source.endpoint_id).active_call_id
        session = lab.calls.get_session(call_id)
        invite, address = await wire.a.receive("INVITE")
        await wait_until(lambda: any(leg.endpoint_id == peer.endpoint_id and leg.state == "remote_ringing"
            for leg in session.legs.values()))
        leg = next(leg for leg in session.legs.values() if leg.endpoint_id == peer.endpoint_id)
        client = leg.dialog
        assert session.answer_committed and client.dialog is None
        assert client._received_provisional and client.last_sip_status_code == 180
        assert set(client.early_dialogs) == {"queue-wire-peer"}
        via = sip.parse_via(invite.header("Via"))
        assert address == wire.listener and (via.host, via.port) == wire.listener
        assert not wire.registrar.registered_contacts("outbound-phone")[0].outbound
        snapshot = phone_states(lab.hass)
        caller = phone_state_dto(snapshot[source.endpoint_id], peer_readable=lambda _identity: True)
        recipient = phone_state_dto(snapshot[peer.endpoint_id], peer_readable=lambda _identity: True)
        assert any(row["state"] == "confirmed" for row in caller["calls"])
        assert recipient["calls"][0]["state"] == "early"
        assert recipient["calls"][0]["id"] == call_id
        assert recipient["calls"][0]["leg_id"] == invite.header("Call-ID")
        assert recipient["calls"][0]["pickup_available"] is True
        await lab.hass.services.async_call("voip_stack", "hangup", {
            "device_id": source.device_id, "call_id": call_id,
        }, blocking=True)
        await asyncio.wait_for(session.terminated.wait(), 2)
        cancel, cancel_address = await wire.a.receive("CANCEL")
        ack, ack_address = await wire.a.receive("ACK")
        assert cancel_address == ack_address == wire.listener
        assert cancel.header("Call-ID") == ack.header("Call-ID") == invite.header("Call-ID")
        assert cancel.header("Via") == ack.header("Via") == invite.header("Via")
        assert cancel.header("CSeq").split()[0] == ack.header("CSeq").split()[0] == invite.header("CSeq").split()[0]
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(lab.manager.policies[lab.identity].ring_timeout + 0.1):
                await wire.a.receive("INVITE")
        assert sum(request.method == "INVITE" for request in requests) == 1
        assert not lab.calls.sessions and not session.tasks
        assert not lab.manager.waiting and not lab.manager.reservations
        assert all(not endpoint.active_call_id for endpoint in lab.runtime.endpoints.endpoints)
        assert not wire.manager.udp_server.endpoint._dialog_queues
        assert not wire.manager.udp_server.endpoint._reused_dialog_keys
        assert set(lab.runtime.rtp_port_pool.get("used", ())) == wire.idle_ports
        assert wire.manager.udp_server.transport is main_transport and not main_transport.is_closing()
        assert wire.registrar.registered_contacts("outbound-phone")
    finally:
        if session is not None and session.live:
            await lab.calls.request_termination(session.call_id, TerminationIntent("test_cleanup"))


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("source_kind", ["sip", "browser"])
async def test_answered_queue_caller_never_confirms_an_unanswered_sip_candidate(queue_lab, monkeypatch, source_kind):
    from unittest.mock import AsyncMock
    from custom_components.voip_stack import call_forwarder, softphone_originate
    from custom_components.voip_stack.phone_state import phone_states, phone_state_dto, phone_state_observer
    from custom_components.voip_stack.roster import dump_roster_json
    lab = queue_lab
    for phone in lab.phones:
        phone.auto_answer = False
    prepared, release = asyncio.Event(), asyncio.Event()
    original_prepare = call_forwarder.async_prepare_group_candidates

    async def hold_after_real_candidate_claims(*args, **kwargs):
        await original_prepare(*args, **kwargs)
        prepared.set()
        await release.wait()

    monkeypatch.setattr(call_forwarder, "async_prepare_group_candidates", hold_after_real_candidate_claims)
    caller = None
    source = None
    session = None
    updates = []
    unsubscribe = phone_state_observer(lab.hass).subscribe(updates.append, kind="projection")
    try:
        if source_kind == "sip":
            caller, _audio = await dial_queue(lab)
            call_id = caller.dialog_ids.call_id
        else:
            source = PhoneEndpoint(endpoint_id="browser-projection", name="Test", username="Test",
                device_id="browser-projection-device", kind=EndpointKind.BROWSER, availability="available")
            lab.runtime.endpoints.register(source)
            lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
            monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
            monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
            await lab.hass.services.async_call("voip_stack", "call", {
                "device_id": source.device_id, "destination": "Reception",
            }, blocking=True)
            call_id = lab.runtime.endpoints.get(source.endpoint_id).active_call_id
        session = lab.calls.get_session(call_id)
        await asyncio.wait_for(prepared.wait(), 2)
        assert session.answer_committed and session.state == "in_call"
        assert "sip:bedroom0" in session.endpoint_claims
        assert not any(leg.endpoint_id == "sip:bedroom0" for leg in session.legs.values())
        assert not lab.phones[0].invites
        source_projection = phone_states(lab.hass)[session.metadata["source_endpoint_id"]]
        assert any(call["state"] == "confirmed" for call in source_projection["calls"])
        delivered = [phone_state_dto(update.phones["sip:bedroom0"], peer_readable=lambda _identity: True)
            for update in updates if "sip:bedroom0" in update.phones]
        assert delivered and not any(call["state"] == "confirmed" for phone in delivered for call in phone["calls"]), delivered
        projected = phone_state_dto(phone_states(lab.hass)["sip:bedroom0"], peer_readable=lambda _identity: True)
        assert projected["calls"] and projected["calls"][0]["state"] in {"trying", "proceeding"}, projected
        assert projected["calls"][0]["pickup_available"] is False
        release.set()
        await asyncio.wait_for(lab.phones[0].invited.wait(), 2)
        await wait_until(lambda: any(leg.endpoint_id == "sip:bedroom0" and leg.state == "remote_ringing"
            for leg in session.legs.values()))
        projected = phone_state_dto(phone_states(lab.hass)["sip:bedroom0"], peer_readable=lambda _identity: True)
        assert projected["state"] == "ringing" and projected["calls"][0]["state"] == "early"
        assert projected["calls"][0]["id"] == call_id
        assert projected["calls"][0]["leg_id"] and projected["calls"][0]["pickup_available"] is True
        leg = next(leg for leg in session.legs.values() if leg.endpoint_id == "sip:bedroom0")
        assert leg.dialog.dialog is None and leg.dialog.last_sip_status_code == 180
        lab.phones[0].answer()
        await wait_for_bridge(lab, call_id)
        projected = phone_state_dto(phone_states(lab.hass)["sip:bedroom0"], peer_readable=lambda _identity: True)
        assert projected["calls"][0]["state"] == "confirmed" and leg.dialog.dialog is not None
    finally:
        release.set()
        if session is not None and session.live:
            await asyncio.wait_for(lab.phones[0].invited.wait(), 2)
        if caller is not None:
            await caller.terminate()
        elif session is not None and session.live:
            await lab.hass.services.async_call("voip_stack", "hangup", {
                "device_id": source.device_id, "call_id": session.call_id,
            }, blocking=True)
        if session is not None:
            await asyncio.wait_for(session.terminated.wait(), 2)
        unsubscribe()
    assert not lab.calls.sessions and not lab.manager.waiting and not lab.manager.reservations


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_queue_ringall_commits_one_agent_media_and_wraps_only_after_hangup(queue_lab):
    lab = queue_lab
    lab.phones[1].auto_answer = False
    client, audio = await dial_queue(lab)
    session = await wait_for_bridge(lab, client.dialog_ids.call_id)
    assert session.answer_committed
    assert session.metadata["dest_endpoint_id"] == "sip:bedroom0"
    assert not lab.manager.waiting and not lab.manager.reservations
    assert not lab.manager.wrap_until
    assert any(request.method == "CANCEL" for request in lab.phones[1].requests)
    dialog = client.dialog
    sample_count = dialog.send_format.audio_format.nominal_frame_bytes // 2
    values = (1700,) * sample_count
    for index in range(5):
        raw = rtp.build_packet(rtp.RtpPacket(dialog.send_format.payload_type, index,
                                            index * dialog.send_format.rtp_timestamp_step, 991,
                                            struct.pack(f"!{sample_count}h", *values)))
        audio.sendto(raw, (dialog.remote_rtp_host, dialog.remote_rtp_port))
        await asyncio.sleep(float(dialog.send_format.frame_ms) / 1000)
    async with asyncio.timeout(2):
        while True:
            raw, _ = await lab.phones[0].audio.get()
            packet = rtp.parse_packet(raw)
            if any(packet.payload):
                assert set(struct.unpack(f"!{len(packet.payload) // 2}h", packet.payload)) == {1700}
                break
    await client.terminate()
    await wait_until(lambda: lab.calls.get_session(client.dialog_ids.call_id) is None)
    assert lab.manager.wrap_until["sip:bedroom0"] > lab.manager.clock()
    assert all(not endpoint.active_call_id for endpoint in lab.runtime.endpoints.endpoints)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_queue_busy_agent_waits_then_first_waiting_call_gets_the_freed_agent(queue_lab):
    lab = queue_lab
    lab.manager.agent_states[lab.identity]["sip:bedroom1"] = "paused"
    lab.calls.upsert("already-busy", state="in_call", owner="bridge")
    lab.calls.claim_endpoint("already-busy", "sip:bedroom0", role="destination")
    client, _audio = await dial_queue(lab)
    assert not lab.phones[0].invites
    session = lab.calls.get_session(client.dialog_ids.call_id)
    assert session.owner == "queue" and len(lab.manager.waiting[lab.identity]) == 1
    await lab.calls.terminate_call_wait("already-busy", reason="done")
    lab.manager._changed()
    await wait_until(lambda: bool(lab.phones[0].invites))
    await wait_for_bridge(lab, client.dialog_ids.call_id)
    assert len(lab.phones[0].invites) == 1
    assert not lab.phones[1].invites
    await client.terminate()
    await wait_until(lambda: lab.calls.get_session(client.dialog_ids.call_id) is None)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_two_real_waiting_callers_reach_the_same_agent_in_fifo_order(queue_lab):
    lab = queue_lab
    lab.manager.policies[lab.identity] = replace(lab.manager.policies[lab.identity], wrap_up=0)
    lab.manager.agent_states[lab.identity]["sip:bedroom1"] = "paused"
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="source2", name="Other", username="other", kind=EndpointKind.SIP_ACCOUNT,
                                                availability="available"))
    lab.roster.append(RosterEntry("other", address="127.0.0.3", metadata={"endpoint_id": "source2"}))
    lab.calls.upsert("already-busy", state="in_call", owner="bridge")
    lab.calls.claim_endpoint("already-busy", "sip:bedroom0", role="destination")
    first, _audio = await dial_queue(lab)
    second, _other_audio = await lab.caller(name="Other", address="127.0.0.3")
    result = await second.invite(target="401", remote_host="127.0.0.1", remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
    if result == "ringing":
        result = await second.wait_for_final(timeout=3)
    assert result == "in_call"
    assert [ticket.call_id for ticket in lab.manager.waiting[lab.identity]] == [first.dialog_ids.call_id, second.dialog_ids.call_id]
    await lab.calls.terminate_call_wait("already-busy", reason="done")
    lab.manager._changed()
    await wait_for_bridge(lab, first.dialog_ids.call_id)
    assert lab.calls.get_session(second.dialog_ids.call_id).owner == "queue"
    assert len(lab.phones[0].invites) == 1
    await first.terminate()
    lab.manager._changed()
    await wait_for_bridge(lab, second.dialog_ids.call_id)
    assert len(lab.phones[0].invites) == 2
    assert not lab.manager.waiting and not lab.manager.reservations
    await second.terminate()
    await wait_until(lambda: not lab.calls.sessions)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("change", ["removed", "disabled", "timeout", "no_agents"])
async def test_queue_changes_while_waiting_use_original_fallback_and_cleanup(queue_lab, change):
    lab = queue_lab
    lab.manager.policies[lab.identity] = replace(lab.manager.policies[lab.identity], fallback="bedroom1",
        max_wait=1 if change == "timeout" else 5, no_agent_wait=0.1 if change == "no_agents" else 5)
    lab.manager.agent_states[lab.identity] = {member: "paused" for member in lab.manager.definitions[lab.identity].members}
    client, _audio = await dial_queue(lab)
    await wait_until(lambda: bool(lab.manager.waiting))
    if change == "removed":
        lab.manager.definitions.pop(lab.identity)
        lab.manager.policies.pop(lab.identity)
    elif change == "disabled":
        lab.manager.policies[lab.identity] = replace(lab.manager.policies[lab.identity], enabled=False)
    lab.manager._changed()
    await wait_until(lambda: bool(lab.phones[1].invites))
    await wait_for_bridge(lab, client.dialog_ids.call_id)
    assert not lab.phones[0].invites
    assert not lab.manager.waiting and not lab.manager.reservations
    exited = [event for event in lab.queue_events if event["event_type"] == "exited"]
    assert len(exited) == 1
    assert exited[0]["reason"] == f"queue_{change}"
    assert exited[0]["outcome"] == change
    assert exited[0]["disposition"] == "forwarded"
    assert exited[0]["destination"] == "bedroom1"
    await client.terminate()
    await wait_until(lambda: lab.calls.get_session(client.dialog_ids.call_id) is None)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_failed_queue_fallback_retains_cause_without_claiming_a_committed_forward(queue_lab):
    lab = queue_lab
    lab.manager.policies[lab.identity] = replace(lab.manager.policies[lab.identity],
        max_wait=1, no_agent_wait=5, fallback_timeout="bedroom1")
    lab.manager.agent_states[lab.identity] = {member: "paused" for member in lab.manager.definitions[lab.identity].members}
    lab.phones[1].final_status = 486
    client, _audio = await dial_queue(lab)
    session = lab.calls.get_session(client.dialog_ids.call_id)
    observer = asyncio.create_task(client.wait_for_dialog_termination(timeout=3))
    try:
        assert await observer == "remote_hangup"
        await asyncio.wait_for(session.terminated.wait(), 3)
    finally:
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
    exited = [event for event in lab.queue_events if event["event_type"] == "exited"]
    assert len(exited) == 1
    assert exited[0]["reason"] == "queue_timeout" and exited[0]["outcome"] == "timeout"
    assert exited[0]["disposition"] == "ended" and exited[0]["destination"] == "bedroom1"
    assert not lab.manager.waiting and not lab.manager.reservations and not lab.calls.sessions


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_queue_fallback_to_another_queue_preserves_original_exit_cause_at_commit(queue_lab):
    lab = queue_lab
    other = "478e08a5a14545e8ad795ec6a4332222"
    lab.manager.definitions[other] = QueueDefinition(other, "Support", ("sip:bedroom1",), "402")
    lab.manager.policies[other] = QueuePolicy(max_wait=5, ring_timeout=1)
    lab.manager.agent_states[other] = {"sip:bedroom1": "available"}
    lab.roster.extend(entry for entry in lab.manager.roster_entries() if entry.id == f"queue:{other}")
    lab.manager.policies[lab.identity] = replace(lab.manager.policies[lab.identity], max_wait=1,
        no_agent_wait=5, fallback_timeout="Support")
    lab.manager.agent_states[lab.identity] = {member: "paused" for member in lab.manager.definitions[lab.identity].members}
    client, _audio = await dial_queue(lab)
    session = await wait_for_bridge(lab, client.dialog_ids.call_id)
    assert session.metadata["dest_endpoint_id"] == "sip:bedroom1"
    exited = [event for event in lab.queue_events if event["event_type"] == "exited" and event["queue_id"] == lab.identity]
    assert len(exited) == 1
    assert exited[0]["reason"] == "queue_timeout" and exited[0]["outcome"] == "timeout"
    assert exited[0]["disposition"] == "forwarded" and exited[0]["destination"] == "Support"
    assert any(event["event_type"] == "answered" and event["queue_id"] == other for event in lab.queue_events)
    await client.terminate()
    await asyncio.wait_for(session.terminated.wait(), 2)
    assert not lab.manager.waiting and not lab.manager.reservations and not lab.calls.sessions


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_live_sip_paging_group_sends_one_way_pcm_and_caller_bye_closes_every_leg(queue_lab):
    from custom_components.voip_stack import paging
    lab = queue_lab
    from custom_components.voip_stack.trunk_runtime import async_stop_sip_trunk
    await async_stop_sip_trunk(lab.hass)
    lab.runtime.trunk_configs.clear()
    lab.runtime.trunk_config = {}
    lab.runtime.default_trunk_id = ""
    identity = "780e47a5b85c4563a3390db63be9fd3f"
    data = {"paging_group_id": identity, "name": "Paging", "extension": "402",
            "members": ["sip:bedroom0", "sip:bedroom1"], "answer_timeout": 1, "timeout": 10}
    entry = SimpleNamespace(subentries={identity: SimpleNamespace(subentry_type="paging", subentry_id=identity, data=data)})
    owner = paging.PagingRuntime(lab.hass, entry)
    lab.calls.adopt_component("paging", owner, closer=owner.close)
    lab.runtime.paging = owner
    from custom_components.voip_stack.paging_config import paging_group_entries
    lab.roster.extend(paging_group_entries(entry))
    for phone in lab.phones:
        phone.direction = "recvonly"
    client, audio = await lab.caller()
    result = await client.invite(target="402", remote_host="127.0.0.1", remote_sip_port=lab.runtime.transport_config["sip_port"],
                                 timeout=3, audio_direction="sendonly")
    if result == "ringing":
        result = await client.wait_for_final(timeout=3)
    assert result == "in_call"
    assert client.dialog.local_audio_direction == "sendonly"
    assert len(owner.jobs) == 1
    job = next(iter(owner.jobs.values()))
    await wait_until(lambda: job.state == "playing")
    from custom_components.voip_stack.endpoint_session import CleanupStage
    source_session = lab.calls.get_session(client.dialog_ids.call_id)
    cleanup_entered, release_cleanup = asyncio.Event(), asyncio.Event()
    async def hold_source_cleanup(_reason):
        cleanup_entered.set()
        await release_cleanup.wait()
    source_session.add_resource("test:source-cleanup-gate", release_cleanup, hold_source_cleanup,
                                stage=CleanupStage.RESERVATION)
    try:
        samples = client.dialog.send_format.audio_format.nominal_frame_bytes // 2
        for index in range(8):
            packet = rtp.RtpPacket(client.dialog.send_format.payload_type, index,
                                   index * client.dialog.send_format.rtp_timestamp_step, 444, struct.pack(f"!{samples}h", *([2300] * samples)))
            audio.sendto(rtp.build_packet(packet), (client.dialog.remote_rtp_host, client.dialog.remote_rtp_port))
            await asyncio.sleep(float(client.dialog.send_format.frame_ms) / 1000)
        for phone in lab.phones:
            assert len(phone.invites) == 1
            assert phone.invites[0].header("Answer-Mode") == "Auto"
            assert b"a=sendonly\r\n" in phone.invites[0].body
            async with asyncio.timeout(2):
                while True:
                    raw, _ = await phone.audio.get()
                    packet = rtp.parse_packet(raw)
                    if any(packet.payload):
                        assert set(struct.unpack(f"!{len(packet.payload) // 2}h", packet.payload)) == {2300}
                        break
        await client.terminate()
        await asyncio.wait_for(cleanup_entered.wait(), 2)
        assert not owner.jobs
        # Child paging work has finished, but the source still owns its final
        # resource/leg cleanup. Its termination event is the complete barrier.
        assert lab.calls.get_session(source_session.call_id) is source_session
        assert not source_session.terminated.is_set()
    finally:
        release_cleanup.set()
        await client.terminate()
        await asyncio.wait_for(source_session.terminated.wait(), 2)
    assert not lab.calls.sessions
    assert all(phone.bye.is_set() for phone in lab.phones)
    assert all(not endpoint.active_call_id for endpoint in lab.runtime.endpoints.endpoints)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_queue_bridge_owner_is_not_the_completion_barrier(queue_lab, monkeypatch):
    from custom_components.voip_stack import outbound_bridge_commit
    lab = queue_lab
    lab.manager.agent_states[lab.identity]["sip:bedroom1"] = "paused"
    preparing, release = asyncio.Event(), asyncio.Event()
    original = outbound_bridge_commit.async_start_sip_bridge_media
    async def start(relay):
        preparing.set()
        await release.wait()
        return await original(relay)
    monkeypatch.setattr(outbound_bridge_commit, "async_start_sip_bridge_media", start)
    client, _audio = await dial_queue(lab)
    try:
        await asyncio.wait_for(preparing.wait(), 2)
        session = lab.calls.get_session(client.dialog_ids.call_id)
        assert session.owner == "bridge"
        assert session.metadata["dest_endpoint_id"] == ""
        assert lab.manager.waiting[lab.identity]
        task = lab.calls.task_for(session.call_id, "forward")
        assert task is not None and not task.done()
        release.set()
        await asyncio.wait_for(asyncio.shield(task), 3)
        assert session.metadata["dest_endpoint_id"] == "sip:bedroom0"
        assert not lab.manager.waiting and not lab.manager.reservations
    finally:
        release.set()
        await client.terminate()
        await wait_until(lambda: lab.calls.get_session(client.dialog_ids.call_id) is None)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_runtime_shutdown_never_originates_the_waiting_call_fallback(queue_lab):
    lab = queue_lab
    lab.manager.policies[lab.identity] = replace(lab.manager.policies[lab.identity], fallback="bedroom1", no_agent_wait=5)
    lab.manager.agent_states[lab.identity] = {member: "paused" for member in lab.manager.definitions[lab.identity].members}
    client, _audio = await dial_queue(lab)
    await lab.manager.close()
    await wait_until(lambda: lab.calls.get_session(client.dialog_ids.call_id) is None)
    assert not any(phone.invites for phone in lab.phones)
    assert not lab.manager.waiting and not lab.manager.reservations


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_queue_to_distinct_queue_commits_one_ticket_and_preserves_source_until_commit(queue_lab):
    lab = queue_lab
    other = "761571623e9d4d28aa518eac0fcb9512"
    lab.manager.definitions[other] = QueueDefinition(other, "Support", ("sip:bedroom0",))
    lab.manager.policies[other] = QueuePolicy(max_wait=5, ring_timeout=1)
    lab.manager.agent_states[other] = {"sip:bedroom0": "available"}
    lab.roster.append(RosterEntry(f"queue:{other}", name="Support", extension="405", ha_bridge=True,
        metadata={"virtual_endpoint": "queue", "queue_id": other, "endpoint_id": f"service:queue:{other}"}))
    lab.manager.agent_states[lab.identity] = {member: "paused" for member in lab.manager.definitions[lab.identity].members}
    lab.manager.policies[lab.identity] = replace(lab.manager.policies[lab.identity], no_agent_wait=5)
    client, _audio = await dial_queue(lab)
    session = lab.calls.get_session(client.dialog_ids.call_id)
    old = lab.calls.resource_for(session.call_id, "queue")
    original_media = old.media
    task = await lab.calls.forward_call(call_id=session.call_id, destination="Support", on_failure="resume")
    result = await asyncio.wait_for(asyncio.shield(task), 3)
    assert result.status == "connected", result
    assert old.detached and old.ticket.closed
    await wait_for_bridge(lab, session.call_id)
    assert session.metadata["dest_endpoint_id"] == "sip:bedroom0"
    assert original_media.closed.is_set()
    assert len(lab.phones[0].invites) == 1
    assert not lab.manager.waiting and not lab.manager.reservations
    await client.terminate()
    await asyncio.wait_for(session.terminated.wait(), 2)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("blocked", ["disabled", "full"])
async def test_rejected_queue_transfer_leaves_original_ticket_media_and_claims_intact(queue_lab, blocked):
    lab = queue_lab
    other = "761571623e9d4d28aa518eac0fcb9512"
    lab.manager.definitions[other] = QueueDefinition(other, "Support", ("sip:bedroom0",))
    lab.manager.policies[other] = QueuePolicy(max_wait=5, max_waiting=1, enabled=blocked != "disabled")
    lab.manager.agent_states[other] = {"sip:bedroom0": "paused"}
    lab.roster.append(RosterEntry(f"queue:{other}", name="Support", ha_bridge=True,
        metadata={"virtual_endpoint": "queue", "queue_id": other, "endpoint_id": f"service:queue:{other}"}))
    if blocked == "full":
        lab.manager.enqueue(other, "other-waiting-call", 1)
    lab.manager.agent_states[lab.identity] = {member: "paused" for member in lab.manager.definitions[lab.identity].members}
    lab.manager.policies[lab.identity] = replace(lab.manager.policies[lab.identity], no_agent_wait=5)
    client, _audio = await dial_queue(lab)
    session = lab.calls.get_session(client.dialog_ids.call_id)
    original = lab.calls.resource_for(session.call_id, "queue")
    media, ticket, claims = original.media, original.ticket, dict(session.endpoint_claims)
    task = await lab.calls.forward_call(call_id=session.call_id, destination="Support", on_failure="resume")
    result = await asyncio.wait_for(asyncio.shield(task), 2)
    assert result.status == "failed_resumed", result
    assert lab.calls.resource_for(session.call_id, "queue") is original
    assert original.media is media and not media.closed.is_set() and not original.detached
    assert not ticket.closed and tuple(lab.manager.waiting[lab.identity]) == (ticket,)
    assert not lab.manager.staged_admissions and dict(session.endpoint_claims) == claims
    assert not any(phone.invites for phone in lab.phones)
    await client.terminate()
    await asyncio.wait_for(session.terminated.wait(), 2)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("automatic", [False, True])
async def test_disabled_queue_forward_uses_its_fallback_before_retiring_source_queue(queue_lab, automatic):
    lab = queue_lab
    other = "1f37a89d29a9436c9ff348d94b1e7777"
    lab.manager.definitions[other] = QueueDefinition(other, "Closed queue", ("sip:bedroom0",))
    lab.manager.policies[other] = QueuePolicy(enabled=False, fallback_disabled="bedroom1")
    lab.manager.agent_states[other] = {"sip:bedroom0": "paused"}
    lab.roster.extend(entry for entry in lab.manager.roster_entries() if entry.id == f"queue:{other}")
    lab.manager.agent_states[lab.identity] = {member: "paused" for member in lab.manager.definitions[lab.identity].members}
    lab.manager.policies[lab.identity] = replace(lab.manager.policies[lab.identity],
        no_agent_wait=0.1 if automatic else 5, fallback_no_agents="Closed queue" if automatic else "")
    lab.phones[1].auto_answer = False
    client, _audio = await dial_queue(lab)
    session = lab.calls.get_session(client.dialog_ids.call_id)
    original = lab.calls.resource_for(session.call_id, "queue")
    media = original.media
    task = None
    try:
        if not automatic:
            task = await lab.calls.forward_call(call_id=session.call_id, destination="Closed queue", on_failure="resume")
        await asyncio.wait_for(lab.phones[1].invited.wait(), 2)
        task = task or lab.calls.task_for(session.call_id, "forward")
        assert not task.done()
        assert lab.calls.resource_for(session.call_id, "queue") is original
        assert original.media is media and not media.closed.is_set() and not original.detached
        assert other not in lab.manager.waiting and not lab.manager.staged_admissions
        assert not lab.phones[0].invites
        lab.phones[1].answer()
        assert (await asyncio.wait_for(asyncio.shield(task), 2)).status == "connected"
        assert session.metadata["dest_endpoint_id"] == "sip:bedroom1"
        exited = [event for event in lab.queue_events if event["event_type"] == "exited"]
        source = [event for event in exited if event["queue_id"] == lab.identity]
        target = [event for event in exited if event["queue_id"] == other]
        assert len(source) == len(target) == 1
        assert source[0]["reason"] == ("queue_no_agents" if automatic else "transferred")
        assert source[0]["disposition"] == "forwarded" and source[0]["destination"] == "bedroom1"
        assert target[0]["reason"] == "queue_disabled" and target[0]["disposition"] == "rejected"
        assert not lab.manager.waiting and not lab.manager.reservations
    finally:
        await client.terminate()
        await asyncio.wait_for(session.terminated.wait(), 2)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_disabled_queue_fallback_cycle_rejects_before_retiring_original_application(queue_lab):
    from homeassistant.exceptions import ServiceValidationError
    lab = queue_lab
    for identity, name, fallback in (("closed-b", "Closed B", "Closed C"), ("closed-c", "Closed C", "Closed B")):
        lab.manager.definitions[identity] = QueueDefinition(identity, name, ("sip:bedroom0",))
        lab.manager.policies[identity] = QueuePolicy(enabled=False, fallback_disabled=fallback)
        lab.manager.agent_states[identity] = {"sip:bedroom0": "paused"}
        lab.roster.extend(entry for entry in lab.manager.roster_entries() if entry.id == f"queue:{identity}")
    lab.manager.agent_states[lab.identity] = {member: "paused" for member in lab.manager.definitions[lab.identity].members}
    lab.manager.policies[lab.identity] = replace(lab.manager.policies[lab.identity], no_agent_wait=5)
    client, _audio = await dial_queue(lab)
    session = lab.calls.get_session(client.dialog_ids.call_id)
    original = lab.calls.resource_for(session.call_id, "queue")
    media, claims = original.media, dict(session.endpoint_claims)
    with pytest.raises(ServiceValidationError, match="routing hops"):
        await lab.calls.forward_call(call_id=session.call_id, destination="Closed B", on_failure="resume")
    assert original.media is media and not media.closed.is_set() and not original.detached
    assert lab.calls.resource_for(session.call_id, "queue") is original
    assert dict(session.endpoint_claims) == claims
    assert set(lab.manager.waiting) == {lab.identity} and not lab.manager.staged_admissions
    assert not any(phone.invites for phone in lab.phones)
    rejected = [event for event in lab.queue_events if event["event_type"] == "exited"]
    assert {event["queue_id"] for event in rejected} == {"closed-b", "closed-c"} and len(rejected) == 2
    await client.terminate()
    await asyncio.wait_for(session.terminated.wait(), 2)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_queue_disabled_after_staging_rolls_back_only_the_new_ticket(queue_lab, monkeypatch):
    from custom_components.voip_stack.queue_call import QueueCall
    lab = queue_lab
    other = "761571623e9d4d28aa518eac0fcb9512"
    lab.manager.definitions[other] = QueueDefinition(other, "Support", ("sip:bedroom0",))
    lab.manager.policies[other] = QueuePolicy(max_wait=5)
    lab.manager.agent_states[other] = {"sip:bedroom0": "paused"}
    lab.roster.append(RosterEntry(f"queue:{other}", name="Support", ha_bridge=True,
        metadata={"virtual_endpoint": "queue", "queue_id": other, "endpoint_id": f"service:queue:{other}"}))
    lab.manager.agent_states[lab.identity] = {member: "paused" for member in lab.manager.definitions[lab.identity].members}
    lab.manager.policies[lab.identity] = replace(lab.manager.policies[lab.identity], no_agent_wait=5)
    client, _audio = await dial_queue(lab)
    session = lab.calls.get_session(client.dialog_ids.call_id)
    original = lab.calls.resource_for(session.call_id, "queue")
    media, ticket, claims = original.media, original.ticket, dict(session.endpoint_claims)
    staged, release = asyncio.Event(), asyncio.Event()
    original_stop_music = QueueCall.stop_music
    async def stop_music(application):
        await original_stop_music(application)
        if application is original and lab.manager.staged_admissions:
            staged.set()
            await release.wait()
    monkeypatch.setattr(QueueCall, "stop_music", stop_music)
    task = await lab.calls.forward_call(call_id=session.call_id, destination="Support", on_failure="resume")
    try:
        await asyncio.wait_for(staged.wait(), 2)
        assert lab.calls.resource_for(session.call_id, "queue") is original
        assert not ticket.closed and tuple(lab.manager.waiting[lab.identity]) == (ticket,)
        assert not lab.manager.waiting.get(other)
        assert original.media is media and not media.closed.is_set()
        lab.manager.policies[other] = replace(lab.manager.policies[other], enabled=False)
    finally:
        release.set()
    result = await asyncio.wait_for(asyncio.shield(task), 2)
    assert result.status == "failed_resumed", result
    assert lab.calls.resource_for(session.call_id, "queue") is original
    assert original.media is media and not media.closed.is_set() and not original.detached
    assert not ticket.closed and tuple(lab.manager.waiting[lab.identity]) == (ticket,)
    assert not lab.manager.staged_admissions and not lab.manager.waiting.get(other)
    assert dict(session.endpoint_claims) == claims
    assert not any(resource.name.startswith("prepared_queue:") for resource in session.resources)
    assert not any(phone.invites for phone in lab.phones)
    await client.terminate()
    await asyncio.wait_for(session.terminated.wait(), 2)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_round_robin_rings_one_phone_then_pauses_only_the_real_missed_offer(queue_lab):
    lab = queue_lab
    lab.manager.policies[lab.identity] = replace(lab.manager.policies[lab.identity], strategy="round_robin", auto_pause_after=1)
    lab.phones[0].auto_answer = False
    client, _audio = await dial_queue(lab)
    session = lab.calls.get_session(client.dialog_ids.call_id)
    await asyncio.wait_for(lab.phones[0].invited.wait(), 2)
    assert not lab.phones[1].invites
    assert tuple(lab.manager.reservations) == ("sip:bedroom0",)
    assert not lab.runtime.endpoints.get("sip:bedroom1").active_call_id
    await wait_for_bridge(lab, session.call_id)
    assert session.metadata["dest_endpoint_id"] == "sip:bedroom1"
    assert len(lab.phones[0].invites) == len(lab.phones[1].invites) == 1
    assert lab.manager.agent_states[lab.identity]["sip:bedroom0"] == "paused"
    assert lab.manager.agent_stats[lab.identity]["sip:bedroom0"]["consecutive_misses"] == 1
    assert lab.manager.agent_stats[lab.identity]["sip:bedroom0"]["pause_reason"] == "no_answer"
    assert lab.manager.agent_stats[lab.identity]["sip:bedroom1"]["consecutive_misses"] == 0
    await client.terminate()
    await asyncio.wait_for(session.terminated.wait(), 2)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_ordered_strategy_uses_saved_order_and_never_claims_unoffered_phone(queue_lab):
    lab = queue_lab
    lab.manager.policies[lab.identity] = replace(lab.manager.policies[lab.identity], strategy="ordered",
        agent_order=("sip:bedroom1", "sip:bedroom0"), auto_pause_after=1)
    client, _audio = await dial_queue(lab)
    session = await wait_for_bridge(lab, client.dialog_ids.call_id)
    assert session.metadata["dest_endpoint_id"] == "sip:bedroom1"
    assert not lab.phones[0].invites and len(lab.phones[1].invites) == 1
    assert not lab.runtime.endpoints.get("sip:bedroom0").active_call_id
    await client.terminate()
    await asyncio.wait_for(session.terminated.wait(), 2)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_caller_hangup_while_agent_rings_is_not_an_auto_pause_miss(queue_lab):
    lab = queue_lab
    lab.manager.policies[lab.identity] = replace(lab.manager.policies[lab.identity], strategy="round_robin", auto_pause_after=1)
    lab.phones[0].auto_answer = False
    client, _audio = await dial_queue(lab)
    session = lab.calls.get_session(client.dialog_ids.call_id)
    await asyncio.wait_for(lab.phones[0].invited.wait(), 2)
    await client.terminate()
    await asyncio.wait_for(session.terminated.wait(), 2)
    assert lab.manager.agent_states[lab.identity]["sip:bedroom0"] == "available"
    assert lab.manager.agent_stats[lab.identity]["sip:bedroom0"]["consecutive_misses"] == 0
    assert not lab.phones[1].invites


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("tcp_udp_fallback", [False, True])
async def test_dashboard_hangup_cancels_queue_sip_offers_and_releases_every_owner(queue_lab, monkeypatch, tcp_udp_fallback):
    from unittest.mock import AsyncMock
    from custom_components.voip_stack import softphone_originate
    from custom_components.voip_stack.roster import dump_roster_json

    lab = queue_lab
    source = PhoneEndpoint(endpoint_id="browser-test", name="Test" + (" source" * 12 if tcp_udp_fallback else ""), username="Test",
        device_id="browser-test-device", kind=EndpointKind.BROWSER, availability="available")
    lab.runtime.endpoints.register(source)
    lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    for phone in lab.phones:
        phone.auto_answer = False
        original_respond = phone.respond
        def observe_response(request, address, status, body=b"", *, phone=phone, original_respond=original_respond):
            if status == 180:
                print("Independent peer180:", {"call_id": request.header("Call-ID"),
                    "via": request.header("Via"), "udp_source": address})
            original_respond(request, address, status, body)
        monkeypatch.setattr(phone, "respond", observe_response)
    session = None
    try:
        await lab.hass.services.async_call("voip_stack", "call", {
            "device_id": source.device_id, "destination": "Reception",
        }, blocking=True)
        call_id = lab.runtime.endpoints.get(source.endpoint_id).active_call_id
        session = lab.calls.get_session(call_id)
        assert session is not None
        await asyncio.wait_for(asyncio.gather(*(phone.invited.wait() for phone in lab.phones)), 2)
        invite_counts = [len(phone.invites) for phone in lab.phones]
        print("Offered clients:", [(leg.sip_call_id, leg.dialog.signaling_transport,
            leg.dialog._received_provisional) for leg in session.legs.values() if leg.dialog is not None])
        for phone in lab.phones:
            phone.invited.clear()
        await lab.hass.services.async_call("voip_stack", "hangup", {
            "device_id": source.device_id, "call_id": call_id,
        }, blocking=True)
        print("Dashboard hangup observed:", {
            "session_live": session.live, "owner": session.owner,
            "source_claim": lab.runtime.endpoints.get(source.endpoint_id).active_call_id,
            "invites_before": invite_counts, "invites_after": [len(phone.invites) for phone in lab.phones],
            "cancels": [sum(request.method == "CANCEL" for request in phone.requests) for phone in lab.phones],
            "waiting": len(lab.manager.waiting), "reservations": len(lab.manager.reservations),
            "tasks": list(session.named_tasks),
        })
        await asyncio.wait_for(session.terminated.wait(), 2)
        assert all(any(request.method == "CANCEL" for request in phone.requests) for phone in lab.phones)
        # Watch the independent peers for a complete former offer window after
        # the session barrier, so an accidentally resumed queue cannot redial.
        for invited in await asyncio.gather(*(asyncio.wait_for(phone.invited.wait(),
                lab.manager.policies[lab.identity].ring_timeout) for phone in lab.phones), return_exceptions=True):
            assert isinstance(invited, TimeoutError)
        assert [len(phone.invites) for phone in lab.phones] == invite_counts
        assert not lab.manager.waiting and not lab.manager.reservations
        assert not lab.calls.sessions and not session.tasks
        assert all(not endpoint.active_call_id for endpoint in lab.runtime.endpoints.endpoints)
        assert not lab.runtime.rtp_port_pool.get("used")
    finally:
        if session is not None and session.live:
            await lab.calls.terminate_call_wait(session.call_id, reason="test_cleanup")
        if session is not None:
            await asyncio.wait_for(session.terminated.wait(), 2)
            print("Explicit owner cleanup:", {"sessions": len(lab.calls.sessions),
                "waiting": len(lab.manager.waiting), "reservations": len(lab.manager.reservations),
                "tasks": len(session.tasks),
                "claims": [endpoint.endpoint_id for endpoint in lab.runtime.endpoints.endpoints if endpoint.active_call_id]})
            assert not lab.calls.sessions and not session.tasks
            assert not lab.manager.waiting and not lab.manager.reservations
            assert all(not endpoint.active_call_id for endpoint in lab.runtime.endpoints.endpoints)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("operation", ["hangup", "decline"])
async def test_browser_queue_candidate_rejects_only_its_offer_and_sip_sibling_can_answer(queue_lab, monkeypatch, operation):
    from unittest.mock import AsyncMock
    from custom_components.voip_stack import softphone_originate
    from custom_components.voip_stack.roster import dump_roster_json

    lab = queue_lab
    source = PhoneEndpoint(endpoint_id="browser-source", name="Test", username="Test",
        device_id="browser-source-device", kind=EndpointKind.BROWSER, availability="available")
    candidate = PhoneEndpoint(endpoint_id="browser-candidate", name="Candidate", username="Candidate",
        device_id="browser-candidate-device", kind=EndpointKind.BROWSER, availability="available")
    for endpoint in (source, candidate):
        lab.runtime.endpoints.register(endpoint)
    lab.runtime.softphone_presence[candidate.endpoint_id] = 1
    lab.roster.append(RosterEntry("Candidate", metadata={"local_ha": True,
        "endpoint_id": candidate.endpoint_id, "endpoint_kind": "browser"}))
    lab.manager.definitions[lab.identity] = replace(lab.manager.definitions[lab.identity],
        members=("sip:bedroom0", candidate.endpoint_id))
    lab.manager.agent_states[lab.identity] = {identity: "available" for identity in lab.manager.definitions[lab.identity].members}
    lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    phone = lab.phones[0]
    phone.auto_answer = False
    session = None
    try:
        await lab.hass.services.async_call("voip_stack", "call", {
            "device_id": source.device_id, "destination": "Reception",
        }, blocking=True)
        call_id = lab.runtime.endpoints.get(source.endpoint_id).active_call_id
        session = lab.calls.get_session(call_id)
        await asyncio.wait_for(phone.invited.wait(), 2)
        await wait_until(lambda: lab.runtime.softphones.get(candidate.endpoint_id, {}).get("state") == "ringing")
        await lab.hass.services.async_call("voip_stack", operation, {
            "device_id": candidate.device_id, "call_id": call_id,
        }, blocking=True)
        assert session.live and not session.terminated.is_set()
        assert lab.runtime.endpoints.get(source.endpoint_id).active_call_id == call_id
        assert not lab.runtime.endpoints.get(candidate.endpoint_id).active_call_id
        assert lab.manager.waiting
        assert not any(request.method == "CANCEL" for request in phone.requests)
        assert len(phone.invites) == 1
        phone.answer()
        session = await wait_for_bridge(lab, call_id)
        assert session.metadata["dest_endpoint_id"] == "sip:bedroom0"
        await lab.hass.services.async_call("voip_stack", "hangup", {
            "device_id": source.device_id, "call_id": call_id,
        }, blocking=True)
        await asyncio.wait_for(session.terminated.wait(), 2)
        assert phone.bye.is_set()
        assert not lab.calls.sessions and not session.tasks
        assert not lab.manager.waiting and not lab.manager.reservations
        assert all(not endpoint.active_call_id for endpoint in lab.runtime.endpoints.endpoints)
    finally:
        if session is not None and session.live:
            await lab.calls.terminate_call_wait(session.call_id, reason="test_cleanup")


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("blocked", ["full", "disabled"])
async def test_queue_rejection_fallback_does_not_answer_until_target_accepts(queue_lab, blocked):
    lab = queue_lab
    changes = {"max_waiting": 1, "fallback_full": "bedroom1", "fallback_disabled": "bedroom1"}
    lab.manager.policies[lab.identity] = replace(lab.manager.policies[lab.identity], **changes)
    if blocked == "full":
        lab.manager.enqueue(lab.identity, "another-waiter", 1)
    else:
        lab.manager.policies[lab.identity] = replace(lab.manager.policies[lab.identity], enabled=False)
        lab.roster[:] = [entry for entry in lab.roster if entry.metadata.get("virtual_endpoint") != "queue"] + lab.manager.roster_entries()
    lab.phones[1].auto_answer = False
    client, _audio = await lab.caller()
    result = await client.invite(target="401", remote_host="127.0.0.1", remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
    assert result == "ringing"
    await asyncio.wait_for(lab.phones[1].invited.wait(), 2)
    assert client.dialog is None
    lab.phones[1].answer()
    assert await client.wait_for_final(timeout=2) == "in_call"
    session = await wait_for_bridge(lab, client.dialog_ids.call_id)
    assert session.metadata["dest_endpoint_id"] == "sip:bedroom1"
    assert not lab.phones[0].invites
    rejected = [event for event in lab.queue_events
        if event["event_type"] == "exited" and event["call_id"] == client.dialog_ids.call_id]
    assert len(rejected) == 1
    assert rejected[0]["reason"] == f"queue_{blocked}"
    assert rejected[0]["outcome"] == blocked
    assert rejected[0]["disposition"] == "rejected"
    assert rejected[0]["destination"] == "bedroom1"
    await client.terminate()
    await asyncio.wait_for(session.terminated.wait(), 2)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_browser_queue_winner_finishes_its_real_offer_only_after_media_commit(queue_lab):
    from custom_components.voip_stack import audio_ws_view
    from custom_components.voip_stack.websocket_owner import MediaWebSocketOwner, async_release_media_owner
    from .test_browser_paging_ha import Browser

    class Client(Browser):
        async def close(self, **_kwargs):
            self.force_close()

    lab = queue_lab
    endpoint = PhoneEndpoint(endpoint_id="browser-agent", name="Browser agent", username="browser-agent",
        device_id="browser-agent-device", kind=EndpointKind.BROWSER, availability="available")
    lab.runtime.endpoints.register(endpoint)
    lab.runtime.softphone_presence[endpoint.endpoint_id] = 1
    lab.roster.append(RosterEntry(endpoint.name, metadata={"local_ha": True,
        "endpoint_id": endpoint.endpoint_id, "endpoint_kind": "browser"}))
    lab.manager.definitions[lab.identity] = replace(lab.manager.definitions[lab.identity],
        members=("sip:bedroom0", endpoint.endpoint_id))
    lab.manager.agent_states[lab.identity] = {identity: "available" for identity in lab.manager.definitions[lab.identity].members}
    lab.phones[0].auto_answer = False
    client, audio = await dial_queue(lab)
    session = lab.calls.get_session(client.dialog_ids.call_id)
    worker = None
    try:
        await asyncio.wait_for(lab.phones[0].invited.wait(), 2)
        await wait_until(lambda: lab.runtime.softphones.get(endpoint.endpoint_id, {}).get("state") == "ringing")
        forwarding = lab.calls.task_for(session.call_id, "forward")
        await lab.hass.services.async_call("voip_stack", "answer", {
            "device_id": endpoint.device_id, "call_id": session.call_id, "media_client_id": "queue-agent-client",
        }, blocking=True)
        await wait_until(lambda: lab.calls.resource_for(session.call_id, f"softphone_media_endpoint:{endpoint.endpoint_id}") is not None)
        assert not any(event["event_type"] == "offer_ended" and event.get("endpoint_id") == endpoint.endpoint_id
                       for event in lab.queue_events)
        live = audio_ws_view._active_softphone_media_session(lab.hass, endpoint.endpoint_id)
        playback = lab.calls.resource_for(session.call_id, "browser_playback")
        browser = Client()
        owner_key = f"{endpoint.endpoint_id}|{session.call_id}"
        owner = MediaWebSocketOwner(websocket=browser, client_id="queue-agent-client")
        lab.runtime.media.owners_for("audio")[owner_key] = owner
        playback.attach(browser)

        async def run_browser():
            try:
                await audio_ws_view._run_audio_session(lab.hass, browser, live, endpoint_id=endpoint.endpoint_id,
                    playback=playback, handoff_requested=owner.handoff_requested)
            finally:
                playback.detach(browser)
                await async_release_media_owner(lab.runtime.media.owners_for("audio"),
                    lab.runtime.media.owner_lock_for("audio"), owner_key, owner)

        worker = asyncio.create_task(run_browser())
        await wait_until(lambda: bool(browser.contracts))
        browser.ready()
        assert (await asyncio.wait_for(asyncio.shield(forwarding), 2)).status == "connected"
        ended = [event for event in lab.queue_events if event["event_type"] == "offer_ended"]
        winning = [event for event in ended if event["endpoint_id"] == endpoint.endpoint_id]
        assert len(winning) == 1 and winning[0]["outcome"] == "in_call"
        assert all(event["outcome"] == "cancelled" for event in ended if event["endpoint_id"] != endpoint.endpoint_id)
        assert any(event["event_type"] == "answered" and event["endpoint_id"] == endpoint.endpoint_id for event in lab.queue_events)
        fmt = client.dialog.send_format
        samples = fmt.audio_format.nominal_frame_bytes // 2
        for index in range(8):
            packet = rtp.RtpPacket(fmt.payload_type, index, index * fmt.rtp_timestamp_step, 447,
                struct.pack(f"!{samples}h", *([2310] * samples)))
            audio.sendto(rtp.build_packet(packet), (client.dialog.remote_rtp_host, client.dialog.remote_rtp_port))
            await asyncio.sleep(float(fmt.frame_ms) / 1000)
        await wait_until(lambda: any(any(frame) for frame in browser.frames))
        voiced = next(frame for frame in browser.frames if any(frame))
        assert set(struct.unpack(f"<{len(voiced)//2}h", voiced)) == {2310}
        assert any(request.method == "CANCEL" for request in lab.phones[0].requests)
        await client.terminate()
        await asyncio.wait_for(session.terminated.wait(), 2)
        assert not lab.calls.sessions and not lab.manager.waiting and not lab.manager.reservations
        assert not lab.runtime.media.owners_for("audio")
    finally:
        await client.terminate()
        if session.live:
            await lab.calls.terminate_call_wait(session.call_id, reason="test_cleanup")
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("caller_kind", ["browser", "sip"])
@pytest.mark.parametrize("other_agent", ["logged_out", "busy", "reserved"])
async def test_caller_member_does_not_mask_no_agents_but_other_busy_members_do(queue_lab, monkeypatch, caller_kind, other_agent):
    """Per-call absence excludes its caller, without treating other busy agents as absent."""
    from unittest.mock import AsyncMock
    from custom_components.voip_stack import softphone_originate
    from custom_components.voip_stack.roster import dump_roster_json

    lab = queue_lab
    source_id = "source"
    source = None
    if caller_kind == "browser":
        source = PhoneEndpoint(endpoint_id="browser-empty-caller", name="Browser caller", device_id="device-empty-caller",
            kind=EndpointKind.BROWSER, availability="available")
        source_id = source.endpoint_id
        lab.runtime.endpoints.register(source)
        lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
        monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
        monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    lab.manager.definitions[lab.identity] = replace(lab.manager.definitions[lab.identity], members=(source_id, "sip:bedroom0"))
    lab.manager.agent_states[lab.identity] = {source_id: "available", "sip:bedroom0": "logged_out" if other_agent == "logged_out" else "available"}
    lab.manager.policies[lab.identity] = replace(lab.manager.policies[lab.identity],
        max_wait=2, no_agent_wait=0.1, retry_delay=0.05, fallback_no_agents="bedroom1")
    other_session = other_ticket = None
    if other_agent in {"busy", "reserved"}:
        other_session = lab.calls.upsert("other-owned-call", state="in_call", owner="bridge")
        if other_agent == "busy":
            lab.calls.claim_endpoint(other_session.call_id, "sip:bedroom0", role="destination")
        else:
            other_id = "eafcd6396efa4be7ad63e052992dce36"
            lab.manager.definitions[other_id] = QueueDefinition(other_id, "Other queue", ("sip:bedroom0",))
            lab.manager.policies[other_id] = QueuePolicy(max_wait=5)
            lab.manager.agent_states[other_id] = {"sip:bedroom0": "available"}
            other_ticket = lab.manager.enqueue(other_id, other_session.call_id, other_session.generation)
            assert lab.manager.claim_turn(other_ticket) == ("sip:bedroom0",)
    session = client = None
    try:
        if source is not None:
            await lab.hass.services.async_call("voip_stack", "call", {"device_id": source.device_id, "destination": "Reception"}, blocking=True)
            call_id = lab.runtime.endpoints.require(source_id).active_call_id
        else:
            client, _audio = await dial_queue(lab)
            call_id = client.dialog_ids.call_id
        session = lab.calls.get_session(call_id)
        assert session.metadata["source_endpoint_id"] == source_id
        caller = next(row for row in lab.manager.get_state(lab.identity)["members"] if row["phone"] == source_id)
        assert caller["state"] == "available" and caller["online"] and caller["busy"]
        assert not lab.manager.eligible(lab.identity, call_id=call_id)
        if other_agent == "logged_out":
            await asyncio.wait_for(lab.phones[1].invited.wait(), 0.5)
            await wait_for_bridge(lab, call_id)
            exit_event, = [event for event in lab.queue_events if event["event_type"] == "exited" and event.get("call_id") == call_id]
            assert exit_event["reason"] == "queue_no_agents"
            assert exit_event["outcome"] == "no_agents"
            assert exit_event["disposition"] == "forwarded" and exit_event["destination"] == "bedroom1"
        else:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(lab.phones[1].invited.wait(), 0.3)
            assert session.live and session.owner == "queue"
            assert not [event for event in lab.queue_events if event["event_type"] == "exited" and event.get("call_id") == call_id]
        assert not lab.phones[0].invites
    finally:
        if session is not None:
            if source is not None:
                await lab.hass.services.async_call("voip_stack", "hangup", {"device_id": source.device_id, "call_id": session.call_id}, blocking=True)
            else:
                await client.terminate()
            await asyncio.wait_for(session.terminated.wait(), 2)
        if other_ticket is not None:
            lab.manager.finish(other_ticket)
        if other_session is not None:
            await lab.calls.terminate_call_wait(other_session.call_id, reason="test_done")
        assert not lab.calls.sessions
        assert not lab.manager.waiting and not lab.manager.reservations
        assert all(not endpoint.active_call_id for endpoint in lab.runtime.endpoints.endpoints)
