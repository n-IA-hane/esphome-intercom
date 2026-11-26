"""An initial trunk group failure must finish its caller without restoring HA."""

import asyncio

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers.trigger import TriggerConfig

from custom_components import voip_stack
from custom_components.voip_stack import services
from custom_components.voip_stack.group_config import group_roster_entries, normalize_group_data
from custom_components.voip_stack.store import config_entry
from custom_components.voip_stack.trigger import VoipCallTrigger

from .test_automation_originate_ha import lab as lab
from .test_trunk_route_outcomes_ha import (
    incoming,
    initial_ha_account,
    invite,
    route_outcome_runtime as route_outcome_runtime,
)

pytestmark = pytest.mark.ha


async def test_initial_dtmf_automation_group_rejection_finishes_caller(lab):
    initial = await initial_ha_account(lab)
    entry = config_entry(lab.hass)
    data = normalize_group_data({"kind": "ring", "name": "Incoming team",
        "members": ["bedroom0", "bedroom1"], "fallback": ""})
    lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(
        subentry_type="group", title=data["name"], unique_id="group:" + data["group_id"], data=data,
    ))
    lab.roster.extend(group_roster_entries(lab.hass, entry))
    lab.runtime.directory.replace(lab.roster)
    for phone in lab.phones:
        phone.final_status = 486
    await services.async_register_services(lab.hass, {
        "select_inbound_destination": voip_stack._handle_select_inbound_destination_service,
    })
    tasks = []

    def select(_payload, _description, context):
        task = lab.hass.async_create_task(lab.hass.services.async_call(
            "voip_stack", "select_inbound_destination", {"destination": "Incoming team"},
            blocking=True, context=context,
        ))
        tasks.append(task)
        return task

    trigger = VoipCallTrigger(lab.hass, TriggerConfig("voip_stack.route_requested",
        options={"trunk": "outcomes"}))
    detach = await trigger.async_attach_runner(select)
    try:
        async with incoming(lab, "UDP", dtmf=True) as (caller, _):
            result = await invite(lab, caller)
            if result == "ringing":
                await caller.wait_for_final(timeout=2)
            await asyncio.gather(*tasks)
            assert [len(phone.invites) for phone in lab.phones] == [1, 1]
            assert caller.last_sip_status_code == 486, (
                caller.last_sip_status_code, caller.last_sip_reason,
                [(session.owner, session.state) for session in lab.calls.sessions.values()],
            )
            assert not initial.active_call_id
            assert lab.runtime.softphones.get(initial.endpoint_id, {}).get("state") != "ringing"
    finally:
        detach()
        await asyncio.gather(*tasks, return_exceptions=True)
