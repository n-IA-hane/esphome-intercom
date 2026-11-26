"""Canonical account admission faults and cleanup through shared call owners.

Complete delivery to every ready adapter is qualified by
test_account_paging_broadcast_ha.py. Historical first-winner paging receipts
belong to commit 27497afa and do not describe the broadcast contract.
"""

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
                # Contact priority determines the first prepared SIP leg.
                # Every physical dialog still belongs to this one account call.
                wire_ids = {invite.header("Call-ID") for phone in lab.phones for invite in phone.invites}
                assert wire_ids
                assert all(lab.calls.resolve_session_id(identity) == session.call_id for identity in wire_ids)
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


@pytest.mark.parametrize("failure", ["second_preparation", "claim", "browser_staging"])
async def test_partial_account_admission_releases_locally_prepared_attempts(account_lab, monkeypatch, failure):
    from custom_components.voip_stack import group_candidates
    from custom_components.voip_stack.endpoint_dialing import EndpointDialer
    from custom_components.voip_stack.runtime_diagnostics import runtime_resource_snapshot

    lab = account_lab.lab
    stream = tts_fixture(monkeypatch, lab)
    # REGISTER discovery is a service task, not call-owned state. Complete the
    # actual known probes before taking an independent resource baseline.
    probes = tuple(lab.runtime.capability_probes.values())
    if probes:
        await asyncio.wait_for(asyncio.gather(*probes), 3)
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
        original_targets = group_candidates.build_sip_contact_targets

        def targets(*args, **kwargs):
            # Preparation fails before the second candidate is constructed.
            return original_targets(*args, **kwargs) * 2

        monkeypatch.setattr(group_candidates, "build_sip_contact_targets", targets)
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
                raise RuntimeError("Injected browser staging failure")
            return original_resource(call_id, key, *args, **kwargs)

        monkeypatch.setattr(lab.calls, "own_resource", resource)

    result = await asyncio.wait_for(paging.async_announce(announce(lab, targets=["bedroom0"])), 3)
    assert result["state"] == "no_recipients", result
    assert injected == [failure]
    # Shared admission claims the account before constructing its candidates.
    assert len(prepared) == (0 if failure == "claim" else 1)
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


async def test_shared_browser_close_reports_an_unconfirmed_media_lease(account_lab, monkeypatch):
    from custom_components.voip_stack import browser_playback
    from custom_components.voip_stack.browser_paging import close_browser_paging
    from custom_components.voip_stack.websocket_owner import MediaWebSocketOwner, async_release_media_owner

    account, lab = account_lab, account_lab.lab
    stream = tts_fixture(monkeypatch, lab)
    lab.phones[0].auto_answer = False
    monkeypatch.setattr(browser_playback, "BROWSER_MEDIA_START_TIMEOUT", .03)
    await _start(account)
    session, application = await _staged(account)
    owner = MediaWebSocketOwner(websocket=Browser(), user_id="fixture", client_id="stuck-browser")
    owners = lab.runtime.media.owners_for("audio")
    key = f"bedroom0|{session.call_id}"
    owners[key] = owner
    try:
        with pytest.raises(ConnectionError, match="did not release"):
            await asyncio.wait_for(close_browser_paging(application, "test_output_close"), .5)
        assert owner.handoff_requested.is_set() and not owner.released.is_set()
        assert session.live and not application.media.closed.is_set()
        assert not stream.calls
    finally:
        await async_release_media_owner(owners, lab.runtime.media.owner_lock_for("audio"), key, owner)
        await account.owner.close()
    assert application.media.closed.is_set()
    assert any(request.method == "CANCEL" for request in lab.phones[0].requests)
    assert_call_resources_released(lab)
