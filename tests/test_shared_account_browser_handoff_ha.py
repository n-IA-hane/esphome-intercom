"""A mixed account's browser winner retains the caller's canonical media and control."""
import asyncio
from dataclasses import replace

from homeassistant.config_entries import ConfigSubentry
import pytest

from custom_components.voip_stack.browser_phone_data import _ha_softphone_store
from custom_components.voip_stack.browser_phone_projection import _ha_softphone_state
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.phone_config import endpoint_from_subentry
from custom_components.voip_stack.local_softphone_runtime import async_setup_local_softphone_bridge
from custom_components.voip_stack.sip_registrar import SipAccount
from custom_components.voip_stack.store import config_entry
from tests.test_shared_account_fork_ha import (
    assert_no_active_calls,
    shared_account as shared_account,
    canonical_browser as canonical_browser,
    logical_browser as logical_browser,
    registered_phones as registered_phones,
    trunks as trunks,
    lab as lab,
)

pytestmark = pytest.mark.ha


@pytest.mark.parametrize('trunks', [True], indirect=True)
@pytest.mark.parametrize('logical_browser', [{'ringing_timeout': 20}], indirect=True)
async def test_mixed_account_browser_winner_retains_source_media_and_control(shared_account):
    runtime, _, handsets = shared_account
    entry = config_entry(runtime.hass)
    data = {**runtime.browser_subentry.data, 'endpoint_id': 'source', 'name': 'Source',
            'username': 'Source', 'password': 'test-only-password', 'extension': '223',
            'ha_bindings': [{'source': 'migration', 'reference': 'legacy_ha_phone:source'}]}
    source = ConfigSubentry(subentry_type='phone', title='Source', unique_id='phone:source', data=data)
    runtime.hass.config_entries.async_add_subentry(entry, source)
    runtime.runtime.endpoints.register(replace(endpoint_from_subentry(entry, source), device_id='source-device'))
    async_setup_local_softphone_bridge(runtime.hass)
    registrar = runtime.calls.component('registrar')
    registrar.update_accounts([*registrar.accounts.values(), SipAccount('Source', 'Source', data['password'], extension='223')])
    _ha_softphone_store(runtime.hass, 'source').update(state='idle', call_id='previous-completed-call')
    reply = None
    try:
        reply = await runtime.hass.services.async_call('voip_stack', 'call', {
            'device_id': 'source-device', 'destination': 'Casa', 'media_client_id': 'source-card',
        }, blocking=True, return_response=True)
        endpoint = runtime.runtime.endpoints.require('source')
        assert endpoint.active_call_id
        assert reply['call']['call_id'] == endpoint.active_call_id, reply
        assert reply['call']['state'] in {'calling', 'connecting', 'remote_ringing', 'ringing', 'in_call'}
        call_id = reply['call']['call_id']
        async with asyncio.timeout(5):
            while _ha_softphone_state(runtime.hass, runtime.browser_endpoint_id)['state'] != 'ringing':
                await asyncio.sleep(.01)
        for handset in handsets:
            await asyncio.to_thread(handset.wait_for, 'Incoming call', 4)
        await runtime.hass.services.async_call('voip_stack', 'answer', {
            'device_id': 'browser-casa', 'call_id': call_id, 'media_client_id': 'destination-card',
        }, blocking=True)
        async with asyncio.timeout(5):
            while runtime.calls.get_session(call_id).state != 'in_call':
                await asyncio.sleep(.01)
        current = runtime.calls.get_session(call_id)
        assert _ha_softphone_state(runtime.hass, 'source')['can_attach_media'], (
            current.owner, current.endpoint_claims, [(x.leg_id,x.endpoint_id,x.role,x.phase) for x in current.legs.values()]
        )
        assert 'source' in current.endpoint_claims
        await runtime.hass.services.async_call('voip_stack', 'hangup', {
            'device_id': 'source-device', 'call_id': reply['call']['call_id'],
        }, blocking=True)
        async with asyncio.timeout(5):
            while runtime.calls.sessions:
                await asyncio.sleep(.01)
        await assert_no_active_calls(handsets)
    finally:
        for session in tuple(runtime.calls.sessions.values()):
            barrier = runtime.calls.request_termination(session.call_id, TerminationIntent('test_cleanup'))
            if barrier is not None:
                await barrier
