"""One registered account races its SIP Contact and live HA playback adapter."""

import asyncio
import os
from types import SimpleNamespace

import pytest

from custom_components.voip_stack import audio_ws_view, paging
from custom_components.voip_stack.account_runtime import recompute_account_availability
from custom_components.voip_stack.browser_paging import async_originate_browser_paging
from custom_components.voip_stack.ha_bindings import HaCardBindings

from .test_automation_originate_ha import (
    assert_call_resources_released, lab as lab, tts_fixture,
)
from .test_browser_paging_ha import Browser, _answer
from .test_paging_ha import announce, manager

pytestmark = pytest.mark.ha


def _socket_fds():
    """Compare real process sockets independently of registry bookkeeping."""
    sockets = set()
    for name in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{name}")
        except FileNotFoundError:
            continue
        if target.startswith("socket:"):
            sockets.add(target)
    return sockets


@pytest.fixture
async def account_lab(lab, monkeypatch):
    owner = manager(lab, monkeypatch)
    owner.browser_originate = async_originate_browser_paging
    entry = lab.hass.config_entries.async_entries("voip_stack")[0]
    subentry = next(value for value in entry.subentries.values()
                    if value.subentry_type == "phone" and value.data.get("username") == "bedroom0")
    lab.hass.config_entries.async_update_subentry(entry, subentry, data={**subentry.data,
        "ha_bindings": [{"source": "migration", "reference": "legacy_ha_phone:mixed-paging"}]})
    bindings = lab.runtime.ha_bindings = HaCardBindings(lab.hass, entry, lab.runtime, lambda _changed: None)
    await bindings.async_start()
    lab.runtime.softphone_presence["bedroom0"] = 1
    recompute_account_availability(lab.hass)
    assert lab.calls.component("registrar").registered_contacts("bedroom0")
    phone = lab.runtime.endpoints.require("bedroom0")
    assert phone.supports_ha_client
    try:
        yield SimpleNamespace(lab=lab, owner=owner, phone=phone)
    finally:
        await owner.close()
        await bindings.async_close()
        lab.runtime.ha_bindings = None


async def _start(account, *, answer_timeout=2):
    return await paging.async_announce(announce(account.lab, targets=["bedroom0"],
        wait=False, answer_timeout=answer_timeout))


async def _staged(account):
    lab = account.lab
    await asyncio.wait_for(lab.phones[0].invited.wait(), 2)
    async with asyncio.timeout(1):
        while True:
            call_id = lab.runtime.endpoints.require("bedroom0").active_call_id
            session = lab.calls.get_session(call_id) if call_id else None
            application = lab.calls.resource_for(session.call_id, "browser_paging") if session else None
            if application is not None:
                # The paging job has a parent; the recipient has exactly one owner.
                recipients = [item for item in lab.calls.sessions.values()
                              if item.metadata.get("dest_endpoint_id") == "bedroom0"]
                assert recipients == [session]
                # Browser playback admission requires the existing paging owner.
                assert session.owner == "paging"
                assert session.call_id == lab.phones[0].invites[0].header("Call-ID")
                return session, application
            await asyncio.sleep(.001)


async def _playback(account, session):
    application = await _answer(account.lab, account.phone, session)
    browser = Browser()
    application.playback.attach(browser)
    media_session = audio_ws_view._active_softphone_media_session(account.lab.hass, "bedroom0")
    assert media_session.local_audio_direction == "recvonly"
    assert media_session.local_rtp_port == 0
    worker = asyncio.create_task(audio_ws_view._run_audio_session(account.lab.hass, browser, media_session,
        endpoint_id="bedroom0", playback=application.playback))
    await asyncio.sleep(0)
    return browser, worker


async def _finished(account, result):
    async with asyncio.timeout(5):
        while result["announcement_id"] in account.owner.jobs:
            await asyncio.sleep(.001)
    return account.owner.completed[result["announcement_id"]]


@pytest.mark.parametrize("late_sip_answer", [False, True])
async def test_live_ha_wins_registered_account_and_receives_exact_tts(account_lab, monkeypatch, late_sip_answer):
    account, lab = account_lab, account_lab.lab
    stream = tts_fixture(monkeypatch, lab)
    sip_phone = lab.phones[0]
    sip_phone.auto_answer = False
    sip_phone.answer_on_cancel = late_sip_answer
    result = await _start(account)
    worker = None
    try:
        session, application = await _staged(account)
        browser, worker = await _playback(account, session)
        assert not stream.calls
        browser.ready()
        report = await _finished(account, result)
        await asyncio.wait_for(worker, 2)
        assert report["state"] == "completed", report
        assert report["targets"] == [{"endpoint_id": "bedroom0", "status": "completed"}]
        assert len(stream.calls) == 1
        assert b"".join(browser.frames) == stream.pcm
        assert browser.closed and application.media.closed.is_set()
        assert any(request.method == "CANCEL" for request in sip_phone.requests)
        assert sip_phone.audio.empty()
        if late_sip_answer:
            assert sip_phone.bye.is_set()
            assert any(request.method == "ACK" for request in sip_phone.requests)
        assert_call_resources_released(lab)
    finally:
        await account.owner.close()
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


