"""SIP login owns the account; native HA IDs own configuration and hardware."""

from copy import deepcopy
from urllib.parse import quote

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from pytest_homeassistant.common import MockConfigEntry

from .test_managed_phones_ha import (
    custom_integration_path as custom_integration_path,
    managed as managed, phone_lab as phone_lab, phone_record, phone_subentry,
    bind_account,
)

pytestmark = pytest.mark.ha


async def register_client(lab, phone, *, host='127.0.0.9', port=42000):
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.core.sip_auth import build_digest_authorization

    uri = 'sip:127.0.0.1:5060'
    user = quote(phone.username, safe='-._~')
    auth = build_digest_authorization(challenge_header=lab.registrar._challenge()[1],
        username=phone.username, password=phone.password, method='REGISTER', uri=uri)
    request = sip.parse_message(sip.build_request('REGISTER', uri, [
        ('Via', f'SIP/2.0/UDP {host}:{port};branch=z9hG4bK-independent;rport'),
        ('From', f'<sip:{user}@127.0.0.1>;tag=independent'), ('To', f'<sip:{user}@127.0.0.1>'),
        ('Call-ID', 'independent-register'), ('CSeq', '1 REGISTER'),
        ('Contact', f'<sip:{user}@{host}:{port}>'), ('Expires', '120'),
        ('User-Agent', 'Independent SIP client'), ('Authorization', auth)]))
    assert (await lab.registrar.handle_register(request, (host, port), 'UDP')).status == 200


async def test_new_esp_and_manual_account_use_stored_username_as_logical_identity(managed):
    from custom_components.voip_stack.phone_config import _add_phone_subentry, phone_endpoint_id, sip_account_phone_data

    lab = managed
    await lab.controller.async_sync()
    phone = phone_record(lab)
    assert phone.endpoint_id == phone.username == 'Ingresso'
    row = phone_subentry(lab)
    assert row.data['kind'] == 'sip_account'
    assert row.data['device_id'] == lab.device.id
    assert row.data['origin_entry_id'] == lab.native.entry_id
    assert phone_endpoint_id(row) == 'Ingresso'
    endpoint = lab.runtime.endpoints.get('Ingresso')
    assert endpoint.username == 'Ingresso' and endpoint.kind.value == 'sip_account'
    assert lab.runtime.endpoints.get('esphome:' + lab.device.id) is None
    manual = _add_phone_subentry(lab.hass, lab.entry, title='No extension', data=sip_account_phone_data({
        'username': 'literal Account', 'password': 'fixture-kept-password', 'display_name': 'No extension'}))
    assert phone_endpoint_id(manual) == 'literal Account'
    assert manual.data['password'] == 'fixture-kept-password'


async def test_v7_3_upgrade_rekeys_only_typed_references_and_keeps_native_owners(hass):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.phone_config import phone_endpoint_id

    old = 'esphome:old-hardware-key'
    data = {'initial_phone_created': True,
            'esphome_phone_policies': {old: {'busy': {'kind': 'contact', 'target': 'sip:desk'}}},
            'esphome_mailboxes': {old: {'enabled': True}},
            'esphome_sip_enrollments': {'hardware': {'origin_entry_id': 'native', 'endpoint_id': old}},
            'unrelated': {'literal': old}}
    entry = MockConfigEntry(domain='voip_stack', version=7, minor_version=3, data=data)
    entry.add_to_hass(hass)
    esp = ConfigSubentry(subentry_type='phone', title='Kitchen', unique_id='phone:' + old,
        data={'kind': 'esphome', 'username': 'kitchen literal', 'password': 'fixture-kept-password',
              'display_name': 'Kitchen', 'device_id': 'hardware', 'origin_entry_id': 'native',
              'mac': '84c7bb000001', 'queues': ['Support']})
    other = ConfigSubentry(subentry_type='phone', title='Desk', unique_id='phone:sip:desk',
        data={'kind': 'sip_account', 'name': 'Desk', 'username': 'desk', 'password': 'fixture-desk-password',
              'call_policy': {'busy': {'kind': 'contact', 'target': old}},
              'ha_bindings': [{'source': 'migration', 'reference': old}]})
    paging = ConfigSubentry(subentry_type='paging', title='Page', unique_id='paging:stable',
        data={'paging_group_id': 'stable', 'name': 'Page', 'members': [old, 'sip:desk']})
    for row in (esp, other, paging):
        hass.config_entries.async_add_subentry(entry, row)
    devices = dr.async_get(hass)
    account = devices.async_get_or_create(config_entry_id=entry.entry_id,
        config_subentry_id=esp.subentry_id, identifiers={('voip_stack', 'phone_endpoint:' + old)},
        name='Kitchen')
    from homeassistant.helpers import area_registry as ar
    area = ar.async_get(hass).async_create('Kitchen area')
    devices.async_update_device(account.id, area_id=area.id)
    entity = er.async_get(hass).async_get_or_create('sensor', 'voip_stack', 'phone_endpoint_' + old + '_state',
        config_entry=entry, config_subentry_id=esp.subentry_id, device_id=account.id,
        suggested_object_id='kept_kitchen_state')
    native_ids = (esp.subentry_id, other.subentry_id, paging.subentry_id, account.id, entity.entity_id)
    assert await async_migrate_entry(hass, entry)
    assert entry.minor_version == 4
    assert phone_endpoint_id(esp) == 'kitchen literal'
    assert phone_endpoint_id(other) == 'desk'
    assert esp.data['kind'] == 'sip_account'
    assert esp.data['password'] == 'fixture-kept-password'
    assert paging.data['members'] == ['kitchen literal', 'desk']
    assert other.data['call_policy']['busy']['target'] == 'kitchen literal'
    assert other.data['ha_bindings'][0]['reference'] == old
    assert entry.data['unrelated']['literal'] == old
    assert entry.data['esphome_sip_enrollments']['hardware']['endpoint_id'] == 'kitchen literal'
    assert esp.data['voicemail'] == {'enabled': True}
    assert esp.data['call_policy']['busy']['target'] == 'desk'
    assert not entry.data['esphome_mailboxes']
    assert not entry.data['esphome_phone_policies']
    assert native_ids == (esp.subentry_id, other.subentry_id, paging.subentry_id, account.id, entity.entity_id)
    assert devices.async_get(account.id).area_id == area.id
    assert devices.async_get(account.id).identifiers == {('voip_stack', 'phone_endpoint:kitchen literal')}
    assert er.async_get(hass).async_get(entity.entity_id).unique_id == 'phone_endpoint_kitchen literal_state'
    after = deepcopy(entry.as_dict())
    assert await async_migrate_entry(hass, entry)
    assert entry.as_dict() == after


