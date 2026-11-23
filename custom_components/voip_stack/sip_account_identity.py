"""Preflight and commit native account references using existing SIP logins."""

from copy import deepcopy
from builtins import BaseExceptionGroup
import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType, SimpleNamespace

from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DOMAIN
from .phone_config import PhoneIdentityConflict, phone_endpoint_id
from .sip_registrar import normalize_username


def _ha_reference_origins(data):
    return tuple(binding['reference'].removeprefix('legacy_ha_phone:')
        for binding in data.get('ha_bindings') or () if isinstance(binding, Mapping)
        and binding.get('source') == 'migration' and isinstance(binding.get('reference'), str)
        and binding['reference'].startswith('legacy_ha_phone:'))


def _historical_ha_origins(data, identity, username):
    origins = [identity] if data.get('kind') == 'browser' else []
    for binding in data.get('ha_bindings') or ():
        if not isinstance(binding, Mapping) or binding.get('source') != 'migration':
            continue
        reference = binding.get('reference')
        if reference is None:
            origins.append(identity)
        elif isinstance(reference, str) and reference.startswith('legacy_ha_phone:'):
            origins.append(reference.removeprefix('legacy_ha_phone:'))
    matched = []
    for origin in dict.fromkeys(origins):
        if not origin:
            continue
        formulas = (('ha_', origin), ('ha_phone_', re.sub(r'^(?:browser|sip):', '', origin)))
        if any(re.fullmatch(re.escape(prefix + re.sub(r'[^A-Za-z0-9_.-]', '_', key)[:48])
                            + r'(?:_(?:[2-9]|[1-9][0-9]+))?', username) for prefix, key in formulas):
            matched.append(origin)
    return tuple(matched)


def _account_username_update(hass, entry, row):
    """Recognize historical automatic logins from their persisted owner evidence."""
    data = row.data
    source = str(data.get('username_source') or '')
    if source in {'user', 'existing'}:
        return None
    username = normalize_username(str(data.get('username') or ''))
    identity = phone_endpoint_id(row)
    if source in {'ha_name', 'esphome'} and data.get('kind') == 'sip_account' and identity == username:
        return None
    if _historical_ha_origins(data, identity, username):
        return {'username': normalize_username(str(data.get('name') or row.title)), 'username_source': 'ha_name'}
    managed = data.get('kind') == 'esphome' or (data.get('managed_esphome') and data.get('managed_exclusive', True))
    if not managed:
        return None
    from .esphome_native import device_info, normalize_mac, runtime_for_entry, verified_action_names
    from .sip_registrar import validate_managed_username

    origin = hass.config_entries.async_get_entry(str(data.get('origin_entry_id') or ''))
    device = dr.async_get(hass).async_get(str(data.get('device_id') or ''))
    try:
        mac = normalize_mac(data.get('mac'))
    except ValueError:
        raise PhoneIdentityConflict(row.title, 'verified native hardware owner', username, subentry_id=row.subentry_id) from None
    historical = {'esp_' + mac, str(getattr(origin, 'data', {}).get('device_name') or '')}
    if (origin is None or origin.domain != 'esphome' or device is None or device.config_entry_id != origin.entry_id
            or not any(kind == dr.CONNECTION_NETWORK_MAC and normalize_mac(value) == mac for kind, value in device.connections)):
        return ({'username': username, 'username_source': 'esphome_pending'}
                if username in historical or source in {'esphome', 'esphome_pending'} else None)
    names = verified_action_names(origin, mac)
    if names is None:
        return {'username': username, 'username_source': source if source in {'esphome', 'esphome_pending'} else
                'esphome_pending' if username in historical else 'esphome_unresolved'}
    info = device_info(runtime_for_entry(origin))
    desired = validate_managed_username(info.friendly_name or info.name)
    anchors = {'esp_' + mac, str(origin.data.get('device_name') or ''), str(info.name), desired}
    if source not in {'esphome', 'esphome_pending'} and username not in anchors:
        return {'username': username, 'username_source': 'existing'}
    return {'username': desired, 'username_source': 'esphome', 'name': desired, 'display_name': desired}


def managed_account_name_updates(hass, entry):
    """Resolve deferred automatic names once the verified native inventory exists."""
    updates = {}
    for row in entry.subentries.values():
        if row.subentry_type != 'phone' or not (row.data.get('managed_esphome') or row.data.get('kind') == 'esphome'):
            continue
        update = _account_username_update(hass, entry, row)
        if update is not None and any(row.data.get(key) != value for key, value in update.items()):
            updates[row.subentry_id] = {**row.data, **update}
    return updates


def _stored_esphome_references(value):
    """Read exact old HA references from existing integration-owned snapshots."""
    if isinstance(value, str):
        return {value} if value.startswith('esphome:') else set()
    if isinstance(value, Mapping):
        return set().union(*(_stored_esphome_references(item) for pair in value.items() for item in pair)) if value else set()
    if isinstance(value, (list, tuple)):
        return set().union(*map(_stored_esphome_references, value)) if value else set()
    return set()


def _runtime_legacy_references(entry):
    runtime = getattr(entry, 'runtime_data', None)
    if runtime is None:
        return set()
    values = []
    if runtime.queues is not None:
        values.append(runtime.queues._snapshot())
    if runtime.ha_bindings is not None:
        values.extend((runtime.ha_bindings._dashboards, runtime.ha_bindings._popups))
    if runtime.physical_phones is not None:
        values.append(runtime.physical_phones.states)
    archive = runtime.sip.component('media_archive') if runtime.sip is not None else None
    if archive is not None:
        values.append(archive.mailbox_owners)
    return _stored_esphome_references(values)


def legacy_esphome_references(hass, entry, *, stored_references=()):
    """Find native phones whose saved HA-owned references predate SIP accounts."""
    devices, entities = dr.async_get(hass), er.async_get(hass)
    managed = {str(row.data.get('device_id') or '') for row in entry.subentries.values()
               if row.subentry_type == 'phone'}
    managed.update(entry.data.get('managed_esphome_phones', {}))
    retained = set(stored_references) | _runtime_legacy_references(entry)
    for origin in hass.config_entries.async_entries('esphome'):
        for device in dr.async_entries_for_config_entry(devices, origin.entry_id):
            if device.id in managed:
                continue
            old = 'esphome:' + device.id
            owned = old in retained or any(old in entry.data.get(field, {}) for field in
                ('esphome_phone_policies', 'esphome_mailboxes', 'esphome_phone_memberships'))
            owned |= old in entry.data.get('external_call_blocked_endpoints', ())
            owned |= entry.data.get('parking', {}).get('return_destination') == old
            owned |= any(_typed_data(row.data, row.subentry_type, {old: ''}) != dict(row.data)
                         for row in entry.subentries.values())
            owned |= devices.async_get_device_by_identifier((DOMAIN, 'phone_endpoint:' + old), entry.entry_id) is not None
            owned |= any(row.unique_id.startswith('phone_endpoint_' + old + '_')
                         for row in er.async_entries_for_config_entry(entities, entry.entry_id))
            if owned:
                yield origin, device, old


