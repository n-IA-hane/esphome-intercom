"""Call tools bind HA context, relay identity and generation without model IDs."""

from types import SimpleNamespace
import asyncio

import pytest
from homeassistant.core import Context
from homeassistant.helpers.llm import LLMContext, ToolInput, LLM_API_ASSIST

from .test_automation_originate_ha import lab as lab

pytestmark = pytest.mark.ha


def add_assist(lab, identity="assist-call"):
    session = lab.calls.upsert(identity, state="in_call", owner="assist", route_kind="assist")
    context = Context()
    media = SimpleNamespace(conversation=SimpleNamespace(context=context, detached=False))
    lab.calls.own_resource(session.call_id, f"relay:{session.call_id}", media, lambda _reason: None)
    return session, media, LLMContext(platform="conversation", context=context, language="en", assistant="conversation",
                                    device_id=None)


async def test_only_this_live_call_context_receives_current_call_tools(lab):
    from custom_components.voip_stack.llm import async_get_tools
    first, _media, context = add_assist(lab)
    other, _other_media, other_context = add_assist(lab, "other-call")
    tools = async_get_tools(lab.hass, context, LLM_API_ASSIST)
    assert {tool.name for tool in tools.tools} == {"VoipCurrentCallStatus", "VoipTransferCurrentCall", "VoipHangupCurrentCall", "VoipResumeCurrentCall"}
    status = next(tool for tool in tools.tools if tool.action == "status")
    assert (await status.async_call(lab.hass, ToolInput(status.name, {}), context))["state"] == "in_call"
    assert (await status.async_call(lab.hass, ToolInput(status.name, {}), other_context))["error"] == "call_context_expired"
    stranger = LLMContext(platform="conversation", context=Context(), language="en", assistant="conversation", device_id=None)
    assert async_get_tools(lab.hass, stranger, LLM_API_ASSIST) is None
    await lab.calls.terminate_call_wait(first.call_id, reason="test_done")
    assert (await status.async_call(lab.hass, ToolInput(status.name, {}), context))["error"] == "call_context_expired"
    assert other.live


async def test_transfer_rejects_external_uri_unknown_and_model_supplied_identity(lab):
    from custom_components.voip_stack.assist_call_tools import current_call_tools
    _session, _media, context = add_assist(lab)
    transfer = next(tool for tool in current_call_tools(lab.hass, context.context) if tool.action == "transfer")
    for destination in ("sip:attacker@example.org", "+390550000", "missing"):
        assert (await transfer.async_call(lab.hass, ToolInput(transfer.name, {"destination": destination}), context))["error"] == "destination_not_allowed"
    with pytest.raises(Exception):
        transfer.parameters({"destination": "bedroom0", "call_id": "other-call"})
    assert not any(phone.invites for phone in lab.phones)


async def test_hangup_only_terminates_the_bound_generation(lab):
    from custom_components.voip_stack.assist_call_tools import current_call_tools
    session, _media, context = add_assist(lab)
    other, _other_media, _other_ctx = add_assist(lab, "unrelated")
    hangup = next(tool for tool in current_call_tools(lab.hass, context.context) if tool.action == "hangup")
    assert await hangup.async_call(lab.hass, ToolInput(hangup.name, {}), context) == {"status": "ending"}
    await asyncio.sleep(0)
    assert not session.live and other.live


async def test_detached_or_replaced_relay_revokes_tool_authority(lab):
    from custom_components.voip_stack.assist_call_tools import current_call_tools
    _session, media, context = add_assist(lab)
    tool = current_call_tools(lab.hass, context.context)[0]
    media.conversation.detached = True
    assert (await tool.async_call(lab.hass, ToolInput(tool.name, {}), context))["error"] == "call_context_expired"