@pytest.mark.parametrize('collision', ['username', 'device', 'entity'])
async def test_upgrade_collision_reports_repair_without_changing_any_native_record(hass, collision):
    from custom_components.voip_stack import async_migrate_entry

    entry = MockConfigEntry(domain='voip_stack', version=7, minor_version=3, data={'initial_phone_created': True})
    entry.add_to_hass(hass)
    rows = []
    for old, username in [('sip:first', 'first'), ('sip:second', 'first' if collision == 'username' else 'second')]:
        row = ConfigSubentry(subentry_type='phone', title=old, unique_id='phone:' + old,
            data={'kind': 'sip_account', 'name': old, 'username': username, 'password': 'fixture-kept-password'})
        hass.config_entries.async_add_subentry(entry, row)
        rows.append(row)
    devices = dr.async_get(hass)
    devices.async_get_or_create(config_entry_id=entry.entry_id, config_subentry_id=rows[0].subentry_id,
                               identifiers={('voip_stack', 'phone_endpoint:sip:first')})
    if collision == 'device':
        devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={('voip_stack', 'phone_endpoint:first')})
    elif collision == 'entity':
        for unique_id in ['phone_endpoint_sip:first_state', 'phone_endpoint_first_state']:
            er.async_get(hass).async_get_or_create('sensor', 'voip_stack', unique_id, config_entry=entry,
                                                  config_subentry_id=rows[0].subentry_id)
    before = deepcopy(entry.as_dict())
    native = [(row.id, row.identifiers) for row in dr.async_entries_for_config_entry(devices, entry.entry_id)]
    entities = [(row.entity_id, row.unique_id) for row in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)]
    assert await async_migrate_entry(hass, entry) is False
    assert entry.as_dict() == before and entry.minor_version == 3
    assert native == [(row.id, row.identifiers) for row in dr.async_entries_for_config_entry(devices, entry.entry_id)]
    assert entities == [(row.entity_id, row.unique_id) for row in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)]
    assert ir.async_get(hass).async_get_issue('voip_stack', 'phone_identity_conflict_' + entry.entry_id) is not None


async def test_sip_registration_keeps_managed_account_callable_without_native_api(managed):
    from custom_components.voip_stack.account_runtime import recompute_account_availability
    from custom_components.voip_stack.directory_runtime import routing_snapshot
    from custom_components.voip_stack.phone_directory import PhoneDirectory
    from custom_components.voip_stack.phone_config import sync_registry_from_entry
    from custom_components.voip_stack.router import resolve_ha_router, RouteAction

    lab = managed
    await lab.controller.async_sync()
    phone = phone_record(lab)
    password = phone.password
    await bind_account(lab, phone)
    lab.native.runtime_data.available = False
    lab.native.runtime_data.client.is_connected = False
    lab.hass.states.async_set('sensor.phone_node_voip_state', 'unavailable')
    await lab.controller.async_sync()
    sync_registry_from_entry(lab.hass, lab.entry)
    recompute_account_availability(lab.hass)
    endpoint = lab.runtime.endpoints.get(phone.username)
    assert endpoint.availability.value == 'available'
    assert lab.registrar.accounts[phone.username.casefold()].password == password
    assert lab.registrar.registered_contacts(phone.username)
    lab.runtime.directory = PhoneDirectory()
    route = resolve_ha_router(phone.username, routing_snapshot(lab.runtime))
    assert route.entry.id == phone.username and route.action is not RouteAction.REJECT


