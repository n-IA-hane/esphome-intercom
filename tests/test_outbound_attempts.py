"""Behavioral tests for outbound dial-attempt resource ownership."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _load_outbound_attempts():
    full_name = f"{PKG_NAME}.outbound_attempts"
    dependency_names = (
        "custom_components",
        PKG_NAME,
        f"{PKG_NAME}.media_ports",
        f"{PKG_NAME}.session_cleanup",
        f"{PKG_NAME}.sip_client",
        f"{PKG_NAME}.sip_video_relay",
        full_name,
    )
    missing = object()
    previous = {name: sys.modules.get(name, missing) for name in dependency_names}
    try:
        if "custom_components" not in sys.modules:
            root_package = types.ModuleType("custom_components")
            root_package.__path__ = [str(ROOT / "custom_components")]
            sys.modules["custom_components"] = root_package
        if PKG_NAME not in sys.modules:
            package = types.ModuleType(PKG_NAME)
            package.__path__ = [str(PKG_DIR)]
            sys.modules[PKG_NAME] = package

        media_ports = types.ModuleType(f"{PKG_NAME}.media_ports")
        media_ports.RtpPortReservation = type("RtpPortReservation", (), {})
        sys.modules[media_ports.__name__] = media_ports
        cleanup = types.ModuleType(f"{PKG_NAME}.session_cleanup")
        cleanup.async_cleanup_sip_runtime = None

        async def _wait_for_cleanup(task):
            return await task

        cleanup.async_wait_for_cleanup = _wait_for_cleanup
        sys.modules[cleanup.__name__] = cleanup
        sip_client = types.ModuleType(f"{PKG_NAME}.sip_client")
        sip_client.SipCallClient = type("SipCallClient", (), {})
        sys.modules[sip_client.__name__] = sip_client
        video_relay = types.ModuleType(f"{PKG_NAME}.sip_video_relay")
        video_relay.SipVideoRtpRelay = type("SipVideoRtpRelay", (), {})
        sys.modules[video_relay.__name__] = video_relay

        spec = importlib.util.spec_from_file_location(
            full_name,
            PKG_DIR / "outbound_attempts.py",
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load {full_name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, original in previous.items():
            if original is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


outbound_attempts = _load_outbound_attempts()


class _Ports:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def release(self) -> None:
        self.events.append("ports")


class _Relay:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def stop(self) -> None:
        self.events.append("video")


class OutboundAttemptsTest(unittest.IsolatedAsyncioTestCase):
    async def test_client_signaling_closes_before_port_release(self) -> None:
        events: list[str] = []

        async def cleanup(**kwargs) -> None:
            events.append(f"signaling:{kwargs['terminate_client']}")

        outbound_attempts.async_cleanup_sip_runtime = cleanup
        client = SimpleNamespace(dialog_ids=SimpleNamespace(call_id="call-1"))

        await outbound_attempts.async_close_client_and_release(
            client,
            _Ports(events),
            bye=True,
        )

        self.assertEqual(events, ["signaling:True", "ports"])

    async def test_leg_failure_still_stops_video_and_releases_ports(self) -> None:
        events: list[str] = []

        async def cleanup(**_kwargs) -> None:
            events.append("signaling")
            raise RuntimeError("failed signaling cleanup")

        outbound_attempts.async_cleanup_sip_runtime = cleanup
        leg = outbound_attempts.OutboundLeg(
            member="Desk",
            uri=object(),
            client=SimpleNamespace(dialog_ids=SimpleNamespace(call_id="call-2")),
            ports=_Ports(events),
            video_relay=_Relay(events),
        )

        with self.assertRaises(RuntimeError):
            await outbound_attempts.async_close_outbound_leg(
                leg,
                bye_or_cancel=True,
            )

        self.assertEqual(events, ["signaling", "video", "ports"])
        self.assertIsNone(leg.video_relay)

    async def test_rejected_video_answer_releases_reserved_relay(self) -> None:
        events: list[str] = []
        leg = outbound_attempts.OutboundLeg(
            member="Audio only",
            uri=object(),
            client=SimpleNamespace(dialog_ids=SimpleNamespace(call_id="call-3")),
            ports=_Ports(events),
            video_relay=_Relay(events),
        )

        answer = await outbound_attempts.async_apply_outbound_video_answer(
            leg,
            None,
        )

        self.assertIsNone(answer)
        self.assertEqual(events, ["video"])
        self.assertIsNone(leg.video_relay)
        self.assertEqual(leg.video_failure_reason, "remote_video_rejected")

    async def test_dial_tasks_are_cancelled_and_joined(self) -> None:
        started = asyncio.Event()

        async def pending() -> None:
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(pending())
        await started.wait()
        await outbound_attempts.async_cancel_and_join_tasks([task])

        self.assertTrue(task.cancelled())


if __name__ == "__main__":
    unittest.main()