def resolve_ha_endpoint(registry, selector, *, entry=None):
    """Adapt saved HA references without adding aliases to SIP or the directory."""
    identity = str(selector or "").strip()
    if current := registry.by_username(identity) or registry.get(identity):
        return current
    mapping = getattr(entry, "data", {}).get("legacy_phone_references", {})
    return registry.get(mapping[identity]) if _typed_ha_reference(identity, mapping) else None


def _typed_ha_reference(identity, mapping):
    """Recognize saved HA identity fields, never a previous plain SIP login."""
    from .const import HA_SOFTPHONE_DEVICE_ID
    return identity in mapping and (identity in {'default', HA_SOFTPHONE_DEVICE_ID}
                                    or identity.startswith(('browser:', 'sip:', 'esphome:')))


def ha_account_username(hass, device_id):
    """Read an account selector through its native device and subentry owner."""
    from .store import config_entry

    if not callable(getattr(getattr(hass, 'config_entries', None), 'async_entries', None)):
        return None
    entry = config_entry(hass)
    if entry is None:
        return None
    device = dr.async_get(hass).async_get(str(device_id))
    if device is None:
        return None
    if device.config_entry_id == entry.entry_id:
        row = entry.subentries.get(device.config_subentry_id)
        return str(row.data.get('username') or '') if row is not None and row.subentry_type == 'phone' else None
    # Existing automations may still select the associated ESPHome hardware.
    rows = [row for row in entry.subentries.values() if row.subentry_type == 'phone'
            and row.data.get('device_id') == device.id]
    return str(rows[0].data.get('username') or '') if len(rows) == 1 else None


def canonical_ha_destination(hass, value, *, device_selector=False):
    """Adapt HA action references without creating a public SIP alias or editing a URI."""
    if not isinstance(value, str):
        return value
    from .store import config_entry

    if not callable(getattr(getattr(hass, 'config_entries', None), 'async_entries', None)):
        return value
    entry = config_entry(hass)
    if entry is None:
        return value
    text = value.strip()
    usernames = {str(row.data.get('username') or '') for row in entry.subentries.values()
                 if row.subentry_type == 'phone'}
    matching = [username for username in usernames if username and username.casefold() == text.casefold()]
    if len(matching) == 1:
        return matching[0]
    aliases = entry.data.get('legacy_phone_references', {})
    if '@' in text or text.casefold().startswith(('sips:', 'tel:')):
        return value
    if device_selector and text in aliases:
        return aliases[text]
    if '@' in text or text.casefold().startswith(('sip:', 'sips:', 'tel:')):
        return value
    if username := ha_account_username(hass, text):
        return username
    return aliases[text] if _typed_ha_reference(text, aliases) else value


def is_legacy_default_ha_account(hass, endpoint):
    """Retain the singleton HA permission association from its stored provenance."""
    from .store import config_entry
    if not callable(getattr(getattr(hass, 'config_entries', None), 'async_entries', None)):
        return False
    entry = config_entry(hass)
    if entry is None:
        return False
    identity = endpoint.endpoint_id
    if entry.data.get('legacy_phone_references', {}).get('default') == identity:
        return True
    runtime = getattr(entry, 'runtime_data', None)
    registry = getattr(runtime, 'endpoints', None)
    row = entry.subentries.get(registry.subentry_ids.get(identity)) if registry is not None else None
    return bool(row is not None and ('default' in _ha_reference_origins(row.data)
                or (row.data.get('kind') == 'browser' and identity == 'default')))


def automation_account_fields(hass, payload):
    """Project configured extensions without inferring accounts from SIP labels."""
    from .runtime_data import runtime_data
    from .store import config_entry

    result = dict(payload)
    runtime = runtime_data(hass)
    if runtime is None:
        return result
    entry = config_entry(hass)

    def endpoint_for(canonical, legacy):
        if canonical is not None:
            return runtime.endpoints.by_username(canonical) if canonical else None
        if not legacy:
            return None
        return runtime.endpoints.get(legacy) or resolve_ha_endpoint(runtime.endpoints, legacy, entry=entry)

    for side, prefix in (('caller', 'source_'), ('destination', 'dest_')):
        canonical, legacy = payload.get(prefix + 'account_username'), payload.get(prefix + 'endpoint_id')
        if canonical is None and not legacy and side == 'destination':
            canonical, legacy = payload.get('target_account_username'), payload.get('target_endpoint_id')
        if canonical is None and not legacy and payload.get('direction') == ('outgoing' if side == 'caller' else 'incoming'):
            canonical, legacy = payload.get('account_username'), payload.get('endpoint_id')
        endpoint = endpoint_for(canonical, legacy)
        if endpoint is not None:
            result[prefix + 'account_username'] = endpoint.username
            result[prefix + 'extension'] = endpoint.extension
        if payload.get('direction') == ('outgoing' if side == 'caller' else 'incoming'):
            owner = endpoint_for(payload.get('account_username'), payload.get('endpoint_id'))
            if owner is not None:
                result[side + '_account_username'] = owner.username
                if endpoint is None:
                    result[prefix + 'extension'] = owner.extension
    return result