@pytest.mark.parametrize('api,registered', [(True, False), (False, False), (False, True), (True, True)])
async def test_account_availability_follows_sip_contacts_instead_of_native_api(managed, api, registered):
    from custom_components.voip_stack.account_runtime import recompute_account_availability
    from custom_components.voip_stack.phone_config import sync_registry_from_entry

    lab = managed
    await lab.controller.async_sync()
    phone = phone_record(lab)
    if registered:
        await register_client(lab, phone)
    lab.native.runtime_data.available = api
    lab.native.runtime_data.client.is_connected = api
    await lab.controller.async_sync()
    sync_registry_from_entry(lab.hass, lab.entry)
    recompute_account_availability(lab.hass)
    assert lab.runtime.endpoints.require(phone.username).availability.value == ('available' if registered else 'offline')
    assert phone_subentry(lab).data['password'] == phone.password


@pytest.mark.parametrize('shared', [False, True])
async def test_hardware_removal_respects_exclusive_account_and_shared_sip_client_ownership(managed, shared):
    from custom_components.voip_stack.managed_phones import managed_records

    lab = managed
    await lab.controller.async_sync()
    phone = phone_record(lab)
    row = phone_subentry(lab)
    account = dr.async_get(lab.hass).async_get_device_by_identifier(
        ('voip_stack', 'phone_endpoint:' + phone.username), lab.entry.entry_id)
    await bind_account(lab, phone)
    if shared:
        await register_client(lab, phone)
        lab.hass.config_entries.async_update_subentry(lab.entry, row,
            data={**row.data, 'ha_bindings': [{'source': 'popup', 'reference': 'retained-client'}]})
    dr.async_get(lab.hass).async_remove_device(lab.device.id)
    await lab.controller.async_sync()
    if shared:
        assert row.subentry_id in lab.entry.subentries
        assert row.data['username'] == phone.username and row.data['password'] == phone.password
        assert 'managed_esphome' not in row.data and 'device_id' not in row.data
        assert not managed_records(lab.entry)
        assert dr.async_get(lab.hass).async_get(account.id) is not None
        contacts = lab.registrar.registered_contacts(phone.username)
        assert len(contacts) == 1 and contacts[0].source_host == '127.0.0.9'
        assert lab.runtime.endpoints.require(phone.username).ha_device_id == account.id
    else:
        assert row.subentry_id not in lab.entry.subentries
        assert lab.runtime.endpoints.get(phone.username) is None
        assert not lab.registrar.registered_contacts(phone.username)
        assert phone.username.casefold() not in lab.registrar.accounts


@pytest.mark.parametrize('version,minor,kind', [(6, 4, 'browser'), (7, 0, 'sip_account'), (7, 2, 'sip_account')])
async def test_older_release_login_and_native_subentry_survive_upgrade(hass, version, minor, kind):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.phone_config import phone_endpoint_id

    entry = MockConfigEntry(domain='voip_stack', version=version, minor_version=minor, data={'initial_phone_created': True})
    entry.add_to_hass(hass)
    row = ConfigSubentry(subentry_type='phone', title='Display', unique_id='phone:browser:retained',
        data={'kind': kind, 'name': 'Display', 'username': 'ha_browser_retained', 'password': 'fixture-kept-password'})
    hass.config_entries.async_add_subentry(entry, row)
    identity = row.subentry_id
    assert await async_migrate_entry(hass, entry)
    assert entry.minor_version == 4 and row.subentry_id == identity
    assert phone_endpoint_id(row) == row.data['username'] == ('Display' if kind == 'browser' else 'ha_browser_retained')
    assert row.data['password'] == 'fixture-kept-password'


async def test_equal_display_labels_keep_distinct_username_owners(phone_lab):
    from custom_components.voip_stack.phone_config import _add_phone_subentry, sip_account_phone_data, sync_registry_from_entry
    from custom_components.voip_stack.endpoint_registry import EndpointAmbiguousError

    lab = phone_lab
    for username in ['alice', 'bob']:
        _add_phone_subentry(lab.hass, lab.entry, title='Kitchen', data=sip_account_phone_data({
            'username': username, 'password': 'fixture-kept-password', 'display_name': 'Kitchen'}))
    sync_registry_from_entry(lab.hass, lab.entry)
    assert lab.runtime.endpoints.resolve('alice').endpoint_id == 'alice'
    assert lab.runtime.endpoints.resolve('bob').endpoint_id == 'bob'
    with pytest.raises(EndpointAmbiguousError):
        lab.runtime.endpoints.by_name('Kitchen')


