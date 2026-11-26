"""Repair issue lifecycle for actionable VoIP Stack conditions."""

from __future__ import annotations

from builtins import BaseExceptionGroup
from inspect import signature
import logging
import math
from pathlib import Path
import voluptuous as vol

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import section
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.selector import (BooleanSelector, DeviceSelector, SelectSelector, SelectSelectorConfig,
    NumberSelector, NumberSelectorConfig, TextSelector, TextSelectorConfig, TextSelectorType)

from .const import DOMAIN
from .runtime_data import runtime_data


_LOGGER = logging.getLogger(__name__)
MEDIA_CAPTURE_ISSUE_ID = "media_capture_enabled"
ESPHOME_ACTION_ISSUE_PREFIX = "esphome_actions_"
ESPHOME_ENDPOINT_COLLISION_ISSUE_PREFIX = "esphome_endpoint_collision_"
_PHONE_ACTIONS = {
    "start_call_on_line": (("dest", 3), ("line", 3), ("send_video", 0), ("receive_video", 0)),
    "answer_call": (), "decline_call": (("reason", 3),), "hangup_call": (),
}


class AccountMigrationRepairFlow(ConfirmRepairFlow):
    """Edit a conflicting native account even while its entry cannot load."""

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id
        self.subentry_id = ""
        self._legacy = {}

    async def async_step_init(self, user_input=None):
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if entry is None:
            return self.async_create_entry(data={})
        rows = [row for row in entry.subentries.values() if row.subentry_type == "phone"]
        choices = [{"value": row.subentry_id, "label": row.title} for row in rows]
        self._legacy = {}
        if entry.version < 3:
            for index, raw in enumerate(entry.data.get('sip_accounts', ())):
                if isinstance(raw, dict):
                    identity = f'legacy_account:{index}'
                    self._legacy[identity] = (index, str(raw.get('username') or raw.get('id') or ''))
                    choices.append({'value': identity, 'label': str(raw.get('display_name') or raw.get('name') or raw.get('username') or 'SIP account')})
            if not any(row.unique_id in {'default', 'phone:default'} or row.data.get('endpoint_id') == 'default' for row in rows):
                self._legacy['legacy_ha_default'] = (None, '')
                choices.append({'value': 'legacy_ha_default', 'label': str(self.hass.config.location_name or 'Home Assistant phone')})
        if not choices:
            return await self.async_step_confirm()
        if user_input is not None and "account" in user_input:
            self.subentry_id = user_input["account"]
            return await self.async_step_edit_account()
        return self.async_show_form(step_id="init", data_schema=vol.Schema({
            vol.Required("account", default=self.subentry_id if self.subentry_id in {row['value'] for row in choices} else choices[0]['value']): SelectSelector(
                SelectSelectorConfig(options=choices, mode="dropdown")),
        }))

    async def async_step_edit_account(self, user_input=None):
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if entry is not None and self.subentry_id in self._legacy:
            return await self._async_edit_legacy_account(entry, user_input)
        row = entry.subentries.get(self.subentry_id) if entry is not None else None
        if row is None:
            return await self.async_step_init()
        values = {"name": row.data.get("name") or row.title,
                  "username": row.data.get("username", ""), "extension": row.data.get("extension", "")}
        errors = {}
        if user_input is not None:
            retrying_migration = entry.state is ConfigEntryState.MIGRATION_ERROR
            values.update(user_input)
            data = {**row.data, **values, 'username_source': 'user'}
            from homeassistant.helpers import entity_registry as er
            if (not entry.data.get('legacy_phone_references', {}).get('default')
                    and er.async_get(self.hass).async_get_entity_id('sensor', DOMAIN, 'voip_stack_ha_softphone_call_state')):
                bindings = list(data.get('ha_bindings') or ())
                if not any(binding.get('reference') == 'legacy_ha_phone:default' for binding in bindings):
                    bindings.append({'source': 'migration', 'reference': 'legacy_ha_phone:default'})
                data['ha_bindings'] = bindings
            if row.unique_id:
                data["endpoint_id"] = row.unique_id.removeprefix("phone:")
            try:
                from .sip_account_identity import async_migrate_account_identities
                if entry.version < 7 or entry.minor_version < 4:
                    # The original migration owner must preflight missing
                    # logins and references before changing account keys.
                    if row.unique_id:
                        data.pop('endpoint_id', None)
                    self.hass.config_entries.async_update_subentry(entry, row, data=data, title=values['name'])
                else:
                    await async_migrate_account_identities(self.hass, entry, account_updates={row.subentry_id: data})
                if retrying_migration:
                    await self.hass.config_entries.async_retry_migration(self.entry_id)
            except (HomeAssistantError, ValueError, TypeError):
                errors["base"] = "migration_retry_failed"
            else:
                if entry.state is not ConfigEntryState.MIGRATION_ERROR:
                    async_sync_phone_identity_issue(self.hass, entry)
                    async_sync_account_route_issue(self.hass, entry)
                    if entry.state is ConfigEntryState.LOADED and not retrying_migration:
                        self.hass.config_entries.async_schedule_reload(entry.entry_id)
                    return self.async_create_entry(data={})
                errors["base"] = "migration_retry_failed"
        return self.async_show_form(step_id="edit_account", errors=errors, data_schema=vol.Schema({
            vol.Required("name", default=values["name"]): TextSelector(),
            vol.Required("username", default=values["username"]): TextSelector(),
            vol.Optional("extension", default=values["extension"]): TextSelector(),
        }))

    async def _async_edit_legacy_account(self, entry, user_input):
        """Edit supported pre-subentry data through its existing config entry owner."""
        from .phone_config import browser_phone_data
        from .sip_registrar import generate_password, normalize_username
        from homeassistant.config_entries import ConfigSubentry

        index, original_username = self._legacy[self.subentry_id]
        accounts = list(entry.data.get('sip_accounts', ()))
        if index is not None and (index >= len(accounts) or not isinstance(accounts[index], dict)):
            return await self.async_step_init()
        raw = accounts[index] if index is not None and index < len(accounts) else {}
        default_row = next((row for row in entry.subentries.values() if row.subentry_type == 'phone'
                            and row.unique_id == 'phone:default'), None) if index is None else None
        defaults = (dict(default_row.data) if default_row is not None else
                    browser_phone_data(self.hass, entry, endpoint_id='default')) if index is None else raw
        values = {'name': defaults.get('display_name') or defaults.get('name') or defaults.get('username') or entry.title,
                  'username': defaults.get('username') or defaults.get('name') or entry.title,
                  'extension': defaults.get('extension', '')}
        errors = {}
        if user_input is not None:
            values.update(user_input)
            try:
                username = normalize_username(values['username'])
                name = str(values['name']).strip()
                extension = str(values['extension']).strip()
                if not name or len(name.encode()) > 1024 or any(ord(char) < 32 or ord(char) == 127 for char in name + extension):
                    raise ValueError('Invalid account label or extension')
                if index is None:
                    data = {**defaults, 'name': name, 'username': username, 'extension': extension,
                            'username_source': 'user', 'password': defaults.get('password') or generate_password()}
                    if default_row is not None:
                        self.hass.config_entries.async_update_subentry(entry, default_row, data=data, title=name)
                    else:
                        self.hass.config_entries.async_add_subentry(entry, ConfigSubentry(subentry_type='phone',
                            title=name, unique_id='phone:default', data=data))
                else:
                    if str(raw.get('username') or raw.get('id') or '') != original_username:
                        raise ValueError('The selected account changed while editing')
                    accounts[index] = {**raw, 'display_name': name, 'username': username,
                                       'extension': extension, 'username_source': 'user'}
                    self.hass.config_entries.async_update_entry(entry, data={**entry.data, 'sip_accounts': accounts})
                await self.hass.config_entries.async_retry_migration(self.entry_id)
            except (HomeAssistantError, ValueError, TypeError):
                errors['base'] = 'migration_retry_failed'
            else:
                if entry.state is not ConfigEntryState.MIGRATION_ERROR:
                    async_sync_phone_identity_issue(self.hass, entry)
                    async_sync_account_route_issue(self.hass, entry)
                    return self.async_create_entry(data={})
                errors['base'] = 'migration_retry_failed'
        return self.async_show_form(step_id='edit_account', errors=errors, data_schema=vol.Schema({
            vol.Required('name', default=values['name']): TextSelector(),
            vol.Required('username', default=values['username']): TextSelector(),
            vol.Optional('extension', default=values['extension']): TextSelector(),
        }))

    async def async_step_confirm(self, user_input=None):
        """Keep the issue open until the native migration succeeds."""
        if user_input is None:
            return await super().async_step_confirm()
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if entry is None:
            return self.async_create_entry(data={})
        retrying_migration = entry.state is ConfigEntryState.MIGRATION_ERROR
        try:
            if retrying_migration:
                await self.hass.config_entries.async_retry_migration(self.entry_id)
            else:
                from .sip_account_identity import async_migrate_account_identities
                await async_migrate_account_identities(self.hass, entry)
        except (HomeAssistantError, OSError, ValueError, TypeError):
            pass
        else:
            if entry.state is not ConfigEntryState.MIGRATION_ERROR:
                async_sync_phone_identity_issue(self.hass, entry)
                async_sync_account_route_issue(self.hass, entry)
                async_sync_account_migration_issue(self.hass, entry)
                if entry.state is ConfigEntryState.LOADED and not retrying_migration:
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_create_entry(data={})
        result = await super().async_step_confirm()
        result["errors"] = {"base": "migration_retry_failed"}
        return result