def ha_event_payload(hass, payload):
    """Preserve existing HA event filters while publishing canonical usernames."""
    from .store import config_entry

    entry = config_entry(hass)
    runtime = getattr(entry, 'runtime_data', None)
    mapping = getattr(entry, "data", {}).get("legacy_phone_references", {})
    current_usernames = {str(row.data.get('username') or '').casefold(): str(row.data.get('username') or '')
                         for row in getattr(entry, 'subentries', {}).values() if row.subentry_type == 'phone'}
    reverse = {}
    for legacy, username in mapping.items():
        current = current_usernames.get(legacy.casefold())
        if legacy != username and _typed_ha_reference(legacy, mapping) and (current is None or current.casefold() == username.casefold()):
            reverse.setdefault(username, legacy)
    result = dict(payload)
    for prefix in ("", "source_", "dest_", "target_", "peer_"):
        field = prefix + "endpoint_id"
        identity = result.get(prefix + "account_username") or result.get(field)
        if isinstance(identity, str) and identity:
            canonical = (result[prefix + 'account_username'] if prefix + 'account_username' in result
                         else canonical_ha_destination(hass, identity, device_selector=True))
            result[prefix + "account_username"] = canonical
            result[field] = reverse.get(canonical, identity)
            endpoint = runtime.endpoints.by_username(canonical) if runtime is not None else None
            if endpoint is not None:
                result.update({prefix + key: value for key, value in ha_phone_device_fields(endpoint).items()})
    if isinstance(result.get("participant_endpoint_ids"), (list, tuple)):
        usernames = (list(result['participant_account_usernames'])
                     if isinstance(result.get('participant_account_usernames'), (list, tuple)) else
                     [canonical_ha_destination(hass, value) for value in result["participant_endpoint_ids"]])
        result["participant_account_usernames"] = usernames
        result["participant_endpoint_ids"] = [reverse.get(value, value) for value in usernames]
    return result


def ha_phone_device_fields(endpoint):
    """Keep the established device field and name the account/hardware associations."""
    return {'device_id': endpoint.device_id, 'account_device_id': endpoint.ha_device_id,
            'hardware_device_id': endpoint.device_id if endpoint.account_device_id
                or str(endpoint.kind) == 'esphome' else ''}


def _reference(value, mapping):
    return mapping.get(value, value) if isinstance(value, str) else value


def _policy(data, mapping):
    for field in ('always', 'busy', 'unavailable', 'no_answer'):
        destination = data.get(field)
        if isinstance(destination, dict) and destination.get('kind') in {'contact', 'voicemail'}:
            destination['target'] = _reference(destination.get('target', ''), mapping)


def _typed_data(data, kind, mapping):
    result = deepcopy(dict(data))
    if kind == 'phone':
        if isinstance(result.get('call_policy'), dict):
            _policy(result['call_policy'], mapping)
        if 'offline_forward_target' in result:
            result['offline_forward_target'] = _reference(result['offline_forward_target'], mapping)
        for binding in result.get('ha_bindings', ()):
            if isinstance(binding, dict) and 'endpoint_id' in binding:
                binding['endpoint_id'] = _reference(binding['endpoint_id'], mapping)
    if kind in {'paging', 'group'}:
        for field in ('members', 'ring_members', 'agent_order', 'member_update'):
            if isinstance(result.get(field), (list, tuple)):
                result[field] = [_reference(value, mapping) for value in result[field]]
        policy = result.get('policy')
        if isinstance(policy, dict) and isinstance(policy.get('agent_order'), (list, tuple)):
            policy['agent_order'] = [_reference(value, mapping) for value in policy['agent_order']]
        if isinstance(policy, dict):
            for field in ('fallback', 'fallback_full', 'fallback_timeout', 'fallback_no_agents',
                          'fallback_disabled', 'fallback_removed'):
                if field in policy:
                    policy[field] = _reference(policy[field], mapping)
    if kind == 'physical_phone':
        for line in result.get('lines', ()):
            if isinstance(line, dict) and 'endpoint_id' in line:
                line['endpoint_id'] = _reference(line['endpoint_id'], mapping)
        for key in result.get('keys', ()):
            if isinstance(key, dict) and key.get('function') in {'line', 'blf', 'speed_dial'}:
                key['target'] = _reference(key.get('target', ''), mapping)
    if kind == 'contact':
        metadata = result.get('metadata')
        if isinstance(metadata, dict):
            for field in ('endpoint_id', 'offline_forward_target'):
                if field in metadata:
                    metadata[field] = _reference(metadata[field], mapping)
            for field in ('members', 'ring_members'):
                if isinstance(metadata.get(field), list):
                    metadata[field] = [_reference(value, mapping) for value in metadata[field]]
            if isinstance(metadata.get('call_policy'), dict):
                _policy(metadata['call_policy'], mapping)
    if kind == 'trunk':
        for field in ('inbound_default_target', 'trunk_inbound_default_target'):
            if field in result:
                result[field] = _reference(result[field], mapping)
    return result