async def test_legacy_managed_binding_joins_only_its_existing_account_without_merging_accounts(hass):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.phone_config import phone_endpoint_id

    legacy = {'device_id': 'hardware', 'origin_entry_id': 'native', 'mac': '84c7bb000001',
              'username': 'kept-login', 'password': 'fixture-kept-password', 'display_name': 'Hardware label'}
    entry = MockConfigEntry(domain='voip_stack', version=7, minor_version=3,
        data={'initial_phone_created': True, 'managed_esphome_phones': {'hardware': legacy}})
    entry.add_to_hass(hass)
    row = ConfigSubentry(subentry_type='phone', title='Account label', unique_id='phone:sip:old',
        data={'kind': 'sip_account', 'name': 'Account label', 'username': legacy['username'],
              'password': legacy['password'], 'ha_bindings': [{'source': 'popup', 'reference': 'kept'}]})
    hass.config_entries.async_add_subentry(entry, row)
    native_id = row.subentry_id
    assert await async_migrate_entry(hass, entry)
    assert list(entry.subentries) == [native_id]
    assert phone_endpoint_id(row) == legacy['username']
    assert row.data['device_id'] == 'hardware' and row.data['name'] == 'Account label'
    assert row.data['managed_exclusive'] is False
    assert row.data['ha_bindings'] == [{'source': 'popup', 'reference': 'kept'}]
    assert 'managed_esphome_phones' not in entry.data


async def test_automation_created_before_upgrade_keeps_native_account_selector_on_ringing(phone_lab):
    from homeassistant.components.automation import DATA_COMPONENT
    from homeassistant.setup import async_setup_component
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.endpoint_device import async_ensure_endpoint_device
    from custom_components.voip_stack.phone_config import sync_registry_from_entry
    from custom_components.voip_stack.const import CALL_EVENT

    lab = phone_lab
    lab.hass.config_entries.async_update_entry(lab.entry, version=7, minor_version=3)
    row = ConfigSubentry(subentry_type='phone', title='Casa', unique_id='phone:browser:former-hardware',
        data={'kind': 'sip_account', 'name': 'Casa', 'username': 'ha_browser_former-hardware',
              'password': 'fixture-kept-password', 'ha_bindings': [{'source': 'migration',
              'reference': 'legacy_ha_phone:browser:former-hardware'}]})
    lab.hass.config_entries.async_add_subentry(lab.entry, row)
    sync_registry_from_entry(lab.hass, lab.entry)
    owner = lab.runtime.endpoints
    native = async_ensure_endpoint_device(lab.hass, lab.entry, owner.require('browser:former-hardware'), owner)
    lab.runtime.sip.bind_endpoint_registry(owner)
    seen = []
    async def notify(call):
        seen.append(dict(call.data))
    lab.hass.services.async_register('identity_test', 'notify', notify)
    configured = {'id': 'retained_account_ring', 'alias': 'Retained account ring',
        'triggers': [{'trigger': 'voip_stack.phone_state_changed', 'options': {'phone': native.id, 'to': 'ringing'}}],
        'actions': [{'action': 'identity_test.notify', 'data': {'message': 'kept-trigger'}}]}
    assert await async_setup_component(lab.hass, 'automation', {'automation': [configured]})
    await lab.hass.async_block_till_done()
    automation = lab.hass.data[DATA_COMPONENT].get_entity('automation.retained_account_ring')
    before = deepcopy(automation.raw_config)
    assert await async_migrate_entry(lab.hass, lab.entry)
    sync_registry_from_entry(lab.hass, lab.entry)
    rebound = async_ensure_endpoint_device(lab.hass, lab.entry, owner.require('Casa'), owner)
    assert rebound.id == native.id
    assert automation.raw_config == before
    session = lab.runtime.sip.upsert('post-upgrade-ring', state='ringing', owner='router',
        caller='Standard SIP caller', callee='Casa', dest_endpoint_id='Casa')
    lab.runtime.sip.add_leg(session.call_id, 'post-upgrade-callee', role='callee',
        state='ringing', endpoint_id='Casa')
    lab.runtime.sip.claim_endpoint(session.call_id, 'Casa', role='destination')
    await lab.hass.async_block_till_done()
    from custom_components.voip_stack.phone_state import phone_states
    assert phone_states(lab.hass)['Casa']['state'] == 'ringing'
    lab.hass.bus.async_fire(CALL_EVENT, {'call_id': session.call_id, 'generation': session.generation})
    await lab.hass.async_block_till_done()
    assert seen == [{'message': 'kept-trigger'}]
    assert automation.raw_config['triggers'][0]['options']['phone'] == native.id
    await lab.runtime.sip.shutdown()


