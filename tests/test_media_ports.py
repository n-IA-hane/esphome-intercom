#!/usr/bin/env python3
"""RTP media port allocator tests."""

from __future__ import annotations

import importlib.util
import socket
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _install_ha_fakes() -> None:
    ha = sys.modules.get("homeassistant")
    if ha is not None and not hasattr(ha, "__path__"):
        ha.__path__ = []
    if "homeassistant.core" not in sys.modules:
        ha = types.ModuleType("homeassistant")
        ha.__path__ = []
        core = types.ModuleType("homeassistant.core")
        config_entries = types.ModuleType("homeassistant.config_entries")
        core.HomeAssistant = type("HomeAssistant", (), {})
        config_entries.ConfigEntry = type("ConfigEntry", (), {})
        config_entries.ConfigSubentry = type("ConfigSubentry", (), {})
        sys.modules["homeassistant"] = ha
        sys.modules["homeassistant.core"] = core
        sys.modules["homeassistant.config_entries"] = config_entries
    elif "homeassistant.config_entries" not in sys.modules:
        config_entries = types.ModuleType("homeassistant.config_entries")
        config_entries.ConfigEntry = type("ConfigEntry", (), {})
        config_entries.ConfigSubentry = type("ConfigSubentry", (), {})
        sys.modules["homeassistant.config_entries"] = config_entries


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


media_ports = _load_module("media_ports")
media_ports.require_runtime_data = lambda hass: hass.runtime


class FakeHass:
    def __init__(self) -> None:
        self.data = {
            "voip_stack": {
                "transport_config": {
                    "sip_port": 5060,
                    "rtp_port": 40000,
                    "advertise_host": "",
                }
            }
        }
        self.call_artifacts = types.SimpleNamespace(delayed_offer_ports=None)
        self.runtime = types.SimpleNamespace(
            next_rtp_port=0,
            rtp_port_pool={},
            sip=types.SimpleNamespace(
                artifacts_for=lambda _call_id: self.call_artifacts
            ),
        )