@dataclass(frozen=True)
class AccountIdentityMigrationPlan:
    """One validated batch; HA observers run after its synchronous commit."""

    hass: object
    entry: object
    mapping: dict
    data: dict
    options: dict
    subentries: tuple
    devices: tuple
    entities: tuple
    creations: tuple = ()
    original_state: tuple = ()
    invalid_managed: tuple = ()
    pending_esphome: tuple = ()
    activate_registrar: bool = False

    def commit(self):
        """Commit through native HA owners without yielding between records."""
        devices, entities = dr.async_get(self.hass), er.async_get(self.hass)
        current = (dict(self.entry.data), dict(self.entry.options),
                   {key: row.as_dict() for key, row in self.entry.subentries.items()})
        if self.original_state and current != self.original_state:
            raise PhoneIdentityConflict(self.entry.title, 'unchanged native configuration', 'concurrent account change')
        snapshots = [(row, dict(row.data), row.unique_id, row.title) for row, _data, _identity in self.subentries]
        old_devices = [(identity, devices.async_get(identity).identifiers, devices.async_get(identity).config_subentry_id)
                       for identity, _values in self.devices]
        old_entities = [(identity, entities.async_get(identity).unique_id, entities.async_get(identity).config_subentry_id)
                        for identity, _value in self.entities]
        try:
            for row in self.creations:
                self.hass.config_entries.async_add_subentry(self.entry, row)
            for row, data, unique_id in self.subentries:
                title = str(data['name']) if data.get('name') and data.get('name') != row.data.get('name') else row.title
                self.hass.config_entries.async_update_subentry(self.entry, row, data=data, unique_id=unique_id, title=title)
            account_owners = {(DOMAIN, 'phone_endpoint:' + phone_endpoint_id(row)): row.subentry_id
                              for row in self.entry.subentries.values() if row.subentry_type == 'phone'}
            for identity, identifiers in self.devices:
                candidates = {account_owners[value] for value in identifiers if value in account_owners}
                ownership = ({'new_config_subentry_id': next(iter(candidates))}
                             if len(candidates) == 1 and devices.async_get(identity).config_subentry_id is None else {})
                devices.async_update_device(identity, new_identifiers=identifiers, **ownership)
            for identity, unique_id in self.entities:
                record = entities.async_get(identity)
                device = devices.async_get(record.device_id) if record.device_id else None
                ownership = ({'config_subentry_id': device.config_subentry_id} if device is not None
                             and device.config_subentry_id and record.config_subentry_id is None else {})
                entities.async_update_entity(identity, new_unique_id=unique_id, **ownership)
            self.hass.config_entries.async_update_entry(self.entry, data=self.data, options=self.options)
        except BaseException as error:
            failures = []
            def restore(callback):
                try:
                    callback()
                except BaseException as rollback_error:
                    failures.append(rollback_error)
            for identity, unique_id, subentry_id in reversed(old_entities):
                record = entities.async_get(identity)
                changes = {}
                if record.unique_id != unique_id:
                    changes['new_unique_id'] = unique_id
                if record.config_subentry_id != subentry_id:
                    changes['config_subentry_id'] = subentry_id
                if changes:
                    restore(lambda: entities.async_update_entity(identity, **changes))
            for identity, identifiers, subentry_id in reversed(old_devices):
                record = devices.async_get(identity)
                changes = {}
                if record.identifiers != identifiers:
                    changes['new_identifiers'] = identifiers
                if record.config_subentry_id != subentry_id:
                    changes['new_config_subentry_id'] = subentry_id
                if changes:
                    restore(lambda: devices.async_update_device(identity, **changes))
            for row, data, unique_id, title in reversed(snapshots):
                restore(lambda: self.hass.config_entries.async_update_subentry(self.entry, row, data=data, unique_id=unique_id, title=title))
            for row in self.creations:
                if row.subentry_id in self.entry.subentries:
                    restore(lambda: self.hass.config_entries.async_remove_subentry(self.entry, row.subentry_id))
            restore(lambda: self.hass.config_entries.async_update_entry(self.entry, data=current[0], options=current[1]))
            if failures:
                raise BaseExceptionGroup('Account identity rollback could not restore every native record', [error, *failures])
            raise
        runtime = getattr(self.entry, 'runtime_data', None)
        if self.activate_registrar:
            from .phone_config import async_enable_registrar_for_new_account
            async_enable_registrar_for_new_account(self.hass, self.entry)
        if runtime is not None and getattr(runtime, 'endpoints', None) is not None:
            runtime.endpoints.subentry_ids = {phone_endpoint_id(row): row.subentry_id
                for row in self.entry.subentries.values() if row.subentry_type == 'phone'}
            runtime.softphones = {self.mapping.get(key, key): {**value, 'endpoint_id': self.mapping.get(key, key)}
                                  for key, value in runtime.softphones.items()}
            runtime.softphone_presence = {self.mapping.get(key, key): value
                                         for key, value in runtime.softphone_presence.items()}
            runtime.endpoints.rekey_accounts(self.mapping)
        from .repairs import async_sync_managed_record_issues
        async_sync_managed_record_issues(self.hass, self.entry, dict(self.invalid_managed))
        from .repairs import async_sync_esphome_account_setup_issue
        for device_id in self.pending_esphome:
            async_sync_esphome_account_setup_issue(self.hass, self.entry, device_id, pending=True)


