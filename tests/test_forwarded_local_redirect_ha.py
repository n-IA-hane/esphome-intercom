"""A forwarded local 302 stays inside the existing call and fork owner."""

import asyncio

import pytest
from homeassistant.config_entries import ConfigSubentry

from custom_components.voip_stack.group_config import group_roster_entries, normalize_group_data
from custom_components.voip_stack.store import config_entry

from .test_automation_originate_ha import assert_call_resources_released
from .test_local_redirect_ha import _redirect_phone, _sip_pcm, _source
from .test_trunk_route_outcomes_ha import (
    initial_ha_account,
    lab as lab,
    route_outcome_runtime as route_outcome_runtime,
)

pytestmark = pytest.mark.ha


def _add_group(lab, members):
    entry = config_entry(lab.hass)
    data = normalize_group_data({"kind": "ring", "name": "Redirect team",
        "members": members, "fallback": ""})
    lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(
        subentry_type="group", title=data["name"], unique_id="group:" + data["group_id"], data=data,
    ))
    lab.roster.extend(group_roster_entries(lab.hass, entry))
    lab.runtime.directory.replace(lab.roster)


@pytest.mark.parametrize("group", [False, True], ids=["account", "group"])
@pytest.mark.parametrize("answered", [False, True], ids=["ringing-source", "answered-source"])
async def test_forward_to_redirecting_account_preserves_the_source_and_pcm(lab, monkeypatch, group, answered):
    initial = await initial_ha_account(lab)
    port = lab.runtime.transport_config["sip_port"]
    _redirect_phone(monkeypatch, lab.phones[0], [f"<sip:bedroom1@127.0.0.1:{port}>"])
    if group:
        _add_group(lab, ["bedroom0"])
    caller, audio = await _source(lab)
    try:
        assert await caller.invite(target="default", remote_host="127.0.0.1",
            remote_sip_port=port, timeout=3) == "ringing"
        call_id = caller.dialog_ids.call_id
        session = lab.calls.get_session(call_id)
        if answered:
            await lab.hass.services.async_call("voip_stack", "answer", {
                "device_id": initial.device_id, "call_id": call_id,
                "media_client_id": "original-browser",
            }, blocking=True)
            assert await caller.wait_for_final(timeout=3) == "in_call"
        task = await lab.calls.forward_call(call_id=call_id,
            destination="Redirect team" if group else "bedroom0", on_failure="terminate")
        await task
        if not answered:
            assert await caller.wait_for_final(timeout=3) == "in_call"
        assert caller.dialog is not None
        assert lab.calls.get_session(call_id) is session and session.live
        assert len(lab.phones[0].invites) == len(lab.phones[1].invites) == 1
        assert not lab.runtime.endpoints.require("bedroom0").active_call_id
        assert lab.runtime.endpoints.require("bedroom1").active_call_id == call_id
        await _sip_pcm(lab, caller, lab.phones[1], audio)
        assert caller.bye()
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(.005)
        assert_call_resources_released(lab)
    finally:
        await caller.close()
        audio.close()


