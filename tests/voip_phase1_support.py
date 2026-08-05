#!/usr/bin/env python3
"""Shared loaders and fixtures for SIP/SDP/RTP profile tests."""

from __future__ import annotations

import importlib.util
import asyncio
import contextlib
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"
CORE_MODULES = {
    "audio_format", "audio_pcm", "codec_capabilities", "g711",
    "g722_codec", "opus_codec", "rtp", "sdp", "sip", "sip_auth",
    "sip_dialog", "sip_transaction", "video_rtcp", "video_rtp",
}


def _install_runtime_data_ha_fakes() -> None:
    """Keep pure protocol tests independent from a Home Assistant install."""

    if "homeassistant" in sys.modules:
        return
    package = types.ModuleType("homeassistant")
    package.__path__ = []
    config_entries = types.ModuleType("homeassistant.config_entries")
    core = types.ModuleType("homeassistant.core")

    class ConfigEntry:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

    config_entries.ConfigEntry = ConfigEntry
    core.HomeAssistant = object
    sys.modules["homeassistant"] = package
    sys.modules["homeassistant.config_entries"] = config_entries
    sys.modules["homeassistant.core"] = core


_install_runtime_data_ha_fakes()


def _load_intercom_module(name: str):
    if "custom_components" not in sys.modules:
        root_pkg = types.ModuleType("custom_components")
        root_pkg.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = root_pkg
    if PKG_NAME not in sys.modules:
        pkg = types.ModuleType(PKG_NAME)
        pkg.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = pkg

    is_core = name in CORE_MODULES
    if is_core and f"{PKG_NAME}.core" not in sys.modules:
        core_pkg = types.ModuleType(f"{PKG_NAME}.core")
        core_pkg.__path__ = [str(PKG_DIR / "core")]
        sys.modules[f"{PKG_NAME}.core"] = core_pkg
    full_name = f"{PKG_NAME}.{'core.' if is_core else ''}{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    path = PKG_DIR / ("core" if is_core else "") / f"{name}.py"
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {full_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


audio_format = _load_intercom_module("audio_format")
audio_pcm = _load_intercom_module("audio_pcm")
sip = _load_intercom_module("sip")
sdp = _load_intercom_module("sdp")
codec_capabilities = _load_intercom_module("codec_capabilities")
g722_codec = _load_intercom_module("g722_codec")
rtp = _load_intercom_module("rtp")
roster = _load_intercom_module("roster")
router = _load_intercom_module("router")
debug_capture = _load_intercom_module("debug_capture")
sip_client = _load_intercom_module("sip_client")
sip_tcp_io = _load_intercom_module("sip_tcp_io")
sip_listener = _load_intercom_module("sip_listener")
sip_registrar = _load_intercom_module("sip_registrar")
sip_auth = _load_intercom_module("sip_auth")
sip_runtime = _load_intercom_module("sip_runtime")
sip_rtp_bridge = _load_intercom_module("sip_rtp_bridge")
sip_bridge = _load_intercom_module("sip_bridge")
sip_trunk = _load_intercom_module("sip_trunk")
sip_endpoint = _load_intercom_module("sip_endpoint")
dtmf = _load_intercom_module("dtmf")


def _load_audio_ws_runtime_module():
    """Load audio_ws_view with minimal HA adapters and real media primitives."""

    package_name = "voip_stack_audio_runtime_test"
    module_name = f"{package_name}.audio_ws_view"
    if module_name in sys.modules:
        return sys.modules[module_name]

    package = types.ModuleType(package_name)
    package.__path__ = [str(PKG_DIR)]
    sys.modules[package_name] = package
    dependencies = {
        "rtp": rtp,
        "audio_ws": _load_intercom_module("audio_ws"),
        "call_registry": _load_intercom_module("call_registry"),
        "const": _load_intercom_module("const"),
        "debug_capture": debug_capture,
        "dtmf": dtmf,
        "media_debug": _load_intercom_module("media_debug"),
        "queue_utils": _load_intercom_module("queue_utils"),
        "sip_client": sip_client,
        "websocket_owner": _load_intercom_module("websocket_owner"),
    }
    for name, module in dependencies.items():
        sys.modules[f"{package_name}.{name}"] = module

    websocket_api = types.ModuleType(f"{package_name}.websocket_api")
    websocket_api.CALL_EVENT = "voip_stack_call_event"
    websocket_api._ha_softphone_store = (
        lambda hass, _endpoint_id="default": hass.store
    )
    websocket_api._publish_ha_softphone_state = (
        lambda _hass, endpoint_id="default": None  # noqa: ARG005
    )
    sys.modules[websocket_api.__name__] = websocket_api

    homeassistant = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
    if not hasattr(homeassistant, "__path__"):
        homeassistant.__path__ = []
    components = sys.modules.setdefault(
        "homeassistant.components", types.ModuleType("homeassistant.components")
    )
    if not hasattr(components, "__path__"):
        components.__path__ = []
    http = sys.modules.setdefault(
        "homeassistant.components.http", types.ModuleType("homeassistant.components.http")
    )
    http.HomeAssistantView = getattr(http, "HomeAssistantView", type("HomeAssistantView", (), {}))
    core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    exceptions = sys.modules.setdefault(
        "homeassistant.exceptions", types.ModuleType("homeassistant.exceptions")
    )
    exceptions.Unauthorized = getattr(
        exceptions, "Unauthorized", type("Unauthorized", (Exception,), {})
    )
    exceptions.UnknownUser = getattr(
        exceptions, "UnknownUser", type("UnknownUser", (Exception,), {})
    )

    spec = importlib.util.spec_from_file_location(module_name, PKG_DIR / "audio_ws_view.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # A failed dynamic import must not poison later tests with a partially
        # initialized module from ``sys.modules``.
        sys.modules.pop(module_name, None)
        raise
    return module


def _load_video_ws_runtime_module():
    """Load video_ws_view with minimal HA adapters and real video primitives."""

    # Reuse the deterministic Home Assistant module stubs installed by the
    # audio runtime loader; the media views share the same HA surface.
    _load_audio_ws_runtime_module()
    core = sys.modules["homeassistant.core"]
    core.callback = getattr(core, "callback", lambda target: target)
    package_name = "voip_stack_video_runtime_test"
    module_name = f"{package_name}.video_ws_view"
    if module_name in sys.modules:
        return sys.modules[module_name]

    package = types.ModuleType(package_name)
    package.__path__ = [str(PKG_DIR)]
    sys.modules[package_name] = package
    websocket_api = types.ModuleType(f"{package_name}.websocket_api")
    websocket_api.CALL_EVENT = "voip_stack_call_event"
    websocket_api._ha_softphone_store = (
        lambda hass, _endpoint_id="default": hass.store
    )
    websocket_api._publish_ha_softphone_state = (
        lambda _hass, endpoint_id="default": None  # noqa: ARG005
    )
    sys.modules[websocket_api.__name__] = websocket_api

    spec = importlib.util.spec_from_file_location(
        module_name, PKG_DIR / "video_ws_view.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _load_sip_transport_with_homeassistant_stubs():
    if "homeassistant" not in sys.modules:
        ha_pkg = types.ModuleType("homeassistant")
        ha_pkg.__path__ = []
        sys.modules["homeassistant"] = ha_pkg
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    sys.modules["homeassistant.core"] = core
    components = types.ModuleType("homeassistant.components")
    components.__path__ = []
    sys.modules["homeassistant.components"] = components
    network = types.ModuleType("homeassistant.components.network")

    async def async_get_announce_addresses(_hass):
        return ["127.0.0.1"]

    network.async_get_announce_addresses = async_get_announce_addresses
    sys.modules["homeassistant.components.network"] = network
    return _load_intercom_module("fsm")


@contextlib.contextmanager
def _reserved_udp_ports(count: int):
    sockets = []
    try:
        ports = []
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
            ports.append(sock.getsockname()[1])
        yield ports
    finally:
        for sock in sockets:
            sock.close()


__all__ = [
    "PKG_DIR",
    "Path",
    "_load_audio_ws_runtime_module",
    "_load_intercom_module",
    "_load_sip_transport_with_homeassistant_stubs",
    "_load_video_ws_runtime_module",
    "_reserved_udp_ports",
    "asyncio",
    "audio_format",
    "audio_pcm",
    "codec_capabilities",
    "contextlib",
    "debug_capture",
    "dtmf",
    "g722_codec",
    "os",
    "patch",
    "roster",
    "router",
    "rtp",
    "sdp",
    "sip",
    "sip_auth",
    "sip_bridge",
    "sip_client",
    "sip_endpoint",
    "sip_listener",
    "sip_registrar",
    "sip_rtp_bridge",
    "sip_runtime",
    "sip_tcp_io",
    "sip_trunk",
    "socket",
    "sys",
    "tempfile",
    "threading",
    "time",
    "types",
    "unittest",
]
