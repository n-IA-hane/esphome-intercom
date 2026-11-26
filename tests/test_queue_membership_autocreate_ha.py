"""A public account membership action creates its persisted queue policy."""

import pytest
from homeassistant.core import ServiceCall

from .test_group_configuration_native_ha import add_account
from .test_group_configuration_native_ha import custom_integration_path as custom_integration_path
from .test_group_configuration_native_ha import phone_lab as phone_lab

pytestmark = pytest.mark.ha


async def test_public_membership_creates_queue_policy_before_runtime_reads_it(phone_lab):
    from custom_components.voip_stack.call_queues import async_setup_queues
    from custom_components.voip_stack.group_config import group_records, group_subentries
    from custom_components.voip_stack.phone_queue_membership import async_set_phone_memberships_service

    lab = phone_lab
    account, device = add_account(lab, "Operator")
    assert not group_records(lab.entry, "queue")
    assert lab.runtime.queues is None

    await async_set_phone_memberships_service(ServiceCall(
        lab.hass, "voip_stack", "set_phone_memberships",
        {"phone": device.id, "field": "queues", "value": ["New support"]},
    ))

    # Inspect the actual stored subentry before constructing the runtime reader.
    (stored,) = group_subentries(lab.entry)
    identity = stored.data["group_id"]
    assert stored.data["name"] == "New support"
    assert stored.data["origin"] == "auto"
    assert stored.data["members"] == ["Operator"]
    durations = {key: stored.data["policy"][key]
                 for key in ("max_wait", "no_agent_wait", "retry_delay", "wrap_up")}
    assert durations == {"max_wait": 60, "no_agent_wait": 1, "retry_delay": 1, "wrap_up": 0}
    assert "queues" not in account.data
    assert lab.runtime.queues is None

    manager = await async_setup_queues(lab.hass, lab.entry)
    try:
        queue = manager.resolve("New support")
        assert queue.queue_id == identity and queue.members == ("Operator",)
        assert {key: getattr(manager.policy(identity), key) for key in durations} == durations
        assert manager.agent_states[identity] == {"Operator": "available"}
        assert stored.data["policy"]["max_wait"] == 60

        await async_set_phone_memberships_service(ServiceCall(
            lab.hass, "voip_stack", "set_phone_memberships",
            {"phone": device.id, "field": "queues", "value": []},
        ))
        assert not group_records(lab.entry, "queue")
        assert manager.resolve("New support") is None
        assert not manager.policies and not manager.agent_states
    finally:
        await manager.close()
