"""Read saved Lovelace declarations for the persistent HA client index.

This compatibility boundary uses HA 2026.11's LovelaceData dashboard exports.
HaCardBindings owns persistence and endpoint associations. Reading saved card
configuration also covers hidden views and browsers that are not connected.
"""

from __future__ import annotations

import logging

from homeassistant.components.lovelace.const import LOVELACE_DATA, ConfigNotFound
from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(f"{__package__}.ha_bindings")
_CARD_TYPES = {"custom:voip-stack-card", "custom:voip-stack-dashboard-card"}


def configured_cards(value, path=""):
    """Read declarations even when their containing view/card is not rendered."""
    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from configured_cards(item, f"{path}/{index}")
    elif isinstance(value, dict):
        strategy = value.get("strategy")
        if isinstance(strategy, dict) and strategy.get("type") == "custom:voip-stack":
            ids = strategy.get("account_ids", ())
            if isinstance(ids, list):
                for index, identity in enumerate(ids):
                    if isinstance(identity, str) and identity:
                        yield {
                            "reference": f"{path}/strategy/account_ids/{index}",
                            "device_id": "",
                            "endpoint_id": identity,
                        }
        if value.get("type") in _CARD_TYPES:
            if (
                value["type"] == "custom:voip-stack-dashboard-card"
                or value.get("mode", value.get("card_mode", "esp_mirror"))
                == "ha_softphone"
            ):
                yield {
                    "reference": path,
                    "device_id": str(
                        value.get("device_id") or value.get("entity_id") or ""
                    ),
                    "endpoint_id": str(value.get("endpoint_id") or ""),
                }
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                yield from configured_cards(item, f"{path}/{key}")


async def async_dashboard_declarations(hass, previous, *, force):
    """Return saved declarations, retaining each unavailable export's last result.

    ``None`` means Lovelace has not exposed a dashboard map yet. The caller
    retains its current index and owns all subsequent persistence and events.
    """
    data = hass.data.get(LOVELACE_DATA)
    dashboards = getattr(data, "dashboards", None)
    if not isinstance(dashboards, dict):
        return None
    # A bootstrap map can be partial. Only a loaded integration can authoritatively
    # remove declarations for dashboards that no longer exist.
    current = {} if "lovelace" in hass.config.components else dict(previous)
    for url, export in tuple(dashboards.items()):
        key = str(url or "default")
        try:
            try:
                config = await export.async_load(force)
            except ConfigNotFound:
                if (
                    hass.config.recovery_mode
                    or (await export.async_get_info()).get("mode") != "auto-gen"
                ):
                    raise
                config = {}
            if not isinstance(config, dict):
                raise ValueError("invalid_dashboard_export")
            current[key] = list(configured_cards(config))
        except (HomeAssistantError, OSError, ValueError, TypeError, RuntimeError):
            if key in previous:
                current[key] = previous[key]
            _LOGGER.warning("VoIP dashboard association export unavailable: %s", key)
            _LOGGER.debug("Dashboard association export failed: %s", key, exc_info=True)
    return current