async def test_sip_winner_closes_staged_ha_without_creating_another_session(account_lab, monkeypatch):
    from custom_components.voip_stack.core import rtp

    account, lab = account_lab, account_lab.lab
    stream = tts_fixture(monkeypatch, lab)
    lab.phones[0].auto_answer = False
    result = await _start(account)
    session, application = await _staged(account)
    browser_media, playback = application.media, application.playback
    lab.phones[0].answer()
    report = await _finished(account, result)
    assert report["state"] == "completed", report
    assert len(stream.calls) == 1
    wire_pcm = bytearray()
    while not lab.phones[0].audio.empty():
        raw, _ = lab.phones[0].audio.get_nowait()
        payload = rtp.parse_packet(raw).payload
        if any(payload):
            wire_pcm.extend(payload)
    assert bytes(wire_pcm) == b"".join(stream.pcm[index:index + 2][::-1]
        for index in range(0, len(stream.pcm), 2))
    assert browser_media.closed.is_set()
    with pytest.raises(ConnectionError, match="ended"):
        await asyncio.wait_for(playback.wait_ready(), .2)
    assert lab.phones[0].bye.is_set()
    assert_call_resources_released(lab)


async def test_ha_answer_without_audio_ready_does_not_win_over_sip(account_lab, monkeypatch):
    account, lab = account_lab, account_lab.lab
    stream = tts_fixture(monkeypatch, lab)
    lab.phones[0].auto_answer = False
    result = await _start(account)
    worker = None
    try:
        session, application = await _staged(account)
        browser, worker = await _playback(account, session)
        browser_media = application.media
        assert not stream.calls and not browser.frames
        lab.phones[0].answer()
        report = await _finished(account, result)
        await asyncio.wait_for(worker, 2)
        assert report["state"] == "completed", report
        assert len(stream.calls) == 1
        assert not browser.frames and browser.closed
        assert browser_media.closed.is_set() and lab.phones[0].bye.is_set()
        assert not any(request.method == "CANCEL" for request in lab.phones[0].requests)
        assert_call_resources_released(lab)
    finally:
        await account.owner.close()
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


async def test_second_paging_job_cannot_claim_an_account_with_a_pending_fork(account_lab, monkeypatch):
    account, lab = account_lab, account_lab.lab
    stream = tts_fixture(monkeypatch, lab)
    lab.phones[0].auto_answer = False
    first = await _start(account)
    session, application = await _staged(account)
    second = await paging.async_announce(announce(lab, targets=["bedroom0"]))
    assert second["state"] == "no_recipients", second
    assert second["targets"] == [{"endpoint_id": "bedroom0", "status": "skipped", "reason": "busy"}]
    assert session.live and not application.media.closed.is_set()
    assert lab.runtime.endpoints.require("bedroom0").active_call_id == session.call_id
    assert len(lab.phones[0].invites) == 1 and not stream.calls
    await account.owner.jobs[first["announcement_id"]].stop()
    assert any(request.method == "CANCEL" for request in lab.phones[0].requests)
    assert_call_resources_released(lab)


async def test_unready_ha_and_unanswered_sip_expire_without_consuming_tts(account_lab, monkeypatch):
    account, lab = account_lab, account_lab.lab
    stream = tts_fixture(monkeypatch, lab)
    lab.phones[0].auto_answer = False
    result = await _start(account, answer_timeout=1)
    worker = None
    try:
        session, application = await _staged(account)
        browser, worker = await _playback(account, session)
        report = await _finished(account, result)
        await asyncio.wait_for(worker, 2)
        assert report["state"] == "no_recipients", report
        assert report["targets"][0]["status"] == "skipped"
        assert not stream.calls and not browser.frames
        assert browser.closed and application.media.closed.is_set()
        assert any(request.method == "CANCEL" for request in lab.phones[0].requests)
        assert_call_resources_released(lab)
    finally:
        await account.owner.close()
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