async def test_forwarded_redirect_can_be_answered_by_the_target_ha_adapter(lab, monkeypatch):
    from custom_components.voip_stack.account_runtime import recompute_account_availability
    from custom_components.voip_stack.directory_runtime import compose_directory_entries

    await initial_ha_account(lab)
    target = lab.runtime.endpoints.require("bedroom1")
    await lab.runtime.ha_bindings.async_set_popup_binding("fixture-user", "redirect-target-card", target.endpoint_id, True)
    registrar = lab.calls.component("registrar")
    for contact in registrar.registered_contacts(target.username):
        assert registrar.remove_contact(contact.flow_key)
    recompute_account_availability(lab.hass)
    lab.roster[:] = compose_directory_entries(lab.hass, [], registrar.registered_roster_entries())
    lab.runtime.directory.replace(lab.roster)
    port = lab.runtime.transport_config["sip_port"]
    _redirect_phone(monkeypatch, lab.phones[0], [f"<sip:bedroom1@127.0.0.1:{port}>"])
    caller, audio = await _source(lab)
    try:
        assert await caller.invite(target="default", remote_host="127.0.0.1",
            remote_sip_port=port, timeout=3) == "ringing"
        call_id = caller.dialog_ids.call_id
        session = lab.calls.get_session(call_id)
        task = await lab.calls.forward_call(call_id=call_id, destination="bedroom0", on_failure="terminate")
        async with asyncio.timeout(3):
            while lab.runtime.softphones.get(target.endpoint_id, {}).get("state") != "ringing":
                await asyncio.sleep(.005)
        await lab.hass.services.async_call("voip_stack", "answer", {
            "device_id": target.device_id, "call_id": call_id, "media_client_id": "redirect-browser",
        }, blocking=True)
        await task
        assert await caller.wait_for_final(timeout=3) == "in_call"
        assert lab.calls.get_session(call_id) is session
        assert not lab.runtime.endpoints.require("bedroom0").active_call_id
        assert lab.runtime.endpoints.require(target.endpoint_id).active_call_id == call_id
        assert not lab.phones[1].invites
        assert caller.bye()
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(.005)
        assert_call_resources_released(lab)
    finally:
        await caller.close()
        audio.close()


@pytest.mark.parametrize("redirect", [False, True], ids=["direct", "redirect"])
async def test_failed_forward_reclaims_original_account_before_resuming(lab, monkeypatch, redirect):
    initial = await initial_ha_account(lab)
    port = lab.runtime.transport_config["sip_port"]
    if redirect:
        _redirect_phone(monkeypatch, lab.phones[0], [f"<sip:bedroom1@127.0.0.1:{port}>"])
    lab.phones[1 if redirect else 0].final_status = 486
    caller, audio = await _source(lab)
    try:
        assert await caller.invite(target="default", remote_host="127.0.0.1",
            remote_sip_port=port, timeout=3) == "ringing"
        call_id = caller.dialog_ids.call_id
        session = lab.calls.get_session(call_id)
        task = await lab.calls.forward_call(call_id=call_id, destination="bedroom0", on_failure="resume")
        result = await task
        assert len(lab.phones[1].invites) == int(redirect)
        assert result.status == "failed_resumed"
        assert lab.calls.get_session(call_id) is session and session.live
        assert not lab.runtime.endpoints.require("bedroom0").active_call_id
        assert not lab.runtime.endpoints.require("bedroom1").active_call_id
        assert lab.runtime.endpoints.require(initial.endpoint_id).active_call_id == call_id
        assert lab.runtime.softphones[initial.endpoint_id]["state"] == "ringing"
    finally:
        await caller.terminate(timeout=1)
        await caller.close()
        audio.close()


@pytest.mark.parametrize("blocked", ["busy", "dnd", "unbound"])
async def test_failed_forward_does_not_resume_an_ineligible_original_receiver(lab, blocked):
    initial = await initial_ha_account(lab)
    port = lab.runtime.transport_config["sip_port"]
    phone = lab.phones[0]
    phone.auto_answer = False
    caller, audio = await _source(lab)
    competing = None
    try:
        assert await caller.invite(target="default", remote_host="127.0.0.1",
            remote_sip_port=port, timeout=3) == "ringing"
        call_id = caller.dialog_ids.call_id
        task = await lab.calls.forward_call(call_id=call_id, destination="bedroom0", on_failure="resume")
        await asyncio.wait_for(phone.invited.wait(), 3)
        assert not lab.runtime.endpoints.require(initial.endpoint_id).active_call_id
        if blocked == "busy":
            competing = lab.calls.upsert("competing-call", state="ringing", owner="ha_softphone")
            lab.calls.claim_endpoint(competing.call_id, initial.endpoint_id, role="destination")
        elif blocked == "dnd":
            lab.runtime.endpoints.update(initial.endpoint_id, dnd=True)
        else:
            await lab.runtime.ha_bindings.async_set_popup_binding(
                "fixture-user", "ha-account-default", initial.endpoint_id, False)
        phone.respond(phone.last_request, phone.remote, 486)
        result = await task
        assert result.status == "ended"
        assert await caller.wait_for_final(timeout=3) not in {"in_call", "ringing", "timeout"}
        async with asyncio.timeout(3):
            while lab.calls.get_session(call_id) is not None:
                await asyncio.sleep(.005)
        assert lab.runtime.softphones[initial.endpoint_id]["state"] != "ringing"
        if competing is not None:
            assert competing.live
            assert lab.runtime.endpoints.require(initial.endpoint_id).active_call_id == competing.call_id
        else:
            assert not lab.runtime.endpoints.require(initial.endpoint_id).active_call_id
    finally:
        if competing is not None:
            await lab.calls.terminate_call_wait(competing.call_id, reason="test_complete")
        await caller.terminate(timeout=1)
        await caller.close()
        audio.close()


