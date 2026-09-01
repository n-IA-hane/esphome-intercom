"""Repair issue lifecycle for actionable VoIP Stack conditions."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import logging
from typing import Iterable

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, INTEGRATION_VERSION
from .runtime_data import runtime_data
from .roster import RosterEntry, normalize_roster_key


MEDIA_CAPTURE_ISSUE_ID = "media_capture_enabled"
ESPHOME_ACTION_ISSUE_PREFIX = "esphome_actions_"
_PHONE_ACTIONS = ("start_call", "answer_call", "decline_call", "hangup_call")
DUPLICATE_EXTENSION_ISSUE_PREFIX = "duplicate_extension_"
ESPHOME_VERSION_ISSUE_PREFIX = "esphome_version_"

_LOGGER = logging.getLogger(__name__)


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


def async_sync_esphome_version_issue(hass: HomeAssistant, device: dict) -> None:
    """Report a known HA and ESP VoIP Stack version mismatch once."""

    device_id = str(device.get("device_id") or "").strip()
    if not device_id:
        return
    issue_id = f"{ESPHOME_VERSION_ISSUE_PREFIX}{device_id}"
    esp_version = str(device.get("stack_version") or "").strip()
    if not esp_version or esp_version == INTEGRATION_VERSION:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return

    issue_registry = ir.async_get(hass)
    if issue_registry.async_get_issue(DOMAIN, issue_id) is None:
        _LOGGER.warning(
            "VoIP Stack version mismatch for %s: Home Assistant %s, ESP %s",
            str(device.get("name") or device_id),
            INTEGRATION_VERSION,
            esp_version,
        )
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="esphome_version_mismatch",
        translation_placeholders={
            "phone": str(device.get("name") or "ESPHome phone"),
            "ha_version": INTEGRATION_VERSION,
            "esp_version": esp_version,
        },
    )
def async_sync_duplicate_extension_issues(
    hass: HomeAssistant,
    entries: Iterable[RosterEntry],
    previous_issue_ids: set[str],
) -> set[str]:
    """Publish one actionable issue per ambiguous phonebook extension."""

    by_extension: dict[str, list[RosterEntry]] = defaultdict(list)
    for entry in entries:
        extension = normalize_roster_key(entry.extension)
        if extension:
            by_extension[extension].append(entry)

    current_issue_ids: set[str] = set()
    for extension, matches in by_extension.items():
        if len(matches) < 2:
            continue
        suffix = hashlib.sha256(extension.encode()).hexdigest()[:12]
        issue_id = f"{DUPLICATE_EXTENSION_ISSUE_PREFIX}{suffix}"
        current_issue_ids.add(issue_id)
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="duplicate_extension",
            translation_placeholders={
                "extension": extension,
                "phones": ", ".join(
                    sorted(entry.display_name for entry in matches)
                ),
            },
        )

    for issue_id in previous_issue_ids - current_issue_ids:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
    return current_issue_ids
