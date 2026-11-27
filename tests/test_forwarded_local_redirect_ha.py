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
