#!/usr/bin/env python3
"""Transport-neutral local softphone bridge contract tests."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _load_module(name: str):
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


phone_endpoint = _load_module("phone_endpoint")
endpoint_registry = _load_module("endpoint_registry")
_load_module("queue_utils")
bridge_module = _load_module("local_softphone_bridge")


def _load_runtime_module():
    package_name = "voip_stack_local_runtime_test"
    module_name = f"{package_name}.local_softphone_runtime"
    if module_name in sys.modules:
        return sys.modules[module_name]

    package = types.ModuleType(package_name)
    package.__path__ = [str(PKG_DIR)]
    sys.modules[package_name] = package
    for name in ("audio_format", "const"):
        sys.modules[f"{package_name}.{name}"] = _load_module(name)
    sys.modules[f"{package_name}.local_softphone_bridge"] = bridge_module
    endpoint_lifecycle = types.ModuleType(
        f"{package_name}.endpoint_lifecycle"
    )
    endpoint_lifecycle.call_registry = lambda _hass: None
    sys.modules[endpoint_lifecycle.__name__] = endpoint_lifecycle
    runtime_data = types.ModuleType(f"{package_name}.runtime_data")
    runtime_data.endpoint_directory = lambda hass: hass.runtime.endpoints
    runtime_data.runtime_data = lambda hass: getattr(hass, "runtime", None)
    sys.modules[runtime_data.__name__] = runtime_data

    homeassistant = sys.modules.setdefault(
        "homeassistant", types.ModuleType("homeassistant")
    )
    if not hasattr(homeassistant, "__path__"):
        homeassistant.__path__ = []
    core = sys.modules.setdefault(
        "homeassistant.core", types.ModuleType("homeassistant.core")
    )
    core.HomeAssistant = getattr(
        core, "HomeAssistant", type("HomeAssistant", (), {})
    )
    core.callback = getattr(core, "callback", lambda target: target)

    spec = importlib.util.spec_from_file_location(
        module_name, PKG_DIR / "local_softphone_runtime.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {module_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def endpoint(endpoint_id: str, *, capabilities=()):
    return phone_endpoint.PhoneEndpoint(
        endpoint_id=endpoint_id,
        name=endpoint_id.title(),
        kind="browser",
        capabilities=capabilities,
    )


class LocalSoftphoneBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = endpoint_registry.EndpointRegistry()
        self.registry.register(endpoint("office", capabilities={"audio", "video"}))
        self.registry.register(endpoint("kitchen", capabilities={"audio", "video"}))
        self.registry.register(endpoint("hall", capabilities={"audio"}))
        tokens = iter(f"lease-{index}" for index in range(100))
        self.bridge = bridge_module.LocalSoftphoneBridge(
            self.registry,
            audio_queue_size=2,
            video_queue_size=1,
            token_factory=lambda: next(tokens),
        )

    def start(self, **kwargs):
        return self.bridge.start_call("office", "kitchen", call_id="call-1", **kwargs)

    def connect(self, *, request_video: bool = True):
        self.start(
            request_video=request_video,
            enable_caller_video_send=request_video,
        )
        caller_lease = self.bridge.acquire_media("call-1", "office", "office-card")
        answer = self.bridge.answer("call-1", "kitchen", "kitchen-card")
        return caller_lease, answer.media_lease

    def test_start_claims_both_endpoints_and_exposes_dual_leg_state(self) -> None:
        call = self.start(request_video=True)

        self.assertEqual(call.call_id, "call-1")
        self.assertIs(call.caller_state, bridge_module.LocalCallState.CALLING)
        self.assertIs(call.callee_state, bridge_module.LocalCallState.RINGING)
        self.assertTrue(call.video_requested)
        self.assertTrue(call.video_enabled)
        self.assertEqual(self.registry.require("office").active_call_id, "call-1")
        self.assertEqual(self.registry.require("kitchen").active_call_id, "call-1")

    def test_generated_call_id_is_stable_and_transport_neutral(self) -> None:
        call = self.bridge.start_call("office", "kitchen")
        self.assertTrue(call.call_id.startswith("local-"))
        self.assertEqual(self.bridge.require_call(call.call_id), call)

    def test_start_rejects_self_call_and_duplicate_logical_call_id(self) -> None:
        with self.assertRaises(bridge_module.LocalBridgeError):
            self.bridge.start_call("office", "OFFICE", call_id="self")
        self.start()
        with self.assertRaises(bridge_module.LocalCallCollisionError):
            self.bridge.start_call("office", "hall", call_id="call-1")

    def test_busy_callee_rolls_back_new_caller_claim(self) -> None:
        self.start()
        with self.assertRaises(endpoint_registry.EndpointBusyError):
            self.bridge.start_call("hall", "kitchen", call_id="call-2")
        self.assertEqual(self.registry.require("hall").active_call_id, "")
        self.assertEqual(self.registry.require("kitchen").active_call_id, "call-1")

    def test_surviving_same_id_registry_claim_is_not_adopted(self) -> None:
        self.registry.claim_call("office", "orphaned-call")
        with self.assertRaises(bridge_module.LocalCallCollisionError):
            self.bridge.start_call("office", "kitchen", call_id="orphaned-call")
        self.assertEqual(
            self.registry.require("office").active_call_id, "orphaned-call"
        )
        self.assertEqual(self.registry.require("kitchen").active_call_id, "")

    def test_video_requires_request_and_capability_on_both_ends(self) -> None:
        audio_only = self.bridge.start_call(
            "office", "hall", call_id="audio-only", request_video=True
        )
        self.assertTrue(audio_only.video_requested)
        self.assertFalse(audio_only.video_enabled)
        self.assertEqual(audio_only.caller_media.audio_queued, 0)
        self.bridge.hangup("audio-only", "office")

        not_requested = self.bridge.start_call(
            "office", "kitchen", call_id="not-requested", request_video=False
        )
        self.assertFalse(not_requested.video_enabled)

    def test_first_answer_wins_media_lease_and_repeated_owner_is_idempotent(
        self,
    ) -> None:
        self.start()
        first = self.bridge.answer("call-1", "kitchen", "wall-tablet-a")
        repeated = self.bridge.answer("call-1", "KITCHEN", "wall-tablet-a")

        self.assertEqual(first.media_lease, repeated.media_lease)
        self.assertIs(first.call.caller_state, bridge_module.LocalCallState.IN_CALL)
        self.assertIs(first.call.callee_state, bridge_module.LocalCallState.IN_CALL)
        self.assertEqual(first.call.callee_media_owner_id, "wall-tablet-a")
        with self.assertRaises(bridge_module.LocalMediaLeaseBusyError):
            self.bridge.answer("call-1", "kitchen", "wall-tablet-b")

    def test_originating_card_can_be_pinned_before_call_events(self) -> None:
        events = []
        self.bridge.subscribe(events.append)

        call = self.bridge.start_call(
            "office",
            "kitchen",
            call_id="owned-call",
            caller_owner_id="office-card-a",
        )

        self.assertEqual(call.caller_media_owner_id, "office-card-a")
        self.assertEqual(
            events[0].call.caller_media_owner_id,
            "office-card-a",
        )
        with self.assertRaises(bridge_module.LocalMediaLeaseBusyError):
            self.bridge.acquire_media(
                "owned-call", "office", "office-card-b"
            )

    def test_video_negotiation_and_each_camera_permission_are_independent(
        self,
    ) -> None:
        call = self.bridge.start_call(
            "office",
            "kitchen",
            call_id="directional",
            request_video=True,
            enable_caller_video_send=False,
        )
        caller = self.bridge.acquire_media(
            "directional", "office", "office-card"
        )
        answered = self.bridge.answer(
            "directional",
            "kitchen",
            "kitchen-card",
            enable_video_send=True,
        )
        callee = answered.media_lease

        self.assertTrue(call.video_enabled)
        self.assertEqual(
            answered.call.video_direction_for("office"), "recvonly"
        )
        self.assertEqual(
            answered.call.video_direction_for("kitchen"), "sendonly"
        )
        with self.assertRaises(bridge_module.LocalMediaNotNegotiatedError):
            self.bridge.send_video(
                "directional", "office", caller.token, b"forbidden"
            )
        self.bridge.send_video(
            "directional", "kitchen", callee.token, b"camera"
        )
        self.assertEqual(
            self.bridge.receive_video_nowait(
                "directional", "office", caller.token
            ),
            b"camera",
        )

    def test_both_cameras_disabled_keeps_negotiated_video_inactive(self) -> None:
        self.bridge.start_call(
            "office",
            "kitchen",
            call_id="inactive-video",
            request_video=True,
        )
        answered = self.bridge.answer(
            "inactive-video", "kitchen", "kitchen-card"
        ).call

        self.assertTrue(answered.video_enabled)
        self.assertEqual(answered.video_direction_for("office"), "inactive")
        self.assertEqual(answered.video_direction_for("kitchen"), "inactive")

    def test_ringing_callee_cannot_bypass_first_answer_with_media_claim(self) -> None:
        self.start()
        with self.assertRaises(bridge_module.LocalCallStateError):
            self.bridge.acquire_media("call-1", "kitchen", "wall-tablet-b")
        lease = self.bridge.acquire_media("call-1", "office", "office-tablet")
        self.assertTrue(
            self.bridge.validate_media_lease("call-1", "office", lease.token)
        )

    def test_stale_media_token_cannot_release_or_use_active_owner(self) -> None:
        caller, _callee = self.connect()
        self.assertFalse(self.bridge.release_media("call-1", "office", "stale"))
        with self.assertRaises(bridge_module.LocalMediaLeaseError):
            self.bridge.send_audio("call-1", "office", "stale", b"audio")
        self.assertTrue(self.bridge.release_media("call-1", "office", caller.token))
        self.assertFalse(
            self.bridge.validate_media_lease("call-1", "office", caller.token)
        )
        replacement = self.bridge.acquire_media("call-1", "office", "second-card")
        self.assertNotEqual(replacement.token, caller.token)

    def test_answered_callee_can_reclaim_media_after_document_disconnect(self) -> None:
        _caller, callee = self.connect()

        self.assertTrue(
            self.bridge.release_media("call-1", "kitchen", callee.token)
        )
        replacement = self.bridge.acquire_media(
            "call-1", "kitchen", "reloaded-kitchen-card"
        )

        self.assertEqual(
            self.bridge.get_call("call-1").callee_state,
            bridge_module.LocalCallState.IN_CALL,
        )
        self.assertEqual(
            self.bridge.get_call("call-1").answer_owner_id,
            "reloaded-kitchen-card",
        )
        self.assertNotEqual(replacement.token, callee.token)

    def test_authorized_adapter_can_atomically_rebind_stale_local_lease(self) -> None:
        _caller, callee = self.connect()

        replacement = self.bridge.rebind_media_owner(
            "call-1", "kitchen", "replacement-document"
        )

        self.assertFalse(
            self.bridge.validate_media_lease("call-1", "kitchen", callee.token)
        )
        self.assertTrue(
            self.bridge.validate_media_lease(
                "call-1", "kitchen", replacement.token
            )
        )
        self.assertEqual(
            self.bridge.get_call("call-1").answer_owner_id,
            "replacement-document",
        )

    def test_release_discards_audio_and_video_queued_for_reloaded_card(self) -> None:
        caller, callee = self.connect()
        self.bridge.send_audio("call-1", "office", caller.token, b"old-audio")
        self.bridge.send_video("call-1", "office", caller.token, b"old-video")
        self.assertEqual(self.bridge.media_stats("call-1", "kitchen").audio_queued, 1)
        self.assertEqual(self.bridge.media_stats("call-1", "kitchen").video_queued, 1)

        self.assertTrue(
            self.bridge.release_media("call-1", "kitchen", callee.token)
        )
        replacement = self.bridge.acquire_media(
            "call-1", "kitchen", "reloaded-kitchen-card"
        )

        with self.assertRaises(asyncio.QueueEmpty):
            self.bridge.receive_audio_nowait(
                "call-1", "kitchen", replacement.token
            )
        with self.assertRaises(asyncio.QueueEmpty):
            self.bridge.receive_video_nowait(
                "call-1", "kitchen", replacement.token
            )

    def test_media_without_peer_lease_is_dropped_never_replayed(self) -> None:
        caller, callee = self.connect()
        self.assertTrue(
            self.bridge.release_media("call-1", "kitchen", callee.token)
        )

        self.assertTrue(
            self.bridge.send_audio("call-1", "office", caller.token, b"lost-audio")
        )
        self.assertTrue(
            self.bridge.send_video("call-1", "office", caller.token, b"lost-video")
        )
        stats = self.bridge.media_stats("call-1", "kitchen")
        self.assertEqual(stats.audio_queued, 0)
        self.assertEqual(stats.video_queued, 0)
        self.assertEqual(stats.audio_dropped, 1)
        self.assertEqual(stats.video_dropped, 1)

        replacement = self.bridge.acquire_media(
            "call-1", "kitchen", "replacement-kitchen-card"
        )
        with self.assertRaises(asyncio.QueueEmpty):
            self.bridge.receive_audio_nowait(
                "call-1", "kitchen", replacement.token
            )
        with self.assertRaises(asyncio.QueueEmpty):
            self.bridge.receive_video_nowait(
                "call-1", "kitchen", replacement.token
            )

    def test_audio_queue_is_bounded_drops_oldest_and_counts_drops(self) -> None:
        caller, callee = self.connect()
        self.assertFalse(
            self.bridge.send_audio("call-1", "office", caller.token, b"one")
        )
        self.assertFalse(
            self.bridge.send_audio("call-1", "office", caller.token, b"two")
        )
        self.assertTrue(
            self.bridge.send_audio("call-1", "office", caller.token, b"three")
        )

        stats = self.bridge.media_stats("call-1", "kitchen")
        self.assertEqual(stats.audio_queued, 2)
        self.assertEqual(stats.audio_dropped, 1)
        self.assertEqual(
            self.bridge.receive_audio_nowait("call-1", "kitchen", callee.token),
            b"two",
        )
        self.assertEqual(
            self.bridge.receive_audio_nowait("call-1", "kitchen", callee.token),
            b"three",
        )

    def test_video_queue_is_bounded_and_bidirectional(self) -> None:
        caller, callee = self.connect()
        self.assertFalse(
            self.bridge.send_video("call-1", "office", caller.token, b"frame-a")
        )
        self.assertTrue(
            self.bridge.send_video("call-1", "office", caller.token, b"frame-b")
        )
        self.assertEqual(
            self.bridge.receive_video_nowait("call-1", "kitchen", callee.token),
            b"frame-b",
        )
        self.bridge.send_audio("call-1", "kitchen", callee.token, b"return-audio")
        self.assertEqual(
            self.bridge.receive_audio_nowait("call-1", "office", caller.token),
            b"return-audio",
        )
        self.assertEqual(self.bridge.media_stats("call-1", "kitchen").video_dropped, 1)

    def test_audio_remains_available_when_video_is_not_negotiated(self) -> None:
        self.bridge.start_call("office", "hall", call_id="audio", request_video=True)
        caller = self.bridge.acquire_media("audio", "office", "office-card")
        callee = self.bridge.answer("audio", "hall", "hall-card").media_lease

        self.bridge.send_audio("audio", "office", caller.token, b"pcm")
        self.assertEqual(
            self.bridge.receive_audio_nowait("audio", "hall", callee.token), b"pcm"
        )
        with self.assertRaises(bridge_module.LocalMediaNotNegotiatedError):
            self.bridge.send_video("audio", "office", caller.token, b"video")

    def test_media_cannot_flow_before_answer(self) -> None:
        self.start()
        caller = self.bridge.acquire_media("call-1", "office", "office-card")
        with self.assertRaises(bridge_module.LocalCallStateError):
            self.bridge.send_audio("call-1", "office", caller.token, b"early")

    def test_decline_returns_terminal_snapshot_and_releases_both_claims(self) -> None:
        self.start()
        ended = self.bridge.decline("call-1", "kitchen")

        self.assertTrue(ended.ended)
        self.assertIs(ended.end_reason, bridge_module.LocalCallEndReason.DECLINED)
        self.assertIsNone(self.bridge.get_call("call-1"))
        self.assertEqual(self.registry.require("office").active_call_id, "")
        self.assertEqual(self.registry.require("kitchen").active_call_id, "")
        with self.assertRaises(bridge_module.LocalCallNotFoundError):
            self.bridge.decline("call-1", "kitchen")

    def test_hangup_from_either_role_has_explicit_terminal_reason(self) -> None:
        self.start()
        caller_end = self.bridge.hangup("call-1", "office")
        self.assertIs(
            caller_end.end_reason, bridge_module.LocalCallEndReason.CALLER_HANGUP
        )

        self.bridge.start_call("office", "kitchen", call_id="call-2")
        callee_end = self.bridge.hangup("call-2", "kitchen")
        self.assertIs(
            callee_end.end_reason, bridge_module.LocalCallEndReason.CALLEE_HANGUP
        )

    def test_terminal_reason_and_origin_are_projected_per_endpoint_leg(self) -> None:
        runtime = _load_runtime_module()
        self.start()
        caller_end = self.bridge.hangup("call-1", "office")

        self.assertEqual(runtime._reason(caller_end, "office"), "local_hangup")
        self.assertEqual(runtime._origin(caller_end, "office"), "self")
        self.assertEqual(runtime._reason(caller_end, "kitchen"), "remote_hangup")
        self.assertEqual(runtime._origin(caller_end, "kitchen"), "remote")

        self.bridge.start_call("office", "kitchen", call_id="call-2")
        callee_end = self.bridge.hangup("call-2", "kitchen")
        self.assertEqual(runtime._reason(callee_end, "office"), "remote_hangup")
        self.assertEqual(runtime._origin(callee_end, "office"), "remote")
        self.assertEqual(runtime._reason(callee_end, "kitchen"), "local_hangup")
        self.assertEqual(runtime._origin(callee_end, "kitchen"), "self")

    def test_decline_is_remote_for_caller_and_self_for_callee(self) -> None:
        runtime = _load_runtime_module()
        self.start()
        declined = self.bridge.decline("call-1", "kitchen")

        self.assertEqual(runtime._reason(declined, "office"), "declined")
        self.assertEqual(runtime._origin(declined, "office"), "remote")
        self.assertEqual(runtime._reason(declined, "kitchen"), "declined")
        self.assertEqual(runtime._origin(declined, "kitchen"), "self")

    def test_events_cover_state_and_lease_lifecycle_without_exposing_token(
        self,
    ) -> None:
        events = []
        unsubscribe = self.bridge.subscribe(events.append)
        self.start()
        caller = self.bridge.acquire_media("call-1", "office", "office-card")
        self.bridge.answer("call-1", "kitchen", "kitchen-card")
        self.bridge.release_media("call-1", "office", caller.token)
        self.bridge.hangup("call-1", "kitchen")
        unsubscribe()

        self.assertEqual(
            [event.event_type for event in events],
            [
                bridge_module.LocalBridgeEventType.STARTED,
                bridge_module.LocalBridgeEventType.MEDIA_LEASE_ACQUIRED,
                bridge_module.LocalBridgeEventType.ANSWERED,
                bridge_module.LocalBridgeEventType.MEDIA_LEASE_ACQUIRED,
                bridge_module.LocalBridgeEventType.MEDIA_LEASE_RELEASED,
                bridge_module.LocalBridgeEventType.ENDED,
            ],
        )
        self.assertNotIn("lease-", repr(events))

    def test_close_terminates_all_calls_and_releases_every_endpoint(self) -> None:
        self.registry.register(endpoint("bedroom"))
        self.start()
        self.bridge.start_call("hall", "bedroom", call_id="call-2")

        ended = self.bridge.close()
        self.assertEqual(len(ended), 2)
        self.assertTrue(
            all(
                call.end_reason is bridge_module.LocalCallEndReason.SHUTDOWN
                for call in ended
            )
        )
        self.assertEqual(self.bridge.calls, ())
        self.assertTrue(
            all(not item.active_call_id for item in self.registry.endpoints)
        )

    def test_constructor_rejects_unbounded_or_invalid_queues(self) -> None:
        for kwargs in (
            {"audio_queue_size": 0},
            {"video_queue_size": -1},
            {"audio_queue_size": True},
            {"video_queue_size": 1.5},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(bridge_module.LocalBridgeError):
                    bridge_module.LocalSoftphoneBridge(self.registry, **kwargs)


class LocalSoftphoneBridgeAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.registry = endpoint_registry.EndpointRegistry()
        self.registry.register(endpoint("office", capabilities={"video"}))
        self.registry.register(endpoint("kitchen", capabilities={"video"}))
        self.bridge = bridge_module.LocalSoftphoneBridge(
            self.registry, token_factory=lambda: "lease-a"
        )
        self.bridge.start_call(
            "office",
            "kitchen",
            call_id="call-1",
            request_video=True,
            enable_caller_video_send=True,
        )
        self.caller = self.bridge.acquire_media("call-1", "office", "office-card")
        self.callee = self.bridge.answer(
            "call-1", "kitchen", "kitchen-card"
        ).media_lease

    async def test_async_receive_routes_audio(self) -> None:
        receive = asyncio.create_task(
            self.bridge.receive_audio("call-1", "kitchen", self.callee.token)
        )
        await asyncio.sleep(0)
        self.bridge.send_audio("call-1", "office", self.caller.token, b"pcm")
        self.assertEqual(await receive, b"pcm")

    async def test_async_receive_wakes_when_lease_is_released(self) -> None:
        receive = asyncio.create_task(
            self.bridge.receive_audio("call-1", "kitchen", self.callee.token)
        )
        await asyncio.sleep(0)
        self.bridge.release_media("call-1", "kitchen", self.callee.token)
        with self.assertRaises(bridge_module.LocalMediaLeaseError):
            await receive

    async def test_async_receive_wakes_when_call_ends(self) -> None:
        receive = asyncio.create_task(
            self.bridge.receive_audio("call-1", "kitchen", self.callee.token)
        )
        await asyncio.sleep(0)
        self.bridge.hangup("call-1", "office")
        with self.assertRaises(bridge_module.LocalCallNotFoundError):
            await receive

    async def test_video_keyframe_control_is_routed_to_the_peer(self) -> None:
        receive = asyncio.create_task(
            self.bridge.receive_video_control(
                "call-1", "kitchen", self.callee.token
            )
        )
        await asyncio.sleep(0)
        self.bridge.send_video_control(
            "call-1",
            "office",
            self.caller.token,
            "force_key_frame",
        )
        self.assertEqual(await receive, "force_key_frame")


if __name__ == "__main__":
    unittest.main()