async def test_upgrade_rebinds_queue_ownership_and_archive_access_without_changing_audio(hass, tmp_path):
    from homeassistant.helpers.storage import Store
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.core.media_archive import MediaArchive, ArchiveError

    old, username = 'sip:old-account', 'kept-user'
    entry = MockConfigEntry(domain='voip_stack', version=7, minor_version=3, data={'initial_phone_created': True})
    entry.add_to_hass(hass)
    row = ConfigSubentry(subentry_type='phone', title='Account', unique_id='phone:' + old,
        data={'kind': 'sip_account', 'name': 'Account', 'username': username, 'password': 'fixture-kept-password'})
    hass.config_entries.async_add_subentry(entry, row)
    queue = Store(hass, 1, f'voip_stack.queues.{entry.entry_id}')
    saved = {'agent_states': {'support': {old: 'available', 'other': 'paused'}},
             'agent_stats': {'support': {old: {'consecutive_misses': 1}, 'other': {'consecutive_misses': 2}}},
             'round_robin': {'support': old}, 'last_ended': {old: 17, 'other': 18},
             'policies': {'support': {'agent_order': [old, 'other']}}, 'unknown': {'literal': old}}
    await queue.async_save(saved)
    hass.config.media_dirs = {'local': str(tmp_path / 'media')}
    archive = MediaArchive(tmp_path / 'media/voip_stack', hass.async_add_executor_job)
    await archive.load()
    messages = []
    fmt = AudioFormat(16000, 's16le', 1, 20)
    for mailbox, kind in [(old, 'voicemail'), (old, 'recording'), ('other', 'voicemail')]:
        writer = await archive.begin(mailbox, kind=kind, caller='Kept caller')
        assert writer.offer(b'\x12\x34' * 320, fmt, offset=0)
        messages.append(await writer.finish())
    before = {path.name: path.read_bytes() for path in archive.root.glob('*.wav')}
    other_metadata = (archive.root / (messages[2]['id'] + '.json')).read_bytes()
    assert await async_migrate_entry(hass, entry)
    current = await queue.async_load()
    assert current['agent_states']['support'] == {username: 'available', 'other': 'paused'}
    assert current['agent_stats']['support'][username] == {'consecutive_misses': 1}
    assert current['round_robin']['support'] == username
    assert current['last_ended'] == {username: 17, 'other': 18}
    assert current['policies']['support']['agent_order'] == [username, 'other']
    assert current['unknown']['literal'] == old
    await archive.load()
    assert archive.counts(username) == (1, 0) and archive.counts(old) == (0, 0)
    assert len(archive.list(username)) == len(archive.list(username, kind='recording')) == 1
    for message in messages[:2]:
        assert archive.segment_path(message['id'], username).read_bytes() == before[message['segments'][0]['file']]
        with pytest.raises(ArchiveError):
            archive.segment_path(message['id'], old)
        with pytest.raises(ArchiveError):
            archive.segment_path(message['id'], 'other')
    assert before == {path.name: path.read_bytes() for path in archive.root.glob('*.wav')}
    assert other_metadata == (archive.root / (messages[2]['id'] + '.json')).read_bytes()
    await archive.close()



async def test_saved_ha_endpoint_reference_is_not_a_public_sip_alias(hass):
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    from custom_components.voip_stack.sip_account_identity import ha_event_payload, resolve_ha_endpoint

    old, username = 'sip:old-native-id', 'Account @ 100%'
    entry = MockConfigEntry(domain='voip_stack', version=7, minor_version=4,
        data={'legacy_phone_references': {old: username}})
    entry.add_to_hass(hass)
    registry = EndpointRegistry()
    endpoint = registry.register(PhoneEndpoint(endpoint_id=username, username=username,
        name='Readable account', kind=EndpointKind.SIP_ACCOUNT))
    assert registry.get(old) is None
    assert registry.resolve(old) is None
    assert resolve_ha_endpoint(registry, old, entry=entry) == endpoint
    projected = ha_event_payload(hass, {'endpoint_id': username, 'source_endpoint_id': username})
    assert projected['endpoint_id'] == old
    assert projected['source_endpoint_id'] == old
    assert projected['account_username'] == username
    assert projected['source_account_username'] == username


