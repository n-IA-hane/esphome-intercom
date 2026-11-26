"""Native group flows preserve ordered canonical membership and HA ownership."""

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import device_registry as dr

from .test_managed_phones_ha import custom_integration_path as custom_integration_path
from .test_managed_phones_ha import phone_lab as phone_lab
from .support.ha_frontend import prepare_frontend_dependency

pytestmark = pytest.mark.ha


def add_account(lab, username, **fields):
    from custom_components.voip_stack.endpoint_device import async_ensure_endpoint_device
    from custom_components.voip_stack.phone_config import sync_registry_from_entry

    row = ConfigSubentry(subentry_type="phone", title=username, unique_id="phone:" + username,
        data={"kind": "sip_account", "name": username, "username": username,
              "password": "fixture-only-password", **fields})
    lab.hass.config_entries.async_add_subentry(lab.entry, row)
    sync_registry_from_entry(lab.hass, lab.entry)
    registry = lab.runtime.endpoints
    device = async_ensure_endpoint_device(lab.hass, lab.entry, registry.require(username), registry)
    return row, device


def prepare_flows(lab):
    lab.hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    prepare_frontend_dependency(lab.hass)


async def test_one_time_phone_declarations_become_the_single_group_owner(phone_lab):
    from custom_components.voip_stack.group_config import group_records, reconcile_group_records
    from custom_components.voip_stack.phone_queue_membership import iter_phone_memberships, project_phone_memberships

    lab = phone_lab
    first, _ = add_account(lab, "First @ 100%", queues=["Support"], ring_group="Reception")
    add_account(lab, "Second, account", queues=["Support"])
    reconcile_group_records(lab.hass, lab.entry)
    groups = group_records(lab.entry)
    queue = next(record for record in groups.values() if record["kind"] == "queue")
    assert queue["members"] == ["First @ 100%", "Second, account"]
    assert queue["policy"]["agent_order"] == queue["members"]
    assert lab.entry.data["group_membership_version"] == 1
    assert "queues" not in first.data and "ring_group" not in first.data
    assert project_phone_memberships(lab.entry, "First @ 100%", {}) == {
        "queues": ["Support"], "ring_group": "Reception"}
    # An obsolete field cannot resurrect a removed assignment after conversion.
    lab.hass.config_entries.async_update_subentry(lab.entry, first, data={**first.data, "queues": ["Injected"]})
    reconcile_group_records(lab.hass, lab.entry)
    assert dict(iter_phone_memberships(lab.hass, lab.entry, "queue"))["First @ 100%"] == ("Support",)
    assert group_records(lab.entry) == groups


async def test_native_ring_editor_projects_policy_and_public_destinations(phone_lab):
    from custom_components.voip_stack.dial_plan import RingPolicy
    from custom_components.voip_stack.group_config import group_roster_entries

    lab = phone_lab
    prepare_flows(lab)
    _account, device = add_account(lab, "Literal @ account")
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data, "group_membership_version": 1})
    form = await lab.hass.config_entries.subentries.async_init(
        (lab.entry.entry_id, "group"), context={"source": "user"})
    form = await lab.hass.config_entries.subentries.async_configure(form["flow_id"], {"kind": "ring"})
    result = await lab.hass.config_entries.subentries.async_configure(form["flow_id"], form["data_schema"]({
        "name": "Reception", "members": [{"device": device.id}, {"destination": "+3902123456"},
                                             {"destination": "sip:door@example.net"}],
        "strategy": "sequential", "inherit_ringing_timeout": False, "overall_timeout": 30, "step_timeout": 15,
        "fallback": "Literal @ account", "keep_empty": True,
    }))
    assert result["type"] == "create_entry"
    row = next(row for row in lab.entry.subentries.values() if row.subentry_type == "group")
    assert row.data["members"] == ["Literal @ account", "+3902123456", "sip:door@example.net"]
    assert "ring_order" not in row.data and "ring_destinations" not in row.data
    (published,) = list(group_roster_entries(lab.hass, lab.entry))
    policy = RingPolicy.from_metadata(published.metadata)
    assert policy.strategy == "sequential" and policy.overall_timeout == 30 and policy.step_timeout == 15
    assert published.metadata["members"] == row.data["members"]
    assert published.metadata["fallback_destination"] == "Literal @ account"


