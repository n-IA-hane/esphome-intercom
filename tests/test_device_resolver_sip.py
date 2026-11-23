#!/usr/bin/env python3
"""Device resolver checks for SIP endpoint discovery."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


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
    config_entries = sys.modules.setdefault(
        "homeassistant.config_entries",
        types.ModuleType("homeassistant.config_entries"),
    )
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
    config_entries.ConfigEntry = getattr(
        config_entries,
        "ConfigEntry",
        type(
            "ConfigEntry",
            (),
            {"__class_getitem__": classmethod(lambda cls, _item: cls)},
        ),
    )
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


class NativeEntityRoleTest(unittest.TestCase):
    def test_descriptor_state_has_no_role_even_after_entity_rename(self):
        entity = SimpleNamespace(entity_id="sensor.renamed_descriptor", unique_id="abc-text_sensor-voip_endpoint")
        self.assertEqual(device_resolver.entity_role(entity), "")
        self.assertEqual(device_resolver.VoipDeviceResolver._collect_entities([entity]), {})

    def test_native_controls_camera_and_membership_roles_keep_stable_unique_ids(self):
        specs = [("sensor", "voip_state"), ("text", "voip_extension"),
                 ("text", "voip_ring_groups"), ("text", "voip_queues"),
                 ("text", "voip_conference_groups"), ("switch", "voip_ring_on_conference"),
                 ("switch", "auto_answer"), ("switch", "video_send"),
                 ("switch", "do_not_disturb"), ("camera", "front")]
        entities = [SimpleNamespace(entity_id=domain+".renamed_"+str(i), unique_id="abc-"+token)
                    for i,(domain,token) in enumerate(specs)]
        found = device_resolver.VoipDeviceResolver._collect_entities(entities)
        self.assertEqual(found, dict(zip(("voip_state", "voip_extension", "voip_ring_groups", "voip_queues",
            "voip_conference_groups", "voip_conference_ring", "auto_answer", "video_send", "dnd", "camera"),
            (item.entity_id for item in entities))))

    def test_control_roles_do_not_match_unrelated_entity_domains(self):
        for domain in ("sensor", "binary_sensor", "text"):
            entity = SimpleNamespace(entity_id=domain+".incoming_call", unique_id="abc-start_call")
            self.assertEqual(device_resolver.entity_role(entity), "")


if __name__ == "__main__":
    unittest.main()