async def test_native_write_failure_rolls_back_queue_bindings_and_registry_owners(hass, monkeypatch):
    from homeassistant.helpers.storage import Store
    from custom_components.voip_stack.sip_account_identity import async_migrate_account_identities

    old, username = 'sip:former-account', 'Restored account'
    entry = MockConfigEntry(domain='voip_stack', version=7, minor_version=3,
        data={'initial_phone_created': True, 'parking': {'return_destination': old}})
    entry.add_to_hass(hass)
    row = ConfigSubentry(subentry_type='phone', title='Restored account', unique_id='phone:' + old,
        data={'kind': 'sip_account', 'name': 'Restored account', 'username': username,
              'password': 'fixture-kept-password'})
    hass.config_entries.async_add_subentry(entry, row)
    devices, entities = dr.async_get(hass), er.async_get(hass)
    device = devices.async_get_or_create(config_entry_id=entry.entry_id,
        config_subentry_id=row.subentry_id, identifiers={('voip_stack', 'phone_endpoint:' + old)})
    entity = entities.async_get_or_create('sensor', 'voip_stack', 'phone_endpoint_' + old + '_state',
        config_entry=entry, config_subentry_id=row.subentry_id, device_id=device.id,
        suggested_object_id='restored_account_state')
    queue = Store(hass, 1, f'voip_stack.queues.{entry.entry_id}')
    bindings = Store(hass, 1, f'voip_stack.ha_bindings.{entry.entry_id}', atomic_writes=True)
    queue_data = {'agent_states': {'support': {old: 'paused'}}}
    binding_data = {'dashboards': {'home': [{'reference': 'card', 'endpoint_id': old}]}, 'popups': {}}
    await queue.async_save(queue_data)
    await bindings.async_save(binding_data)
    before = (deepcopy(dict(entry.data)), deepcopy(dict(entry.options)), deepcopy(row.as_dict()))
    original_update = entities.async_update_entity
    failed = False

    def fail_once(entity_id, **kwargs):
        nonlocal failed
        if not failed and kwargs.get('new_unique_id') == 'phone_endpoint_' + username + '_state':
            failed = True
            raise RuntimeError('Injected native registry failure')
        return original_update(entity_id, **kwargs)

    monkeypatch.setattr(entities, 'async_update_entity', fail_once)
    with pytest.raises(RuntimeError, match='Injected native registry failure'):
        await async_migrate_account_identities(hass, entry)
    assert failed
    assert (dict(entry.data), dict(entry.options), row.as_dict()) == before
    assert devices.async_get(device.id).identifiers == {('voip_stack', 'phone_endpoint:' + old)}
    assert entities.async_get(entity.entity_id).unique_id == 'phone_endpoint_' + old + '_state'
    assert await queue.async_load() == queue_data
    assert await bindings.async_load() == binding_data
    # The exact same mapping is safe to retry after the failed native write.
    await async_migrate_account_identities(hass, entry)
    assert row.unique_id == 'phone:' + username
    assert entry.data['parking']['return_destination'] == username


async def test_ldap_invalid_record_does_not_block_an_independent_account_search():
    from custom_components.voip_stack.ldap_server import LdapDirectoryServer, matches
    from custom_components.voip_stack.ldap_codec import Filter
    from custom_components.voip_stack.phone_directory import PhoneDirectory
    from custom_components.voip_stack.roster import RosterEntry

    username = 'Account, @ 100%'
    invalid = RosterEntry(id='oversized', name='x' * 5000,
        metadata={'endpoint_kind': 'sip_account', 'endpoint_id': 'private-invalid-id'})
    valid = RosterEntry(id=username, name='Readable account',
        metadata={'endpoint_kind': 'sip_account', 'endpoint_id': 'private-hardware-id'})
    directory = PhoneDirectory([invalid, valid])
    server = LdapDirectoryServer(directory, None,
        uri_for=lambda row: 'sip:Account%2C%20%40%20100%25@127.0.0.1')
    rows = list(server.records())
    selected = [attributes for _dn, attributes in rows
                if matches(Filter('equalityMatch', attribute='uid', value=username.encode()), attributes)]
    assert len(selected) == 1
    assert selected[0]['uid'] == (username.encode(),)
    assert b'private-hardware-id' not in tuple(value for attributes in selected for values in attributes.values() for value in values)
    assert server.username(username) == username


