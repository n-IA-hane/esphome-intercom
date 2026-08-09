"""Translated Home Assistant service errors."""

from __future__ import annotations

from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN


def service_error(
    message: str,
    key: str,
    **placeholders: object,
) -> ServiceValidationError:
    """Return one translated service error with a stable log message."""

    translated = {name: str(value) for name, value in placeholders.items()}
    return ServiceValidationError(
        message,
        translation_domain=DOMAIN,
        translation_key=key,
        **({"translation_placeholders": translated} if translated else {}),
    )