class AccountMigrationRetryRepairFlow(AccountMigrationRepairFlow):
    """Retry a failed migration without pretending an account edit will fix it."""

    async def async_step_init(self, user_input=None):
        return await self.async_step_confirm(user_input)


class DurationMigrationRepairFlow(ConfirmRepairFlow):
    """Correct saved duration fields even when migration prevents PBX setup."""

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id
        self._selected = None

    async def async_step_init(self, user_input=None):
        from .duration_repair import duration_repair_owners

        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if entry is None:
            return self.async_create_entry(data={})
        owners = duration_repair_owners(entry)
        if not owners:
            return await self.async_step_confirm()
        if user_input is not None:
            self._selected = next((owner for owner in owners if owner.identity == user_input.get("owner")), None)
            if self._selected is not None:
                return await self.async_step_edit_durations()
        return self.async_show_form(step_id="init", data_schema=vol.Schema({
            vol.Required("owner", default=owners[0].identity): SelectSelector(SelectSelectorConfig(
                options=[{"value": owner.identity, "label": owner.title} for owner in owners], mode="dropdown")),
        }))

    async def async_step_edit_durations(self, user_input=None):
        from .duration_repair import _at_path, duration_repair_owners, duration_repair_update

        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if entry is None:
            return self.async_create_entry(data={})
        if self._selected is None:
            return await self.async_step_init()
        errors = {}
        detail = self._selected.error()
        if user_input is not None:
            try:
                row, data = duration_repair_update(entry, self._selected, user_input.get("advanced", user_input))
            except RuntimeError:
                self._selected = None
                result = await self.async_step_init()
                if result["type"] == "form":
                    result["errors"] = {"base": "configuration_changed"}
                return result
            except (ValueError, TypeError, KeyError, OverflowError) as error:
                errors["base"] = "invalid_duration"
                detail = str(error)
            else:
                try:
                    if row is None:
                        self.hass.config_entries.async_update_entry(entry, data=data)
                    else:
                        self.hass.config_entries.async_update_subentry(entry, row, data=data)
                except (HomeAssistantError, OSError, ValueError, TypeError, RuntimeError):
                    errors["base"] = "migration_retry_failed"
                else:
                    self._selected = None
                    if duration_repair_owners(entry):
                        return await self.async_step_init()
                    return await self.async_step_confirm({})
        schema = {}
        for field in self._selected.fields:
            value = (user_input or {}).get(field.name, _at_path(self._selected.data, field.path))
            try:
                numeric = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            except OverflowError:
                numeric = False
            required = vol.Required(field.name, **({"default": value} if numeric else {}))
            schema[required] = NumberSelector(NumberSelectorConfig(mode="box", step="any", unit_of_measurement="s"))
        return self.async_show_form(step_id="edit_durations", errors=errors, data_schema=vol.Schema({
                vol.Optional("advanced"): section(vol.Schema(schema), {"collapsed": True}),
            }), description_placeholders={"owner": self._selected.title, "error": detail})

    def _diagnostic(self, entry):
        """Expose only controlled duration errors or a failure class, never data."""
        from .duration_config import duration_migration_plan

        try:
            duration_migration_plan(entry)
        except (ValueError, TypeError, KeyError, OSError, RuntimeError, OverflowError) as error:
            return type(error).__name__
        return ""

    async def async_step_confirm(self, user_input=None):
        from .duration_repair import duration_repair_owners

        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if entry is None:
            return self.async_create_entry(data={})
        if duration_repair_owners(entry):
            return await self.async_step_init()
        if user_input is None:
            return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}),
                description_placeholders={"error": self._diagnostic(entry)})
        try:
            if entry.state is ConfigEntryState.MIGRATION_ERROR:
                await self.hass.config_entries.async_retry_migration(self.entry_id)
            else:
                from . import async_migrate_entry

                await async_migrate_entry(self.hass, entry)
        except (HomeAssistantError, OSError, ValueError, TypeError, RuntimeError):
            pass
        else:
            from .duration_config import duration_migration_plan
            try:
                data, updates = duration_migration_plan(entry)
            except (ValueError, TypeError, KeyError, OSError, RuntimeError, OverflowError):
                data, updates = None, [None]
            if entry.version == 7 and entry.minor_version >= 7 and data == entry.data and not updates:
                if entry.state is ConfigEntryState.SETUP_ERROR:
                    await self.hass.config_entries.async_reload(self.entry_id)
                return self.async_create_entry(data={})
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}),
            errors={"base": "migration_retry_failed"}, description_placeholders={"error": self._diagnostic(entry)})


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data) -> RepairsFlow:
    """Create the native retry flow only for an account migration conflict."""
    if issue_id.startswith("duration_configuration_invalid_") and data is not None:
        return DurationMigrationRepairFlow(str(data["entry_id"]))
    if issue_id.startswith("account_migration_failed_") and data is not None:
        return AccountMigrationRetryRepairFlow(str(data["entry_id"]))
    if issue_id.startswith('managed_record_') and data is not None:
        return ManagedRecordRepairFlow(str(data['entry_id']), str(data['hardware']),
                                       membership_reference=data.get('membership_reference'))
    if issue_id.startswith('archive_record_') and data is not None:
        return ArchiveRecordRepairFlow(str(data['entry_id']), str(data['filename']))
    if issue_id.startswith('ldap_record_invalid_') and data is not None:
        return LdapRecordRepairFlow(str(data['entry_id']))
    if issue_id.startswith(("account_route_conflict_", "phone_identity_conflict_")) and data is not None:
        return AccountMigrationRepairFlow(str(data["entry_id"]))
    return ConfirmRepairFlow()


