"""Native HA action selectors save the public dial string of a phone."""

from copy import deepcopy

import pytest
from homeassistant.helpers.selector import SelectSelector
from homeassistant.helpers.service import async_get_all_descriptions

from custom_components.voip_stack.phone_directory import PhoneDirectory
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.services import async_update_phone_service_selectors
from .test_managed_phones_ha import (
    custom_integration_path as custom_integration_path,
    phone_lab as phone_lab,
    managed as managed,
)

pytestmark = pytest.mark.ha


async def register_actions(hass):
    async def handler(call):
        return None

    for name in ('call', 'forward', 'transfer', 'pickup', 'set_phone_queues', 'provision_phone', 'purge_devices'):
        hass.services.async_register('voip_stack', name, handler)


def configured_rows():
    return [
        RosterEntry('Ws3', name='Ws3', extension='102', metadata={
            'endpoint_kind': 'esphome', 'endpoint_id': 'private-ws3', 'device_id': '54da57478a40e69420df7586730f6706'}),
        RosterEntry('Casa', name='Casa', extension='101', metadata={
            'endpoint_kind': 'sip_account', 'endpoint_id': 'private-casa', 'device_id': 'private-device'}),
        RosterEntry('private-queue', name='Assistenza', metadata={'virtual_endpoint': 'queue'}),
        RosterEntry('contact-entry', name='Citofono', number='7998'),
    ]


async def descriptions(lab):
    await async_update_phone_service_selectors(lab.hass, lab.runtime.directory)
    return (await async_get_all_descriptions(lab.hass))['voip_stack']


async def test_action_selectors_save_public_strings_and_allow_manual_number(phone_lab):
    lab = phone_lab
    await register_actions(lab.hass)
    lab.runtime.directory = PhoneDirectory(configured_rows())
    actions = await descriptions(lab)
    source = actions['call']['fields']['device_id']['selector']['select']
    destination = actions['call']['fields']['destination']['selector']['select']
    assert source['options'] == ['Casa', 'Ws3']
    assert destination['options'] == ['Assistenza', 'Casa', 'Citofono', 'Ws3']
    assert SelectSelector(source)('Ws3') == 'Ws3'
    assert SelectSelector(destination)('Casa') == 'Casa'
    assert SelectSelector(destination)('101') == '101'
    assert SelectSelector(destination)('Reception@Hotel') == 'Reception@Hotel'
    assert SelectSelector(source)('102') == '102'
    assert '54da57478a40e69420df7586730f6706' not in repr(actions)
    assert actions['pickup']['fields']['target_device_id']['selector']['select']['options'] == ['Casa', 'Ws3']
    advanced = actions['transfer']['fields']['advanced']['fields']
    assert advanced['device_id']['selector']['select']['options'] == ['Casa', 'Ws3']
    assert 'device' in actions['provision_phone']['fields']['device_id']['selector']
    assert 'device' in actions['purge_devices']['fields']['device_id']['selector']


async def test_directory_changes_refresh_current_service_choices(phone_lab):
    lab = phone_lab
    await register_actions(lab.hass)
    lab.runtime.directory = PhoneDirectory(configured_rows())
    old = deepcopy(await descriptions(lab))
    await lab.hass.async_block_till_done()
    updates = []
    unsubscribe = lab.hass.bus.async_listen('service_registered', lambda event: updates.append(event.data))
    lab.runtime.directory.replace([RosterEntry('Studio', name='Studio', metadata={'endpoint_kind': 'sip_account'})])
    current = await descriptions(lab)
    assert old['call']['fields']['destination']['selector']['select']['options'] != ['Studio']
    assert current['call']['fields']['destination']['selector']['select']['options'] == ['Studio']
    assert current['call']['fields']['device_id']['selector']['select']['options'] == ['Studio']
    await lab.hass.async_block_till_done()
    assert updates == [{'domain': 'voip_stack', 'service': 'call'}]
    updates.clear()
    await descriptions(lab)
    await lab.hass.async_block_till_done()
    assert not updates
    unsubscribe()


async def test_normal_phonebook_refresh_publishes_native_action_choices(managed):
    from custom_components.voip_stack.phonebook_coordinator import entry_phonebook

    lab = managed
    await register_actions(lab.hass)
    await lab.controller.async_sync()
    owner = entry_phonebook(lab.hass, lab.runtime)
    try:
        await owner.async_start()
        current = (await async_get_all_descriptions(lab.hass))['voip_stack']
        assert 'Ingresso' in current['call']['fields']['device_id']['selector']['select']['options']
        assert 'Ingresso' in current['call']['fields']['destination']['selector']['select']['options']
    finally:
        await owner.async_close()