def account_identity_migration_plan(hass, entry, *, account_updates=None, stored_references=()):
    """Resolve exact old keys from configured accounts, never from a label."""
    mapping, owners = {}, {}
    accounts = []
    rows = dict(entry.subentries)
    for identity, updates in (account_updates or {}).items():
        row = rows[identity]
        if row.subentry_type != 'phone':
            raise PhoneIdentityConflict(row.title, 'phone', row.subentry_type)
        rows[identity] = replace(row, data=MappingProxyType(dict(updates)))
    creations, legacy_keys, invalid_managed = set(), {}, {}
    for hardware, raw in entry.data.get('managed_esphome_phones', {}).items():
        from homeassistant.config_entries import ConfigSubentry
        from .managed_phones import ManagedPhone

        try:
            if not isinstance(raw, Mapping):
                raise ValueError('The saved account record is not an object')
            phone = ManagedPhone(**{key: value for key, value in raw.items() if key in ManagedPhone.__dataclass_fields__})
            if phone.device_id != hardware:
                raise ValueError('The saved hardware association does not match the record')
        except (ValueError, TypeError) as error:
            invalid_managed[hardware] = type(error).__name__
            continue
        existing = next((row for row in rows.values() if row.subentry_type == 'phone'
                         and str(row.data.get('username') or '').casefold() == phone.username.casefold()), None)
        old = str(raw.get('endpoint_id') or 'esphome:' + hardware)
        legacy_keys[old] = phone.username
        if existing is not None:
            if (existing.data.get('username') != phone.username or existing.data.get('password') != phone.password
                    or (existing.data.get('device_id') and existing.data['device_id'] != hardware)):
                raise PhoneIdentityConflict(existing.title, phone.username, old, subentry_id=existing.subentry_id)
            native = {key: value for key, value in phone.record().items() if key in {
                'device_id', 'origin_entry_id', 'mac', 'route_id', 'api_host', 'active',
                'membership_pending', 'membership_observed'}}
            rows[existing.subentry_id] = replace(existing, data=MappingProxyType({**existing.data, **native,
                'managed_esphome': True, 'managed_exclusive': bool(existing.data.get('managed_esphome'))}))
        else:
            row = ConfigSubentry(subentry_type='phone', title=phone.display_name or phone.username,
                unique_id='phone:' + old, data=MappingProxyType({**raw, **phone.record(), 'endpoint_id': old, 'kind': 'esphome',
                    **({'username_source': raw['username_source']} if raw.get('username_source') == 'user' else {})}))
            rows[row.subentry_id] = row
            creations.add(row.subentry_id)
    pending_esphome, legacy_memberships = [], {}
    from .esphome_native import device_info, normalize_mac, runtime_for_entry
    from .managed_phones import ManagedPhone, managed_device_username, managed_phone_mode
    from .sip_registrar import generate_password
    from homeassistant.config_entries import ConfigSubentry
    for origin, device, old in legacy_esphome_references(hass, entry, stored_references=stored_references):
        native = runtime_for_entry(origin)
        info = device_info(native)
        try:
            mac = normalize_mac(info.mac_address if info is not None else '')
            if not any(kind == dr.CONNECTION_NETWORK_MAC and normalize_mac(value) == mac for kind, value in device.connections):
                raise ValueError('The native phone MAC is not verified')
            username = managed_device_username(native)
        except (ValueError, TypeError):
            pending_esphome.append(device.id)
            continue
        membership = entry.data.get('esphome_phone_memberships', {}).get(old, {})
        membership = membership if isinstance(membership, Mapping) else {}
        phone = ManagedPhone(device_id=device.id, origin_entry_id=origin.entry_id, mac=mac,
            username=username, password=generate_password(), endpoint_id=old, display_name=username,
            route_id=str(info.name), api_host=str(origin.data.get('host') or ''),
            queues=membership.get('queues', ()), ring_group=str(membership.get('ring_group') or ''),
            membership_pending=membership.get('membership_pending', {}), membership_observed=membership.get('membership_observed', {}),
            active=managed_phone_mode(hass, origin, device.id, mac) is True)
        legacy_memberships[username] = {field: membership[field] for field in ('queues', 'ring_group') if field in membership}
        row = ConfigSubentry(subentry_type='phone', title=username, unique_id='phone:' + old,
            data=MappingProxyType({**membership, **phone.record(), 'kind': 'esphome', 'username_source': 'esphome'}))
        rows[row.subentry_id] = row
        creations.add(row.subentry_id)
    account_sources, account_overrides = {}, {}
    for row in rows.values():
        if row.subentry_type != 'phone' or row.data.get('kind') not in {'sip_account', 'browser', 'esphome'}:
            continue
        old = phone_endpoint_id(row)
        previous_username = normalize_username(str(row.data.get('username') or ''))
        update = _account_username_update(hass, entry, row)
        if update is not None:
            account_overrides[row.subentry_id] = update
        username = update['username'] if update is not None else previous_username
        account_sources[row.subentry_id] = update.get('username_source') if update is not None else row.data.get('username_source', 'user')
        owner = owners.get(username.casefold())
        if owner is not None and owner.subentry_id != row.subentry_id:
            raise PhoneIdentityConflict(row.title, username, phone_endpoint_id(owner), subentry_id=row.subentry_id)
        owners[username.casefold()] = row
        accounts.append((row, old, username))
    for row, old, username in accounts:
        previous_username = normalize_username(str(row.data.get('username') or ''))
        original = entry.subentries.get(row.subentry_id)
        original_username = str(original.data.get('username') or '') if original is not None else ''
        references = (old, *_ha_reference_origins(row.data),
                      *((previous_username,) if previous_username != username else ()),
                      *((original_username,) if original_username and original_username != username else ()))
        for legacy in references:
            current_owner = owners.get(legacy.casefold())
            if current_owner is not None and current_owner.subentry_id != row.subentry_id:
                continue
            existing = mapping.get(legacy)
            if existing is not None and existing != username:
                raise PhoneIdentityConflict(row.title, username, legacy, subentry_id=row.subentry_id)
            mapping[legacy] = username
    for old, username in legacy_keys.items():
        target = mapping.get(username, username)
        if old in mapping and mapping[old] != target:
            raise PhoneIdentityConflict(entry.title, target, old)
        mapping[old] = target
    legacy_state_id = 'voip_stack_ha_softphone_call_state'
    native_entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    for entity in native_entities:
        if entity.unique_id != legacy_state_id:
            continue
        selected = [username for row, _old, username in accounts if row.subentry_id == entity.config_subentry_id]
        if not selected and 'default' not in mapping:
            selected = [username for row, _old, username in accounts if row.data.get('kind') == 'browser'
                        or row.data.get('username_source') == 'ha_name'
                        or any(isinstance(binding, Mapping) and binding.get('source') == 'migration'
                               for binding in row.data.get('ha_bindings') or ())]
        target = mapping.get('default') or (selected[0] if len(selected) == 1 else '')
        if not target:
            raise PhoneIdentityConflict(entity.entity_id, 'the original Home Assistant account', legacy_state_id)
        mapping['default'] = target
    if 'default' in mapping:
        from .const import HA_SOFTPHONE_DEVICE_ID
        mapping[HA_SOFTPHONE_DEVICE_ID] = mapping['default']
    runtime = getattr(entry, 'runtime_data', None)
    if (runtime is not None and getattr(runtime, 'sip', None) is not None
            and any(str(getattr(session, 'phase', '')) != 'terminated'
                    for session in runtime.sip.sessions.values())):
        raise PhoneIdentityConflict(entry.title, 'inactive account ownership', 'active calls')
    if runtime is not None and getattr(runtime, 'endpoints', None) is not None:
        runtime.endpoints.rekey_accounts(mapping, apply=False)
        for values in (runtime.softphones, runtime.softphone_presence):
            future = {}
            for old, value in values.items():
                identity = _reference(old, mapping)
                if identity in future and future[identity] != value:
                    raise PhoneIdentityConflict(entry.title, identity, old)
                future[identity] = value
    data = deepcopy(dict(entry.data))
    if invalid_managed:
        data['managed_esphome_phones'] = {key: deepcopy(value) for key, value in
            data.get('managed_esphome_phones', {}).items() if key in invalid_managed}
    else:
        data.pop('managed_esphome_phones', None)
    if isinstance(data.get('external_call_blocked_endpoints'), (list, tuple)):
        data['external_call_blocked_endpoints'] = [_reference(value, mapping)
                                                  for value in data['external_call_blocked_endpoints']]
    # Keep the first pre-upgrade HA key for event compatibility. A later GUI
    # rename updates destinations, never creates a public SIP alias.
    aliases = {old: _reference(username, mapping) for old, username in
               data.get('legacy_phone_references', {}).items()}
    persistent_references = set(legacy_keys)
    for row, old, username in accounts:
        original = entry.subentries.get(row.subentry_id)
        original_username = str(original.data.get('username') or '') if original is not None else str(row.data.get('username') or '')
        origins = _ha_reference_origins(row.data)
        persistent_references.update(origins)
        if old != original_username or old in origins or row.data.get('kind') in {'browser', 'esphome'}:
            persistent_references.add(old)
        elif original_username != username:
            aliases.pop(original_username, None)
        previous_username = str(row.data.get('username') or '')
        if previous_username != username and account_sources[row.subentry_id] in {'ha_name', 'esphome'}:
            persistent_references.add(previous_username)
    for old, username in mapping.items():
        if old != username and old in persistent_references:
            aliases.setdefault(old, username)
    if 'default' in aliases:
        from .const import HA_SOFTPHONE_DEVICE_ID
        aliases[HA_SOFTPHONE_DEVICE_ID] = aliases['default']
    if aliases:
        data['legacy_phone_references'] = aliases
    else:
        data.pop('legacy_phone_references', None)
    if isinstance(data.get('parking'), dict):
        parking = data['parking']
        parking['return_destination'] = _reference(parking.get('return_destination', ''), mapping)
    for field in ('trunk_inbound_default_target', 'inbound_default_target'):
        if field in data:
            data[field] = _reference(data[field], mapping)
    if isinstance(data.get('phonebook_contacts'), list):
        data['phonebook_contacts'] = [_typed_data(row, 'contact', mapping)
                                     for row in data['phonebook_contacts']]
    options = deepcopy(dict(entry.options))
    activate_registrar = any(row.subentry_id in creations and row.data.get('enabled', True)
                             for row, _old, _user in accounts)
    activate_registrar |= entry.version == 7 and entry.minor_version < 4 and any(
            row.data.get('enabled', True) and (
                row.data.get('kind') in {'browser', 'esphome'}
                or row.data.get('managed_esphome') and row.data.get('active', True)
                or row.data.get('username_source') == 'ha_name'
                or any(isinstance(binding, Mapping) and binding.get('source') == 'migration'
                       for binding in row.data.get('ha_bindings') or ())) for row, _old, _user in accounts)
    if activate_registrar:
        from .const import CONF_REGISTRAR_ENABLED
        data[CONF_REGISTRAR_ENABLED] = True
        if CONF_REGISTRAR_ENABLED in options:
            options[CONF_REGISTRAR_ENABLED] = True
    if isinstance(options.get('parking'), dict):
        options['parking']['return_destination'] = _reference(options['parking'].get('return_destination', ''), mapping)
    for field in ('trunk_inbound_default_target', 'inbound_default_target'):
        if field in options:
            options[field] = _reference(options[field], mapping)
    for field in ('esphome_phone_policies', 'esphome_mailboxes', 'esphome_phone_memberships'):
        previous = data.get(field)
        if isinstance(previous, dict):
            future = {}
            for key, value in previous.items():
                target = _reference(key, mapping)
                if target in future and future[target] != value:
                    raise PhoneIdentityConflict(entry.title, target, key)
                future[target] = deepcopy(value)
                if field == 'esphome_phone_policies' and isinstance(future[target], dict):
                    _policy(future[target], mapping)
            data[field] = future
    for receipt in data.get('esphome_sip_enrollments', {}).values():
        if isinstance(receipt, dict) and 'endpoint_id' in receipt:
            receipt['endpoint_id'] = _reference(receipt['endpoint_id'], mapping)
    updates = []
    by_subentry = {row.subentry_id: (old, username) for row, old, username in accounts}
    final_rows = {}
    for row in rows.values():
        future = _typed_data(row.data, row.subentry_type, mapping)
        future.update(account_overrides.get(row.subentry_id, {}))
        if row.subentry_type == 'phone' and data.get('group_membership_version') == 1:
            future.pop('queues', None)
            future.pop('ring_group', None)
        unique_id = row.unique_id
        if row.subentry_id in by_subentry:
            old, username = by_subentry[row.subentry_id]
            future.pop('endpoint_id', None)
            future['username'] = username
            future['username_source'] = account_sources[row.subentry_id]
            unique_id = 'phone:' + username
            if future.get('kind') == 'browser':
                from .sip_registrar import generate_password
                future['kind'] = 'sip_account'
                future['password'] = future.get('password') or generate_password()
                bindings = list(future.get('ha_bindings') or ())
                if not any(isinstance(binding, dict) and binding.get('source') == 'migration' for binding in bindings):
                    bindings.append({'source': 'migration', 'reference': 'legacy_ha_phone:' + old})
                future['ha_bindings'] = bindings
            if future.get('kind') == 'esphome':
                future['kind'] = 'sip_account'
                future['name'] = future.get('name') or future.get('display_name') or row.title
                future['managed_esphome'] = True
                future['managed_exclusive'] = True
            if future.get('managed_esphome'):
                for field, destination in (('esphome_phone_policies', 'call_policy'), ('esphome_mailboxes', 'voicemail')):
                    saved = data.get(field, {}).pop(username, None)
                    if saved is not None:
                        if destination in future and future[destination] != saved:
                            raise PhoneIdentityConflict(row.title, username, old, subentry_id=row.subentry_id)
                        future[destination] = saved
        if future != dict(row.data) or unique_id != row.unique_id:
            updates.append((entry.subentries.get(row.subentry_id, row), future, unique_id))
        final_rows[row.subentry_id] = replace(row, data=MappingProxyType(future), unique_id=unique_id)
        if row.subentry_id not in creations and dict(row.data) != dict(entry.subentries[row.subentry_id].data):
            if not any(record[0].subentry_id == row.subentry_id for record in updates):
                updates.append((entry.subentries[row.subentry_id], future, unique_id))
    from .phone_queue_membership import GROUP_MEMBERSHIP_VERSION, _membership_update_plan
    if data.get(GROUP_MEMBERSHIP_VERSION) == 1 and legacy_memberships:
        prospective = SimpleNamespace(data=data, options=options, subentries=final_rows, runtime_data=None)
        group_creations, group_updates = _membership_update_plan(hass, prospective, legacy_memberships, reconcile=False)
        for group in group_creations:
            row = ConfigSubentry(subentry_type='group', title=group['name'], unique_id='group:' + group['group_id'], data=group)
            final_rows[row.subentry_id] = row
            creations.add(row.subentry_id)
        for identity, row in tuple(final_rows.items()):
            if row.subentry_type == 'group' and row.data.get('group_id') in group_updates:
                values = group_updates[row.data['group_id']]
                final_rows[identity] = replace(row, data=MappingProxyType(values))
                updates = [record for record in updates if record[0].subentry_id != identity]
                updates.append((entry.subentries[identity], values, row.unique_id))
    for username in legacy_memberships:
        data.get('esphome_phone_memberships', {}).pop(username, None)
    from .phone_config import phone_subentries, phone_subentry_data, validate_sip_account_namespace
    prospective = SimpleNamespace(data=data, options=options, subentries=final_rows, runtime_data=None)
    validate_sip_account_namespace(prospective, [phone_subentry_data(row) for row in phone_subentries(prospective)])
    devices, entities = dr.async_get(hass), er.async_get(hass)
    device_updates, entity_updates = [], []
    for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
        identifiers = {(domain, 'phone_endpoint:' + mapping[value[len('phone_endpoint:'):]])
                       if domain == DOMAIN and value.startswith('phone_endpoint:') and value[len('phone_endpoint:'):] in mapping
                       else (domain, value) for domain, value in device.identifiers}
        for identifier in identifiers:
            existing = devices.async_get_device_by_identifier(identifier, entry.entry_id)
            if existing is not None and existing.id != device.id:
                raise PhoneIdentityConflict(device.name or entry.title, identifier[1], device.id)
        if identifiers != device.identifiers:
            device_updates.append((device.id, identifiers))
    for entity in er.async_entries_for_config_entry(entities, entry.entry_id):
        if entity.unique_id.startswith('queue_agent:'):
            device = devices.async_get(entity.device_id) if entity.device_id else None
            queue_ids = {value.removeprefix('group:') for domain, value in device.identifiers
                         if domain == DOMAIN and value.startswith('group:')} if device is not None else set()
            row = entry.subentries.get(entity.config_subentry_id)
            if row is not None and row.subentry_type == 'group':
                queue_ids.add(str(row.data.get('group_id') or row.subentry_id))
            for queue_id in queue_ids:
                prefix = f'queue_agent:{queue_id}:'
                if not entity.unique_id.startswith(prefix):
                    continue
                old = entity.unique_id[len(prefix):]
                unique_id = prefix + mapping.get(old, old)
                existing = entities.async_get_entity_id(entity.domain, entity.platform, unique_id)
                if existing is not None and existing != entity.entity_id:
                    raise PhoneIdentityConflict(entity.entity_id, unique_id, entity.unique_id)
                if unique_id != entity.unique_id:
                    entity_updates.append((entity.entity_id, unique_id))
                break
            continue
        if entity.unique_id == legacy_state_id:
            unique_id = 'phone_endpoint_' + mapping['default'] + '_call_state'
            existing = entities.async_get_entity_id(entity.domain, entity.platform, unique_id)
            if existing is not None and existing != entity.entity_id:
                raise PhoneIdentityConflict(entity.entity_id, unique_id, entity.unique_id)
            entity_updates.append((entity.entity_id, unique_id))
            continue
        owned = by_subentry.get(entity.config_subentry_id)
        references = (owned,) if owned is not None else tuple(mapping.items())
        for old, username in references:
            prefix = 'phone_endpoint_' + old + '_'
            if entity.unique_id.startswith(prefix):
                unique_id = 'phone_endpoint_' + username + '_' + entity.unique_id[len(prefix):]
                existing = entities.async_get_entity_id(entity.domain, entity.platform, unique_id)
                if existing is not None and existing != entity.entity_id:
                    raise PhoneIdentityConflict(entity.entity_id, unique_id, entity.unique_id)
                if unique_id != entity.unique_id:
                    entity_updates.append((entity.entity_id, unique_id))
                break
    updates = [record for record in updates if record[0].subentry_id not in creations]
    original = (deepcopy(dict(entry.data)), deepcopy(dict(entry.options)),
                {key: deepcopy(row.as_dict()) for key, row in entry.subentries.items()})
    return AccountIdentityMigrationPlan(hass, entry, mapping, data, options, tuple(updates), tuple(device_updates),
                                        tuple(entity_updates), tuple(final_rows[key] for key in sorted(creations)), original,
                                        tuple(invalid_managed.items()), tuple(pending_esphome), activate_registrar)