class ManagedRecordRepairFlow(RepairsFlow):
    """Correct a retained malformed account through its native hardware association."""

    def __init__(self, entry_id, hardware, *, membership_reference=None):
        self.entry_id, self.hardware = entry_id, hardware
        self.membership_reference = membership_reference

    async def async_step_init(self, user_input=None):
        return await self.async_step_account()

    async def async_step_account(self, user_input=None):
        from .esphome_native import device_info, normalize_mac, runtime_for_entry
        from .sip_registrar import normalize_username
        from homeassistant.helpers import device_registry as dr
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        records = entry.data.get('esphome_phone_memberships' if self.membership_reference else 'managed_esphome_phones', {}) if entry is not None else {}
        record_key = self.membership_reference or self.hardware
        if entry is None or record_key not in records:
            return self.async_create_entry(data={})
        raw = records[record_key]
        saved = dict(raw) if isinstance(raw, dict) else {}
        from .phone_queue_membership import membership_names, normalize_phone_queues
        try:
            queues = list(normalize_phone_queues(saved.get('queues')))
        except ValueError:
            queues = []
        try:
            ring_groups = list(membership_names('ring_group', saved.get('ring_group')))
        except ValueError:
            ring_groups = []
        try:
            conference_groups = list(membership_names('ring_group', saved.get('conference_group')))
        except ValueError:
            conference_groups = []
        saved_name = saved.get('display_name') or saved.get('name') or ''
        saved_username = saved.get('username', '')
        values = {'device_id': self.hardware, 'username': saved_username if isinstance(saved_username, str) else '',
                  'name': saved_name if isinstance(saved_name, str) else '',
                  'queues': queues, 'ring_group': ring_groups, 'conference_group': conference_groups,
                  'conference_ring': saved.get('conference_ring') if isinstance(saved.get('conference_ring'), bool) else False,
                  'extension': saved.get('extension') if isinstance(saved.get('extension'), str) else ''}
        errors = {}
        if user_input is not None:
            values.update(user_input)
            try:
                device = dr.async_get(self.hass).async_get(values['device_id'])
                original_device = dr.async_get(self.hass).async_get(self.hardware)
                origin = self.hass.config_entries.async_get_entry(device.config_entry_id) if device is not None else None
                info = device_info(runtime_for_entry(origin))
                mac = normalize_mac(info.mac_address if info is not None else '')
                if (origin is None or origin.domain != 'esphome' or (original_device is not None and device.id != self.hardware)
                        or not any(kind == dr.CONNECTION_NETWORK_MAC and normalize_mac(value) == mac
                                   for kind, value in device.connections)):
                    raise ValueError('Choose the original native hardware owner')
                from .device_resolver import slugify_route_id
                from .sip_registrar import generate_password
                password = user_input.get('password') or saved.get('password')
                if self.membership_reference and not password:
                    password = generate_password()
                if not isinstance(password, str):
                    raise ValueError('Provide a valid replacement credential')
                old = saved.get('endpoint_id')
                if (not isinstance(old, str) or not 1 <= len(old) <= 255 or old != old.strip()
                        or any(ord(char) < 32 or ord(char) == 127 for char in old)):
                    old = 'esphome:' + self.hardware
                corrected = {**saved, 'device_id': device.id, 'origin_entry_id': origin.entry_id, 'mac': mac,
                    'username': normalize_username(values['username']), 'username_source': 'user',
                    'display_name': str(values['name']).strip() or normalize_username(values['username']),
                    'password': password, 'endpoint_id': old, 'queues': list(normalize_phone_queues(values['queues'])),
                    'ring_group': ', '.join(membership_names('ring_group', values['ring_group'])),
                    'extension': str(values['extension']), 'route_id': slugify_route_id(info.name),
                    'conference_group': ', '.join(membership_names('ring_group', values['conference_group'])),
                    'conference_ring': bool(values['conference_ring']), 'membership_pending': {}, 'membership_observed': {},
                    'api_host': str(origin.data.get('host') or '')}
                from .managed_phones import ManagedPhone
                ManagedPhone(**{key: value for key, value in corrected.items() if key in ManagedPhone.__dataclass_fields__})
                if self.membership_reference:
                    memberships = dict(entry.data['esphome_phone_memberships'])
                    memberships.pop(self.membership_reference)
                    records = {**entry.data.get('managed_esphome_phones', {}), device.id: corrected}
                    self.hass.config_entries.async_update_entry(entry, data={**entry.data,
                        'esphome_phone_memberships': memberships, 'managed_esphome_phones': records})
                else:
                    records = {**entry.data['managed_esphome_phones']}
                    records.pop(self.hardware)
                    records[device.id] = corrected
                    self.hass.config_entries.async_update_entry(entry, data={**entry.data, 'managed_esphome_phones': records})
                if entry.state is ConfigEntryState.MIGRATION_ERROR:
                    await self.hass.config_entries.async_retry_migration(entry.entry_id)
                else:
                    from .sip_account_identity import async_migrate_account_identities
                    await async_migrate_account_identities(self.hass, entry)
                    if entry.state is ConfigEntryState.LOADED:
                        self.hass.config_entries.async_schedule_reload(entry.entry_id)
            except (HomeAssistantError, ValueError, TypeError):
                errors['base'] = 'migration_retry_failed'
            else:
                if self.hardware not in entry.data.get('managed_esphome_phones', {}):
                    return self.async_create_entry(data={})
                errors['base'] = 'migration_retry_failed'
        return self.async_show_form(step_id='account', errors=errors, data_schema=vol.Schema({
            vol.Required('device_id', default=values['device_id']): DeviceSelector({'integration': 'esphome'}),
            vol.Required('name', default=values['name']): TextSelector(),
            vol.Required('username', default=values['username']): TextSelector(),
            vol.Optional('password'): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            vol.Optional('queues', default=values['queues']): SelectSelector({'options': [], 'multiple': True, 'custom_value': True}),
            vol.Optional('ring_group', default=values['ring_group']): SelectSelector({'options': [], 'multiple': True, 'custom_value': True}),
            vol.Optional('extension', default=values['extension']): TextSelector(),
            vol.Optional('conference_group', default=values['conference_group']): SelectSelector({'options': [], 'multiple': True, 'custom_value': True}),
            vol.Optional('conference_ring', default=values['conference_ring']): BooleanSelector(),
        }))


