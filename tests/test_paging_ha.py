"""Paging uses real SIP/RTP calls, one PCM producer, and joined teardown."""

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.core import Context, ServiceCall
from homeassistant.exceptions import ServiceValidationError

from custom_components.voip_stack import paging
from custom_components.voip_stack.core import rtp
from .test_automation_originate_ha import lab as lab, tts_fixture

pytestmark = pytest.mark.ha


def manager(lab, monkeypatch):
    from custom_components.voip_stack.phone_endpoint import EndpointAvailability
    lab.calls.bind_endpoint_registry(lab.runtime.endpoints)
    owner = paging.PagingRuntime(lab.hass, SimpleNamespace(subentries={}))
    lab.calls.adopt_component("paging", owner, closer=owner.close)
    monkeypatch.setattr(paging, "paging_group_configs", lambda _entry: {
        "all": {"paging_group_id": "all", "name": "All", "members": ["sip:bedroom0", "sip:bedroom1"]},
    })
    for phone in lab.phones:
        phone.direction = "recvonly"
    for endpoint in lab.runtime.endpoints.endpoints:
        lab.runtime.endpoints.update(endpoint.endpoint_id, availability=EndpointAvailability.AVAILABLE)
    return owner


def announce(lab, **kwargs):
    return ServiceCall(lab.hass, "voip_stack", "announce", {
        "targets": ["paging:all", "sip:bedroom0"], "tts_entity_id": "tts.test", "message": "Wake up", **kwargs,
    }, context=Context())


async def test_two_phones_receive_one_complete_synthesis_and_release_claims(lab, monkeypatch):
    owner = manager(lab, monkeypatch)
    stream = tts_fixture(monkeypatch, lab)
    result = await asyncio.wait_for(paging.async_announce(announce(lab)), 8)
    assert result["state"] == "completed", result
    assert len(stream.calls) == 1
    assert [target["status"] for target in result["targets"]] == ["completed", "completed"]
    for phone in lab.phones:
        assert len(phone.invites) == 1
        assert phone.invites[0].header("Answer-Mode") == "Auto"
        assert b"a=sendonly\r\n" in phone.invites[0].body
        assert phone.bye.is_set()
        audio, nonzero_at = bytearray(), []
        while not phone.audio.empty():
            raw, timestamp = phone.audio.get_nowait()
            packet = rtp.parse_packet(raw)
            payload = packet.payload
            if any(payload):
                audio.extend(payload)
                nonzero_at.append(timestamp)
        # Wire L16 is big endian, source TTS PCM is little endian.
        expected = b"".join(stream.pcm[index:index + 2][::-1] for index in range(0, len(stream.pcm), 2))
        assert bytes(audio) == expected
        assert phone.bye_at - nonzero_at[-1] >= 0.19
    assert not owner.jobs
    assert not lab.calls.sessions
    assert all(not endpoint.active_call_id for endpoint in lab.runtime.endpoints.endpoints)


async def test_wait_false_stop_cancels_one_producer_and_both_calls_idempotently(lab, monkeypatch):
    owner = manager(lab, monkeypatch)
    stream = tts_fixture(monkeypatch, lab, block=True)
    pending = await paging.async_announce(announce(lab, wait=False))
    await asyncio.wait_for(stream.started.wait(), 3)
    stop = ServiceCall(lab.hass, "voip_stack", "stop_announcement", {"announcement_id": pending["announcement_id"]}, context=Context())
    first = await asyncio.wait_for(paging.async_stop_announcement(stop), 4)
    second = await paging.async_stop_announcement(stop)
    assert first == second
    assert first["state"] == "stopped"
    assert len(stream.calls) == 1
    assert stream.cancelled.is_set()
    assert all(phone.bye.is_set() for phone in lab.phones)
    assert not owner.jobs and not lab.calls.sessions