async def async_migrate_account_identities(hass, entry, *, account_updates=None):
    """Stage identity changes before invoking storage adapters or native writes."""
    from .session_cleanup import async_wait_for_cleanup
    task = asyncio.create_task(_async_migrate_account_identities(hass, entry, account_updates=account_updates),
                               name='voip-account-identity-migration')
    return await async_wait_for_cleanup(task)


async def _async_migrate_account_identities(hass, entry, *, account_updates=None):
    """Finish the owned batch or all of its rollback before a caller can leave."""
    from homeassistant.helpers.storage import Store
    from homeassistant.helpers import issue_registry as ir
    fault_prefix = 'archive_record_' + entry.entry_id + '_'
    quarantined = {issue_id[len(fault_prefix):].removesuffix('.json').removesuffix('.pending')
                   for domain, issue_id in ir.async_get(hass).issues if domain == DOMAIN and issue_id.startswith(fault_prefix)}
    references = _runtime_legacy_references(entry)
    for name in ('queues', 'ha_bindings', 'physical_phones'):
        value = await Store(hass, 1, f'voip_stack.{name}.{entry.entry_id}').async_load()
        references.update(_stored_esphome_references(value))
    from .mailbox import archive_path
    from .core.media_archive import _account_rebind_entries
    path = archive_path(hass)
    if path is not None:
        entries, _faults = await hass.async_add_executor_job(_account_rebind_entries, path, {})
        references.update(_stored_esphome_references([row['mailbox_id'] for identity, row in entries.items()
                                                     if identity not in quarantined]))
    plan = account_identity_migration_plan(hass, entry, account_updates=account_updates, stored_references=references)
    resources = [await _async_prepare_account_queue_migration(hass, entry, plan.mapping),
                 await _async_prepare_account_binding_migration(hass, entry, plan.mapping),
                 await _async_prepare_physical_phone_migration(hass, entry, plan.mapping)]
    from .mailbox import archive_path
    from .core.media_archive import MediaArchive

    path = archive_path(hass)
    if path is not None:
        runtime = getattr(entry, 'runtime_data', None)
        owner = getattr(runtime, 'sip', None)
        archive = owner.component('media_archive') if owner is not None else None
        if archive is None:
            archive = MediaArchive(path, hass.async_add_executor_job)
            resources.append(await archive.prepare_account_rebind(plan.mapping, load_from_storage=True, exclude_ids=quarantined))
        else:
            resources.append(await archive.prepare_account_rebind(plan.mapping, exclude_ids=quarantined))
    committed = []
    try:
        for resource in resources:
            committed.append(resource)
            await resource.commit()
        for resource in resources:
            if isinstance(resource, _PreparedQueueRebind):
                resource.validate_runtime()
        for resource in resources:
            if isinstance(resource, _PreparedQueueRebind):
                resource.apply_runtime()
        plan.commit()
    except BaseException as error:
        failures = []
        for resource in reversed(committed):
            if isinstance(resource, _PreparedQueueRebind):
                try:
                    resource.restore_runtime()
                except BaseException as rollback_error:
                    failures.append(rollback_error)
        for resource in reversed(committed):
            try:
                await resource.rollback()
            except BaseException as rollback_error:
                failures.append(rollback_error)
        if failures:
            raise BaseExceptionGroup('Account storage rollback could not restore every owner', [error, *failures])
        raise
    from .repairs import async_sync_archive_record_issues
    faults = {filename: reason for resource in resources for filename, reason in getattr(resource, 'faults', {}).items()}
    async_sync_archive_record_issues(hass, entry, faults)
    return plan.mapping


