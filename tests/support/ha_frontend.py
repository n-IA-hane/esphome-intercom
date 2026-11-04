"""Frontend dependency surfaces for isolated Home Assistant test bootstraps."""

from homeassistant.components import frontend
from homeassistant.core import HomeAssistant


def prepare_frontend_dependency(hass: HomeAssistant) -> None:
    """Model a ready frontend owner without starting its unrelated services.

    Only isolated service/flow bootstraps use this helper. The real frontend
    dependency-order test must initialize its owner through HA setup instead.
    """
    if frontend.DATA_EXTRA_MODULE_URL not in hass.data:
        hass.data[frontend.DATA_EXTRA_MODULE_URL] = frontend.UrlManager(
            lambda *_: None, []
        )
    hass.config.components.add("frontend")