async def test_cancelling_forwarded_redirect_acknowledges_and_closes_late_answer(lab, monkeypatch):
    from custom_components.voip_stack.local_redirect import LocalAccountRedirect

    release = LocalAccountRedirect.release_closed_account
    cleanup_failures = []

    def observed_release(self, endpoint_id):
        try:
            return release(self, endpoint_id)
        except Exception as error:
            cleanup_failures.append((endpoint_id, str(self.session.phase), str(error)))
            raise

    monkeypatch.setattr(LocalAccountRedirect, "release_closed_account", observed_release)
    await initial_ha_account(lab)
    port = lab.runtime.transport_config["sip_port"]
    _redirect_phone(monkeypatch, lab.phones[0], [f"<sip:bedroom1@127.0.0.1:{port}>"])
    lab.phones[1].auto_answer = False
    lab.phones[1].answer_on_cancel = True
    caller, audio = await _source(lab)
    try:
        assert await caller.invite(target="default", remote_host="127.0.0.1",
            remote_sip_port=port, timeout=3) == "ringing"
        call_id = caller.dialog_ids.call_id
        task = await lab.calls.forward_call(call_id=call_id, destination="bedroom0", on_failure="terminate")
        await asyncio.wait_for(lab.phones[1].invited.wait(), 3)
        await caller.terminate(timeout=1)
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(lab.phones[1].bye.wait(), 3)
        assert any(request.method == "ACK" for request in lab.phones[1].requests)
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(.005)
        assert_call_resources_released(lab)
        assert not cleanup_failures, cleanup_failures
    finally:
        await caller.close()
        audio.close()


async def test_failed_redirect_releases_target_while_an_original_group_offer_continues(lab, monkeypatch):
    initial = await initial_ha_account(lab)
    port = lab.runtime.transport_config["sip_port"]
    _redirect_phone(monkeypatch, lab.phones[0], [f"<sip:bedroom1@127.0.0.1:{port}>"])
    lab.phones[1].final_status = 486
    _add_group(lab, ["bedroom0", "default"])
    caller, audio = await _source(lab)
    try:
        assert await caller.invite(target="Redirect team", remote_host="127.0.0.1",
            remote_sip_port=port, timeout=3) == "ringing"
        await asyncio.wait_for(lab.phones[1].invited.wait(), 3)
        call_id = caller.dialog_ids.call_id
        session = lab.calls.get_session(call_id)
        async with asyncio.timeout(3):
            while any(leg.endpoint_id == "bedroom1" for leg in session.legs.values()):
                await asyncio.sleep(.005)
        assert session.live
        assert not lab.runtime.endpoints.require("bedroom1").active_call_id
        assert lab.runtime.softphones[initial.endpoint_id]["state"] == "ringing"
        await lab.hass.services.async_call("voip_stack", "answer", {
            "device_id": initial.device_id, "call_id": call_id,
            "media_client_id": "original-group-browser",
        }, blocking=True)
        assert await caller.wait_for_final(timeout=3) == "in_call"
        assert lab.calls.get_session(call_id) is session
    finally:
        await caller.terminate(timeout=1)
        await caller.close()
        audio.close()


@pytest.mark.parametrize("failed_endpoint,cancel_cleanup", [("bedroom1", False),
    ("bedroom1", True), ("bedroom0", False)],
    ids=["close-error", "cancelled-cleanup", "redirector-close-error"])
