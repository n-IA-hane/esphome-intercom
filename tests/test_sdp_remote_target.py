#!/usr/bin/env python3
"""Remote RTP/RTCP target construction contracts."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"
CORE_MODULES = {"audio_format", "sdp"}


def _load_module(name: str):
    if "custom_components" not in sys.modules:
        root = types.ModuleType("custom_components")
        root.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = root
    if PKG_NAME not in sys.modules:
        package = types.ModuleType(PKG_NAME)
        package.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = package
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


audio_format = _load_module("audio_format")
sdp = _load_module("sdp")


class RemoteMediaTargetTest(unittest.TestCase):
    def test_defaults_rtcp_to_rtp_plus_one(self) -> None:
        target = sdp.RemoteMediaTarget.from_section(
            {
                "connection_ip": "192.0.2.10",
                "media_port": 41000,
                "rtcp_address": "",
                "rtcp_port": 0,
                "rtcp_mux": False,
                "payload_order": [103, 104],
                "connection_held": False,
            }
        )

        self.assertEqual(target.rtcp_host, "192.0.2.10")
        self.assertEqual(target.rtcp_port, 41001)
        self.assertEqual(target.payload_types, (103, 104))

    def test_explicit_rtcp_and_mux_policy_are_preserved(self) -> None:
        target = sdp.RemoteMediaTarget.from_section(
            {
                "connection_ip": "192.0.2.10",
                "media_port": 41000,
                "rtcp_address": "192.0.2.20",
                "rtcp_port": 42000,
                "rtcp_mux": True,
                "payload_order": [103],
                "connection_held": True,
            },
            rtcp_mux=False,
        )

        self.assertEqual(target.rtcp_host, "192.0.2.20")
        self.assertEqual(target.rtcp_port, 42000)
        self.assertFalse(target.rtcp_mux)
        self.assertTrue(target.connection_held)

    def test_absent_section_projects_empty_video_fields(self) -> None:
        self.assertEqual(
            sdp.RemoteMediaTarget.from_section(None).as_remote_video_fields(),
            {
                "remote_video_rtp_host": "",
                "remote_video_rtp_port": 0,
                "remote_video_rtcp_host": "",
                "remote_video_rtcp_port": 0,
                "remote_video_rtcp_mux": False,
                "remote_video_payload_types": (),
                "remote_video_connection_held": False,
            },
        )

    def test_invalid_port_is_rejected(self) -> None:
        with self.assertRaises(sdp.SdpError):
            sdp.RemoteMediaTarget.from_section(
                {"connection_ip": "192.0.2.10", "media_port": 65535}
            )