async def test_queue_missing_device_remains_editable_and_uses_one_ordered_list(phone_lab):
    from custom_components.voip_stack.group_config import group_records, normalize_group_data

    lab = phone_lab
    prepare_flows(lab)
    _account, device = add_account(lab, "Live account")
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data, "group_membership_version": 1})
    data = normalize_group_data({"kind": "queue", "name": "Support", "members": ["Missing account", "Live account"]})
    row = ConfigSubentry(subentry_type="group", title="Support", unique_id="group:" + data["group_id"], data=data)
    lab.hass.config_entries.async_add_subentry(lab.entry, row)
    form = await lab.hass.config_entries.subentries.async_init((lab.entry.entry_id, "group"),
        context={"source": "reconfigure", "subentry_id": row.subentry_id})
    assert form["type"] == "form"
    defaults = form["data_schema"]({})
    assert defaults["members"] == [{"destination": "Missing account"}, {"device": device.id}]
    assert "queue_members" not in defaults and "agent_order" not in defaults
    result = await lab.hass.config_entries.subentries.async_configure(form["flow_id"], {
        **defaults, "members": [{"device": device.id}], "retry_delay": 0.1,
        "hold_media": {"media_content_id": "media-source://media_source/local/wait.wav", "media_content_type": "music"},
        "fallback_timeout": "sip:fallback@example.net",
    })
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    assert row.data["members"] == ["Live account"]
    assert "agent_order" not in row.data["policy"]
    assert group_records(lab.entry)[data["group_id"]]["policy"]["agent_order"] == ["Live account"]
    assert row.data["policy"]["retry_delay"] == 0.1
    assert row.data["policy"]["hold_media"] == "media-source://media_source/local/wait.wav"
    assert row.data["policy"]["fallback_timeout"] == "sip:fallback@example.net"


async def test_paging_flow_owns_one_service_device_and_keeps_receiver_order(phone_lab):
    from custom_components.voip_stack.paging_config import sync_paging_devices

    lab = phone_lab
    prepare_flows(lab)
    _first, first_device = add_account(lab, "First")
    _second, second_device = add_account(lab, "Second")
    form = await lab.hass.config_entries.subentries.async_init(
        (lab.entry.entry_id, "paging"), context={"source": "user"})
    result = await lab.hass.config_entries.subentries.async_configure(form["flow_id"], form["data_schema"]({
        "name": "Announcement", "members": [{"device": second_device.id}, {"device": first_device.id}], "enabled": False,
    }))
    assert result["type"] == "create_entry"
    row = next(row for row in lab.entry.subentries.values() if row.subentry_type == "paging")
    sync_paging_devices(lab.hass, lab.entry)
    registry = dr.async_get(lab.hass)
    device = registry.async_get_device_by_identifier(("voip_stack", "paging:" + row.data["paging_group_id"]), lab.entry.entry_id)
    identity = device.id
    assert device.config_subentry_id == row.subentry_id
    assert row.data["members"] == ["Second", "First"] and row.data["enabled"] is False
    assert row.data["answer_timeout"] is None
    registry.async_update_device(identity, name_by_user="My announcement")
    form = await lab.hass.config_entries.subentries.async_init((lab.entry.entry_id, "paging"),
        context={"source": "reconfigure", "subentry_id": row.subentry_id})
    defaults = form["data_schema"]({})
    result = await lab.hass.config_entries.subentries.async_configure(form["flow_id"], {**defaults, "name": "Renamed announcement"})
    assert result["type"] == "abort"
    sync_paging_devices(lab.hass, lab.entry)
    kept = registry.async_get_device_by_identifier(("voip_stack", "paging:" + row.data["paging_group_id"]), lab.entry.entry_id)
    assert kept.id == identity and kept.name_by_user == "My announcement"
    assert row.data["members"] == ["Second", "First"]
    assert row.data["answer_timeout"] is None