class MediaPortPoolTest(unittest.TestCase):
    def test_single_allocator_uses_base_when_available(self) -> None:
        hass = FakeHass()
        with patch.object(media_ports, "rtp_port_available", return_value=True):
            self.assertEqual(media_ports.allocate_sip_rtp_port(hass), 40000)
            self.assertEqual(hass.runtime.next_rtp_port, 40002)

    def test_single_allocator_raises_when_no_port_is_available(self) -> None:
        hass = FakeHass()
        with patch.object(media_ports, "rtp_port_available", return_value=False):
            with self.assertRaises(RuntimeError):
                media_ports.allocate_sip_rtp_port(hass)

    def test_pair_allocator_wraps_and_reuses_released_ports(self) -> None:
        hass = FakeHass()
        with patch.object(media_ports, "RTP_RELAY_POOL_WIDTH", 4), patch.object(media_ports, "rtp_port_available", return_value=True):
            self.assertEqual(media_ports.allocate_sip_rtp_port_pair(hass), (40002, 40004))
            with self.assertRaises(RuntimeError):
                media_ports.allocate_sip_rtp_port_pair(hass)
            media_ports.release_sip_rtp_port_pair(hass, (40002, 40004))
            self.assertEqual(media_ports.allocate_sip_rtp_port_pair(hass), (40002, 40004))

    def test_pair_allocator_skips_in_use_and_unavailable_ports(self) -> None:
        hass = FakeHass()

        def available(port: int) -> bool:
            return int(port) not in {40002, 40004}

        with patch.object(media_ports, "rtp_port_available", side_effect=available):
            self.assertEqual(media_ports.allocate_sip_rtp_port_pair(hass), (40006, 40008))

    def test_pair_release_is_idempotent(self) -> None:
        hass = FakeHass()
        with patch.object(media_ports, "rtp_port_available", return_value=True):
            ports = media_ports.allocate_sip_rtp_port_pair(hass)
            media_ports.release_sip_rtp_port_pair(hass, ports)
            media_ports.release_sip_rtp_port_pair(hass, ports)
            self.assertEqual(hass.runtime.rtp_port_pool["used"], set())

    def test_reservation_release_and_detach_ownership(self) -> None:
        hass = FakeHass()
        with patch.object(media_ports, "rtp_port_available", return_value=True):
            reservation = media_ports.RtpPortReservation.allocate(hass)
            self.assertEqual(hass.runtime.rtp_port_pool["used"], set(reservation.ports))
            reservation.release()
            reservation.release()
            self.assertEqual(hass.runtime.rtp_port_pool["used"], set())

            detached = media_ports.RtpPortReservation.allocate(hass)
            ports = detached.detach()
            detached.release()
            self.assertEqual(hass.runtime.rtp_port_pool["used"], set(ports))
            media_ports.release_sip_rtp_port_pair(hass, ports)

    def test_delayed_offer_reservation_transfers_once(self) -> None:
        hass = FakeHass()
        with patch.object(media_ports, "rtp_port_available", return_value=True):
            reservation = media_ports.reserve_delayed_offer_ports(hass, "call-1")
            self.assertIs(hass.call_artifacts.delayed_offer_ports, reservation)
            self.assertIs(
                media_ports.take_delayed_offer_ports(hass, "call-1"),
                reservation,
            )
            self.assertIsNone(
                media_ports.take_delayed_offer_ports(hass, "call-1")
            )
            reservation.release()
            self.assertEqual(hass.runtime.rtp_port_pool["used"], set())

    def test_release_media_reservation_releases_runtime_metadata(self) -> None:
        hass = FakeHass()
        with patch.object(media_ports, "rtp_port_available", return_value=True):
            reservation = media_ports.RtpPortReservation.allocate(hass)
            item = {"rtp_reservation": reservation}
            self.assertEqual(hass.runtime.rtp_port_pool["used"], set(reservation.ports))
            media_ports.release_media_reservation(item)
            media_ports.release_media_reservation(item)
            self.assertEqual(hass.runtime.rtp_port_pool["used"], set())

    def test_release_media_reservation_releases_separate_video_reservation(self) -> None:
        hass = FakeHass()
        with patch.object(media_ports, "rtp_port_available", return_value=True):
            audio = media_ports.RtpPortReservation.allocate(hass)
            video = media_ports.RtpPortReservation.allocate(hass)
            item = {
                "rtp_reservation": audio,
                "video_rtp_reservation": video,
            }
            media_ports.release_media_reservation(item)
            media_ports.release_media_reservation(item)
            self.assertEqual(
                hass.runtime.rtp_port_pool["used"], set()
            )

    def test_release_video_media_preserves_audio_bridge_ownership(self) -> None:
        hass = FakeHass()
        with patch.object(media_ports, "rtp_port_available", return_value=True):
            audio = media_ports.RtpPortReservation.allocate(hass)
            video = media_ports.RtpPortReservation.allocate(hass)
            rtp_socket = Mock()
            rtcp_socket = Mock()
            item = {
                "rtp_reservation": audio,
                "video_rtp_reservation": video,
                "video_rtp_socket": rtp_socket,
                "video_rtcp_socket": rtcp_socket,
            }

            media_ports.release_video_media_reservation(item)
            media_ports.release_video_media_reservation(item)

            rtp_socket.close.assert_called_once_with()
            rtcp_socket.close.assert_called_once_with()
            self.assertEqual(
                hass.runtime.rtp_port_pool["used"],
                set(audio.ports),
            )
            self.assertIn("rtp_reservation", item)
            self.assertNotIn("video_rtp_reservation", item)
            media_ports.release_media_reservation(item)
            self.assertEqual(
                hass.runtime.rtp_port_pool["used"], set()
            )

    def test_bound_video_socket_is_nonblocking_and_closed_with_reservation(self) -> None:
        hass = FakeHass()
        with patch.object(media_ports, "rtp_port_available", return_value=True):
            reservation = media_ports.RtpPortReservation.allocate(hass)
            sock = media_ports.bind_sip_rtp_socket(0)
            self.assertFalse(sock.getblocking())
            self.assertGreaterEqual(sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF), 1024 * 1024)
            rtcp_sock = media_ports.bind_sip_rtp_socket(0)
            item = {
                "rtp_reservation": reservation,
                "video_rtp_socket": sock,
                "video_rtcp_socket": rtcp_sock,
            }
            media_ports.release_media_reservation(item)
            self.assertEqual(sock.fileno(), -1)
            self.assertEqual(rtcp_sock.fileno(), -1)
            self.assertNotIn("video_rtp_socket", item)
            self.assertNotIn("video_rtcp_socket", item)
            self.assertEqual(hass.runtime.rtp_port_pool["used"], set())

    def test_bind_video_sockets_reserves_adjacent_rtp_and_rtcp_ports(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind(("127.0.0.1", 0))
            base = int(probe.getsockname()[1])
        finally:
            probe.close()
        if base >= 65535:
            self.skipTest("no adjacent UDP port available")

        rtp_socket = rtcp_socket = None
        try:
            rtp_socket, rtcp_socket = media_ports.bind_sip_video_sockets(base)
            self.assertEqual(rtp_socket.getsockname()[1], base)
            self.assertEqual(rtcp_socket.getsockname()[1], base + 1)
            self.assertFalse(rtp_socket.getblocking())
            self.assertFalse(rtcp_socket.getblocking())
        except OSError:
            self.skipTest("adjacent UDP port became unavailable")
        finally:
            if rtp_socket is not None:
                rtp_socket.close()
            if rtcp_socket is not None:
                rtcp_socket.close()

    def test_bind_video_sockets_closes_rtp_when_rtcp_bind_fails(self) -> None:
        first = Mock()
        second = Mock()
        with patch.object(
            media_ports,
            "bind_sip_rtp_socket",
            side_effect=[first, OSError("RTCP busy")],
        ):
            with self.assertRaisesRegex(OSError, "RTCP busy"):
                media_ports.bind_sip_video_sockets(40000)
        first.close.assert_called_once_with()
        second.close.assert_not_called()

    def test_video_media_reservation_retries_after_busy_rtcp_port(self) -> None:
        first = Mock(ports=(40002, 40004))
        second = Mock(ports=(40006, 40008))
        rtp_socket = Mock()
        rtcp_socket = Mock()
        with patch.object(
            media_ports.RtpPortReservation,
            "allocate",
            side_effect=[first, second],
        ), patch.object(
            media_ports,
            "bind_sip_video_sockets",
            side_effect=[OSError("RTCP busy"), (rtp_socket, rtcp_socket)],
        ) as bind:
            reservation, bound_rtp, bound_rtcp = media_ports.reserve_sip_video_media(
                FakeHass(), attempts=2
            )

        first.release.assert_called_once_with()
        second.release.assert_not_called()
        self.assertIs(reservation, second)
        self.assertIs(bound_rtp, rtp_socket)
        self.assertIs(bound_rtcp, rtcp_socket)
        self.assertEqual([item.args[0] for item in bind.call_args_list], [40004, 40008])

    def test_video_relay_reservation_retries_and_closes_partial_pair(self) -> None:
        first = Mock(ports=(40002, 40004))
        second = Mock(ports=(40006, 40008))
        first_rtp = Mock()
        first_rtcp = Mock()
        final_sockets = tuple(Mock() for _ in range(4))
        with patch.object(
            media_ports.RtpPortReservation,
            "allocate",
            side_effect=[first, second],
        ), patch.object(
            media_ports,
            "bind_sip_video_sockets",
            side_effect=[
                (first_rtp, first_rtcp),
                OSError("second relay RTCP busy"),
                final_sockets[:2],
                final_sockets[2:],
            ],
        ):
            reservation, sockets = media_ports.reserve_sip_video_relay_media(
                FakeHass(), attempts=2
            )

        first.release.assert_called_once_with()
        first_rtp.close.assert_called_once_with()
        first_rtcp.close.assert_called_once_with()
        second.release.assert_not_called()
        self.assertIs(reservation, second)
        self.assertEqual(sockets, final_sockets)


if __name__ == "__main__":
    unittest.main()