def async_sync_managed_record_issues(hass, entry, invalid):
    """Retain each damaged payload and expose its existing native object for repair."""
    prefix = 'managed_record_' + entry.entry_id + '_'
    registry = ir.async_get(hass)
    from homeassistant.helpers import device_registry as dr
    for domain, issue_id in tuple(registry.issues):
        if domain == DOMAIN and issue_id.startswith(prefix) and issue_id[len(prefix):] not in invalid:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
    for hardware in invalid:
        device = dr.async_get(hass).async_get(hardware)
        ir.async_create_issue(hass, DOMAIN, prefix + hardware, config_entry_id=entry.entry_id,
            is_fixable=True, is_persistent=False,
            severity=ir.IssueSeverity.ERROR, translation_key='managed_record_invalid',
            translation_placeholders={'phone': (device.name_by_user or device.name or 'Saved phone account')
                if device is not None else 'Saved phone account'},
            data={'entry_id': entry.entry_id, 'hardware': hardware,
                  **({'membership_reference': invalid[hardware]['membership_reference']}
                     if isinstance(invalid[hardware], dict) and 'membership_reference' in invalid[hardware] else {})})


def async_sync_esphome_account_setup_issue(hass, entry, device_id, *, pending):
    """Preserve a migrated account while its firmware cannot register it yet."""
    issue_id = 'esphome_account_setup_' + entry.entry_id + '_' + device_id
    if not pending:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    from homeassistant.helpers import device_registry as dr
    device = dr.async_get(hass).async_get(device_id)
    ir.async_create_issue(hass, DOMAIN, issue_id, config_entry_id=entry.entry_id, is_fixable=False, is_persistent=False,
        severity=ir.IssueSeverity.ERROR, translation_key='esphome_account_setup_required',
        translation_placeholders={'phone': device.name if device is not None and device.name else 'ESPHome phone'},
        learn_more_url='/config/devices/device/' + device_id)