async def test_second_registered_sip_contact_answers_without_an_ha_binding(lab, monkeypatch):
    """One account must ring every Contact even when no HA adapter exists."""
    from custom_components.voip_stack.core import rtp, sip
    from custom_components.voip_stack.core.sip_auth import build_digest_authorization
    from .test_automation_originate_ha import PHONE_PASSWORD

    owner = manager(lab, monkeypatch)
    stream = tts_fixture(monkeypatch, lab)
    lab.phones[0].auto_answer = False
    second = lab.phones[1]
    address = second.transport.get_extra_info("sockname")
    uri = f"sip:127.0.0.1:{lab.runtime.transport_config['sip_port']}"
    registration_id = "second-contact-for-bedroom0"
    responses = asyncio.Queue()
    previous = second.datagram_received

    def receive(raw, remote):
        message = sip.parse_message(raw)
        if message.is_response and message.header("Call-ID") == registration_id:
            responses.put_nowait(message)
        else:
            previous(raw, remote)

    second.datagram_received = receive

    async def exchange(cseq, authorization=""):
        headers = [
            ("Via", f"SIP/2.0/UDP {address[0]}:{address[1]};branch=z9hG4bK-second-{cseq};rport"),
            ("From", "<sip:bedroom0@127.0.0.1>;tag=second-contact"),
            ("To", "<sip:bedroom0@127.0.0.1>"),
            ("Call-ID", registration_id), ("CSeq", f"{cseq} REGISTER"),
            ("Contact", f"<sip:bedroom0@{address[0]}:{address[1]}>"), ("Expires", "120"),
        ]
        if authorization:
            headers.append(("Authorization", authorization))
        second.transport.sendto(sip.build_request("REGISTER", uri, headers),
            ("127.0.0.1", lab.runtime.transport_config["sip_port"]))
        return await asyncio.wait_for(responses.get(), 2)

    challenge = await exchange(1)
    assert challenge.status_code == 401
    authorization = build_digest_authorization(challenge_header=challenge.header("WWW-Authenticate"),
        username="bedroom0", password=PHONE_PASSWORD, method="REGISTER", uri=uri)
    assert (await exchange(2, authorization)).status_code == 200
    assert len(lab.calls.component("registrar").registered_contacts("bedroom0")) == 2
    assert lab.runtime.ha_bindings is None
    assert not lab.runtime.softphone_presence
    result = await asyncio.wait_for(paging.async_announce(announce(lab, targets=["bedroom0"], answer_timeout=2)), 6)
    assert result["state"] == "completed", result
    assert result["targets"] == [{"endpoint_id": "bedroom0", "status": "completed"}]
    assert len(stream.calls) == 1
    assert lab.phones[0].invites and second.invites
    assert any(request.method == "CANCEL" for request in lab.phones[0].requests)
    assert lab.phones[0].audio.empty() and second.bye.is_set()
    wire_pcm = bytearray()
    while not second.audio.empty():
        raw, _ = second.audio.get_nowait()
        payload = rtp.parse_packet(raw).payload
        if any(payload):
            wire_pcm.extend(payload)
    assert bytes(wire_pcm) == b"".join(stream.pcm[index:index + 2][::-1]
        for index in range(0, len(stream.pcm), 2))
    assert not owner.jobs
    assert_call_resources_released(lab)