@pytest.mark.parametrize("saved", [0, 5])
async def test_paging_form_preserves_explicit_answer_window_and_can_restore_inheritance(phone_lab, saved):
    lab = phone_lab
    prepare_flows(lab)
    from custom_components.voip_stack.paging_config import normalize_paging_group
    add_account(lab, "Receiver")
    data = normalize_paging_group({"name": "Announcement", "members": ["Receiver"], "answer_timeout": saved})
    row = ConfigSubentry(subentry_type="paging", title="Announcement", unique_id="paging:" + data["paging_group_id"], data=data)
    lab.hass.config_entries.async_add_subentry(lab.entry, row)
    form = await lab.hass.config_entries.subentries.async_init((lab.entry.entry_id, "paging"),
        context={"source": "reconfigure", "subentry_id": row.subentry_id})
    defaults = form["data_schema"]({})
    assert defaults["inherit_ringing_timeout"] is False
    assert defaults["answer_timeout"] == saved
    result = await lab.hass.config_entries.subentries.async_configure(form["flow_id"],
        {**defaults, "inherit_ringing_timeout": True})
    assert result["type"] == "abort", result.get("errors")
    assert row.data["answer_timeout"] is None

    form = await lab.hass.config_entries.subentries.async_init((lab.entry.entry_id, "paging"),
        context={"source": "reconfigure", "subentry_id": row.subentry_id})
    result = await lab.hass.config_entries.subentries.async_configure(form["flow_id"],
        {**form["data_schema"]({}), "name": "Receiver"})
    assert result["errors"] == {"base": "invalid_paging_group"}
    assert row.data["name"] == "Announcement"


