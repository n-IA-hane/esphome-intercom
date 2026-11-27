"""Canonical per-phone voicemail settings and their initialization boundary."""

from collections.abc import Mapping
import math

INITIAL_MAILBOX_MAX_SECONDS = 180
MAILBOX_MAX_SECONDS_LIMITS = (1, 3600)


def validate_mailbox_settings(data) -> dict:
    """Validate saved mailbox durations without choosing a runtime value."""
    if not isinstance(data, Mapping):
        raise ValueError("Invalid mailbox configuration")
    result = dict(data)
    if result.get("enabled") or "max_seconds" in result:
        value = result.get("max_seconds")
        minimum, maximum = MAILBOX_MAX_SECONDS_LIMITS
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not minimum <= value <= maximum):
            raise ValueError(f"Mailbox message duration must be between {minimum} and {maximum} seconds")
    return result


def initial_mailbox_settings(data=None) -> dict:
    """Materialize an absent maximum when creating or migrating a mailbox."""
    if data is not None and not isinstance(data, Mapping):
        raise ValueError("Invalid mailbox configuration")
    result = dict(data or {})
    result.setdefault("max_seconds", INITIAL_MAILBOX_MAX_SECONDS)
    return validate_mailbox_settings(result)