class ArchiveRecordRepairFlow(RepairsFlow):
    """Retry one restored recording with an explicitly selected native account."""

    def __init__(self, entry_id, filename):
        self.entry_id, self.filename = entry_id, filename

    async def async_step_init(self, user_input=None):
        return await self.async_step_recording()

    async def async_step_recording(self, user_input=None):
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if entry is None:
            return self.async_create_entry(data={})
        await async_sync_archive_record_issues(self.hass, entry, {})
        issue_id = 'archive_record_' + entry.entry_id + '_' + self.filename
        if ir.async_get(self.hass).async_get_issue(DOMAIN, issue_id) is None:
            return self.async_create_entry(data={})
        errors = {}
        if user_input is not None:
            from .core.media_archive import ArchiveError, MediaArchive
            from .mailbox import archive_path
            from .sip_account_identity import ha_account_username
            try:
                username = ha_account_username(self.hass, user_input['account'])
                path = archive_path(self.hass)
                if not username or path is None:
                    raise ValueError('Select an existing account and configured media archive')
                runtime = getattr(entry, 'runtime_data', None)
                owner = getattr(runtime, 'sip', None)
                archive = owner.component('media_archive') if owner is not None else None
                archive = archive or MediaArchive(path, self.hass.async_add_executor_job)
                identity = self.filename.removesuffix('.json').removesuffix('.pending')
                scan = await archive.prepare_account_rebind({}, load_from_storage=True, record_ids={identity})
                if self.filename in scan.faults:
                    raise ArchiveError('Restore the recording metadata and WAV segments before retrying')
                prepared = await archive.prepare_account_rebind({scan.recording_mailbox(identity): username},
                    load_from_storage=True, record_ids={identity})
                if self.filename in prepared.faults:
                    raise ArchiveError('Complete the interrupted recording recovery before retrying')
                await prepared.commit()
                ir.async_delete_issue(self.hass, DOMAIN, 'archive_record_' + entry.entry_id + '_' + self.filename)
                await async_sync_archive_record_issues(self.hass, entry, prepared.faults)
            except (ArchiveError, HomeAssistantError, OSError, ValueError, TypeError):
                errors['base'] = 'recording_recovery_failed'
            else:
                return self.async_create_entry(data={})
        return self.async_show_form(step_id='recording', errors=errors,
            description_placeholders={'filename': self.filename}, data_schema=vol.Schema({
                vol.Required('account'): DeviceSelector({'filter': [{'integration': DOMAIN, 'model': 'SIP account'}]}),
            }))


