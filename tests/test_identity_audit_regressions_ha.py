"""Native migration and configuration regressions from the consolidation audit."""

from copy import deepcopy
import json

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from pytest_homeassistant.common import MockConfigEntry

from .test_managed_phones_ha import (
    custom_integration_path as custom_integration_path,
    phone_lab as phone_lab,
)

pytestmark = pytest.mark.ha


def add_account(hass, entry, old, username):
    row = ConfigSubentry(subentry_type='phone', title=username, unique_id='phone:' + old,
        data={'kind': 'sip_account', 'name': username, 'username': username,
              'password': 'fixture-account-secret', 'username_source': 'user'})
    hass.config_entries.async_add_subentry(entry, row)
    return row


async def test_queue_agent_registry_identity_survives_account_migration(hass):
    from custom_components.voip_stack.group_config import normalize_group_data, sync_group_devices
    from custom_components.voip_stack.sip_account_identity import async_migrate_account_identities
    entry = MockConfigEntry(domain='voip_stack', version=7, minor_version=3,
        data={'initial_phone_created': True, 'group_membership_version': 1})
    entry.add_to_hass(hass)
    account = add_account(hass, entry, 'sip:old-desk', 'Desk @ literal')
    group = normalize_group_data({'kind': 'queue', 'name': 'Support', 'members': ['sip:old-desk']})
    queue = ConfigSubentry(subentry_type='group', title='Support', unique_id='group:' + group['group_id'], data=group)
    hass.config_entries.async_add_subentry(entry, queue)
    sync_group_devices(hass, entry)
    device = dr.async_get(hass).async_get_device_by_identifier(('voip_stack', 'group:' + group['group_id']), entry.entry_id)
    entities = er.async_get(hass)
    selected = entities.async_get_or_create('select', 'voip_stack', f"queue_agent:{group['group_id']}:sip:old-desk",
        config_entry=entry, config_subentry_id=queue.subentry_id, device_id=device.id)
    selected = entities.async_update_entity(selected.entity_id, name='Operator chosen name')
    native_id, entity_id = selected.id, selected.entity_id
    await async_migrate_account_identities(hass, entry)
    saved = entities.async_get(entity_id)
    assert saved.id == native_id and saved.name == 'Operator chosen name'
    assert saved.device_id == device.id and saved.config_subentry_id == queue.subentry_id
    assert saved.unique_id == f"queue_agent:{group['group_id']}:Desk @ literal"
    assert queue.data['members'] == ['Desk @ literal']
    assert account.unique_id == 'phone:Desk @ literal'
    await async_migrate_account_identities(hass, entry)
    assert entities.async_get(entity_id).id == native_id


@pytest.mark.parametrize('username', ['Desk', 'sip:Desk'])
async def test_previous_username_can_be_reused_without_redirecting_native_account_selectors(phone_lab, username):
    from custom_components.voip_stack.endpoint_device import async_ensure_endpoint_device
    from custom_components.voip_stack.phone_config import sync_registry_from_entry
    from custom_components.voip_stack.sip_account_identity import (
        async_migrate_account_identities, canonical_ha_destination, ha_account_username,
        ha_event_payload, resolve_ha_endpoint,
    )
    from custom_components.voip_stack.trigger import matches_call
    lab = phone_lab
    original = add_account(lab.hass, lab.entry, username, username)
    sync_registry_from_entry(lab.hass, lab.entry)
    old_device = async_ensure_endpoint_device(lab.hass, lab.entry, lab.runtime.endpoints.require(username), lab.runtime.endpoints)
    await async_migrate_account_identities(lab.hass, lab.entry,
        account_updates={original.subentry_id: {**original.data, 'name': 'Office', 'username': 'Office'}})
    replacement = add_account(lab.hass, lab.entry, username, username)
    sync_registry_from_entry(lab.hass, lab.entry)
    new_device = async_ensure_endpoint_device(lab.hass, lab.entry, lab.runtime.endpoints.require(username), lab.runtime.endpoints)
    assert new_device.id != old_device.id
    assert ha_account_username(lab.hass, old_device.id) == 'Office'
    assert ha_account_username(lab.hass, new_device.id) == username
    assert resolve_ha_endpoint(lab.runtime.endpoints, username, entry=lab.entry).endpoint_id == username
    assert canonical_ha_destination(lab.hass, username) == username
    for uri in ('sip:Desk@pbx.example', 'Desk@pbx.example'):
        assert canonical_ha_destination(lab.hass, uri) == uri
    assert matches_call({'source_account_username': 'Office'}, {'caller_account': old_device.id}, hass=lab.hass)
    assert not matches_call({'source_account_username': username}, {'caller_account': old_device.id}, hass=lab.hass)
    for current, native in (('Office', old_device.id), (username, new_device.id)):
        projected = ha_event_payload(lab.hass, {'endpoint_id': current, 'account_username': current})
        assert projected['account_username'] == current and projected['account_device_id'] == native
    assert original.subentry_id != replacement.subentry_id
    lab.hass.config_entries.async_remove_subentry(lab.entry, replacement.subentry_id)
    sync_registry_from_entry(lab.hass, lab.entry)
    assert canonical_ha_destination(lab.hass, username) == username
    assert resolve_ha_endpoint(lab.runtime.endpoints, username, entry=lab.entry) is None


