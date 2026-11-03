"""Frontend registration for VoIP Stack. Auto-serves the Lovelace card."""

import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.panel_custom import async_register_panel
from homeassistant.components.frontend import (
    DATA_EXTRA_MODULE_URL,
    add_extra_js_url,
    async_panel_exists,
    async_remove_panel,
    remove_extra_js_url,
)
from homeassistant.core import HomeAssistant

from ..const import URL_BASE, INTEGRATION_VERSION

_LOGGER = logging.getLogger(__name__)

CARD_JS = "voip-stack-card.js"
GLOBAL_JS = "voip-stack-popup.js"
FRONTEND_DIR = Path(__file__).parent


def _frontend_asset_stamp() -> int:
    """Return the newest frontend JS mtime for Lovelace cache busting."""
    return max(
        (int(path.stat().st_mtime) for path in FRONTEND_DIR.glob("*.js")),
        default=0,
    )


class JSModuleRegistration:
    """Registers the Lovelace card JS module in Home Assistant."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.lovelace = hass.data.get("lovelace")

    async def async_register(self) -> None:
        """Register static path and Lovelace resource."""
        await self._async_register_path()
        modules = self.hass.data.get(DATA_EXTRA_MODULE_URL)
        asset_stamp = await self.hass.async_add_executor_job(_frontend_asset_stamp)
        popup_url = f"{URL_BASE}/{GLOBAL_JS}"
        if modules is not None:
            menu_url = f"{URL_BASE}/voip-stack-menu.js"
            for registered in tuple(modules.urls):
                if registered.split("?")[0] in {menu_url, popup_url}:
                    remove_extra_js_url(self.hass, registered)
        # Load before any dashboard card is mounted, including on other panels.
        add_extra_js_url(
            self.hass, f"{popup_url}?v={INTEGRATION_VERSION}-{asset_stamp}"
        )
        if async_panel_exists(self.hass, "voip-stack-popup"):
            async_remove_panel(self.hass, "voip-stack-popup")
        await async_register_panel(
            self.hass,
            frontend_url_path="voip-stack-popup",
            webcomponent_name="voip-stack-popup-settings",
            module_url=f"{popup_url}?v={INTEGRATION_VERSION}-{asset_stamp}",
            embed_iframe=False,
        )

        if self.lovelace is None:
            _LOGGER.warning("Lovelace data not available, skipping resource registration")
            return

        # HA 2026.2+ renamed .mode → .resource_mode
        mode = getattr(self.lovelace, "resource_mode",
                       getattr(self.lovelace, "mode", None))

        if mode == "storage":
            await self._async_register_resource()
        else:
            _LOGGER.debug("Lovelace mode is %s, skipping auto-registration", mode)

    async def _async_register_path(self) -> None:
        """Register the static HTTP path for frontend files."""
        try:
            await self.hass.http.async_register_static_paths(
                [StaticPathConfig(URL_BASE, str(FRONTEND_DIR), True)]
            )
            _LOGGER.debug("Registered static path: %s -> %s", URL_BASE, FRONTEND_DIR)
        except RuntimeError:
            _LOGGER.debug("Static path already registered: %s", URL_BASE)

    async def _async_register_resource(self) -> None:
        """Register or update the Lovelace resource for the card."""
        resources = self.lovelace.resources

        # Force load from storage if not loaded yet
        await resources.async_get_info()

        url = f"{URL_BASE}/{CARD_JS}"
        asset_stamp = await self.hass.async_add_executor_job(_frontend_asset_stamp)
        url_versioned = f"{url}?v={INTEGRATION_VERSION}-{asset_stamp}"

        for item in resources.async_items():
            item_url = item.get("url", "")
            if item_url.split("?")[0] == url:
                # Already registered; update version if needed
                if item_url != url_versioned:
                    _LOGGER.info("Updating VoIP Stack card to v%s", INTEGRATION_VERSION)
                    await resources.async_update_item(
                        item["id"], {"res_type": "module", "url": url_versioned}
                    )
                else:
                    _LOGGER.debug("VoIP Stack card v%s already registered", INTEGRATION_VERSION)
                return

        # Not registered yet, create
        _LOGGER.info("Registering VoIP Stack card v%s", INTEGRATION_VERSION)
        await resources.async_create_item({"res_type": "module", "url": url_versioned})