async def _async_prepare_account_binding_migration(hass, entry, mapping):
    """Update typed cached associations, preserving dashboard and card configurations."""
    from homeassistant.helpers.storage import Store

    runtime = getattr(entry, 'runtime_data', None)
    owner = getattr(runtime, 'ha_bindings', None)
    store = owner._store if owner is not None else Store(
        hass, 1, f'voip_stack.ha_bindings.{entry.entry_id}', atomic_writes=True)
    previous = await store.async_load()
    current = (lambda: {'dashboards': owner._dashboards, 'popups': owner._popups}) if owner is not None else None
    original = deepcopy(current()) if current is not None else None
    future = deepcopy(original if current is not None else previous)
    if isinstance(future, dict):
        for rows in future.get('dashboards', {}).values():
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and 'endpoint_id' in row:
                        row['endpoint_id'] = _reference(row['endpoint_id'], mapping)
        for row in future.get('popups', {}).values():
            if isinstance(row, dict) and 'endpoint_id' in row:
                row['endpoint_id'] = _reference(row['endpoint_id'], mapping)
    def apply(value):
        owner._dashboards, owner._popups = deepcopy(value.get('dashboards', {})), deepcopy(value.get('popups', {}))
    return _PreparedQueueRebind(store, previous, future, current=current,
                               apply=apply if owner is not None else None, original=original)


