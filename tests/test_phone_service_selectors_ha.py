"""HA controls select devices and dial destinations accept public strings."""

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import subprocess

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
    source = actions['call']['fields']['device_id']['selector']
    destination = actions['call']['fields']['destination']['selector']['select']
    assert 'device' in source
    assert destination['options'] == [
        {'value': 'Assistenza', 'label': 'Assistenza'},
        {'value': '101', 'label': 'Casa'},
        {'value': '7998', 'label': 'Citofono'},
        {'value': '102', 'label': 'Ws3'},
    ]
    assert SelectSelector(destination)('Casa') == 'Casa'
    assert SelectSelector(destination)('101') == '101'
    assert SelectSelector(destination)('Reception@Hotel') == 'Reception@Hotel'
    assert '54da57478a40e69420df7586730f6706' not in repr(actions)
    assert 'device' in actions['pickup']['fields']['target_device_id']['selector']
    advanced = actions['transfer']['fields']['advanced']['fields']
    assert 'device' in advanced['device_id']['selector']
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
    assert current['call']['fields']['destination']['selector']['select']['options'] == [{'value': 'Studio', 'label': 'Studio'}]
    assert current['call']['fields']['device_id']['selector'] == old['call']['fields']['device_id']['selector']
    await lab.hass.async_block_till_done()
    assert updates == [{'domain': 'voip_stack', 'service': 'call'}]
    updates.clear()
    await descriptions(lab)
    await lab.hass.async_block_till_done()
    assert not updates
    unsubscribe()


async def test_selected_addresses_keep_duplicate_labels_distinct_and_use_known_routes(phone_lab):
    from custom_components.voip_stack.router import resolve_ha_router, RouteAction, RouteReason

    lab = phone_lab
    await register_actions(lab.hass)
    rows = [
        RosterEntry('alice', name='Kitchen', extension='201', metadata={'endpoint_kind': 'sip_account', 'local_ha': True}),
        RosterEntry('bob', name='Kitchen', extension='202', metadata={'endpoint_kind': 'sip_account', 'local_ha': True}),
        RosterEntry('charlie', name='Kitchen', metadata={'endpoint_kind': 'sip_account', 'local_ha': True}),
        RosterEntry('external-a', name='Supplier', number='+12025550123'),
        RosterEntry('external-b', name='Supplier', number='+12025550124'),
        RosterEntry('group:ring:private', name='Ring', extension='601', metadata={'group_type': 'ring'}),
        RosterEntry('queue:private', name='Reception', metadata={'virtual_endpoint': 'queue'}),
        RosterEntry('group:conference:private', name='Conference', metadata={'group_type': 'conference'}),
    ]
    lab.runtime.directory = PhoneDirectory(rows)
    actions = await descriptions(lab)
    selector = actions['call']['fields']['destination']['selector']['select']
    options = selector['options']
    assert sorted(option['value'] for option in options if option['label'] == 'Kitchen') == ['201', '202', 'charlie']
    assert sorted(option['value'] for option in options if option['label'] == 'Supplier') == ['+12025550123', '+12025550124']
    assert 'device' in actions['call']['fields']['device_id']['selector']
    assert 'device' in actions['pickup']['fields']['target_device_id']['selector']
    model = Path(__file__).parents[1] / 'custom_components/voip_stack/frontend/voip-stack-card-model.js'
    script = (
        f'import {{publicDialTarget, targetFromRosterEntry}} from {json.dumps(model.as_uri())};'
        f'const rows={json.dumps([asdict(row) for row in rows])};'
        'console.log(JSON.stringify(rows.map(row=>publicDialTarget(targetFromRosterEntry(row)))));'
    )
    result = subprocess.run(['node', '--input-type=module', '-e', script], check=True, capture_output=True, text=True)
    addresses = json.loads(result.stdout)
    assert addresses == ['201', '202', 'charlie', '+12025550123', '+12025550124', '601', 'Reception', 'Conference']
    assert set(addresses) == {option['value'] for option in options}
    for row, destination in zip(rows, addresses, strict=True):
        assert SelectSelector(selector)(destination) == destination
        route = resolve_ha_router(destination, rows, trunk_ready=True)
        assert route.entry is row
        assert route.action is not RouteAction.REJECT
        assert route.reason is not RouteReason.DIRECT_URI
    assert resolve_ha_router('Kitchen', rows).status == 485
    for value in ['Kitchen', 'alice', '*42#', 'sip:MiXeD%20User@pbx.example;transport=tcp']:
        assert SelectSelector(selector)(value) == value


async def test_normal_phonebook_refresh_publishes_native_action_choices(managed):
    from custom_components.voip_stack.phonebook_coordinator import entry_phonebook

    lab = managed
    await register_actions(lab.hass)
    await lab.controller.async_sync()
    owner = entry_phonebook(lab.hass, lab.runtime)
    try:
        await owner.async_start()
        current = (await async_get_all_descriptions(lab.hass))['voip_stack']
        assert 'device' in current['call']['fields']['device_id']['selector']
        assert any(option['label'] == 'Ingresso' for option in current['call']['fields']['destination']['selector']['select']['options'])
    finally:
        await owner.async_close()