@pytest.mark.parametrize(('origin', 'expected'), [
    ('binding', 'Casa'), ('browser', 'Casa'), ('user', 'ha_browser_legacy'), ('manual', 'ha_browser_legacy'),
])
async def test_generated_ha_login_uses_exact_origin_reference_and_preserves_native_owners(hass, origin, expected):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.phone_config import phone_endpoint_id

    old = 'browser:legacy'
    entry = MockConfigEntry(domain='voip_stack', version=7, minor_version=3, data={'initial_phone_created': True})
    entry.add_to_hass(hass)
    data = {'kind': 'browser' if origin == 'browser' else 'sip_account', 'name': 'Casa',
            'username': 'ha_browser_legacy', 'password': 'fixture-kept-password'}
    if origin != 'manual':
        data['ha_bindings'] = [{'source': 'migration', 'reference': 'legacy_ha_phone:' + old}]
    if origin == 'user':
        data['username_source'] = 'user'
    row = ConfigSubentry(subentry_type='phone', title='Casa', unique_id='phone:' + old, data=data)
    hass.config_entries.async_add_subentry(entry, row)
    devices, entities = dr.async_get(hass), er.async_get(hass)
    device = devices.async_get_or_create(config_entry_id=entry.entry_id, config_subentry_id=row.subentry_id,
        identifiers={('voip_stack', 'phone_endpoint:' + old)}, name='Casa')
    devices.async_update_device(device.id, name_by_user='My home account')
    entity = entities.async_get_or_create('sensor', 'voip_stack', 'phone_endpoint_' + old + '_call_state',
        config_entry=entry, config_subentry_id=row.subentry_id, device_id=device.id, suggested_object_id='kept_home_state')
    entities.async_update_entity(entity.entity_id, name='My call state')
    native = row.subentry_id, device.id, entity.entity_id
    assert await async_migrate_entry(hass, entry)
    assert phone_endpoint_id(row) == row.data['username'] == expected
    assert row.data['password'] == 'fixture-kept-password'
    assert row.data['kind'] == 'sip_account'
    assert (row.subentry_id, device.id, entity.entity_id) == native
    assert devices.async_get(device.id).name_by_user == 'My home account'
    assert entities.async_get(entity.entity_id).name == 'My call state'
    assert entities.async_get(entity.entity_id).unique_id == 'phone_endpoint_' + expected + '_call_state'
    before = dict(row.data)
    assert await async_migrate_entry(hass, entry)
    assert dict(row.data) == before


async def test_generated_login_rebinds_both_old_username_and_endpoint_without_touching_wav(hass, tmp_path):
    from homeassistant.helpers.storage import Store
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.core.media_archive import MediaArchive

    old, login = 'browser:legacy', 'ha_browser_legacy'
    entry = MockConfigEntry(domain='voip_stack', version=7, minor_version=3,
        data={'initial_phone_created': True, 'parking': {'return_destination': login},
              'external_call_blocked_endpoints': [old]})
    entry.add_to_hass(hass)
    row = ConfigSubentry(subentry_type='phone', title='Casa', unique_id='phone:' + old,
        data={'kind': 'sip_account', 'name': 'Casa', 'username': login, 'password': 'fixture-kept-password',
              'ha_bindings': [{'source': 'migration', 'reference': 'legacy_ha_phone:' + old}]})
    hass.config_entries.async_add_subentry(entry, row)
    queue = Store(hass, 1, 'voip_stack.queues.' + entry.entry_id)
    await queue.async_save({'agent_states': {'support': {login: 'paused'}}, 'round_robin': {'support': old}})
    hass.config.media_dirs = {'local': str(tmp_path / 'media')}
    archive = MediaArchive(tmp_path / 'media/voip_stack', hass.async_add_executor_job)
    await archive.load()
    writer = await archive.begin(login, caller='Kept caller')
    assert writer.offer(b'\x12\x34' * 320, AudioFormat(16000, 's16le', 1, 20), offset=0)
    message = await writer.finish()
    wave_before = {path.name: path.read_bytes() for path in archive.root.glob('*.wav')}
    assert await async_migrate_entry(hass, entry)
    assert entry.data['parking']['return_destination'] == 'Casa'
    assert entry.data['external_call_blocked_endpoints'] == ['Casa']
    saved = await queue.async_load()
    assert saved['agent_states']['support'] == {'Casa': 'paused'}
    assert saved['round_robin']['support'] == 'Casa'
    await archive.load()
    assert archive.counts('Casa') == (1, 0)
    assert archive.segment_path(message['id'], 'Casa').read_bytes() == wave_before[message['segments'][0]['file']]
    assert wave_before == {path.name: path.read_bytes() for path in archive.root.glob('*.wav')}
    assert entry.data['legacy_phone_references'][old] == entry.data['legacy_phone_references'][login] == 'Casa'
    await archive.close()


