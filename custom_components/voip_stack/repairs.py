"""Repair issue lifecycle for actionable VoIP Stack conditions."""

from __future__ import annotations

from inspect import signature
import voluptuous as vol

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, TextSelector

from .const import DOMAIN
from .runtime_data import runtime_data


MEDIA_CAPTURE_ISSUE_ID = "media_capture_enabled"
ESPHOME_ACTION_ISSUE_PREFIX = "esphome_actions_"
ESPHOME_ENDPOINT_COLLISION_ISSUE_PREFIX = "esphome_endpoint_collision_"
_PHONE_ACTIONS = ("start_call", "answer_call", "decline_call", "hangup_call")


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
            return await self.async_step_confirm(user_input)
        if user_input is not None:
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
                if entry.state is ConfigEntryState.MIGRATION_ERROR:
                    await self.hass.config_entries.async_retry_migration(self.entry_id)
            except (HomeAssistantError, ValueError, TypeError):
                errors["base"] = "migration_retry_failed"
            else:
                if entry.state is not ConfigEntryState.MIGRATION_ERROR:
                    async_sync_phone_identity_issue(self.hass, entry)
                    async_sync_account_route_issue(self.hass, entry)
                    if entry.state is ConfigEntryState.LOADED:
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
        try:
            await self.hass.config_entries.async_retry_migration(self.entry_id)
        except HomeAssistantError:
            pass
        else:
            if entry.state is not ConfigEntryState.MIGRATION_ERROR:
                return self.async_create_entry(data={})
        result = await super().async_step_confirm()
        result["errors"] = {"base": "migration_retry_failed"}
        return result


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data) -> RepairsFlow:
    """Create the native retry flow only for an account migration conflict."""
    if issue_id.startswith(("account_route_conflict_", "phone_identity_conflict_")) and data is not None:
        return AccountMigrationRepairFlow(str(data["entry_id"]))
    return ConfirmRepairFlow()


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
    route_id = str(device.get("route_id") or "").strip()
    missing = tuple(
        action
        for action in _PHONE_ACTIONS
        if not route_id
        or not hass.services.has_service("esphome", f"{route_id}_{action}")
    )
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
