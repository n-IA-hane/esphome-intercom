"""An update preserves an existing trunk's explicit unmatched-number rejection."""

import pytest

from custom_components.voip_stack.config import normalized_trunk_config

from .test_trunk_route_outcomes_ha import (
    incoming,
    initial_ha_account,
    invite,
    lab as lab,
    route_outcome_runtime as route_outcome_runtime,
)

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("transport", ["UDP", "TCP"])
async def test_saved_reject_policy_never_rings_the_retained_default(lab, transport):
    await initial_ha_account(lab)
    async with incoming(lab, transport) as (caller, config):
        normalized = normalized_trunk_config({
            **config,
            "trunk_inbound_default_target": "HA",
            "trunk_inbound_unknown_target": "reject",
        })
        config.clear()
        config.update(normalized)
        await invite(lab, caller, "unknown-extension")
        assert caller.last_sip_status_code == 404
        assert not lab.runtime.endpoints.require("default").active_call_id
        assert lab.runtime.softphones.get("default", {}).get("state") != "ringing"
        assert not any(phone.invites for phone in lab.phones)