async def test_redirect_cleanup_error_releases_only_retired_claims(lab, monkeypatch, failed_endpoint, cancel_cleanup):
    from custom_components.voip_stack import local_redirect

    initial = await initial_ha_account(lab)
    port = lab.runtime.transport_config["sip_port"]
    _redirect_phone(monkeypatch, lab.phones[0], [f"<sip:bedroom1@127.0.0.1:{port}>"])
    lab.phones[1].final_status = 486
    _add_group(lab, ["bedroom0", "default"])
    stopped = []
    final_cleanup = asyncio.Event()
    allow_cleanup = asyncio.Event()
    finished = asyncio.Event()
    follow_tasks = []
    follow_errors = []
    close = local_redirect.async_close_outbound_leg
    follow = local_redirect.LocalAccountRedirect.follow

    class BrokenRelay:
        async def stop(self):
            stopped.append(True)
            raise OSError("injected video relay stop failure")

    async def observed_close(attempt, **kwargs):
        if attempt.endpoint_id == failed_endpoint:
            attempt.video_relay = BrokenRelay()
        if attempt.endpoint_id == "bedroom1" and kwargs.get("bye_or_cancel"):
            final_cleanup.set()
            if cancel_cleanup:
                await allow_cleanup.wait()
        await close(attempt, **kwargs)

    async def observed_follow(self, *args):
        follow_tasks.append(asyncio.current_task())
        try:
            return await follow(self, *args)
        except BaseException as error:
            follow_errors.append(error)
            raise
        finally:
            finished.set()

    monkeypatch.setattr(local_redirect, "async_close_outbound_leg", observed_close)
    monkeypatch.setattr(local_redirect.LocalAccountRedirect, "follow", observed_follow)
    caller, audio = await _source(lab)
    try:
        assert await caller.invite(target="Redirect team", remote_host="127.0.0.1",
            remote_sip_port=port, timeout=3) == "ringing"
        call_id = caller.dialog_ids.call_id
        session = lab.calls.get_session(call_id)
        if cancel_cleanup:
            await asyncio.wait_for(final_cleanup.wait(), 3)
            # Repeated cancellation of this retired branch must not abandon
            # its cleanup or cancel the independent original browser offer.
            assert len(follow_tasks) == 1
            follow_tasks[0].cancel()
            await asyncio.sleep(0)
            follow_tasks[0].cancel()
            await asyncio.sleep(0)
            allow_cleanup.set()
        await asyncio.wait_for(finished.wait(), 3)
        assert stopped
        assert session.live and lab.calls.get_session(call_id) is session
        assert not lab.runtime.endpoints.require("bedroom0").active_call_id
        assert not lab.runtime.endpoints.require("bedroom1").active_call_id
        assert lab.runtime.endpoints.require(initial.endpoint_id).active_call_id == call_id
        assert lab.runtime.softphones[initial.endpoint_id]["state"] == "ringing"
        if failed_endpoint == "bedroom0":
            assert len(follow_errors) == 1 and isinstance(follow_errors[0], OSError)
            assert not lab.phones[1].invites
        else:
            assert not [error for error in follow_errors if not isinstance(error, asyncio.CancelledError)]
            assert bool(follow_errors) is cancel_cleanup
        await lab.hass.services.async_call("voip_stack", "answer", {
            "device_id": initial.device_id, "call_id": call_id,
            "media_client_id": "surviving-original-browser",
        }, blocking=True)
        assert await caller.wait_for_final(timeout=3) == "in_call"
        assert lab.calls.get_session(call_id) is session
    finally:
        allow_cleanup.set()
        await caller.terminate(timeout=1)
        await caller.close()
        audio.close()


