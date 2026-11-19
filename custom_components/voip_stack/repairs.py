"""Repair issue lifecycle for actionable VoIP Stack conditions."""

from __future__ import annotations

from inspect import signature

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .runtime_data import runtime_data


MEDIA_CAPTURE_ISSUE_ID = "media_capture_enabled"
ESPHOME_ACTION_ISSUE_PREFIX = "esphome_actions_"
ESPHOME_ENDPOINT_COLLISION_ISSUE_PREFIX = "esphome_endpoint_collision_"
_PHONE_ACTIONS = ("start_call", "answer_call", "decline_call", "hangup_call")


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
    ir.async_create_issue(hass, DOMAIN, issue_id, **ownership, is_fixable=False,
        is_persistent=False, severity=ir.IssueSeverity.ERROR,
        translation_key="phone_identity_conflict", translation_placeholders={
            "phone": str(getattr(error, "phone", entry.title)),
            "canonical": str(getattr(error, "canonical", "")),
            "legacy": str(getattr(error, "legacy", "")),
        })


def async_sync_account_route_issue(hass, entry, error=None) -> None:
    """Identify the routing conflict that blocks the account upgrade."""
    issue_id = "account_route_conflict_" + entry.entry_id
    if error is None:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(hass, DOMAIN, issue_id, config_entry_id=entry.entry_id,
        is_fixable=False, is_persistent=False, severity=ir.IssueSeverity.ERROR,
        translation_key="account_route_conflict",
        translation_placeholders={"phone": error.phone, "route": error.route})


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
