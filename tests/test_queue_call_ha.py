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
    manager.definitions[identity] = QueueDefinition(identity, "Reception", ("sip:bedroom0", "sip:bedroom1"))
    manager.policies[identity] = QueuePolicy(max_wait=5, ring_timeout=1, retry_delay=0.1, no_agent_wait=1, wrap_up=1)
    manager.agent_states[identity] = {member: "available" for member in manager.definitions[identity].members}
    lab.runtime.queues = manager
    contact = RosterEntry(f"queue:{identity}", name="Reception", extension="401", ha_bridge=True,
                          metadata={"virtual_endpoint": "queue", "queue_id": identity, "endpoint_id": f"service:queue:{identity}"})
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

    yield SimpleNamespace(**vars(lab), manager=manager, identity=identity, contact=contact, caller=caller)
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
@pytest.mark.parametrize("change", ["removed", "disabled"])
async def test_queue_changes_while_waiting_use_original_fallback_and_cleanup(queue_lab, change):
    lab = queue_lab
    lab.manager.policies[lab.identity] = replace(lab.manager.policies[lab.identity], fallback="bedroom1", no_agent_wait=5)
    lab.manager.agent_states[lab.identity] = {member: "paused" for member in lab.manager.definitions[lab.identity].members}
    client, _audio = await dial_queue(lab)
    await wait_until(lambda: bool(lab.manager.waiting))
    if change == "removed":
        lab.manager.definitions.pop(lab.identity)
        lab.manager.policies.pop(lab.identity)
    else:
        lab.manager.policies[lab.identity] = replace(lab.manager.policies[lab.identity], enabled=False)
    lab.manager._changed()
    await wait_until(lambda: bool(lab.phones[1].invites))
    await wait_for_bridge(lab, client.dialog_ids.call_id)
    assert not lab.phones[0].invites
    assert not lab.manager.waiting and not lab.manager.reservations
    await client.terminate()
    await wait_until(lambda: lab.calls.get_session(client.dialog_ids.call_id) is None)


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
@pytest.mark.parametrize("blocked", ["full", "disabled"])
async def test_queue_rejection_fallback_does_not_answer_until_target_accepts(queue_lab, blocked):
    lab = queue_lab
    changes = {"max_waiting": 1, "fallback_full": "bedroom1", "fallback_disabled": "bedroom1"}
    lab.manager.policies[lab.identity] = replace(lab.manager.policies[lab.identity], **changes)
    if blocked == "full":
        lab.manager.enqueue(lab.identity, "another-waiter", 1)
    else:
        lab.manager.policies[lab.identity] = replace(lab.manager.policies[lab.identity], enabled=False)
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
    await client.terminate()
    await asyncio.wait_for(session.terminated.wait(), 2)