PHONE_ACTION_FIELDS = [
    ('call', ('device_id',)),
    ('set_phone_memberships', ('phone',)),
    ('answer', ('device_id',)), ('decline', ('device_id',)),
    ('forward', ('device_id',)), ('transfer', ('advanced', 'device_id')),
    ('hangup', ('device_id',)), ('set_external_call_access', ('device_id',)),
    ('set_dnd', ('device_id',)), ('set_auto_answer', ('device_id',)),
    ('set_send_video', ('device_id',)), ('set_phone_settings', ('device_id',)),
    ('set_ha_softphone_settings', ('device_id',)), ('play_media', ('device_id',)),
    ('tts_say', ('advanced', 'device_id')), ('wait_unanswered', ('advanced', 'device_id')),
    ('wait_for_dtmf', ('advanced', 'device_id')),
    ('set_voicemail_settings', ('device_id',)), ('get_voicemail', ('mailbox',)),
    ('transcribe_voicemail', ('mailbox',)), ('send_to_voicemail', ('mailbox',)),
    ('park', ('device_id',)), ('start_recording', ('device_id',)),
    ('stop_recording', ('device_id',)), ('get_recording', ('device_id',)),
    ('delete_recording', ('device_id',)), ('get_phone_state', ('device_id',)),
    ('pickup', ('device_id',)), ('pickup', ('target_device_id',)),
]


def phone_field(actions, service, path):
    fields = actions[service]['fields']
    for part in path[:-1]:
        fields = fields[part]['fields']
    return fields[path[-1]]


@pytest.mark.parametrize('service,path', PHONE_ACTION_FIELDS)
async def test_native_phone_action_selectors_filter_account_models(phone_lab, service, path):
    from homeassistant.helpers.selector import DeviceSelector
    from custom_components.voip_stack.endpoint_device import (
        BROWSER_PHONE_DEVICE_MODEL, SIP_ACCOUNT_DEVICE_MODEL,
    )
    lab = phone_lab
    async def handler(call):
        return None
    lab.hass.services.async_register('voip_stack', service, handler)
    actions = (await async_get_all_descriptions(lab.hass))['voip_stack']
    config = phone_field(actions, service, path)['selector']['device']
    # Validate the supported HA schema itself. DeviceSelector validates IDs;
    # its model filters are applied by the native frontend device picker.
    selector = DeviceSelector(config)
    assert selector.config['filter'] == [
        {'integration': 'voip_stack', 'model': SIP_ACCOUNT_DEVICE_MODEL},
        {'integration': 'voip_stack', 'model': BROWSER_PHONE_DEVICE_MODEL},
    ]


async def test_account_models_cover_managed_esp_sip_and_browser_without_claiming_generic_esp(managed):
    from homeassistant.core import ServiceCall
    from homeassistant.exceptions import ServiceValidationError
    from homeassistant.helpers import device_registry as dr
    from pytest_homeassistant.common import MockConfigEntry
    from custom_components.voip_stack.endpoint_device import async_ensure_endpoint_device
    from custom_components.voip_stack.phone_control import PhoneOperation
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
    from .test_managed_phones_ha import account_service_device

    lab = managed
    await lab.controller.async_sync()
    registry = dr.async_get(lab.hass)
    selected = [('managed_esp', account_service_device(lab))]
    for kind in ('sip_account', 'browser'):
        endpoint = PhoneEndpoint(endpoint_id='selector-' + kind, name='Selector ' + kind,
                                 username='selector_' + kind, kind=kind)
        lab.runtime.endpoints.register(endpoint)
        device = async_ensure_endpoint_device(lab.hass, lab.entry, endpoint, lab.runtime.endpoints)
        selected.append((kind, device))
    for kind, device in selected:
        assert device.model == 'SIP account', kind
        assert device.config_entry_id == lab.entry.entry_id
        assert lab.runtime.endpoints.by_device_id(device.id) is not None
    generic_entry = MockConfigEntry(domain='esphome', data={'device_name': 'kitchen_power_strip'})
    generic_entry.add_to_hass(lab.hass)
    generic = registry.async_get_or_create(config_entry_id=generic_entry.entry_id,
        identifiers={('esphome', 'kitchen-power-strip')}, name='Kitchen power strip',
        manufacturer='Espressif', model='esp01_1m')
    assert lab.runtime.endpoints.by_device_id(generic.id) is None
    with pytest.raises(ServiceValidationError):
        await lab.runtime.phones.resolve_source(ServiceCall(lab.hass, 'voip_stack', 'call',
            {'device_id': generic.id}), PhoneOperation.ORIGINATE)


async def test_provisioning_admin_and_trunk_selectors_keep_their_existing_scope(phone_lab):
    lab = phone_lab
    async def handler(call):
        return None
    for service in ('purge_devices', 'provision_phone', 'get_phone_provisioning_state',
                    'rotate_phone_provisioning_token', 'call', 'send_to_voicemail'):
        lab.hass.services.async_register('voip_stack', service, handler)
    actions = (await async_get_all_descriptions(lab.hass))['voip_stack']
    assert phone_field(actions, 'purge_devices', ('device_id',))['selector']['device'] == {
        'filter': [{'integration': 'esphome'}], 'multiple': False}
    for service in ('provision_phone', 'get_phone_provisioning_state', 'rotate_phone_provisioning_token'):
        assert phone_field(actions, service, ('device_id',))['selector']['device'] == {'integration': 'voip_stack', 'multiple': False}
    assert phone_field(actions, 'call', ('trunk_id',))['selector']['device'] == {
        'filter': [{'integration': 'voip_stack', 'model': 'SIP trunk'}], 'multiple': False}
    assert phone_field(actions, 'send_to_voicemail', ('advanced', 'device_id'))['selector']['device'] == {
        'filter': [{'integration': 'voip_stack', 'model': 'Home Assistant softphone'}], 'multiple': False}
