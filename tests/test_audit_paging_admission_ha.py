"""Paging admission policy and job-scoped preparation regressions."""

import asyncio

import pytest

from custom_components.voip_stack import paging, automation_originate
from custom_components.voip_stack.core import rtp
from .test_automation_originate_ha import lab as lab, tts_fixture
from .test_paging_ha import manager, announce

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("case", ["inherited-equal", "explicit-zero", "explicit-short", "saved-five"])
async def test_paging_waits_for_admission_before_broadcast(lab, monkeypatch, case):
    owner = manager(lab, monkeypatch)
    stream = tts_fixture(monkeypatch, lab)
    jobs = []
    start = owner.start
    def observe_start(*args, **kwargs):
        job = start(*args, **kwargs)
        jobs.append(job)
        return job
    monkeypatch.setattr(owner, 'start', observe_start)
    lab.phones[1].auto_answer = False
    lab.runtime.transport_config["default_ringing_timeout"] = 1
    settings = {"timeout": 1}
    if case == "explicit-zero":
        settings["answer_timeout"] = 0
    elif case == "explicit-short":
        settings["answer_timeout"] = 0.1
    elif case == "saved-five":
        settings.update(answer_timeout=5, timeout=8)
    result = await asyncio.wait_for(paging.async_announce(announce(lab, **settings)), 12)
    voice = [bytearray(), bytearray()]
    for index, phone in enumerate(lab.phones):
        while not phone.audio.empty():
            packet = rtp.parse_packet(phone.audio.get_nowait()[0])
            if any(packet.payload):
                voice[index].extend(packet.payload)
    voice_bytes = [len(payload) for payload in voice]
    print({"case": case, "state": result["state"], "targets": result["targets"],
           "tts_calls": len(stream.calls), "voice_bytes": voice_bytes,
           "answered_first": lab.phones[0].answered.is_set(),
           "cancelled_second": any(request.method == "CANCEL" for request in lab.phones[1].requests)})
    assert not lab.calls.sessions and not owner.jobs
    assert all(not endpoint.active_call_id for endpoint in lab.runtime.endpoints.endpoints)
    # Each ready client receives the complete finite source independently.
    # Another client's failed admission makes the aggregate partial; it cannot
    # suppress or truncate the already admitted client's message.
    assert result['state'] == 'partial'
    assert len(jobs) == 1 and len(stream.calls) == 1
    job = jobs[0]
    assert set(job.clients) == {'bedroom0', 'bedroom1'}
    clients = {endpoint: list(values.values()) for endpoint, values in job.clients.items()}
    assert all(len(values) == 1 and values[0]['transport'] == 'sip' for values in clients.values())
    assert clients['bedroom0'][0]['status'] == 'completed'
    targets = {target['endpoint_id']: target for target in result['targets']}
    assert targets['bedroom0'] == {'endpoint_id': 'bedroom0', 'status': 'completed'}
    expected = b''.join(stream.pcm[index:index + 2][::-1] for index in range(0, len(stream.pcm), 2))
    assert bytes(voice[0]) == expected and not voice[1]
    assert lab.phones[0].answered.is_set() and lab.phones[0].bye.is_set()
    assert len(lab.phones[0].invites) == len(lab.phones[1].invites) == 1
    second_id = lab.phones[1].invites[0].header('Call-ID')
    assert any(request.method == 'CANCEL' and request.header('Call-ID') == second_id for request in lab.phones[1].requests)
    assert any(request.method == 'ACK' and request.header('Call-ID') == second_id for request in lab.phones[1].requests)
    assert not lab.phones[1].answered.is_set() and not lab.phones[1].bye.is_set()
    if case in {"inherited-equal", "explicit-zero"}:
        assert job.deadline_expired
        assert clients['bedroom1'][0]['status'] == 'timeout'
        assert targets['bedroom1'] == {'endpoint_id': 'bedroom1', 'status': 'no_recipients', 'reason': 'no_ready_output'}
    else:
        assert not job.deadline_expired
        assert clients['bedroom1'][0]['status'] == 'skipped' and clients['bedroom1'][0]['reason'] == 'no_answer'
        assert targets['bedroom1'] == {'endpoint_id': 'bedroom1', 'status': 'skipped', 'reason': 'no_answer'}


async def test_paging_targets_share_one_job_route_preparation(lab, monkeypatch):
    manager(lab, monkeypatch)
    tts_fixture(monkeypatch, lab)
    calls = {"peers": 0, "roster": 0, "advertise": 0}
    old_peers = automation_originate.async_build_peer_snapshot
    old_roster = automation_originate.roster_from_peers
    old_advertise = automation_originate.async_advertise_host
    async def peers(*args, **kwargs):
        calls["peers"] += 1
        return await old_peers(*args, **kwargs)
    def roster(*args, **kwargs):
        calls["roster"] += 1
        return old_roster(*args, **kwargs)
    async def advertise(*args, **kwargs):
        calls["advertise"] += 1
        return await old_advertise(*args, **kwargs)
    monkeypatch.setattr(automation_originate, "async_build_peer_snapshot", peers)
    monkeypatch.setattr(automation_originate, "roster_from_peers", roster)
    monkeypatch.setattr(automation_originate, "async_advertise_host", advertise)
    result = await paging.async_announce(announce(lab))
    print({"paging_targets": len(result["targets"]), "preparation_calls": calls})
    assert result["state"] == "completed"
    assert calls == {"peers": 1, "roster": 1, "advertise": 1}