async def async_sync_archive_record_issues(hass, entry, faults):
    """Report retained archive files without changing their payload or ownership."""
    prefix = 'archive_record_' + entry.entry_id + '_'
    registry = ir.async_get(hass)
    from .mailbox import archive_path
    root = archive_path(hass)
    filenames = {issue_id[len(prefix):] for domain, issue_id in registry.issues
                 if domain == DOMAIN and issue_id.startswith(prefix)} | set(faults)
    if root is not None and filenames:
        # A repaired but still present record needs explicit confirmation. Only
        # an actual missing file closes its stale issue and quarantine entry.
        def missing_files():
            try:
                if not root.is_dir():
                    return set()
            except OSError:
                return set()
            missing = set()
            for filename in filenames:
                if Path(filename).name != filename:
                    continue
                try:
                    (root / filename).lstat()
                except FileNotFoundError:
                    missing.add(filename)
                except OSError:
                    continue
            try:
                return missing if root.is_dir() else set()
            except OSError:
                return set()
        missing = await hass.async_add_executor_job(missing_files)
        for filename in missing:
            ir.async_delete_issue(hass, DOMAIN, prefix + filename)
        faults = {filename: reason for filename, reason in faults.items() if filename not in missing}
    for filename in faults:
        ir.async_create_issue(hass, DOMAIN, prefix + filename, config_entry_id=entry.entry_id,
            is_fixable=True, is_persistent=True,
            severity=ir.IssueSeverity.ERROR, translation_key='archive_record_invalid',
            translation_placeholders={'filename': filename}, data={'entry_id': entry.entry_id, 'filename': filename})
    retained = {issue_id[len(prefix):].removesuffix('.json').removesuffix('.pending')
                for domain, issue_id in registry.issues if domain == DOMAIN and issue_id.startswith(prefix)}
    runtime = getattr(entry, 'runtime_data', None)
    owner = getattr(runtime, 'sip', None)
    archive = owner.component('media_archive') if owner is not None else None
    if archive is not None:
        archive.quarantine_records(retained)


class LdapRecordRepairFlow(ConfirmRepairFlow):
    """Keep invalid configured directory rows visible until their source is corrected."""

    def __init__(self, entry_id):
        self.entry_id = entry_id

    async def async_step_confirm(self, user_input=None):
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if entry is None:
            return self.async_create_entry(data={})
        if user_input is not None:
            server = getattr(getattr(entry, 'runtime_data', None), 'ldap_server', None)
            if server is not None and not server.validate_records():
                async_sync_ldap_record_issue(self.hass, entry, {})
                return self.async_create_entry(data={})
        result = await super().async_step_confirm()
        if user_input is not None:
            result['errors'] = {'base': 'directory_record_invalid'}
        return result


def async_sync_ldap_record_issue(hass, entry, faults):
    """Publish one actionable configuration fault per directory generation."""
    issue_id = 'ldap_record_invalid_' + entry.entry_id
    if not faults:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    names = ', '.join(str(name)[:160] for name in list(faults.values())[:5])
    ir.async_create_issue(hass, DOMAIN, issue_id, config_entry_id=entry.entry_id, is_fixable=True, is_persistent=False,
        severity=ir.IssueSeverity.ERROR, translation_key='directory_record_invalid',
        translation_placeholders={'objects': names, 'count': str(len(faults))},
        learn_more_url='/config/integrations/integration/' + DOMAIN,
        data={'entry_id': entry.entry_id})