async def test_native_application_entities_follow_existing_owners_without_polling(phone_lab):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from homeassistant.components.sensor import SensorEntity
    from homeassistant.helpers import entity_registry as er
    from custom_components.voip_stack import sensor, switch
    from custom_components.voip_stack.call_queues import async_setup_queues
    from custom_components.voip_stack.group_config import normalize_group_data, sync_group_devices
    from custom_components.voip_stack.paging_config import normalize_paging_group, sync_paging_devices
    from custom_components.voip_stack.endpoint_session import TerminationIntent
    from custom_components.voip_stack.endpoint_termination import EndpointTerminationHandler
    from custom_components.voip_stack.service_entity import NativeServiceEntity
    from custom_components.voip_stack.sip_registrar import SipAccount
    from custom_components.voip_stack.account_runtime import recompute_account_availability
    from .test_sip_account_username_identity_ha import register_client

    lab = phone_lab
    username = "Queue @ account"
    add_account(lab, username)
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data, "group_membership_version": 1})
    queue = normalize_group_data({"kind": "queue", "name": "Support", "members": [username]})
    row = ConfigSubentry(subentry_type="group", title="Support", unique_id="group:" + queue["group_id"], data=queue)
    lab.hass.config_entries.async_add_subentry(lab.entry, row)
    paging = normalize_paging_group({"name": "Announcement", "members": [username]})
    page = ConfigSubentry(subentry_type="paging", title="Announcement", unique_id="paging:" + paging["paging_group_id"], data=paging)
    lab.hass.config_entries.async_add_subentry(lab.entry, page)
    sync_group_devices(lab.hass, lab.entry)
    sync_paging_devices(lab.hass, lab.entry)
    devices, registry = dr.async_get(lab.hass), er.async_get(lab.hass)
    pbx = devices.async_get_or_create(config_entry_id=lab.entry.entry_id,
        identifiers={("voip_stack", lab.entry.entry_id)}, name="VoIP Stack", model="PBX")
    manager = await async_setup_queues(lab.hass, lab.entry)
    captured = []

    def capture(entities, **kwargs):
        for entity in entities:
            if not isinstance(entity, NativeServiceEntity):
                continue
            domain = "sensor" if isinstance(entity, SensorEntity) else "switch"
            identifier = next(iter(entity.device_info["identifiers"]))
            device = devices.async_get_device_by_identifier(identifier, lab.entry.entry_id)
            native = registry.async_get_or_create(domain, "voip_stack", entity.unique_id, config_entry=lab.entry,
                config_subentry_id=kwargs.get("config_subentry_id"), device_id=device.id)
            entity.hass, entity.entity_id, entity._ready = lab.hass, native.entity_id, True
            entity.async_write_ha_state = Mock()
            entity.async_remove = AsyncMock()
            captured.append(entity)

    await sensor.async_setup_entry(lab.hass, lab.entry, capture)
    await switch.async_setup_entry(lab.hass, lab.entry, capture)
    try:
        assert all(not entity.should_poll for entity in captured)
        pbx_counts = {entity.key: entity for entity in captured if isinstance(entity, sensor.PbxCountSensor)}
        queue_counts = {entity.key: entity for entity in captured if isinstance(entity, sensor.QueueCountSensor)}
        toggles = {entity.subentry_id: entity for entity in captured if isinstance(entity, switch.NativeServiceEnabledSwitch)}
        assert set(pbx_counts) == {"active_calls", "registered_contacts"}
        assert all(registry.async_get(entity.entity_id).device_id == pbx.id for entity in pbx_counts.values())
        assert all(registry.async_get(entity.entity_id).config_subentry_id == row.subentry_id for entity in queue_counts.values())
        assert set(toggles) == {row.subentry_id, page.subentry_id}
        assert queue_counts["waiting"].native_value == queue_counts["available_agents"].native_value == 0
        session = lab.runtime.sip.upsert("native-active-counter", state="ringing", owner="router")
        await lab.hass.async_block_till_done()
        assert pbx_counts["active_calls"].native_value == 1
        await EndpointTerminationHandler(lab.hass).terminate(session.call_id, TerminationIntent.bye("local_hangup"))
        await lab.hass.async_block_till_done()
        assert pbx_counts["active_calls"].native_value == 0
        lab.registrar.enabled = True
        lab.registrar.update_accounts([SipAccount(username, username, "fixture-only-password")])
        await register_client(lab, SimpleNamespace(username=username, password="fixture-only-password"))
        recompute_account_availability(lab.hass)
        await lab.hass.async_block_till_done()
        assert pbx_counts["registered_contacts"].native_value == 1
        assert queue_counts["available_agents"].native_value == 1
        queue_counts["available_agents"].async_write_ha_state.assert_called()
        manager.set_agent_state(queue["group_id"], username, "paused")
        await lab.hass.async_block_till_done()
        assert queue_counts["available_agents"].native_value == 0
        ticket = manager.enqueue(queue["group_id"], "queued-native-state", 1)
        await lab.hass.async_block_till_done()
        assert queue_counts["waiting"].native_value == 1
        queue_counts["waiting"].async_write_ha_state.assert_called()
        manager.finish(ticket)
        await lab.hass.async_block_till_done()
        assert queue_counts["waiting"].native_value == 0
        await toggles[row.subentry_id].async_turn_off()
        await toggles[page.subentry_id].async_turn_off()
        await lab.hass.async_block_till_done()
        assert row.data["enabled"] is False and page.data["enabled"] is False
        assert manager.policy(queue["group_id"]).enabled is False
        assert row.data["members"] == [username] and page.data["members"] == [username]
        kept_ids = {entity.unique_id: entity.entity_id for entity in captured}
        lab.hass.config_entries.async_update_subentry(lab.entry, row, title="Renamed", data={**row.data, "name": "Renamed"})
        await lab.hass.async_block_till_done()
        assert all(entity.entity_id == kept_ids[entity.unique_id] for entity in captured)
        assert all(registry.async_get(entity.entity_id) is not None for entity in captured)
    finally:
        await manager.close()
        lab.runtime.queues = None