async def test_corrupt_managed_record_is_retained_while_healthy_account_migrates_and_native_repair_recovers(phone_lab):
    from homeassistant.components.repairs import repairs_flow_manager
    from homeassistant.components.repairs.issue_handler import async_setup
    from custom_components.voip_stack.sip_account_identity import async_migrate_account_identities
    lab = phone_lab
    healthy = add_account(lab.hass, lab.entry, 'sip:healthy', 'Healthy')
    broken = {'device_id': lab.device.id, 'origin_entry_id': lab.native.entry_id,
              'mac': '84c7bb000001', 'username': '', 'password': 'fixture-retained-password',
              'endpoint_id': 'esphome:' + lab.device.id, 'display_name': 'Retained name'}
    lab.hass.config_entries.async_update_entry(lab.entry, version=7, minor_version=4,
        data={**lab.entry.data, 'managed_esphome_phones': {lab.device.id: broken}})
    before = deepcopy(broken)
    await async_migrate_account_identities(lab.hass, lab.entry)
    assert healthy.unique_id == 'phone:Healthy'
    assert lab.entry.data['managed_esphome_phones'][lab.device.id] == before
    issue_id = 'managed_record_' + lab.entry.entry_id + '_' + lab.device.id
    assert ir.async_get(lab.hass).async_get_issue('voip_stack', issue_id).is_fixable
    lab.hass.config.components.add('voip_stack')
    lab.hass.data.setdefault('repairs', {})
    async_setup(lab.hass)
    manager = repairs_flow_manager(lab.hass)
    result = await manager.async_init('voip_stack', context={'issue_id': issue_id})
    assert result['step_id'] == 'account'
    result = await manager.async_configure(result['flow_id'], {
        'device_id': lab.device.id, 'name': 'Restored account', 'username': 'Recovered'})
    assert result['type'] == 'create_entry', result
    recovered = next(row for row in lab.entry.subentries.values() if row.data.get('username') == 'Recovered')
    assert recovered.data['password'] == broken['password']
    assert recovered.data['device_id'] == lab.device.id and recovered.data['username_source'] == 'user'
    assert not lab.entry.data.get('managed_esphome_phones')
    assert ir.async_get(lab.hass).async_get_issue('voip_stack', issue_id) is None


