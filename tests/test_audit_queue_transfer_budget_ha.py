"""Read-only V3 witnesses: queue-to-queue admission must retain route policy."""

import asyncio
from dataclasses import replace

import pytest

from custom_components.voip_stack.call_queues import QueueDefinition, QueuePolicy
from custom_components.voip_stack.roster import RosterEntry

from .test_queue_call_ha import queue_lab, dial_queue, lab, trunks

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("override", [None, 0, 0.8], ids=["inherit-control", "explicit-unlimited", "explicit-longer"])
async def test_queue_transfer_preserves_configured_offer_budget(queue_lab, override):
    lab = queue_lab
    other = "761571623e9d4d28aa518eac0fcb9512"
    lab.manager.definitions[other] = QueueDefinition(other, "Support", ("bedroom0",))
    lab.manager.policies[other] = QueuePolicy(max_wait=5, ring_timeout=0.12, retry_delay=2, no_agent_wait=5)
    lab.manager.agent_states[other] = {"bedroom0": "available"}
    lab.roster.append(RosterEntry(f"queue:{other}", name="Support", extension="405", ha_bridge=True,
        metadata={"virtual_endpoint": "queue", "queue_id": other, "endpoint_id": f"service:queue:{other}"}))
    lab.manager.agent_states[lab.identity] = {member: "paused" for member in lab.manager.definitions[lab.identity].members}
    lab.manager.policies[lab.identity] = replace(lab.manager.policies[lab.identity], no_agent_wait=5)
    lab.phones[0].auto_answer = False
    client, _audio = await dial_queue(lab)
    session = lab.calls.get_session(client.dialog_ids.call_id)
    try:
        task = await lab.calls.forward_call(call_id=session.call_id, destination="Support",
            on_failure="resume", ringing_timeout=override)
        result = await asyncio.wait_for(asyncio.shield(task), 3)
        assert result.status == "connected", result
        application = lab.calls.resource_for(session.call_id, "queue")
        await asyncio.wait_for(lab.phones[0].invited.wait(), 2)
        await asyncio.sleep(0.3)
        cancelled = any(message.method == "CANCEL" for message in lab.phones[0].requests)
        print({"route_override": override, "captured_override": application.ringing_timeout,
               "queue_offer_timeout": lab.manager.policies[other].ring_timeout,
               "cancel_seen_after_300ms": cancelled})
        assert cancelled is (override is None), "The explicit route value must prevail over the destination queue value"
    finally:
        await client.terminate()
        await asyncio.wait_for(session.terminated.wait(), 3)
