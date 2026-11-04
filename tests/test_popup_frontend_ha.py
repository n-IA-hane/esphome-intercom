"""Register the card-independent module and hidden panel with real HA APIs."""

import importlib
from pathlib import Path
import sys
import types
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

pytestmark = pytest.mark.ha
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("previous_dependency_contract", [False, True])
async def test_public_setup_initializes_frontend_before_popup_registration(
    hass, monkeypatch, tmp_path, enable_custom_integrations, caplog,
    previous_dependency_contract,
):
    """Run the HA dependency graph; only unrelated dependencies/assets are isolated."""
    from homeassistant import loader
    from homeassistant.components import frontend
    from homeassistant.setup import async_setup_component
    import custom_components

    monkeypatch.setattr(custom_components, "__path__", [
        *custom_components.__path__, str(ROOT / "custom_components"),
    ])
    integration = await loader.async_get_integration(hass, "voip_stack")
    frontend_integration = await loader.async_get_integration(hass, "frontend")
    if previous_dependency_contract:
        # HA caches this declared dependency view while constructing Integration.
        # Model the former graph without rewriting the checkout or frontend state.
        monkeypatch.setattr(integration, "dependencies", [
            name for name in integration.dependencies if name != "frontend"
        ])

    # The witness owns no server or sockets. Other component setup and HTTP
    # asset serving are outside the frontend's module-manager lifecycle.
    hass.config.components.update(
        (set(integration.dependencies) | set(frontend_integration.dependencies))
        - {"frontend"}
    )
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    monkeypatch.setattr(frontend, "_frontend_root", lambda _: tmp_path)
    assert "frontend" not in hass.config.components
    assert frontend.DATA_EXTRA_MODULE_URL not in hass.data

    from custom_components.voip_stack.frontend import JSModuleRegistration

    setup_frontend = frontend.async_setup
    register_popup = JSModuleRegistration.async_register
    observed = []

    async def frontend_setup(owner, config):
        assert frontend.DATA_EXTRA_MODULE_URL not in owner.data
        result = await setup_frontend(owner, config)
        assert isinstance(owner.data[frontend.DATA_EXTRA_MODULE_URL], frontend.UrlManager)
        observed.append("frontend_ready")
        return result

    async def popup_registration(registration):
        observed.append("popup_registration")
        await register_popup(registration)

    monkeypatch.setattr(frontend, "async_setup", frontend_setup)
    monkeypatch.setattr(JSModuleRegistration, "async_register", popup_registration)
    loaded = await async_setup_component(hass, "voip_stack", {})
    await hass.async_block_till_done()

    if previous_dependency_contract:
        assert loaded is False
        assert observed == ["popup_registration"]
        assert frontend.DATA_EXTRA_MODULE_URL not in hass.data
        assert any(
            record.exc_info and isinstance(record.exc_info[1], KeyError)
            and record.exc_info[1].args == (frontend.DATA_EXTRA_MODULE_URL,)
            for record in caplog.records
        )
        return

    assert loaded is True
    assert observed == ["frontend_ready", "popup_registration"]
    assert "frontend" in hass.config.components
    modules = hass.data[frontend.DATA_EXTRA_MODULE_URL]
    popup_urls = [url for url in modules.urls if url.split("?")[0].endswith("/voip-stack-popup.js")]
    assert len(popup_urls) == 1
    assert hass.data[frontend.DATA_PANELS]["voip-stack-popup"].config[
        "_panel_custom"
    ]["module_url"] == popup_urls[0]


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