async def test_busy_phone_is_skipped_without_interrupting_it(lab, monkeypatch):
    manager(lab, monkeypatch)
    tts_fixture(monkeypatch, lab)
    lab.calls.upsert("other", state="in_call", owner="bridge")
    lab.calls.claim_endpoint("other", "sip:bedroom0", role="callee")
    result = await asyncio.wait_for(paging.async_announce(announce(lab)), 8)
    assert result["state"] == "completed", result
    assert result["targets"][0] == {"endpoint_id": "sip:bedroom0", "status": "skipped", "reason": "busy"}
    assert not lab.phones[0].invites
    assert lab.calls.get_session("other").live
    assert lab.runtime.endpoints.get("sip:bedroom0").active_call_id == "other"


async def test_no_answer_is_cancelled_and_does_not_get_partial_speech(lab, monkeypatch):
    manager(lab, monkeypatch)
    stream = tts_fixture(monkeypatch, lab)
    lab.phones[1].auto_answer = False
    result = await asyncio.wait_for(paging.async_announce(announce(lab, answer_timeout=1)), 8)
    assert result["state"] == "completed", result
    assert result["targets"][1]["status"] == "skipped"
    assert len(stream.calls) == 1
    assert any(request.method == "CANCEL" for request in lab.phones[1].requests)
    assert not lab.phones[1].audio.qsize()
    assert not lab.calls.sessions


async def test_answer_budget_starts_at_wire_invite_after_transport_preparation(lab, monkeypatch):
    from custom_components.voip_stack.sip_client import SipCallClient
    manager(lab, monkeypatch)
    stream = tts_fixture(monkeypatch, lab)
    original = SipCallClient._send_raw
    delayed = set()
    async def send(client, raw, host, port):
        if raw.startswith(b"INVITE ") and client.dialog_ids.call_id not in delayed:
            delayed.add(client.dialog_ids.call_id)
            await asyncio.sleep(1.1)
        return await original(client, raw, host, port)
    monkeypatch.setattr(SipCallClient, "_send_raw", send)
    result = await asyncio.wait_for(paging.async_announce(announce(lab, answer_timeout=1)), 8)
    assert result["state"] == "completed", result
    assert len(delayed) == 2 and len(stream.calls) == 1
    assert all(target["status"] == "completed" for target in result["targets"])


async def test_media_is_decoded_once_for_both_phone_outputs(lab, monkeypatch):
    from custom_components.voip_stack import announcement_media
    manager(lab, monkeypatch)
    calls = []
    async def decoded(hass, identity):
        calls.append(identity)
        yield b"\x80\x01" * 960
    monkeypatch.setattr(announcement_media, "async_audio_stream", decoded)
    result = await asyncio.wait_for(paging.async_announce(announce(lab, message="", tts_entity_id="", media={"media_content_id": "media-source://recorded"})), 8)
    assert result["state"] == "completed", result
    assert calls == ["media-source://recorded"]
    assert all(target["status"] == "completed" for target in result["targets"])


async def test_all_recipients_hangup_cancels_a_blocked_producer(lab, monkeypatch):
    owner = manager(lab, monkeypatch)
    stream = tts_fixture(monkeypatch, lab, block=True)
    result = await paging.async_announce(announce(lab, wait=False))
    await asyncio.wait_for(stream.started.wait(), 3)
    for phone in lab.phones:
        phone.remote_hangup()
    async with asyncio.timeout(3):
        while owner.jobs:
            await asyncio.sleep(0.01)
    assert owner.completed[result["announcement_id"]]["state"] == "failed"
    assert stream.cancelled.is_set()
    assert not lab.calls.sessions


@pytest.mark.parametrize("extra", [
    {"media": "https://example.invalid/audio.wav"},
    {"message": ""}, {"targets": []}, {"timeout": 0}, {"busy_policy": "interrupt"},
])
async def test_invalid_input_never_calls_a_phone(lab, monkeypatch, extra):
    manager(lab, monkeypatch)
    with pytest.raises(ServiceValidationError):
        await paging.async_announce(announce(lab, **extra))
    assert not any(phone.invites for phone in lab.phones)
