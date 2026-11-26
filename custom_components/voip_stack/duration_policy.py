"""Read user-owned action durations without inventing runtime defaults."""

from __future__ import annotations

from collections.abc import Mapping
import math

CONF_SERVICE_TIMEOUTS = "service_timeouts"

# Validation limits are constraints, not initial or fallback durations.
SERVICE_TIMEOUT_LIMITS = {
    "contact_step_timeout": (0.1, 3600),
    "media_timeout": (1, 600),
    "dtmf_timeout": (0.1, 300),
    "automation_timeout": (1, 300),
    "announcement_timeout": (1, 600),
    "unanswered_wait": (1, 3600),
    "transcription_timeout": (1, 600),
    "recording_timeout": (1, 3600),
    "ivr_digit_timeout": (0.1, 300),
    "ivr_interdigit_timeout": (0.1, 300),
    "ivr_prompt_timeout": (1, 600),
    "voicemail_menu_setup_timeout": (1, 600),
    "voicemail_greeting_timeout": (1, 600),
    "conference_sip_ringing_timeout": (0, 3600),
}


def validate_duration(value, key: str) -> float:
    """Validate one policy duration; only conference ringing allows zero."""
    minimum, maximum = SERVICE_TIMEOUT_LIMITS[key]
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not minimum <= value <= maximum
        or not math.isfinite(value)
    ):
        raise ValueError(f"{key} must be between {minimum} and {maximum} seconds")
    return float(value)


def configured_service_timeout(config: Mapping, key: str) -> float:
    """Read the single persisted owner; absent configuration is an error."""
    settings = config.get(CONF_SERVICE_TIMEOUTS)
    if not isinstance(settings, Mapping) or key not in settings:
        raise ValueError(f"Missing configured action duration: {key}")
    return validate_duration(settings[key], key)


def resolve_service_timeout(hass, key: str, override=None) -> float:
    """Use an explicit action value or the live PBX setting for omission."""
    from homeassistant.exceptions import ServiceValidationError

    from .store import config_entry

    try:
        if override is not None:
            return validate_duration(override, key)
        entry = config_entry(hass)
        if entry is None:
            raise ValueError("The PBX is not configured")
        return configured_service_timeout(entry.data, key)
    except ValueError as error:
        raise ServiceValidationError(str(error)) from error