async def test_corrupt_archive_metadata_does_not_block_healthy_rebind_and_repair_is_record_scoped(hass, tmp_path):
    from homeassistant.components.repairs import repairs_flow_manager
    from homeassistant.components.repairs.issue_handler import async_setup
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.core.media_archive import MediaArchive
    from custom_components.voip_stack.sip_account_identity import async_migrate_account_identities
    entry = MockConfigEntry(domain='voip_stack', version=7, minor_version=3, data={'initial_phone_created': True})
    entry.add_to_hass(hass)
    account = add_account(hass, entry, 'sip:recording-owner', 'Archive account')
    hass.config.media_dirs = {'local': str(tmp_path / 'media')}
    root = tmp_path / 'media/voip_stack'
    archive = MediaArchive(root, hass.async_add_executor_job)
    await archive.load()
    writer = await archive.begin('sip:recording-owner')
    assert writer.offer(b'\x12\x34' * 320, AudioFormat(16000, 's16le', 1, 20), offset=0)
    message = await writer.finish()
    await archive.close()
    damaged_id = 'a' * 32
    restored = {**deepcopy(message), 'id': damaged_id}
    for segment in restored['segments']:
        previous = root / segment['file']
        segment['file'] = segment['file'].replace(message['id'], damaged_id)
        (root / segment['file']).write_bytes(previous.read_bytes())
    damaged = root / (damaged_id + '.json')
    damaged.write_bytes(b'{invalid-metadata')
    original_bad = damaged.read_bytes()
    waves = {path.name: path.read_bytes() for path in root.glob('*.wav')}
    await async_migrate_account_identities(hass, entry)
    assert json.loads((root / (message['id'] + '.json')).read_text())['mailbox_id'] == 'Archive account'
    assert damaged.read_bytes() == original_bad
    assert {path.name: path.read_bytes() for path in root.glob('*.wav')} == waves
    issue_id = 'archive_record_' + entry.entry_id + '_' + damaged.name
    assert ir.async_get(hass).async_get_issue('voip_stack', issue_id).is_fixable
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
        config_subentry_id=account.subentry_id, identifiers={('voip_stack', 'phone_endpoint:Archive account')},
        model='SIP account')
    hass.config.components.add('voip_stack')
    hass.data.setdefault('repairs', {})
    async_setup(hass)
    manager = repairs_flow_manager(hass)
    result = await manager.async_init('voip_stack', context={'issue_id': issue_id})
    result = await manager.async_configure(result['flow_id'], {'account': device.id})
    assert result['type'] == 'form' and result['errors'] == {'base': 'recording_recovery_failed'}
    damaged.write_text(json.dumps(restored))
    result = await manager.async_configure(result['flow_id'], {'account': device.id})
    assert result['type'] == 'create_entry', result
    assert json.loads(damaged.read_text())['mailbox_id'] == 'Archive account'
    assert {path.name: path.read_bytes() for path in root.glob('*.wav')} == waves
    assert ir.async_get(hass).async_get_issue('voip_stack', issue_id) is None


async def test_account_creation_and_crud_membership_share_the_ordered_group_owner(phone_lab):
    from custom_components.voip_stack.phone_config import _add_phone_subentry, replace_sip_account_subentries
    from custom_components.voip_stack.group_config import group_records
    lab = phone_lab
    first = _add_phone_subentry(lab.hass, lab.entry, title='First', data={
        'kind': 'sip_account', 'name': 'First', 'username': 'First', 'password': 'fixture-first',
        'ring_group': 'Reception', 'queues': ['Support']})
    assert 'ring_group' not in first.data and 'queues' not in first.data
    assert next(row for row in group_records(lab.entry, 'ring').values() if row['name'] == 'Reception')['members'] == ['First']
    native_id = first.subentry_id
    replace_sip_account_subentries(lab.hass, lab.entry, [
        {'username': 'First', 'password': 'fixture-first', 'display_name': 'First', 'ring_group': 'Updated'},
        {'username': 'Second', 'password': 'fixture-second', 'display_name': 'Second', 'ring_group': 'Updated'}])
    assert first.subentry_id == native_id
    assert 'ring_group' not in first.data and 'queues' not in first.data
    assert next(row for row in group_records(lab.entry, 'ring').values() if row['name'] == 'Updated')['members'] == ['First', 'Second']
    assert 'First' not in next((row['members'] for row in group_records(lab.entry, 'ring').values() if row['name'] == 'Reception'), [])


async def test_disabled_queue_form_does_not_reactivate_from_legacy_policy(phone_lab):
    from .test_group_configuration_native_ha import add_account as native_account, prepare_flows
    from custom_components.voip_stack.group_config import group_records
    lab = phone_lab
    native_account(lab, 'Operator')
    prepare_flows(lab)
    group = ConfigSubentry(subentry_type='group', title='Support', unique_id='group:disabled',
        data={'group_id': 'disabled', 'kind': 'queue', 'name': 'Support', 'members': ['Operator'],
              'origin': 'manual', 'enabled': False, 'policy': {'enabled': True}})
    lab.hass.config_entries.async_add_subentry(lab.entry, group)
    assert group_records(lab.entry)['disabled']['policy']['enabled'] is False
    result = await lab.hass.config_entries.subentries.async_init((lab.entry.entry_id, 'group'),
        context={'source': 'reconfigure', 'subentry_id': group.subentry_id})
    result = await lab.hass.config_entries.subentries.async_configure(result['flow_id'], {'name': 'Support'})
    assert result['type'] == 'abort' and result['reason'] == 'reconfigure_successful', result
    assert group.data['enabled'] is False and 'enabled' not in group.data['policy']