@pytest.mark.parametrize("failure", ["second_preparation", "claim", "browser_staging"])
async def test_partial_account_admission_releases_locally_prepared_attempts(account_lab, monkeypatch, failure):
    from custom_components.voip_stack import account_paging
    from custom_components.voip_stack.endpoint_dialing import EndpointDialer
    from custom_components.voip_stack.runtime_diagnostics import runtime_resource_snapshot

    lab = account_lab.lab
    stream = tts_fixture(monkeypatch, lab)
    baseline_fds = _socket_fds()
    baseline = runtime_resource_snapshot(lab.calls, rtp_port_pool=lab.runtime.rtp_port_pool,
        call_artifacts=lab.calls, browser_media=lab.runtime.media, runtime_tasks=lab.runtime.tasks)
    prepared, bound_sockets, injected = [], [], []
    original_prepare = EndpointDialer.prepare_outbound_leg

    def prepare(dialer, **kwargs):
        if failure == "second_preparation" and prepared:
            injected.append(failure)
            raise OSError("Injected second candidate preparation failure")
        attempt = original_prepare(dialer, **kwargs)
        assert attempt is not None
        # Materialize the production reservation's sockets so allocator-only
        # cleanup cannot conceal a leaked operating-system resource.
        attempt.ports.bind_audio_sockets()
        bound_sockets.extend(attempt.ports.audio_socket(port) for port in attempt.ports.ports)
        assert all(sock.fileno() >= 0 for sock in bound_sockets)
        prepared.append(attempt)
        return attempt

    monkeypatch.setattr(EndpointDialer, "prepare_outbound_leg", prepare)
    if failure == "second_preparation":
        original_targets = account_paging.build_sip_contact_targets

        def targets(*args, **kwargs):
            # Preparation fails before the second candidate is constructed.
            return original_targets(*args, **kwargs) * 2

        monkeypatch.setattr(account_paging, "build_sip_contact_targets", targets)
    elif failure == "claim":
        original_claim = lab.calls.claim_endpoint

        def claim(call_id, endpoint_id, **kwargs):
            if endpoint_id == "bedroom0":
                injected.append(failure)
                raise RuntimeError("Injected account claim failure")
            return original_claim(call_id, endpoint_id, **kwargs)

        monkeypatch.setattr(lab.calls, "claim_endpoint", claim)
    else:
        original_resource = lab.calls.own_resource

        def resource(call_id, key, *args, **kwargs):
            if key.startswith("browser_paging:"):
                injected.append(failure)
                assert not lab.calls.sip_client_for(call_id)
                raise RuntimeError("Injected browser staging failure")
            return original_resource(call_id, key, *args, **kwargs)

        monkeypatch.setattr(lab.calls, "own_resource", resource)

    result = await asyncio.wait_for(paging.async_announce(announce(lab, targets=["bedroom0"])), 3)
    assert result["state"] == "no_recipients", result
    assert injected == [failure] and len(prepared) == 1
    assert not stream.calls and not lab.phones[0].invites
    for attempt in prepared:
        assert attempt.ports.released
        assert attempt.client.audio_rtp_socket is None
        assert attempt.client._close_task is not None and attempt.client._close_task.done()
    assert all(sock.fileno() == -1 for sock in bound_sockets)
    assert _socket_fds() == baseline_fds
    assert_call_resources_released(lab)
    after = runtime_resource_snapshot(lab.calls, rtp_port_pool=lab.runtime.rtp_port_pool,
        call_artifacts=lab.calls, browser_media=lab.runtime.media, runtime_tasks=lab.runtime.tasks)
    assert after["resource_counts"] == baseline["resource_counts"], after


async def test_unconfirmed_losing_browser_lease_rejects_sip_winner_before_tts(account_lab, monkeypatch):
    from custom_components.voip_stack import account_paging
    from custom_components.voip_stack.endpoint_dialing import EndpointDialer
    from custom_components.voip_stack.core import rtp
    from custom_components.voip_stack.websocket_owner import MediaWebSocketOwner, async_release_media_owner

    account, lab = account_lab, account_lab.lab
    stream = tts_fixture(monkeypatch, lab)
    lab.phones[0].auto_answer = False
    prepared = []
    original_prepare = EndpointDialer.prepare_outbound_leg

    def prepare(dialer, **kwargs):
        attempt = original_prepare(dialer, **kwargs)
        prepared.append(attempt)
        return attempt

    monkeypatch.setattr(EndpointDialer, "prepare_outbound_leg", prepare)
    monkeypatch.setattr(account_paging, "BROWSER_MEDIA_START_TIMEOUT", .03)
    baseline_fds = _socket_fds()
    result = await _start(account)
    session, application = await _staged(account)
    owner = MediaWebSocketOwner(websocket=Browser(), user_id="fixture", client_id="stuck-browser")
    owners = lab.runtime.media.owners_for("audio")
    owner_key = f"bedroom0|{session.call_id}"
    # This fault models a request that receives revocation but never executes
    # its release handler. Keep its acknowledgment absent through the report.
    owners[owner_key] = owner
    try:
        lab.phones[0].answer()
        report = await asyncio.wait_for(_finished(account, result), 2)
        assert report["state"] == "no_recipients", report
        assert "did not release" in report["targets"][0]["reason"]
        assert owner.handoff_requested.is_set() and not owner.released.is_set()
        assert not stream.calls
        while not lab.phones[0].audio.empty():
            raw, _ = lab.phones[0].audio.get_nowait()
            # Prepared RTP media sends silence before a producer is admitted.
            assert not any(rtp.parse_packet(raw).payload)
        assert lab.phones[0].bye.is_set() and application.media.closed.is_set()
        assert prepared and all(attempt.ports.released for attempt in prepared)
        assert all(attempt.client.audio_rtp_socket is None for attempt in prepared)
        assert not lab.calls.sessions and not lab.calls.endpoint_claims_snapshot()
        assert not lab.runtime.rtp_port_pool["used"]
        assert _socket_fds() == baseline_fds
    finally:
        # End the injected fault using the same release handler as a request.
        await async_release_media_owner(owners, lab.runtime.media.owner_lock_for("audio"), owner_key, owner)
    assert_call_resources_released(lab)
