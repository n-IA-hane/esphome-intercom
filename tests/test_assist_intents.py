"""Behavioral tests for VoIP commands issued through Home Assistant Assist."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "assist_intents.py"
PACKAGE = "voip_stack_assist_intents_test"


class _Context:
    pass


class _IntentResponse:
    def __init__(self) -> None:
        self.speech = ""

    def async_set_speech(self, speech: str) -> None:
        self.speech = speech


class _IntentHandler:
    pass


class _Services:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict, dict]] = []

    async def async_call(self, domain: str, service: str, data: dict, **kwargs) -> None:
        self.calls.append((domain, service, dict(data), dict(kwargs)))


class _Intent:
    def __init__(self, hass, *, device_id: str = "esp-device", target: str = "") -> None:
        self.hass = hass
        self.device_id = device_id
        self.slots = {"target": {"value": target}} if target else {}
        self.context = _Context()

    def create_response(self) -> _IntentResponse:
        return _IntentResponse()


def _load_assist_intents(monkeypatch):
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(MODULE.parent)]
    monkeypatch.setitem(sys.modules, PACKAGE, package)

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    helpers = types.ModuleType("homeassistant.helpers")
    helpers.__path__ = []
    core = types.ModuleType("homeassistant.core")
    core.Context = _Context
    core.HomeAssistant = object
    config_validation = types.ModuleType("homeassistant.helpers.config_validation")
    config_validation.string = str
    intent = types.ModuleType("homeassistant.helpers.intent")
    intent.Intent = _Intent
    intent.IntentHandler = _IntentHandler
    intent.IntentResponse = _IntentResponse
    intent.async_register = lambda *_args, **_kwargs: None
    intent.async_remove = lambda *_args, **_kwargs: None
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    helpers.config_validation = config_validation
    helpers.intent = intent
    helpers.device_registry = device_registry
    for dependency in (
        homeassistant,
        helpers,
        core,
        config_validation,
        intent,
        device_registry,
    ):
        monkeypatch.setitem(sys.modules, dependency.__name__, dependency)

    const = types.ModuleType(f"{PACKAGE}.const")
    const.DOMAIN = "voip_stack"
    monkeypatch.setitem(sys.modules, const.__name__, const)

    runtime = types.ModuleType(f"{PACKAGE}.runtime_data")
    runtime.registration_data = lambda hass: hass.registration
    monkeypatch.setitem(sys.modules, runtime.__name__, runtime)

    name = f"{PACKAGE}.assist_intents"
    spec = importlib.util.spec_from_file_location(name, MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_exact_assist_device_id_selects_the_originating_voip_endpoint(monkeypatch) -> None:
    module = _load_assist_intents(monkeypatch)
    expected = {
        "device_id": "waveshare-device",
        "route_id": "cucina_waveshare_s3_audio",
        "name": "Waveshare S3 Audio",
    }

    async def devices(_hass):
        return [expected, {"device_id": "other-device", "name": "Other"}]

    monkeypatch.setattr(module, "_voip_devices", devices)
    intent_obj = _Intent(
        types.SimpleNamespace(),
        device_id="waveshare-device",
    )

    assert asyncio.run(module._origin_device(intent_obj)) is expected


def test_call_intent_uses_the_origin_device_and_preserves_ha_context(monkeypatch) -> None:
    module = _load_assist_intents(monkeypatch)
    hass = types.SimpleNamespace(services=_Services())
    intent_obj = _Intent(hass, target="Daniele")

    async def origin(_intent):
        return {"device_id": "esp-device", "name": "Kitchen", "route_id": "kitchen"}

    async def resolve(_hass, _target):
        return module.ContactResolution(canonical="Daniele", source="phonebook")

    monkeypatch.setattr(module, "_origin_device", origin)
    monkeypatch.setattr(module, "_resolve_contact_or_area", resolve)
    monkeypatch.setattr(module, "_ha_peer_name", lambda _hass: "Home Assistant")

    response = asyncio.run(module.VoipCallIntentHandler().async_handle(intent_obj))

    assert response.speech == "Calling Daniele."
    assert hass.services.calls == [
        (
            "voip_stack",
            "call",
            {"destination": "Daniele", "device_id": "esp-device"},
            {"blocking": True, "context": intent_obj.context},
        )
    ]


def test_calling_ha_peer_uses_the_originating_esphome_action_and_context(
    monkeypatch,
) -> None:
    module = _load_assist_intents(monkeypatch)
    hass = types.SimpleNamespace(services=_Services())
    intent_obj = _Intent(hass, target="Home Assistant")
    origin = {"device_id": "esp-device", "name": "Kitchen", "route_id": "kitchen"}

    async def find_origin(_intent):
        return origin

    async def resolve(_hass, _target):
        return module.ContactResolution(canonical="Home Assistant", source="phonebook")

    runtime = types.ModuleType(f"{PACKAGE}.phonebook_runtime")
    runtime.available_esphome_services = lambda _hass: {"kitchen_start_call"}
    monkeypatch.setitem(sys.modules, runtime.__name__, runtime)
    monkeypatch.setattr(module, "_origin_device", find_origin)
    monkeypatch.setattr(module, "_resolve_contact_or_area", resolve)
    monkeypatch.setattr(module, "_ha_peer_name", lambda _hass: "Home Assistant")

    response = asyncio.run(module.VoipCallIntentHandler().async_handle(intent_obj))

    assert response.speech == "Calling Home Assistant."
    assert hass.services.calls == [
        (
            "esphome",
            "kitchen_start_call",
            {"dest": "Home Assistant"},
            {"blocking": True, "context": intent_obj.context},
        )
    ]


@pytest.mark.parametrize(
    ("handler_name", "service", "expected_data", "speech"),
    [
        ("VoipHangupIntentHandler", "hangup", {"device_id": "esp-device"}, "OK."),
        ("VoipAnswerIntentHandler", "answer", {"device_id": "esp-device"}, "Answering."),
        (
            "VoipDeclineIntentHandler",
            "decline",
            {"device_id": "esp-device", "reason": "declined by voice command"},
            "Declining.",
        ),
    ],
)
def test_call_control_intents_preserve_device_scope_and_ha_context(
    monkeypatch,
    handler_name: str,
    service: str,
    expected_data: dict,
    speech: str,
) -> None:
    module = _load_assist_intents(monkeypatch)
    hass = types.SimpleNamespace(services=_Services())
    intent_obj = _Intent(hass)

    async def origin(_intent):
        return {"device_id": "esp-device", "name": "Kitchen", "route_id": "kitchen"}

    monkeypatch.setattr(module, "_origin_device", origin)

    response = asyncio.run(getattr(module, handler_name)().async_handle(intent_obj))

    assert response.speech == speech
    assert hass.services.calls == [
        (
            "voip_stack",
            service,
            expected_data,
            {"blocking": True, "context": intent_obj.context},
        )
    ]
