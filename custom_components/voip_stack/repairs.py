"""Repair issue lifecycle for actionable VoIP Stack conditions."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .runtime_data import runtime_data


MEDIA_CAPTURE_ISSUE_ID = "media_capture_enabled"
ESPHOME_ACTION_ISSUE_PREFIX = "esphome_actions_"
_PHONE_ACTIONS = ("start_call", "answer_call", "decline_call", "hangup_call")


def async_sync_runtime_issues(hass: HomeAssistant) -> None:
    """Expose private media capture while enabled and clear stale warnings."""

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
