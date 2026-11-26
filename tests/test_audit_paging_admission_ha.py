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
    voice_bytes = [0, 0]
    for index, phone in enumerate(lab.phones):
        while not phone.audio.empty():
            packet = rtp.parse_packet(phone.audio.get_nowait()[0])
            if any(packet.payload):
                voice_bytes[index] += len(packet.payload)
    print({"case": case, "state": result["state"], "targets": result["targets"],
           "tts_calls": len(stream.calls), "voice_bytes": voice_bytes,
           "answered_first": lab.phones[0].answered.is_set(),
           "cancelled_second": any(request.method == "CANCEL" for request in lab.phones[1].requests)})
    assert not lab.calls.sessions and not owner.jobs
    assert all(not endpoint.active_call_id for endpoint in lab.runtime.endpoints.endpoints)
    # docs/PAGING.md requires admission to close before playback and forbids
    # late joining. An unlimited/equal answer window can consume the complete
    # job lifetime; those configurations intentionally produce no partial audio.
    if case in {"inherited-equal", "explicit-zero"}:
        assert result["state"] == "timeout"
        assert len(stream.calls) == 0
        assert voice_bytes == [0, 0]
        assert lab.phones[0].answered.is_set()
        assert any(request.method == "CANCEL" for request in lab.phones[1].requests)
    else:
        assert result["state"] == "completed" and voice_bytes[0] > 0
        assert len(stream.calls) == 1


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