async def test_phone_event_device_fields_keep_hardware_and_name_the_account_association(phone_lab):
    from custom_components.voip_stack.endpoint_device import async_ensure_endpoint_device
    from custom_components.voip_stack.phone_config import sync_registry_from_entry
    from custom_components.voip_stack.phone_state import phone_states
    from custom_components.voip_stack.sip_account_identity import ha_event_payload
    lab = phone_lab
    row = add_account(lab.hass, lab.entry, 'Phone account', 'Phone account')
    lab.hass.config_entries.async_update_subentry(lab.entry, row, data={**row.data,
        'managed_esphome': True, 'device_id': lab.device.id, 'origin_entry_id': lab.native.entry_id,
        'mac': '84c7bb000001'})
    sync_registry_from_entry(lab.hass, lab.entry)
    account = async_ensure_endpoint_device(lab.hass, lab.entry, lab.runtime.endpoints.require('Phone account'), lab.runtime.endpoints)
    state = phone_states(lab.hass)['Phone account']
    assert state['device_id'] == state['hardware_device_id'] == lab.device.id
    assert state['account_device_id'] == account.id and state['account_username'] == 'Phone account'
    event = ha_event_payload(lab.hass, {'endpoint_id': 'Phone account', 'device_id': lab.device.id})
    assert event['device_id'] == event['hardware_device_id'] == lab.device.id
    assert event['account_device_id'] == account.id


async def test_legacy_native_phone_waits_for_verified_info_then_migrates_without_a_direct_route(phone_lab):
    from custom_components.voip_stack.phone_config import sync_registry_from_entry
    from custom_components.voip_stack.sip_account_identity import async_migrate_account_identities
    lab = phone_lab
    old = 'esphome:' + lab.device.id
    lab.hass.config_entries.async_update_entry(lab.entry, version=7, minor_version=3, data={**lab.entry.data,
        'esphome_mailboxes': {old: {'enabled': True, 'greeting_media': 'media-source://fixture'}},
        'esphome_phone_memberships': {old: {'device_id': lab.device.id, 'queues': ['Support']}},
        'group_membership_version': 1})
    original_info = lab.native.runtime_data.device_info
    lab.native.runtime_data.device_info = None
    await async_migrate_account_identities(lab.hass, lab.entry)
    sync_registry_from_entry(lab.hass, lab.entry)
    assert not lab.runtime.endpoints.endpoints and not lab.entry.subentries
    assert lab.entry.data['esphome_mailboxes'][old]['enabled']
    issue_id = 'esphome_account_setup_' + lab.entry.entry_id + '_' + lab.device.id
    assert ir.async_get(lab.hass).async_get_issue('voip_stack', issue_id) is not None
    lab.hass.config_entries.async_update_entry(lab.entry, minor_version=4)
    lab.native.runtime_data.device_info = original_info
    lab.native.runtime_data.services = {}
    await async_migrate_account_identities(lab.hass, lab.entry)
    sync_registry_from_entry(lab.hass, lab.entry)
    account = next(row for row in lab.entry.subentries.values() if row.subentry_type == 'phone')
    assert account.data['username'] == account.data['name'] == 'Ingresso'
    assert account.data['voicemail']['enabled']
    assert account.data['voicemail']['greeting_media'] == 'media-source://fixture'
    assert account.data['device_id'] == lab.device.id
    assert account.data['origin_entry_id'] == lab.native.entry_id
    assert lab.entry.data['legacy_phone_references'][old] == 'Ingresso'
    assert 'queues' not in account.data and 'ring_group' not in account.data
    group = next(row for row in lab.entry.subentries.values() if row.subentry_type == 'group')
    assert group.data['members'] == ['Ingresso']
    assert not lab.entry.data['esphome_phone_memberships']
    endpoint = lab.runtime.endpoints.require('Ingresso')
    assert endpoint.kind.value == 'sip_account' and endpoint.availability.value == 'offline'
    assert lab.registrar.enabled and lab.entry.data['sip_registrar_enabled']
    assert not lab.sent


