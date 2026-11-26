"""Configured inbound trunk destination."""

from .const import CONF_TRUNK_INBOUND_DEFAULT_TARGET


def trunk_default_target(trunk_config: dict) -> str:
    """Read the configured incoming destination without inventing a fallback."""
    return str(trunk_config.get(CONF_TRUNK_INBOUND_DEFAULT_TARGET) or "").strip()