async def test_contact_ring_declarations_convert_once_to_public_group_members(phone_lab):
    from custom_components.voip_stack.group_config import group_records, reconcile_group_records

    lab = phone_lab
    add_account(lab, 'Operator', ring_group='Reception')
    contact = ConfigSubentry(subentry_type='contact', title='Courier', unique_id='contact:private-contact-id',
        data={'id': 'private-contact-id', 'name': 'Courier', 'number': '+3902123456',
              'metadata': {'ring_group': 'Reception'}})
    lab.hass.config_entries.async_add_subentry(lab.entry, contact)
    reconcile_group_records(lab.hass, lab.entry)
    group = next(row for row in lab.entry.subentries.values() if row.subentry_type == 'group')
    assert group.data['members'] == ['Operator', '+3902123456']
    assert 'ring_group' not in contact.data['metadata']
    assert all('private-contact-id' not in value for value in group.data['members'])
    lab.hass.config_entries.async_update_subentry(lab.entry, group, data={**group.data, 'members': ['Operator']})
    reconcile_group_records(lab.hass, lab.entry)
    assert next(iter(group_records(lab.entry).values()))['members'] == ['Operator']


async def test_administrator_contact_import_consumes_group_commands_without_shadow_membership(phone_lab):
    import json
    from unittest.mock import AsyncMock
    from homeassistant.core import ServiceCall
    from custom_components.voip_stack.contact_config import contact_dicts
    from custom_components.voip_stack.group_config import group_records, reconcile_group_records
    from custom_components.voip_stack.phonebook_services import build_phonebook_service_handlers

    lab = phone_lab
    add_account(lab, 'Operator', ring_group='Reception')
    reconcile_group_records(lab.hass, lab.entry)
    refresh = AsyncMock()
    handlers = build_phonebook_service_handlers(refresh_and_push_phonebook=refresh)
    await handlers['add_contact'](ServiceCall(lab.hass, 'voip_stack', 'add_contact', {
        'name': 'Courier', 'number': '+3902123456', 'ring_group': 'Reception'}))
    assert next(iter(group_records(lab.entry).values()))['members'] == ['Operator', '+3902123456']
    assert 'ring_group' not in contact_dicts(lab.entry)[0]['metadata']
    await handlers['add_contact'](ServiceCall(lab.hass, 'voip_stack', 'add_contact', {
        'name': 'Courier', 'number': '+3902123456', 'ring_group': ''}))
    assert next(iter(group_records(lab.entry).values()))['members'] == ['Operator']
    await handlers['set_contacts'](ServiceCall(lab.hass, 'voip_stack', 'set_contacts', {'roster_json': json.dumps([
        {'id': 'administrator-import', 'name': 'Dispatcher', 'sip_uri': 'sip:dispatcher@example.net',
         'metadata': {'ring_group': 'Reception'}}])}))
    assert next(iter(group_records(lab.entry).values()))['members'] == ['Operator', 'sip:dispatcher@example.net']
    assert 'ring_group' not in contact_dicts(lab.entry)[0]['metadata']
    assert refresh.await_count == 3


async def test_group_namespace_rejects_exact_account_username_without_alphanumeric_alias_guess(phone_lab):
    from custom_components.voip_stack.group_config import normalize_group_data, validate_group_namespace
    lab = phone_lab
    add_account(lab, 'A-B')
    validate_group_namespace(lab.hass, lab.entry, normalize_group_data({'kind': 'ring', 'name': 'AB'}))
    with pytest.raises(ValueError, match='another destination'):
        validate_group_namespace(lab.hass, lab.entry, normalize_group_data({'kind': 'ring', 'name': 'a-b'}))