async def test_external_native_phone_without_ha_account_provenance_creates_no_pbx_route(phone_lab):
    from custom_components.voip_stack.esphome_phone_inventory import _get_voip_devices
    from custom_components.voip_stack.phone_config import sync_registry_from_entry
    from custom_components.voip_stack.sip_account_identity import async_migrate_account_identities
    lab = phone_lab
    lab.native.runtime_data.services = {}
    await async_migrate_account_identities(lab.hass, lab.entry)
    sync_registry_from_entry(lab.hass, lab.entry)
    assert not lab.entry.subentries and not lab.runtime.endpoints.endpoints
    assert await _get_voip_devices(lab.hass) == []
    assert dr.async_get(lab.hass).async_get(lab.device.id) is not None


async def test_ldap_invalid_records_raise_one_actionable_fault_per_generation(phone_lab):
    from custom_components.voip_stack.ldap_server import LdapDirectoryServer
    from custom_components.voip_stack.phone_directory import PhoneDirectory
    from custom_components.voip_stack.repairs import async_sync_ldap_record_issue
    from custom_components.voip_stack.roster import RosterEntry
    lab = phone_lab
    good = RosterEntry('Healthy', name='Healthy', sip_uri='sip:Healthy@127.0.0.1')
    bad = RosterEntry('Broken', name='x' * 20000, sip_uri='sip:Broken@127.0.0.1')
    directory = PhoneDirectory([good, bad])
    notifications = []
    def fault(generation, rows):
        notifications.append((generation, dict(rows)))
        async_sync_ldap_record_issue(lab.hass, lab.entry, rows)
    server = LdapDirectoryServer(directory, lambda *_: None, fault_callback=fault)
    records = list(server.records())
    assert any(attributes.get('uid') == (b'Healthy',) for _dn, attributes in records)
    assert not any(attributes.get('uid') == (b'Broken',) for _dn, attributes in records)
    list(server.records())
    assert len(notifications) == 1
    issue_id = 'ldap_record_invalid_' + lab.entry.entry_id
    assert ir.async_get(lab.hass).async_get_issue('voip_stack', issue_id).is_fixable
    directory.replace([good])
    server.validate_records()
    assert len(notifications) == 2
    assert ir.async_get(lab.hass).async_get_issue('voip_stack', issue_id) is None


async def test_live_queue_agent_select_keeps_its_native_entity_during_username_rename(phone_lab):
    from custom_components.voip_stack import select
    from custom_components.voip_stack.call_queues import async_setup_queues
    from custom_components.voip_stack.group_config import normalize_group_data, sync_group_devices
    from custom_components.voip_stack.phone_config import sync_registry_from_entry
    from custom_components.voip_stack.sip_account_identity import async_migrate_account_identities
    from .test_native_phone_primitives_ha import platform_for
    lab = phone_lab
    account = add_account(lab.hass, lab.entry, 'Old login', 'Old login')
    group = normalize_group_data({'kind': 'queue', 'name': 'Support', 'members': ['Old login']})
    row = ConfigSubentry(subentry_type='group', title='Support', unique_id='group:' + group['group_id'], data=group)
    lab.hass.config_entries.async_add_subentry(lab.entry, row)
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data, 'group_membership_version': 1})
    sync_registry_from_entry(lab.hass, lab.entry)
    sync_group_devices(lab.hass, lab.entry)
    manager = await async_setup_queues(lab.hass, lab.entry)
    platform = platform_for(lab, 'select')
    def add_entities(values, *, config_subentry_id=None):
        lab.entry.async_create_task(lab.hass, platform.async_add_entities(values,
            config_subentry_id=config_subentry_id), 'test queue selector add')
    await select.async_setup_entry(lab.hass, lab.entry, add_entities)
    await lab.hass.async_block_till_done()
    registry = er.async_get(lab.hass)
    entity_id = registry.async_get_entity_id('select', 'voip_stack', f"queue_agent:{group['group_id']}:Old login")
    assert entity_id is not None
    entity, native_id = platform.entities[entity_id], registry.async_get(entity_id).id
    try:
        await async_migrate_account_identities(lab.hass, lab.entry,
            account_updates={account.subentry_id: {**account.data, 'username': 'New login'}})
        manager.refresh()
        await lab.hass.async_block_till_done()
        assert registry.async_get(entity_id).id == native_id
        assert platform.entities[entity_id] is entity
        assert entity.endpoint_id == 'New login'
        assert entity.unique_id == f"queue_agent:{group['group_id']}:New login"
    finally:
        await platform.async_reset()
        await manager.close()