@pytest.mark.parametrize("forwarded", [False, True], ids=["initial-group", "forwarded-group"])
async def test_redirect_does_not_republish_existing_browser_offers(lab, monkeypatch, forwarded):
    from homeassistant.core import callback
    from custom_components.voip_stack.account_runtime import recompute_account_availability
    from custom_components.voip_stack.automation_routing import automation_event_type
    from custom_components.voip_stack.const import CALL_EVENT
    from custom_components.voip_stack.directory_runtime import compose_directory_entries
    from custom_components.voip_stack.route_decisions import set_pending_route_decision

    await initial_ha_account(lab)
    declined = await initial_ha_account(lab, "card-a")
    waiting = await initial_ha_account(lab, "card-b")
    target = lab.runtime.endpoints.require("bedroom1")
    await lab.runtime.ha_bindings.async_set_popup_binding("fixture-user", "redirect-target-card", target.endpoint_id, True)
    registrar = lab.calls.component("registrar")
    for contact in registrar.registered_contacts(target.username):
        assert registrar.remove_contact(contact.flow_key)
    recompute_account_availability(lab.hass)
    lab.roster[:] = compose_directory_entries(lab.hass, [], registrar.registered_roster_entries())
    lab.runtime.directory.replace(lab.roster)
    _add_group(lab, [declined.endpoint_id, waiting.endpoint_id, "bedroom0"])
    port = lab.runtime.transport_config["sip_port"]
    _redirect_phone(monkeypatch, lab.phones[0], [f"<sip:bedroom1@127.0.0.1:{port}>"], defer=True)
    caller, audio = await _source(lab)
    task = stop = None
    try:
        assert await caller.invite(target="default" if forwarded else "Redirect team",
            remote_host="127.0.0.1", remote_sip_port=port, timeout=3) == "ringing"
        call_id = caller.dialog_ids.call_id
        if forwarded:
            task = await lab.calls.forward_call(call_id=call_id, destination="Redirect team", on_failure="terminate")
        await asyncio.wait_for(lab.phones[0].invited.wait(), 3)
        lab.phones[0].last_request = lab.phones[0].invites[-1]
        async with asyncio.timeout(3):
            while lab.runtime.softphones.get(waiting.endpoint_id, {}).get("state") != "ringing":
                await asyncio.sleep(.005)
        assert lab.runtime.softphones[declined.endpoint_id]["state"] == "ringing"
        set_pending_route_decision(lab.hass, {"call_id": call_id, "action": "decline", "endpoint_id": declined.endpoint_id})
        assert lab.runtime.softphones[declined.endpoint_id]["state"] != "ringing"
        assert not lab.runtime.endpoints.require(declined.endpoint_id).active_call_id
        await lab.hass.async_block_till_done()
        events = []

        @callback
        def capture(event):
            events.append(dict(event.data))

        stop = lab.hass.bus.async_listen(CALL_EVENT, capture)
        lab.phones[0].redirect_response()
        async with asyncio.timeout(3):
            while lab.runtime.softphones.get(target.endpoint_id, {}).get("state") != "ringing":
                await asyncio.sleep(.005)
        await lab.hass.async_block_till_done()
        assert not [event for event in events if event.get("endpoint_id") == declined.endpoint_id
            and event.get("state") == "ringing"], events
        # An existing offer still receives state_changed projections when the
        # group changes. It must not become a new ringing occurrence.
        assert not [event for event in events if event.get("endpoint_id") == waiting.endpoint_id
            and automation_event_type(event) == "ringing"], events
        assert lab.runtime.softphones[declined.endpoint_id]["state"] != "ringing"
        assert lab.runtime.softphones[waiting.endpoint_id]["state"] == "ringing"
        assert lab.runtime.endpoints.require(target.endpoint_id).active_call_id == call_id
        session = lab.calls.get_session(call_id)
        assert {waiting.endpoint_id, target.endpoint_id} <= set(session.metadata["ring_endpoint_ids"])
        await lab.hass.services.async_call("voip_stack", "answer", {
            "device_id": waiting.device_id, "call_id": call_id, "media_client_id": "original-waiting-browser",
        }, blocking=True)
        assert await caller.wait_for_final(timeout=3) == "in_call"
        assert lab.calls.get_session(call_id) is session
    finally:
        if stop is not None:
            stop()
        await caller.terminate(timeout=1)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await caller.close()
        audio.close()
