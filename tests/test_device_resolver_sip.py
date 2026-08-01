#!/usr/bin/env python3
"""Device resolver checks for SIP endpoint discovery."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _install_ha_fakes() -> None:
    ha = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
    if not hasattr(ha, "__path__"):
        ha.__path__ = []
    helpers = sys.modules.setdefault("homeassistant.helpers", types.ModuleType("homeassistant.helpers"))
    if not hasattr(helpers, "__path__"):
        helpers.__path__ = []
    core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
    device_registry = sys.modules.setdefault(
        "homeassistant.helpers.device_registry",
        types.ModuleType("homeassistant.helpers.device_registry"),
    )
    entity_registry = sys.modules.setdefault(
        "homeassistant.helpers.entity_registry",
        types.ModuleType("homeassistant.helpers.entity_registry"),
    )
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    core.ServiceCall = getattr(core, "ServiceCall", type("ServiceCall", (), {}))
    core.callback = getattr(core, "callback", lambda fn: fn)
    device_registry.async_get = getattr(device_registry, "async_get", lambda _hass: None)
    entity_registry.async_get = getattr(entity_registry, "async_get", lambda _hass: None)


def _load_module(name: str):
    _install_ha_fakes()
    if "custom_components" not in sys.modules:
        root_pkg = types.ModuleType("custom_components")
        root_pkg.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = root_pkg
    if PKG_NAME not in sys.modules:
        pkg = types.ModuleType(PKG_NAME)
        pkg.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = pkg
    full_name = f"{PKG_NAME}.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {full_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


device_resolver = _load_module("device_resolver")


class SipEndpointParseTest(unittest.TestCase):
    def test_parses_sip_endpoint_sensor_with_audio_formats(self) -> None:
        endpoint = (
            "Waveshare S3 Audio | 192.168.1.47 | 5060 | 40000 | "
            "full_duplex | 16000:s16le:1:16 | 48000:s16le:1:10;16000:s16le:1:16;16000:s16le:1:32 | sip_tcp"
        )
        parsed = device_resolver.parse_voip_endpoint(endpoint)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["sip_transport"], "tcp")
        self.assertEqual(parsed["host"], "192.168.1.47")
        self.assertEqual(parsed["sip_port"], 5060)
        self.assertEqual(parsed["rtp_port"], 40000)
        self.assertEqual([fmt.wire_token() for fmt in parsed["tx_formats"]], ["16000:s16le:1:16"])
        self.assertEqual(
            [fmt.wire_token() for fmt in parsed["rx_formats"]],
            ["48000:s16le:1:10", "16000:s16le:1:16", "16000:s16le:1:32"],
        )

    def test_parses_optional_extension_from_endpoint_sensor(self) -> None:
        endpoint = (
            "Spotpear | 192.168.1.31 | 5060 | 40000 | "
            "full_duplex | 16000:s16le:1:10 | 48000:s16le:1:10 | sip_udp | 101"
        )
        parsed = device_resolver.parse_voip_endpoint(endpoint)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["extension"], "101")
        self.assertEqual(parsed["extras"], [])
        self.assertEqual(parsed["sip_video_codec"], "")

    def test_parses_forward_compatible_endpoint_extras(self) -> None:
        base = (
            "Spotpear | 192.168.1.31 | 5060 | 40000 | "
            "full_duplex | 16000:s16le:1:10 | 48000:s16le:1:10 | sip_udp"
        )
        for suffix, extension, extras in (
            ("", "", []),
            (" | 101", "101", []),
            (" | 101 | Conference", "101", ["Conference"]),
            (" | 101 | Conference | Casa", "101", ["Conference", "Casa"]),
            (" | 101 | Conference | Casa | future", "101", ["Conference", "Casa", "future"]),
        ):
            parsed = device_resolver.parse_voip_endpoint(base + suffix)
            self.assertIsNotNone(parsed)
            assert parsed is not None
            self.assertEqual(parsed["extension"], extension)
            self.assertEqual(parsed["extras"], extras)
            self.assertEqual(parsed["sip_video_codec"], "")

    def test_parses_compile_time_sip_video_codec_extra(self) -> None:
        base = (
            "P4 | 192.168.1.31 | 5060 | 40000 | "
            "full_duplex | 16000:s16le:1:10 | 48000:s16le:1:10 | sip_tcp | 104"
        )
        for suffix, expected in (
            (" | video=jpeg", "jpeg"),
            (" | video=H264", "h264"),
            (" | video=vp8", ""),
            (" | future=value | video=h264", "h264"),
        ):
            parsed = device_resolver.parse_voip_endpoint(base + suffix)
            self.assertIsNotNone(parsed)
            assert parsed is not None
            self.assertEqual(parsed["sip_video_codec"], expected)

    def test_does_not_parse_group_membership_from_endpoint_extras(self) -> None:
        endpoint = (
            "Spotpear | 192.168.1.31 | 5060 | 40000 | "
            "full_duplex | 16000:s16le:1:10 | 48000:s16le:1:10 | sip_udp | 101 | CG Casa | RG Casa | 1"
        )
        parsed = device_resolver.parse_voip_endpoint(endpoint)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["extras"], ["CG Casa", "RG Casa", "1"])
        self.assertNotIn("conference_group", parsed)
        self.assertNotIn("ring_group", parsed)
        self.assertNotIn("conference_ring", parsed)

    def test_list_devices_reads_group_membership_from_sibling_entities(self) -> None:
        source = (PKG_DIR / "device_resolver.py").read_text(encoding="utf-8")
        list_devices = source[source.index("async def list_devices") : source.index("async def resolve_target")]
        self.assertIn('"conference_group": conference_group', list_devices)
        self.assertIn('"conference_ring": _parse_bool(self._state_value(entities.get("voip_conference_ring")))', list_devices)
        self.assertIn('"ring_group": ring_group', list_devices)
        self.assertIn('extension = self._state_value(entities.get("voip_extension")) or endpoint.get("extension") or ""', list_devices)
        self.assertIn('"extension": extension', list_devices)
        self.assertIn('entities["start_call_service"] = f"esphome.{route_id}_start_call"', list_devices)
        collect_entities = source[source.index("def _collect_entities") : source.index("def _state_value")]
        self.assertIn('"auto_answer"', collect_entities)
        self.assertIn('"dnd"', collect_entities)
        self.assertIn('"voip_extension"', collect_entities)
        self.assertIn('"voip_ring_groups"', collect_entities)
        self.assertIn('"voip_conference_groups"', collect_entities)
        self.assertIn('"voip_ring_on_conference"', collect_entities)
        self.assertIn('eid.startswith("camera.")', collect_entities)

    def test_list_devices_re_reads_live_endpoint_state(self) -> None:
        class FakeEntity:
            def __init__(self, entity_id: str) -> None:
                self.entity_id = entity_id
                self.device_id = "dev-ws3"

        class FakeEntityRegistry:
            entities = {
                "sensor.ws3_voip_endpoint": FakeEntity("sensor.ws3_voip_endpoint"),
                "text.ws3_voip_extension": FakeEntity("text.ws3_voip_extension"),
                "text.ws3_voip_ring_groups": FakeEntity("text.ws3_voip_ring_groups"),
                "text.ws3_voip_conference_groups": FakeEntity("text.ws3_voip_conference_groups"),
                "switch.ws3_voip_ring_on_conference": FakeEntity("switch.ws3_voip_ring_on_conference"),
                "switch.ws3_auto_answer": FakeEntity("switch.ws3_auto_answer"),
                "switch.ws3_do_not_disturb": FakeEntity("switch.ws3_do_not_disturb"),
            }

        class FakeDevice:
            name = "WS3"
            device_id = "dev-ws3"
            identifiers = {("esphome", "waveshare-s3")}
            connections = set()
            config_entries = {"entry-ws3"}

        class FakeDeviceRegistry:
            devices = {"dev-ws3": FakeDevice()}

            def async_get(self, device_id):
                return self.devices.get(device_id)

        class FakeState:
            def __init__(self, state: str) -> None:
                self.state = state

        class FakeStates:
            def __init__(self) -> None:
                self.value = "unknown"

            def get(self, entity_id):
                if entity_id == "sensor.ws3_voip_endpoint":
                    return FakeState(self.value)
                if entity_id == "text.ws3_voip_extension":
                    return FakeState("999")
                if entity_id == "text.ws3_voip_ring_groups":
                    return FakeState("RG Casa")
                if entity_id == "text.ws3_voip_conference_groups":
                    return FakeState("CG Casa")
                if entity_id == "switch.ws3_voip_ring_on_conference":
                    return FakeState("on")
                return None

        class FakeConfigEntry:
            domain = "esphome"
            title = "waveshare-s3"
            data = {"device_name": "waveshare-s3"}

        class FakeConfigEntries:
            def async_entries(self, _domain):
                return [FakeConfigEntry()]

            def async_get_entry(self, _entry_id):
                return FakeConfigEntry()

        class FakeHass:
            def __init__(self) -> None:
                self.states = FakeStates()
                self.config_entries = FakeConfigEntries()

        old_er_async_get = device_resolver.er.async_get
        old_dr_async_get = device_resolver.dr.async_get
        try:
            device_resolver.er.async_get = lambda _hass: FakeEntityRegistry()
            device_resolver.dr.async_get = lambda _hass: FakeDeviceRegistry()
            hass = FakeHass()
            resolver = device_resolver.VoipDeviceResolver(hass)

            import asyncio

            self.assertEqual(asyncio.run(resolver.list_devices()), [])
            hass.states.value = (
                "Waveshare S3 Audio | 192.168.1.47 | 5060 | 40000 | "
                "full_duplex | 16000:s16le:1:10 | 48000:s16le:1:10 | sip_udp"
            )
            devices = asyncio.run(resolver.list_devices())
        finally:
            device_resolver.er.async_get = old_er_async_get
            device_resolver.dr.async_get = old_dr_async_get

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["name"], "Waveshare S3 Audio")
        self.assertEqual(devices[0]["sip_uri_user"], "waveshare-s3")
        self.assertEqual(devices[0]["extension"], "999")
        self.assertEqual(devices[0]["conference_group"], "CG Casa")
        self.assertEqual(devices[0]["ring_group"], "RG Casa")
        self.assertTrue(devices[0]["conference_ring"])
        self.assertEqual(devices[0]["entities"]["auto_answer"], "switch.ws3_auto_answer")
        self.assertEqual(devices[0]["entities"]["dnd"], "switch.ws3_do_not_disturb")
        self.assertEqual(devices[0]["entities"]["start_call_service"], "esphome.waveshare_s3_start_call")
        self.assertEqual(devices[0]["entities"]["voip_conference_ring"], "switch.ws3_voip_ring_on_conference")

    def test_rejects_obsolete_minimal_endpoint_sensor(self) -> None:
        parsed = device_resolver.parse_voip_endpoint("Kitchen|192.168.1.4|5060|40000|sip_udp")
        self.assertIsNone(parsed)

    def test_rejects_malformed_sip_endpoint(self) -> None:
        self.assertIsNone(device_resolver.parse_voip_endpoint("Kitchen|sip|192.168.1.4|5060"))
        self.assertIsNone(device_resolver.parse_voip_endpoint("Kitchen|192.168.1.4|x|40000|sip_udp"))
        self.assertIsNone(device_resolver.parse_voip_endpoint("Kitchen|192.168.1.4|5060|40000|udp"))

    def test_rejects_signaling_only_endpoint_role(self) -> None:
        self.assertIsNone(
            device_resolver.parse_voip_endpoint(
                "Panel|192.168.1.8|5060|40000|control_only|"
                "16000:s16le:1:20|16000:s16le:1:20|sip_udp"
            )
        )


if __name__ == "__main__":
    unittest.main()
