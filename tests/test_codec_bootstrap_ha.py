"""Optional codec discovery runs outside HA's event loop at each cold entry."""

import threading
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant.common import MockConfigEntry

from .test_managed_phones_ha import custom_integration_path as custom_integration_path

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("surface", ["setup", "global", "trunk", "account", "physical"])
@pytest.mark.parametrize("backend", ["installed", "absent"])
async def test_cold_setup_and_codec_forms_do_not_find_native_libraries_in_event_loop(hass, monkeypatch, surface, backend):
    from custom_components import voip_stack
    from custom_components.voip_stack.config_flow import PhoneSubentryFlowHandler, VoipStackConfigFlow
    from custom_components.voip_stack.core import codec_capabilities, g726_codec
    from .test_codec_policy_forms_ha import TrunkEditor

    codec_capabilities.codec_available.cache_clear()
    g726_codec._spandsp.cache_clear()
    loop_thread = threading.get_ident()
    searched = []
    original = g726_codec.find_library

    def tracked_search(name):
        searched.append(threading.get_ident())
        return original(name) if backend == "installed" else None

    monkeypatch.setattr(g726_codec, "find_library", tracked_search)
    try:
        if surface == "setup":
            # Only unrelated HTTP/frontend setup is isolated; the actual codec
            # adapters, native library loader and HA executor remain in use.
            monkeypatch.setattr(voip_stack, "_async_setup_shared", AsyncMock())
            assert await voip_stack.async_setup(hass, {})
        elif surface in {"account", "physical"}:
            from custom_components.voip_stack.physical_phone_flow import PhysicalPhoneSubentryFlowHandler

            entry = MockConfigEntry(domain="voip_stack", data={"initial_phone_created": True,
                "phone_provisioning": {"enabled": True}})
            entry.add_to_hass(hass)
            flow = PhysicalPhoneSubentryFlowHandler() if surface == "physical" else PhoneSubentryFlowHandler()
            flow.hass = hass
            flow._get_entry = lambda: entry
            result = await (flow.async_step_account() if surface == "physical" else flow.async_step_sip_account())
            assert result["step_id"] == ("account" if surface == "physical" else "sip_account")
        else:
            flow = TrunkEditor() if surface == "trunk" else VoipStackConfigFlow()
            flow.hass = hass
            flow._current_entry_data = lambda: (None, {})
            result = await (flow.async_step_user() if surface == "global" else flow.async_step_trunk())
            assert result["type"] == "form"
        assert searched and loop_thread not in searched
        search_count = len(searched)
        # Warm both independent directions even when a missing encoder would
        # short-circuit the old common-codec list. Later SDP work must be cache-only.
        assert codec_capabilities.codec_directions("G726-32") == {
            "send": backend == "installed", "receive": True,
        }
        assert codec_capabilities.codec_directions("G726-40") == {
            "send": backend == "installed", "receive": backend == "installed",
        }
        assert len(searched) == search_count
        if backend == "installed":
            encoder = g726_codec.G726Encoder("G726-32")
            try:
                assert len(encoder.encode(bytes(320))) == 80
            finally:
                encoder.close()
            assert len(searched) == search_count
    finally:
        codec_capabilities.codec_available.cache_clear()
        g726_codec._spandsp.cache_clear()
