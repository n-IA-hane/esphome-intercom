"""Register the card-independent module and hidden panel with real HA APIs."""

import importlib
from pathlib import Path
import sys
import types
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

pytestmark = pytest.mark.ha
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("existing_manager", [False, True])
def test_isolated_frontend_bootstrap_preserves_the_module_owner(hass, existing_manager):
    from homeassistant.components import frontend

    from .support.ha_frontend import prepare_frontend_dependency

    changed = Mock()
    if existing_manager:
        hass.data[frontend.DATA_EXTRA_MODULE_URL] = frontend.UrlManager(
            changed, ["/unrelated.js"]
        )
    prepare_frontend_dependency(hass)
    modules = hass.data[frontend.DATA_EXTRA_MODULE_URL]
    modules.add("/retained.js")
    prepare_frontend_dependency(hass)

    assert hass.data[frontend.DATA_EXTRA_MODULE_URL] is modules
    assert "/retained.js" in modules.urls
    assert "frontend" in hass.config.components
    if existing_manager:
        assert "/unrelated.js" in modules.urls
        changed.assert_called_once_with("added", "/retained.js")


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


async def test_shared_setup_reload_refreshes_assets_without_recreating_services(hass, monkeypatch):
    """Exercise the real initialized branch and registrar, isolating non-frontend I/O."""
    from homeassistant.components import frontend
    from homeassistant.core import CoreState
    import custom_components

    monkeypatch.setattr(custom_components, "__path__", [
        *custom_components.__path__, str(ROOT / "custom_components"),
    ])
    from custom_components import voip_stack as integration
    from custom_components.voip_stack import audio_ws_view, mailbox_api, sip_capture_service
    from custom_components.voip_stack import frontend as assets
    from .support.ha_frontend import prepare_frontend_dependency

    prepare_frontend_dependency(hass)
    hass.set_state(CoreState.running)
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    items = [{"id": "unrelated", "url": "/unrelated-card.js"}]

    async def create_item(data):
        item = {"id": "voip-card", **data}
        items.append(item)
        return item

    async def update_item(item_id, data):
        item = next(item for item in items if item["id"] == item_id)
        item.update(data)
        return item

    resources = types.SimpleNamespace(async_get_info=AsyncMock(), async_items=lambda: items,
        async_create_item=AsyncMock(side_effect=create_item), async_update_item=AsyncMock(side_effect=update_item))
    hass.data["lovelace"] = types.SimpleNamespace(resource_mode="storage", resources=resources)
    services, websocket, bridge, phonebook = AsyncMock(), Mock(), Mock(), Mock()
    monkeypatch.setattr(integration, "_async_register_services", services)
    monkeypatch.setattr(integration, "async_register_websocket_api", websocket)
    monkeypatch.setattr(integration, "_register_esp_state_event_bridge", bridge)
    monkeypatch.setattr(integration, "_register_phonebook_service_event_sync", phonebook)
    monkeypatch.setattr(integration, "_get_transport_config", lambda _: {})
    monkeypatch.setattr(audio_ws_view, "async_register_audio_ws_view", Mock())
    monkeypatch.setattr(mailbox_api, "async_register_mailbox_api", Mock())
    capture = AsyncMock()
    monkeypatch.setattr(sip_capture_service, "async_register_sip_capture", capture)
    stamp = [100]
    monkeypatch.setattr(assets, "_frontend_asset_stamp", lambda: stamp[0])
    observed = AsyncMock()
    original_register = assets.JSModuleRegistration.async_register

    async def register(registration):
        await observed(stamp[0])
        await original_register(registration)

    monkeypatch.setattr(assets.JSModuleRegistration, "async_register", register)
    await integration._async_setup_shared(hass)
    observed.assert_awaited_once_with(100)
    registration = integration.registration_data(hass)
    modules = hass.data[frontend.DATA_EXTRA_MODULE_URL]
    modules.add("/unrelated.js")
    stamp[0] = 200
    await integration._async_setup_shared(hass)

    assert observed.await_count == 2
    observed.assert_awaited_with(200)
    assert integration.registration_data(hass) is registration and registration.initialized
    services.assert_awaited_once()
    websocket.assert_called_once()
    capture.assert_awaited_once()
    assert bridge.call_count == phonebook.call_count == 2
    assert hass.data[frontend.DATA_EXTRA_MODULE_URL] is modules
    urls = [url for url in modules.urls if url.split("?")[0].endswith("/voip-stack-popup.js")]
    assert len(urls) == 1 and urls[0].endswith("-200")
    assert "/unrelated.js" in modules.urls
    panel = hass.data[frontend.DATA_PANELS]["voip-stack-popup"]
    assert panel.config["_panel_custom"]["module_url"] == urls[0]
    cards = [item for item in items if item["url"].split("?")[0].endswith("/voip-stack-card.js")]
    assert len(cards) == 1 and cards[0]["url"].endswith("-200")
    assert items[0] == {"id": "unrelated", "url": "/unrelated-card.js"}
    resources.async_create_item.assert_awaited_once()
    resources.async_update_item.assert_awaited_once()
