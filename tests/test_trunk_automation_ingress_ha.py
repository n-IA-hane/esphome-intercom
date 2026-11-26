"""Native trunk origin survives an Automation destination and filters its trigger."""
import asyncio

import pytest
from homeassistant.helpers.script import Script
from homeassistant.helpers.trigger import TriggerConfig
from custom_components import voip_stack
from custom_components.voip_stack import trigger
from .test_automation_originate_ha import lab as lab
from .test_peer_trunk_ingress_ha import bind_peer, peer_client

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("protocol", ["UDP", "TCP"])
async def test_authenticated_trunk_automation_native_ingress_filter(lab, protocol):
    await voip_stack._async_register_services(lab.hass)
    bind_peer(lab)
    lab.runtime.trunk_configs["remote"]["trunk_inbound_default_target"] = "Wakeup-Caller"
    executed = asyncio.Event()
    script = Script(lab.hass, [{"action": "voip_stack.wait_unanswered", "data": {"duration": 1}}],
        "Trunk Automation ingress", "automation", script_mode="parallel")
    tasks = []
    def runner(payload, description, context):
        executed.set()
        task = lab.hass.async_create_task(script.async_run({"trigger": payload}, context))
        tasks.append(task)
        return task
    native = trigger.VoipCallTrigger(lab.hass, TriggerConfig("voip_stack.call_received",
        options={"destination": "Wakeup-Caller", "ingress": "trunk"}))
    detach = await native.async_attach_runner(runner)
    caller = peer_client(protocol, host="127.0.0.1")
    try:
        result = await caller.invite(target="450", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
        assert result == "ringing", (result, caller.last_sip_status_code)
        registrar = lab.calls.component("registrar")
        assert any(principal == "remote_pbx" for _nonce, principal, _cnonce in registrar.nonce_uses)
        session = lab.calls.get_session(caller.dialog_ids.call_id)
        assert session is not None and session.owner == "automation"
        assert session.metadata["inbound_trunk_id"] == "remote"
        async with asyncio.timeout(1):
            await executed.wait()
        assert session.metadata.get("ingress") == "trunk"
        assert session.metadata.get("origin") == "trunk"
    finally:
        detach()
        await caller.terminate()
        await caller.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(.005)
