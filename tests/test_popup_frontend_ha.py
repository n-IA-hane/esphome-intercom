"""Register the card-independent module and hidden panel with real HA APIs."""

import importlib
from pathlib import Path
import sys
import types
from unittest.mock import AsyncMock, Mock

import pytest

pytestmark = pytest.mark.ha
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_popup_global_registration_without_lovelace(monkeypatch):
    from homeassistant.components import frontend

    package = types.ModuleType("popup_provider")
    package.__path__ = [str(ROOT / "custom_components/voip_stack")]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    module = importlib.import_module("popup_provider.frontend")
    urls = frontend.UrlManager(lambda *_: None, ["/unrelated.js", f"{module.URL_BASE}/voip-stack-popup.js?v=old"])

    async def executor(function, *args):
        return function(*args)

    hass = types.SimpleNamespace(
        data={frontend.DATA_EXTRA_MODULE_URL: urls},
        bus=types.SimpleNamespace(async_fire=Mock()),
        async_add_executor_job=executor,
    )
    registration = module.JSModuleRegistration(hass)
    registration._async_register_path = AsyncMock()
    await registration.async_register()
    await registration.async_register()
    popup_urls = [url for url in urls.urls if url.split("?")[0].endswith("/voip-stack-popup.js")]
    assert len(popup_urls) == 1
    assert "v=old" not in popup_urls[0]
    assert "/unrelated.js" in urls.urls
    panel = hass.data[frontend.DATA_PANELS]["voip-stack-popup"]
    assert panel.sidebar_title is None
    # Preserve the hub's existing Options button; this panel configures a phone.
    assert panel.config_panel_domain is None
    assert panel.config["_panel_custom"]["name"] == "voip-stack-popup-settings"
    assert panel.config["_panel_custom"]["module_url"] == popup_urls[0]
    assert panel.config["_panel_custom"]["embed_iframe"] is False
