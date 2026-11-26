"""Configured ringing budgets terminate real SIP legs and retain caller policy."""

import asyncio

import pytest
from homeassistant.core import Context, ServiceCall
from homeassistant.exceptions import ServiceValidationError

from custom_components.voip_stack.automation_originate import async_originate_automation_call
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.phone_policy import PhonePolicy

from .test_browser_phone_policy_ha import browser_lab as browser_lab, originate
from .test_automation_originate_ha import lab as lab

pytestmark = pytest.mark.ha


async def wait_until(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def test_inherited_global_timeout_cancels_sip_and_reports_no_answer(browser_lab):
    lab = browser_lab
    lab.runtime.transport_config['default_ringing_timeout'] = 0.15
    lab.phones[0].auto_answer = False
    session = await originate(lab)
    await wait_until(lambda: not lab.calls.sessions)
    assert session.terminal_reason == 'no_answer'
    assert session.termination_intent.response_status == 480
    assert any(request.method == 'CANCEL' for request in lab.phones[0].requests)


@pytest.mark.parametrize('global_timeout, account_timeout', [(0, None), (0.1, 0)])
async def test_unlimited_setting_retains_ringing_until_user_hangs_up(browser_lab, global_timeout, account_timeout):
    lab = browser_lab
    lab.runtime.transport_config['default_ringing_timeout'] = global_timeout
    lab.runtime.endpoints.update('bedroom0', policy=PhonePolicy.from_data({'ringing_timeout': account_timeout}))
    lab.phones[0].auto_answer = False
    session = await originate(lab)
    await asyncio.sleep(0.2)
    assert session.live and session.state == 'remote_ringing'
    assert lab.calls.resource_for(session.call_id, 'deadline:phone_policy') is None
    assert not any(request.method == 'CANCEL' for request in lab.phones[0].requests)
    await lab.calls.request_termination(session.call_id, TerminationIntent('local_hangup'))
    assert any(request.method == 'CANCEL' for request in lab.phones[0].requests)


async def test_inherited_account_no_answer_routes_the_same_browser_caller(browser_lab):
    lab = browser_lab
    lab.runtime.transport_config['default_ringing_timeout'] = 0.15
    lab.runtime.endpoints.update('bedroom0', policy=PhonePolicy.from_data({
        'no_answer': {'kind': 'contact', 'target': 'bedroom1'},
    }))
    lab.phones[0].auto_answer = False
    session = await originate(lab)
    token = session.token
    await wait_until(lambda: session.state == 'in_call')
    assert session.token == token
    assert len(lab.phones[1].invites) == 1
    assert any(request.method == 'CANCEL' for request in lab.phones[0].requests)
    await lab.calls.request_termination(session.call_id, TerminationIntent('test_done'))


@pytest.mark.parametrize('override', [0, 0.12])
async def test_public_forward_override_reaches_sip_destination(browser_lab, override):
    lab = browser_lab
    lab.runtime.transport_config['default_ringing_timeout'] = 0.1 if override == 0 else 0
    for phone in lab.phones:
        phone.auto_answer = False
    session = await originate(lab)
    task = await lab.calls.forward_call(call_id=session.call_id, destination='bedroom1',
        ringing_timeout=override, on_failure='terminate')
    await wait_until(lambda: bool(lab.phones[1].invites))
    if override == 0:
        await asyncio.sleep(0.2)
        assert session.live and not task.done()
        assert not any(request.method == 'CANCEL' for request in lab.phones[1].requests)
        lab.phones[1].answer()
        assert (await asyncio.wait_for(task, 3)).status == 'connected'
        await lab.calls.request_termination(session.call_id, TerminationIntent('test_done'))
    else:
        result = await asyncio.wait_for(task, 3)
        assert result.reason == 'no_answer'
        await wait_until(lambda: not lab.calls.sessions)
        assert any(request.method == 'CANCEL' for request in lab.phones[1].requests)


async def test_forward_to_pending_browser_inherits_session_deadline(browser_lab):
    lab = browser_lab
    lab.runtime.transport_config['default_ringing_timeout'] = 0.15
    lab.phones[0].auto_answer = False
    session = await originate(lab)
    task = await lab.calls.forward_call(call_id=session.call_id, destination='kitchen', on_failure='terminate')
    assert (await asyncio.wait_for(task, 3)).status == 'ringing'
    await wait_until(lambda: not session.live)
    assert session.terminal_reason == 'no_answer'


@pytest.mark.parametrize('timeout', [None, 0, 0.15])
async def test_automation_call_inherits_global_or_honors_explicit_override(lab, timeout):
    lab.runtime.transport_config['default_ringing_timeout'] = 0.1
    phone = lab.phones[0]
    phone.auto_answer = False
    data = {'source_automation': 'Wakeup-Caller', 'destination': 'bedroom0'}
    if timeout is not None:
        data['answer_timeout'] = timeout
    task = asyncio.create_task(async_originate_automation_call(
        ServiceCall(lab.hass, 'voip_stack', 'call', data, context=Context())))
    try:
        await asyncio.wait_for(phone.invited.wait(), 2)
        session = next(iter(lab.calls.sessions.values()))
        if timeout == 0:
            await asyncio.sleep(0.2)
            assert not task.done() and session.live
            phone.answer()
            result = await asyncio.wait_for(task, 3)
            await lab.calls.request_termination(result['call_id'], TerminationIntent('test_done'))
        else:
            with pytest.raises(ServiceValidationError):
                await asyncio.wait_for(task, 3)
            assert session.terminal_reason == 'no_answer'
            assert any(request.method == 'CANCEL' for request in phone.requests)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_pending_browser_handoff_keeps_the_admitted_budget_after_live_edit(browser_lab, monkeypatch):
    from custom_components.voip_stack.forward_operation import ForwardOperation
    lab = browser_lab
    lab.runtime.transport_config['default_ringing_timeout'] = 0.15
    lab.phones[0].auto_answer = False
    session = await originate(lab)
    prepared, release = asyncio.Event(), asyncio.Event()
    original = ForwardOperation.retire_application
    async def gated(operation, **kwargs):
        await original(operation, **kwargs)
        if kwargs.get('pending_browser'):
            prepared.set()
            await release.wait()
    monkeypatch.setattr(ForwardOperation, 'retire_application', gated)
    task = await lab.calls.forward_call(call_id=session.call_id, destination='kitchen', on_failure='terminate')
    await asyncio.wait_for(prepared.wait(), 2)
    lab.runtime.transport_config['default_ringing_timeout'] = 0
    release.set()
    assert (await asyncio.wait_for(task, 3)).status == 'ringing'
    await wait_until(lambda: not session.live)
    assert session.terminal_reason == 'no_answer'
