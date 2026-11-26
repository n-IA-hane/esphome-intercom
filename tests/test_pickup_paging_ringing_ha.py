"""Pickup and paging inherit the same ringing policy as ordinary phone calls."""

import asyncio
import pytest
from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError

from custom_components.voip_stack import paging, services
from custom_components.voip_stack.call_pickup import _pickup_group_phone
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.paging_config import normalize_paging_group
from custom_components.voip_stack.phone_policy import PhonePolicy

from .test_automation_originate_ha import lab as lab, tts_fixture
from .test_browser_phone_policy_ha import browser_lab as browser_lab, originate
from .test_browser_paging_ha import browser_lab as paging_browser_lab, _ringing
from .test_group_pickup_regressions_ha import prepare_browser_group
from .test_paging_ha import manager, announce

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("name, data", [
    ("pickup", {"device_id": "picker", "target_device_id": "target"}),
    ("announce", {"targets": ["target"], "media": "media-source://test"}),
])
@pytest.mark.parametrize("override", [None, 0, 300])
async def test_public_answer_windows_do_not_inject_another_default(hass, name, data, override):
    calls = []

    async def capture(call):
        calls.append(call.data)

    await services.async_register_services(hass, {name: capture})
    if override is not None:
        data = {**data, "answer_timeout": override}
    await hass.services.async_call("voip_stack", name, data, blocking=True)
    assert calls[0].get("answer_timeout") == override


@pytest.mark.parametrize("override", [0, 1])
async def test_pickup_override_reaches_the_existing_forward_owner(browser_lab, override):
    lab = browser_lab
    lab.runtime.transport_config["default_ringing_timeout"] = 3
    lab.runtime.endpoints.update("bedroom0", policy=PhonePolicy(ringing_timeout=0))
    lab.runtime.endpoints.update("bedroom1", policy=PhonePolicy(ringing_timeout=.1))
    for phone in lab.phones:
        phone.auto_answer = False
    session = await originate(lab)
    target = lab.runtime.endpoints.require("bedroom0")
    picker = lab.runtime.endpoints.require("bedroom1")
    task = asyncio.create_task(lab.hass.services.async_call("voip_stack", "pickup", {
        "device_id": picker.device_id, "target_device_id": target.device_id,
        "answer_timeout": override,
    }, blocking=True, return_response=True))
    try:
        await asyncio.wait_for(lab.phones[1].invited.wait(), 2)
        await asyncio.sleep(.2)
        assert not task.done(), "The explicit pickup value must override the account's .1-second limit"
        assert not any(request.method == "CANCEL" for request in lab.phones[1].requests)
        lab.phones[1].answer()
        result = await asyncio.wait_for(task, 3)
        assert result["state"] == "in_call" and result["call_id"] == session.call_id
        await lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("override", [None, 0])
async def test_group_pickup_inherits_or_disables_its_single_callback_limit(lab, monkeypatch, override):
    from custom_components.voip_stack import softphone_commands

    lab.runtime.transport_config["default_ringing_timeout"] = .05
    session, route, gate, (_, _, picker) = prepare_browser_group(lab)
    entered = asyncio.Event()

    async def pending_command(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(softphone_commands, "async_resolve_browser_call_command", pending_command)
    data = {"device_id": picker.device_id, "media_client_id": "picker"}
    if override is not None:
        data["answer_timeout"] = override
    task = asyncio.create_task(_pickup_group_phone(ServiceCall(lab.hass, "voip_stack", "pickup", data), picker, session))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(.15)
        if override == 0:
            assert not task.done()
        else:
            assert task.done(), "An inherited group pickup must not keep an implicit 30-second limit"
            with pytest.raises(TimeoutError):
                await task
        assert not route["future"].done() and not gate.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("override", [None, 0, .5])
async def test_sip_announcement_inherits_or_overrides_the_account_budget(lab, monkeypatch, override):
    owner = manager(lab, monkeypatch)
    tts_fixture(monkeypatch, lab)
    lab.runtime.transport_config["default_ringing_timeout"] = .1
    phone = lab.phones[0]
    phone.auto_answer = False
    kwargs = {"targets": ["bedroom0"]}
    if override is not None:
        kwargs["answer_timeout"] = override
    task = asyncio.create_task(paging.async_announce(announce(lab, **kwargs)))
    try:
        await asyncio.wait_for(phone.invited.wait(), 2)
        if override is None:
            result = await asyncio.wait_for(task, 1)
            assert result["state"] == "no_recipients"
            assert any(request.method == "CANCEL" for request in phone.requests)
        else:
            await asyncio.sleep(.2)
            assert not task.done()
            assert not any(request.method == "CANCEL" for request in phone.requests)
            phone.answer()
            result = await asyncio.wait_for(task, 3)
            assert result["state"] == "completed"
        assert not owner.jobs
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("override", [None, 0])
async def test_browser_announcement_has_the_same_inherit_and_zero_semantics(paging_browser_lab, override):
    lab, phone, job = paging_browser_lab
    lab.runtime.transport_config["default_ringing_timeout"] = .05
    job.data = {} if override is None else {"answer_timeout": override}
    task, session = await _ringing(lab, phone, job)
    try:
        await asyncio.sleep(.15)
        if override == 0:
            assert not task.done() and session.live
        else:
            assert task.done()
            with pytest.raises(ServiceValidationError):
                await task
            assert not session.live
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("override", [None, 0, 5])
def test_saved_paging_answer_value_distinguishes_inheritance_zero_and_explicit_five(override):
    data = {"name": "Paging", "members": ["desk"]}
    if override is not None:
        data["answer_timeout"] = override
    assert normalize_paging_group(data)["answer_timeout"] == override
