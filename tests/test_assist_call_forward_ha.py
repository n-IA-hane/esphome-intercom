"""Assist forwards its real SIP call, retaining its consumer on no answer."""

import asyncio
import struct

import pytest

from custom_components.voip_stack import assist_runtime
from custom_components.voip_stack.router import RouteAction, RouteDecision
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.core import rtp, sdp
from .test_queue_call_ha import queue_lab as queue_lab, lab as lab, trunks as trunks
from .test_sip_capabilities_ha import wait_until

pytestmark = pytest.mark.ha


async def enter_assist(lab, monkeypatch):
    started = asyncio.Event()
    async def consume(conversation):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(assist_runtime.AssistConversation, "_pipeline_loop", consume)
    lab.runtime.assist_config.update(assist_name="Assistant", assist_pipeline="preferred")
    lab.roster.append(RosterEntry("Assistant", extension="403", metadata={"virtual_endpoint": "assist_pipeline"}))
    client, audio = await lab.caller()
    result = await client.invite(target="403", remote_host="127.0.0.1", remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
    if result == "ringing":
        result = await client.wait_for_final(timeout=3)
    assert result == "in_call"
    await asyncio.wait_for(started.wait(), 2)
    return client, audio


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_assist_noanswer_retains_same_consumer_context_media_and_call(queue_lab, monkeypatch):
    lab = queue_lab
    client, _audio = await enter_assist(lab, monkeypatch)
    registry = lab.calls
    call_id = client.dialog_ids.call_id
    session = registry.get_session(call_id)
    application = registry.resource_for(call_id, "assist_application")
    media, conversation = application.media, application.conversation
    consumer, context = media._consumer_task, conversation.context
    lab.phones[0].auto_answer = False
    entry = RosterEntry("attempt", name="Reception", ha_bridge=True,
                        metadata={"group_type": "ring", "members": ["bedroom0"]})
    await registry.forward_call(call_id=call_id, destination="Reception", on_failure="resume",
                                _resolved_decision=RouteDecision(RouteAction.GROUP, target="Reception", entry=entry), _ring_timeout=0.2)
    task = registry.task_for(call_id, "forward")
    if task is not None:
        await asyncio.shield(task)
    assert session.live and session.owner == "assist"
    assert registry.resource_for(call_id, "assist_application") is application
    assert application.media is media and application.conversation is conversation
    assert conversation.context is context and not conversation.detached
    assert media._consumer_task is consumer and not consumer.done() and not media.closed.is_set()
    assert any(request.method == "CANCEL" for request in lab.phones[0].requests)
    await client.terminate()
    await wait_until(lambda: registry.get_session(call_id) is None)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_assist_successful_forward_retires_only_old_consumer_and_commits_bridge(queue_lab, monkeypatch):
    lab = queue_lab
    client, audio = await enter_assist(lab, monkeypatch)
    registry, call_id = lab.calls, client.dialog_ids.call_id
    application = registry.resource_for(call_id, "assist_application")
    media, consumer = application.media, application.media._consumer_task
    context = application.conversation.context
    from homeassistant.helpers.llm import LLMContext, ToolInput
    from custom_components.voip_stack.assist_call_tools import current_call_tools
    from custom_components.voip_stack.phone_directory import PhoneDirectory
    lab.runtime.directory = PhoneDirectory(lab.roster)
    llm_context = LLMContext(platform="conversation", context=context, language="en", assistant="conversation", device_id=None)
    transfer = next(tool for tool in current_call_tools(lab.hass, context) if tool.action == "transfer")
    result = await transfer.async_call(lab.hass, ToolInput(transfer.name, {"destination": "bedroom0"}), llm_context)
    assert result == {"status": "connected"}
    session = registry.get_session(call_id)
    assert session.live and session.owner == "bridge"
    assert session.metadata["dest_endpoint_id"] == "sip:bedroom0"
    assert registry.resource_for(call_id, "assist_application") is None
    assert consumer.done() and media.closed.is_set()
    assert application.conversation.context is context and application.conversation.detached
    assert registry.resource_for(call_id, "relay") is not media
    assert len(lab.phones[0].invites) == 1
    dialog = client.dialog
    sample_count = dialog.send_format.audio_format.nominal_frame_bytes // 2
    for index in range(5):
        raw = rtp.build_packet(rtp.RtpPacket(dialog.send_format.payload_type, index,
            index * dialog.send_format.rtp_timestamp_step, 889, struct.pack(f"!{sample_count}h", *([3210] * sample_count))))
        audio.sendto(raw, (dialog.remote_rtp_host, dialog.remote_rtp_port))
        await asyncio.sleep(float(dialog.send_format.frame_ms) / 1000)
    async with asyncio.timeout(2):
        while True:
            raw, _ = await lab.phones[0].audio.get()
            payload = rtp.parse_packet(raw).payload
            if any(payload):
                assert set(struct.unpack(f"!{len(payload) // 2}h", payload)) == {3210}
                break
    target_offer = sdp.parse_sdp(lab.phones[0].invites[0].body)
    for index in range(5):
        packet = rtp.RtpPacket(lab.phones[0].payload_type, index, index * 160, 890, struct.pack("!160h", *([-1230] * 160)))
        lab.phones[0].rtp_socket.sendto(rtp.build_packet(packet), (target_offer["connection_ip"], target_offer["media_port"]))
        await asyncio.sleep(0.01)
    async with asyncio.timeout(2):
        while True:
            raw = await lab.hass.loop.sock_recv(audio, 4096)
            payload = rtp.parse_packet(raw).payload
            if any(payload):
                assert set(struct.unpack(f"!{len(payload) // 2}h", payload)) == {-1230}
                break
    await client.terminate()
    await wait_until(lambda: registry.get_session(call_id) is None)
    assert not lab.phones[1].invites


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_assist_transfer_tool_reports_busy_and_keeps_trusted_conversation(queue_lab, monkeypatch):
    from homeassistant.helpers.llm import LLMContext, ToolInput
    from custom_components.voip_stack.assist_call_tools import current_call_tools
    from custom_components.voip_stack.phone_directory import PhoneDirectory
    lab = queue_lab
    client, _audio = await enter_assist(lab, monkeypatch)
    application = lab.calls.resource_for(client.dialog_ids.call_id, "assist_application")
    media, context = application.media, application.conversation.context
    lab.runtime.directory = PhoneDirectory(lab.roster)
    lab.phones[0].final_status = 486
    transfer = next(tool for tool in current_call_tools(lab.hass, context) if tool.action == "transfer")
    llm_context = LLMContext(platform="conversation", context=context, language="en", assistant="conversation", device_id=None)
    result = await transfer.async_call(lab.hass, ToolInput(transfer.name, {"destination": "bedroom0"}), llm_context)
    assert result == {"status": "not_answered", "resumed": True}
    assert application.session.live and application.session.owner == "assist"
    assert application.media is media and not media.closed.is_set()
    assert application.conversation.context is context and not application.conversation.detached
    await client.terminate()
    await wait_until(lambda: lab.calls.get_session(client.dialog_ids.call_id) is None)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_assist_resume_tool_cancels_ringing_and_restores_same_conversation(queue_lab, monkeypatch):
    from homeassistant.helpers.llm import LLMContext, ToolInput
    from custom_components.voip_stack.assist_call_tools import current_call_tools
    lab = queue_lab
    client, _audio = await enter_assist(lab, monkeypatch)
    call_id = client.dialog_ids.call_id
    application = lab.calls.resource_for(call_id, "assist_application")
    media, context, consumer = application.media, application.conversation.context, application.media._consumer_task
    lab.phones[0].auto_answer = False
    await lab.calls.forward_call(call_id=call_id, destination="bedroom0", on_failure="resume")
    await asyncio.wait_for(lab.phones[0].invited.wait(), 2)
    resume = next(tool for tool in current_call_tools(lab.hass, context) if tool.action == "resume")
    llm_context = LLMContext(platform="conversation", context=context, language="en", assistant="conversation", device_id=None)
    assert await resume.async_call(lab.hass, ToolInput(resume.name, {}), llm_context) == {"status": "resumed"}
    assert application.session.owner == "assist" and application.media is media
    assert application.conversation.context is context and not application.conversation.detached
    assert media._consumer_task is consumer and not consumer.done()
    assert any(request.method == "CANCEL" for request in lab.phones[0].requests)
    await client.terminate()
    await wait_until(lambda: lab.calls.get_session(call_id) is None)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_assist_transfer_tool_can_enter_queue_and_keep_the_original_call(queue_lab, monkeypatch):
    from homeassistant.helpers.llm import LLMContext, ToolInput
    from custom_components.voip_stack.assist_call_tools import current_call_tools
    from custom_components.voip_stack.phone_directory import PhoneDirectory
    from .test_queue_call_ha import wait_for_bridge
    lab = queue_lab
    client, _audio = await enter_assist(lab, monkeypatch)
    call_id = client.dialog_ids.call_id
    application = lab.calls.resource_for(call_id, "assist_application")
    media, context = application.media, application.conversation.context
    lab.runtime.directory = PhoneDirectory(lab.roster)
    lab.manager.agent_states[lab.identity]["sip:bedroom1"] = "paused"
    transfer = next(tool for tool in current_call_tools(lab.hass, context) if tool.action == "transfer")
    llm_context = LLMContext(platform="conversation", context=context, language="en", assistant="conversation", device_id=None)
    result = await transfer.async_call(lab.hass, ToolInput(transfer.name, {"destination": "Reception"}), llm_context)
    assert result["status"] == "connected"
    await wait_for_bridge(lab, call_id)
    assert lab.calls.get_session(call_id).metadata["dest_endpoint_id"] == "sip:bedroom0"
    assert application.conversation.detached and media.closed.is_set()
    assert not lab.manager.waiting and not lab.manager.reservations
    assert len(lab.phones[0].invites) == 1
    await client.terminate()
    await wait_until(lambda: lab.calls.get_session(call_id) is None)