def async_sync_esphome_account_name_issue(hass, device_id, *, name="", error=None) -> None:
    """Clear the enrollment issue once this ESP can supply its SIP username."""
    issue_id = f"esphome_account_name_{device_id}"
    if error is None:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    placeholders = {"phone": str(name)}
    if error.value is not None:
        placeholders["username"] = error.value
    ir.async_create_issue(hass, DOMAIN, issue_id, is_fixable=False, is_persistent=False,
        severity=ir.IssueSeverity.WARNING, translation_key=error.issue, translation_placeholders=placeholders,
        learn_more_url=f"/config/devices/device/{device_id}")


def async_sync_esphome_endpoint_collision_issue(hass, device_id, *, name="", error=None) -> None:
    """Expose one rejected native identity without rewriting either owner."""
    issue_id = f"{ESPHOME_ENDPOINT_COLLISION_ISSUE_PREFIX}{device_id}"
    if error is None:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(hass, DOMAIN, issue_id, is_fixable=False,
        is_persistent=False, severity=ir.IssueSeverity.WARNING,
        translation_key="esphome_endpoint_collision",
        translation_placeholders={"phone": str(name), "field": error.field, "value": error.value},
        learn_more_url=f"/config/devices/device/{device_id}")


def async_sync_phone_identity_issue(hass, entry, error=None) -> None:
    """Expose an inconsistent identity without guessing or rewriting either ID."""
    issue_id = "phone_identity_conflict_" + entry.entry_id
    if error is None:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ownership = {"config_entry_id": entry.entry_id}
    subentry_id = str(getattr(error, "subentry_id", "") or "")
    if subentry_id in entry.subentries and "config_subentry_id" in signature(ir.async_create_issue).parameters:
        ownership["config_subentry_id"] = subentry_id
    ir.async_create_issue(hass, DOMAIN, issue_id, **ownership, is_fixable=True,
        is_persistent=False, severity=ir.IssueSeverity.ERROR,
        translation_key="phone_identity_conflict", translation_placeholders={
            "phone": str(getattr(error, "phone", entry.title)),
            "canonical": str(getattr(error, "canonical", "")),
            "legacy": str(getattr(error, "legacy", "")),
        }, data={"entry_id": entry.entry_id})


def async_sync_account_route_issue(hass, entry, error=None) -> None:
    """Identify the routing conflict that blocks the account upgrade."""
    issue_id = "account_route_conflict_" + entry.entry_id
    if error is None:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(hass, DOMAIN, issue_id, config_entry_id=entry.entry_id,
        is_fixable=True, is_persistent=False, severity=ir.IssueSeverity.ERROR,
        translation_key="account_route_conflict",
        translation_placeholders={"phone": error.phone, "route": error.route},
        data={"entry_id": entry.entry_id})


def _migration_error_origin(error) -> str:
    """Name code locations, never exception values which may contain credentials."""
    pending, origins = [error], []
    while pending and len(origins) < 4:
        current = pending.pop(0)
        if isinstance(current, BaseExceptionGroup):
            pending[0:0] = current.exceptions[:4]
            continue
        trace = current.__traceback__
        if trace is None:
            origins.append(type(current).__name__)
            continue
        while trace.tb_next is not None:
            trace = trace.tb_next
        code = trace.tb_frame.f_code
        origins.append(f"{type(current).__name__} at {Path(code.co_filename).name}:{trace.tb_lineno} ({code.co_name})")
    return "; ".join(origins)


def async_sync_account_migration_issue(hass, entry, error=None) -> None:
    """Keep storage/programming failures separate from proven identity conflicts."""
    issue_id = "account_migration_failed_" + entry.entry_id
    if error is None:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(hass, DOMAIN, issue_id, config_entry_id=entry.entry_id,
        is_fixable=True, is_persistent=False, severity=ir.IssueSeverity.ERROR,
        translation_key="account_migration_failed", translation_placeholders={
            "error_type": type(error).__name__, "source": _migration_error_origin(error),
        }, data={"entry_id": entry.entry_id})


def async_report_account_migration_failure(hass, entry, error) -> None:
    """Publish the matching repair and bounded, non-secret diagnostic fields."""
    from .phone_config import PhoneIdentityConflict, PhoneRouteConflict

    identity = isinstance(error, PhoneIdentityConflict)
    route = isinstance(error, PhoneRouteConflict)
    async_sync_phone_identity_issue(hass, entry, error if identity else None)
    async_sync_account_route_issue(hass, entry, error if route else None)
    async_sync_account_migration_issue(hass, entry, None if identity or route else error)
    if identity:
        _LOGGER.error("Cannot migrate SIP account identities entry=%s: phone=%r native=%r previous=%r subentry=%r",
            entry.entry_id, error.phone, error.canonical, error.legacy, error.subentry_id)
    elif route:
        _LOGGER.error("Cannot migrate SIP account routes entry=%s: account=%r route=%r",
            entry.entry_id, error.phone, error.route)
    else:
        _LOGGER.error("Cannot migrate SIP accounts entry=%s: %s; no identity conflict was established",
            entry.entry_id, _migration_error_origin(error))