async def test_membership_actions_adapt_native_group_devices_and_reject_wrong_group_type(phone_lab):
    from homeassistant.core import ServiceCall
    from homeassistant.exceptions import ServiceValidationError
    from custom_components.voip_stack.group_config import normalize_group_data, sync_group_devices
    from custom_components.voip_stack.paging_config import normalize_paging_group, sync_paging_devices
    from custom_components.voip_stack.phone_queue_membership import async_set_phone_memberships_service

    lab = phone_lab
    _phone, phone_device = add_account(lab, 'Operator')
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data, 'group_membership_version': 1})
    queue = normalize_group_data({'kind': 'queue', 'name': 'Support'})
    queue_row = ConfigSubentry(subentry_type='group', title='Support', unique_id='group:' + queue['group_id'], data=queue)
    paging = normalize_paging_group({'name': 'Announcement'})
    page_row = ConfigSubentry(subentry_type='paging', title='Announcement', unique_id='paging:' + paging['paging_group_id'], data=paging)
    for row in (queue_row, page_row):
        lab.hass.config_entries.async_add_subentry(lab.entry, row)
    sync_group_devices(lab.hass, lab.entry)
    sync_paging_devices(lab.hass, lab.entry)
    devices = dr.async_get(lab.hass)
    queue_device = devices.async_get_device_by_identifier(('voip_stack', 'group:' + queue['group_id']), lab.entry.entry_id)
    page_device = devices.async_get_device_by_identifier(('voip_stack', 'paging:' + paging['paging_group_id']), lab.entry.entry_id)
    await async_set_phone_memberships_service(ServiceCall(lab.hass, 'voip_stack', 'set_phone_memberships', {
        'phone': phone_device.id, 'field': 'queues', 'value': [queue_device.id]}))
    await async_set_phone_memberships_service(ServiceCall(lab.hass, 'voip_stack', 'set_phone_memberships', {
        'phone': phone_device.id, 'field': 'paging_groups', 'value': [page_device.id]}))
    assert queue_row.data['members'] == page_row.data['members'] == ['Operator']
    before = dict(queue_row.data)
    with pytest.raises(ServiceValidationError):
        await async_set_phone_memberships_service(ServiceCall(lab.hass, 'voip_stack', 'set_phone_memberships', {
            'phone': phone_device.id, 'field': 'queues', 'value': [page_device.id]}))
    assert dict(queue_row.data) == before


async def test_registrar_activation_is_immediate_and_later_global_off_uses_same_live_owner(phone_lab, monkeypatch):
    from unittest.mock import AsyncMock
    from custom_components.voip_stack import config_entry_runtime, phone_services, trunk_runtime
    from custom_components.voip_stack.phone_config import _add_phone_subentry, sip_account_phone_data

    lab = phone_lab
    key = 'sip_registrar_enabled'
    lab.hass.config_entries.async_update_entry(lab.entry, version=7, minor_version=4,
        data={**lab.entry.data, key: False, 'group_membership_version': 1}, options={key: False})
    lab.runtime.transport_config[key] = False
    lab.registrar.enabled = False
    _add_phone_subentry(lab.hass, lab.entry, title='Desk', data=sip_account_phone_data({
        'username': 'Desk', 'password': 'fixture-activation-password'}))
    assert lab.entry.data[key] is True and lab.entry.options[key] is True
    assert lab.runtime.transport_config[key] is True and lab.registrar.enabled is True
    reload_entry = AsyncMock()
    monkeypatch.setattr(lab.hass.config_entries, 'async_reload', reload_entry)
    monkeypatch.setattr(config_entry_runtime, 'async_refresh_directory', AsyncMock())
    monkeypatch.setattr(trunk_runtime, 'async_reconcile_trunks', AsyncMock())
    monkeypatch.setattr(phone_services, 'async_reconcile_phone_services', AsyncMock())
    lab.runtime.entry_runtime_signature = config_entry_runtime.entry_runtime_signature(lab.entry)
    lab.runtime.entry_phone_signature = config_entry_runtime.entry_phone_signature(lab.entry)
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data, key: False}, options={key: False})
    await config_entry_runtime.async_config_entry_updated(lab.hass, lab.entry)
    assert lab.runtime.transport_config[key] is False and lab.registrar.enabled is False
    assert lab.entry.data[key] is False and lab.entry.options[key] is False
    reload_entry.assert_not_awaited()