@pytest.mark.parametrize('offline', [False, True])
async def test_managed_generated_login_uses_verified_native_name_or_defers_until_inventory(phone_lab, offline):
    from custom_components.voip_stack.esphome_provisioning import EspHomeProvisioner
    from custom_components.voip_stack.sip_account_identity import async_migrate_account_identities

    lab = phone_lab
    native = lab.native.runtime_data
    old = 'esphome:' + lab.device.id
    row = ConfigSubentry(subentry_type='phone', title='Visible account', unique_id='phone:' + old,
        data={'kind': 'esphome', 'display_name': 'Visible account', 'username': 'esp_84c7bb000001',
              'password': 'fixture-kept-password', 'device_id': lab.device.id, 'origin_entry_id': lab.native.entry_id,
              'mac': '84c7bb000001', 'api_host': '127.0.0.2', 'route_id': 'phone_node'})
    lab.hass.config_entries.async_add_subentry(lab.entry, row)
    devices = dr.async_get(lab.hass)
    devices.async_update_device(lab.device.id, name_by_user='User hardware label')
    account = devices.async_get_or_create(config_entry_id=lab.entry.entry_id, config_subentry_id=row.subentry_id,
        identifiers={('voip_stack', 'phone_endpoint:' + old)}, name='Visible account')
    account_id = account.id
    if offline:
        native.first_connect_done.clear()
    await async_migrate_account_identities(lab.hass, lab.entry)
    assert row.data['username'] == ('esp_84c7bb000001' if offline else 'Ingresso')
    assert row.data['username_source'] == ('esphome_pending' if offline else 'esphome')
    native.first_connect_done.set()
    controller = lab.runtime.phone_provisioning = EspHomeProvisioner(lab.hass, lab.entry)
    try:
        await controller.async_sync()
        assert row.data['username'] == 'Ingresso' and row.data['username_source'] == 'esphome'
        assert row.data['password'] == 'fixture-kept-password'
        kept = devices.async_get_device_by_identifier(('voip_stack', 'phone_endpoint:Ingresso'), lab.entry.entry_id)
        assert kept.id == account_id and kept.config_subentry_id == row.subentry_id
        assert devices.async_get(lab.device.id).name_by_user == 'User hardware label'
        delivered = next(data for action, data in lab.sent if action == 'set_account')
        assert delivered['username'] == 'Ingresso'
        assert ('register_ha_account', {}) in lab.sent
    finally:
        await controller.async_close()
        lab.runtime.phone_provisioning = None


@pytest.mark.parametrize('automatic', [True, False])
async def test_account_migration_activates_existing_registrar_setting_once_for_auto_origin(hass, automatic):
    from custom_components.voip_stack import async_migrate_entry
    key = 'sip_registrar_enabled'
    entry = MockConfigEntry(domain='voip_stack', version=7, minor_version=3,
        data={'initial_phone_created': True, key: False}, options={key: False})
    entry.add_to_hass(hass)
    data = {'kind': 'sip_account', 'name': 'Casa', 'username': 'Casa', 'password': 'fixture-kept-password'}
    if automatic:
        data['ha_bindings'] = [{'source': 'migration', 'reference': 'legacy_ha_phone:default'}]
    else:
        data['username_source'] = 'user'
    row = ConfigSubentry(subentry_type='phone', title='Casa', unique_id='phone:previous', data=data)
    hass.config_entries.async_add_subentry(entry, row)
    assert await async_migrate_entry(hass, entry)
    assert entry.data[key] is automatic and entry.options[key] is automatic
    hass.config_entries.async_update_entry(entry, data={**entry.data, key: False}, options={key: False})
    assert await async_migrate_entry(hass, entry)
    assert entry.data[key] is False and entry.options[key] is False


async def test_new_enabled_account_activates_server_but_existing_account_updates_preserve_off(hass):
    from custom_components.voip_stack.phone_config import _add_phone_subentry, sip_account_phone_data, update_phone_subentry
    key = 'sip_registrar_enabled'
    entry = MockConfigEntry(domain='voip_stack', version=7, minor_version=4,
        data={'initial_phone_created': True, key: False}, options={key: False})
    entry.add_to_hass(hass)
    row = _add_phone_subentry(hass, entry, title='Desk', data=sip_account_phone_data({
        'username': 'Desk', 'password': 'fixture-only-password'}))
    assert entry.data[key] is True and entry.options[key] is True
    hass.config_entries.async_update_entry(entry, data={**entry.data, key: False}, options={key: False})
    assert update_phone_subentry(hass, entry, 'Desk', {'dnd': True})
    assert entry.data[key] is False and entry.options[key] is False
    assert row.data['username_source'] == 'user'


async def test_legacy_singleton_state_registry_entry_moves_in_the_same_identity_batch(hass):
    from custom_components.voip_stack import async_migrate_entry
    entry = MockConfigEntry(domain='voip_stack', version=7, minor_version=3, data={'initial_phone_created': True})
    entry.add_to_hass(hass)
    row = ConfigSubentry(subentry_type='phone', title='Casa', unique_id='phone:default', data={
        'kind': 'sip_account', 'name': 'Casa', 'username': 'Casa', 'password': 'fixture-only-password',
        'ha_bindings': [{'source': 'migration', 'reference': 'legacy_ha_phone:default'}]})
    hass.config_entries.async_add_subentry(entry, row)
    entities = er.async_get(hass)
    before = entities.async_get_or_create('sensor', 'voip_stack', 'voip_stack_ha_softphone_call_state',
        config_entry=entry, suggested_object_id='my_existing_home_call')
    entities.async_update_entity(before.entity_id, name='My existing call state')
    assert await async_migrate_entry(hass, entry)
    after = entities.async_get(before.entity_id)
    assert after.unique_id == 'phone_endpoint_Casa_call_state'
    assert after.name == 'My existing call state'
    assert entry.data['legacy_phone_references']['default'] == 'Casa'