class _PreparedQueueRebind:
    def __init__(self, store, previous, future, *, current=None, apply=None, restore=None, original=None):
        self.store, self.previous, self.future = store, previous, future
        self.current, self.apply, self.original = current, apply, original
        self.restore = restore or apply
        self.started = False
        self.applied = False

    def validate_runtime(self):
        if self.current is not None and self.current() != self.original:
            raise ValueError('Account storage changed during identity migration')

    def apply_runtime(self):
        if self.apply is not None:
            self.apply(self.future)
            self.applied = True

    def restore_runtime(self):
        if self.applied:
            self.restore(self.original)
            self.applied = False

    async def commit(self):
        self.validate_runtime()
        self.started = True
        if self.previous != self.future:
            await self.store.async_save(self.future)

    async def rollback(self):
        if not self.started:
            return
        self.restore_runtime()
        if self.previous != self.future:
            if self.previous is None:
                await self.store.async_remove()
            else:
                await self.store.async_save(self.previous)


async def _async_prepare_account_queue_migration(hass, entry, mapping):
    from homeassistant.helpers.storage import Store

    runtime = getattr(entry, 'runtime_data', None)
    owner = getattr(runtime, 'queues', None)
    store = owner.store if owner is not None else Store(hass, 1, f'voip_stack.queues.{entry.entry_id}')
    previous = await store.async_load()
    current = owner._snapshot if owner is not None else None
    original = deepcopy(current()) if current is not None else None
    future = deepcopy(original if current is not None else previous)
    def rekey(rows):
        result = {}
        for old, value in rows.items():
            identity = _reference(old, mapping)
            if identity in result and result[identity] != value:
                raise PhoneIdentityConflict(entry.title, identity, old)
            result[identity] = value
        return result
    if isinstance(future, dict):
        for field in ('agent_states', 'agent_stats'):
            if isinstance(future.get(field), dict):
                future[field] = {queue: rekey(rows) for queue, rows in future[field].items()}
        if isinstance(future.get('last_ended'), dict):
            future['last_ended'] = rekey(future['last_ended'])
        if isinstance(future.get('round_robin'), dict):
            future['round_robin'] = {queue: _reference(value, mapping) for queue, value in future['round_robin'].items()}
        for policy in future.get('policies', {}).values():
            if isinstance(policy, dict) and isinstance(policy.get('agent_order'), (list, tuple)):
                policy['agent_order'] = [_reference(value, mapping) for value in policy['agent_order']]
            if isinstance(policy, dict):
                for field in ('fallback', 'fallback_full', 'fallback_timeout', 'fallback_no_agents',
                              'fallback_disabled', 'fallback_removed'):
                    if field in policy:
                        policy[field] = _reference(policy[field], mapping)
    definitions = dict(owner.definitions) if owner is not None else {}
    renamed_definitions = {key: replace(queue, members=tuple(mapping.get(member, member) for member in queue.members))
                           for key, queue in definitions.items()}
    def install_operations(value):
        from .call_queues import normalize_queue_operations
        from .queue_policy import QueuePolicy
        for field, rows in normalize_queue_operations(value).items():
            setattr(owner, field, deepcopy(rows))
        owner.policies = {key: QueuePolicy.from_data(rows) for key, rows in value.get('policies', {}).items()}
    def apply(value):
        install_operations(value)
        owner.definitions = dict(renamed_definitions)
    def restore(value):
        install_operations(value)
        owner.definitions = dict(definitions)
    return _PreparedQueueRebind(store, previous, future, current=current,
                               apply=apply if owner is not None else None,
                               restore=restore if owner is not None else None, original=original)


async def _async_prepare_physical_phone_migration(hass, entry, mapping):
    """Rebind the existing delivery footprint, retaining profile revisions."""
    from homeassistant.helpers.storage import Store

    runtime = getattr(entry, 'runtime_data', None)
    owner = getattr(runtime, 'physical_phones', None)
    store = owner.store if owner is not None else Store(
        hass, 1, f'voip_stack.physical_phones.{entry.entry_id}', atomic_writes=True)
    previous = await store.async_load()
    current = (lambda: owner.states) if owner is not None else None
    original = deepcopy(current()) if current is not None else None
    future = deepcopy(original if current is not None else previous)
    if isinstance(future, dict):
        for state in future.values():
            if not isinstance(state, dict):
                continue
            footprint = state.get('managed_slots')
            if isinstance(footprint, dict) and isinstance(footprint.get('lines'), dict):
                footprint['lines'] = {slot: [_reference(value, mapping) for value in identities]
                                      for slot, identities in footprint['lines'].items()}
            for line in state.get('lines', ()):
                if isinstance(line, dict) and 'endpoint_id' in line:
                    line['endpoint_id'] = _reference(line['endpoint_id'], mapping)
    def apply(value):
        owner.states = deepcopy(value)
    return _PreparedQueueRebind(store, previous, future, current=current,
                               apply=apply if owner is not None else None, original=original)