def async_sync_runtime_issues(hass: HomeAssistant) -> None:
    """Expose private media capture while enabled and clear stale warnings."""

    async_sync_pbx_configuration_issues(hass)
    runtime = runtime_data(hass)
    if runtime is not None and runtime.media_capture:
        ir.async_create_issue(
            hass,
            DOMAIN,
            MEDIA_CAPTURE_ISSUE_ID,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=MEDIA_CAPTURE_ISSUE_ID,
        )
        return
    ir.async_delete_issue(hass, DOMAIN, MEDIA_CAPTURE_ISSUE_ID)


def async_sync_esphome_action_issue(hass: HomeAssistant, device: dict) -> None:
    """Report an incomplete ESP phone control surface without changing it."""

    device_id = str(device.get("device_id") or "").strip()
    if not device_id:
        return
    issue_id = f"{ESPHOME_ACTION_ISSUE_PREFIX}{device_id}"
    from homeassistant.helpers import device_registry as dr
    from .esphome_native import device_info, normalize_mac, runtime_for_entry, verified_action_names
    hardware = dr.async_get(hass).async_get(device_id)
    if hardware is None:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    origin = hass.config_entries.async_get_entry(str(hardware.config_entry_id or ""))
    native = runtime_for_entry(origin)
    info = device_info(native)
    if info is None or origin is None:
        return
    try:
        mac = normalize_mac(info.mac_address)
        owns_hardware = any(kind == dr.CONNECTION_NETWORK_MAC and normalize_mac(value) == mac
                            for kind, value in hardware.connections)
    except ValueError:
        return
    if not owns_hardware or verified_action_names(origin, mac) is None:
        return
    services = {service.name: service for service in native.services.values()}
    missing = tuple(name for name, expected in _PHONE_ACTIONS.items()
        if name not in services or tuple((arg.name, int(arg.type)) for arg in services[name].args) != expected)
    if not missing:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="esphome_actions_incomplete",
        translation_placeholders={
            "phone": str(device.get("name") or "ESPHome phone"),
            "actions": ", ".join(missing),
        },
    )


def async_sync_trunk_default_issue(hass, configs, default_id) -> None:
    """Make a missing global line choice actionable without provider failover."""
    if configs and default_id not in configs:
        ir.async_create_issue(hass, DOMAIN, 'default_trunk_missing', is_fixable=False,
            is_persistent=False, severity=ir.IssueSeverity.WARNING,
            translation_key='default_trunk_missing')
    else:
        ir.async_delete_issue(hass, DOMAIN, 'default_trunk_missing')


def async_sync_extension_issues(hass: HomeAssistant, entries) -> None:
    """Report conflicting extensions in the complete discovered phonebook."""
    from .roster import duplicate_numeric_extensions

    async_sync_pbx_configuration_issues(hass)
    conflicts = duplicate_numeric_extensions(entries)
    issue_id = "duplicate_extensions"
    if not conflicts:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass, DOMAIN, issue_id, is_fixable=False, is_persistent=False,
        severity=ir.IssueSeverity.ERROR, translation_key=issue_id,
        translation_placeholders={"conflicts": "; ".join(
            f"{extension}: {', '.join(names)}"
            for extension, names in sorted(conflicts.items())
        )},
    )


def async_sync_pbx_configuration_issues(hass: HomeAssistant) -> None:
    """Leave existing routes intact while exposing application prerequisites."""
    from .parking_config import parking_settings
    from .voicemail_config import mailbox_routes_available
    from .store import config_entry

    if config_entry(hass) is None:
        return
    parking = parking_settings(hass)
    for issue_id, failed in (
        ("parking_configuration", not parking.get("enabled")),
        ("mailbox_route_conflict", not mailbox_routes_available(hass)),
    ):
        if failed:
            ir.async_create_issue(hass, DOMAIN, issue_id, is_fixable=False,
                is_persistent=False, severity=ir.IssueSeverity.ERROR,
                translation_key=issue_id)
        else:
            ir.async_delete_issue(hass, DOMAIN, issue_id)



def async_report_trunk_menu_retired(hass, entry, record) -> None:
    """Keep a native migration notice until the owner dismisses it."""
    ownership = {"config_entry_id": entry.entry_id}
    if "config_subentry_id" in signature(ir.async_create_issue).parameters:
        ownership["config_subentry_id"] = record.subentry_id
    ir.async_create_issue(hass, DOMAIN, "trunk_dtmf_retired_" + record.subentry_id,
        **ownership, is_fixable=False, is_persistent=True, severity=ir.IssueSeverity.WARNING,
        translation_key="trunk_dtmf_retired", translation_placeholders={
            "trunk": record.title,
            "destination": str(record.data.get("trunk_inbound_default_target") or "No destination configured"),
        }, learn_more_url="https://github.com/n-IA-hane/esphome-intercom/blob/dev/docs/AUTOMATION_DIALPLAN.md#build-a-small-keypad-menu")
