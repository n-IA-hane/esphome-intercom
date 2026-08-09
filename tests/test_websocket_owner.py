#!/usr/bin/env python3
"""Concurrency tests for browser media WebSocket ownership."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "voip_stack"
    / "websocket_owner.py"
)
SPEC = importlib.util.spec_from_file_location(
    "voip_stack_websocket_owner_test", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
OWNER_MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = OWNER_MODULE
SPEC.loader.exec_module(OWNER_MODULE)

MediaWebSocketOwner = OWNER_MODULE.MediaWebSocketOwner
WebSocketOwnerBusyError = OWNER_MODULE.WebSocketOwnerBusyError
async_claim_media_owner = OWNER_MODULE.async_claim_media_owner
async_claim_call_media_owner = OWNER_MODULE.async_claim_call_media_owner
async_release_local_media_if_unowned = OWNER_MODULE.async_release_local_media_if_unowned
async_release_media_owner = OWNER_MODULE.async_release_media_owner
async_revoke_media_owners = OWNER_MODULE.async_revoke_media_owners
media_websocket_owner_status = OWNER_MODULE.media_websocket_owner_status


class _MediaRuntime(dict):
    """Test view that keeps legacy assertions readable around the new owner."""

    @property
    def identity_locks(self):
        return self.setdefault("media_identity_locks", {})

    def owners_for(self, channel):
        return self.setdefault(f"{channel}_ws_owners", {})

    def owner_lock_for(self, channel):
        return self.setdefault(f"{channel}_ws_owner_lock", asyncio.Lock())


class _FakeWebSocket:
    def __init__(self, *, fail: bool = False) -> None:
        self.force_close_calls = 0
        self.fail = fail

    def force_close(self) -> None:
        self.force_close_calls += 1
        if self.fail:
            raise RuntimeError("synthetic close failure")


class _FakeTransport:
    def __init__(self, *, fail: bool = False) -> None:
        self.close_calls = 0
        self.fail = fail

    def close(self) -> None:
        self.close_calls += 1
        if self.fail:
            raise RuntimeError("synthetic transport failure")


class _CallRegistry:
    def __init__(self, client_id: str = "") -> None:
        self.sessions = {
            "call-1": types.SimpleNamespace(
                metadata={"media_client_id": client_id},
                revision=1,
            )
        }
        self.softphone_media = {"call-1": {"media_client_id": client_id}}

    @staticmethod
    def resolve_session_id(call_id: str) -> str:
        return call_id

    def resource_for(self, call_id: str, _kind: str):
        return self.softphone_media.get(call_id)

    def add_call(self, call_id: str, client_id: str = "") -> None:
        self.sessions[call_id] = types.SimpleNamespace(
            metadata={"media_client_id": client_id},
            revision=1,
        )
        self.softphone_media[call_id] = {"media_client_id": client_id}


class WebSocketOwnerTest(unittest.IsolatedAsyncioTestCase):
    def test_owner_status_never_exposes_browser_identity(self) -> None:
        key = "kitchen|call-1"
        bucket = _MediaRuntime()
        self.assertEqual(
            media_websocket_owner_status(
                bucket, _CallRegistry(), "call-1", "kitchen", "document-a"
            ),
            "available",
        )
        bucket["audio_ws_owners"] = {key: MediaWebSocketOwner(client_id="document-a")}
        self.assertEqual(
            media_websocket_owner_status(
                bucket,
                _CallRegistry("document-a"),
                "call-1",
                "kitchen",
                "document-a",
            ),
            "self",
        )
        self.assertEqual(
            media_websocket_owner_status(
                bucket,
                _CallRegistry("document-a"),
                "call-1",
                "kitchen",
                "document-b",
            ),
            "other",
        )
        bucket["video_ws_owners"] = {key: object()}
        self.assertEqual(
            media_websocket_owner_status(
                bucket,
                _CallRegistry("document-a"),
                "call-1",
                "kitchen",
                "document-a",
            ),
            "other",
        )

    async def test_handoff_for_one_call_does_not_block_an_unrelated_call(
        self,
    ) -> None:
        bucket = _MediaRuntime()
        registry = _CallRegistry("document-a")
        registry.add_call("call-2", "document-b")
        previous = MediaWebSocketOwner(
            user_id="user-a",
            client_id="document-a",
        )
        await async_claim_call_media_owner(
            bucket,
            registry,
            "call-1",
            "kitchen",
            previous,
            channel="audio",
            timeout=1.0,
        )

        handoff = asyncio.create_task(
            async_claim_call_media_owner(
                bucket,
                registry,
                "call-1",
                "kitchen",
                MediaWebSocketOwner(
                    user_id="user-a",
                    client_id="document-a",
                ),
                channel="audio",
                timeout=1.0,
            )
        )
        await asyncio.wait_for(previous.handoff_requested.wait(), timeout=0.2)

        unrelated = MediaWebSocketOwner(
            user_id="user-b",
            client_id="document-b",
        )
        owners, _lock, owner_key = await asyncio.wait_for(
            async_claim_call_media_owner(
                bucket,
                registry,
                "call-2",
                "office",
                unrelated,
                channel="audio",
                timeout=1.0,
            ),
            timeout=0.2,
        )
        self.assertIs(owners[owner_key], unrelated)

        await async_release_media_owner(
            bucket["audio_ws_owners"],
            bucket["audio_ws_owner_lock"],
            "kitchen|call-1",
            previous,
        )
        await handoff

    def test_pinned_originator_blocks_other_client_before_socket_attach(
        self,
    ) -> None:
        bucket = _MediaRuntime()
        registry = _CallRegistry("android-originator")
        self.assertEqual(
            media_websocket_owner_status(
                bucket,
                registry,
                "call-1",
                "kitchen",
                "pc-observer",
            ),
            "other",
        )
        self.assertEqual(
            media_websocket_owner_status(
                bucket,
                registry,
                "call-1",
                "kitchen",
                "android-originator",
            ),
            "available",
        )

    async def test_disconnected_other_document_cannot_rebind_call_identity(
        self,
    ) -> None:
        bucket = _MediaRuntime()
        registry = _CallRegistry("closed-document")
        replacement = MediaWebSocketOwner(
            user_id="user-a",
            client_id="reloaded-document",
        )

        with self.assertRaises(WebSocketOwnerBusyError):
            await async_claim_call_media_owner(
                bucket,
                registry,
                "call-1",
                "kitchen",
                replacement,
                channel="audio",
                timeout=1.0,
            )

        self.assertEqual(
            registry.sessions["call-1"].metadata["media_client_id"],
            "closed-document",
        )
        self.assertEqual(
            registry.softphone_media["call-1"]["media_client_id"],
            "closed-document",
        )
        self.assertEqual(registry.sessions["call-1"].revision, 1)

    async def test_live_channel_blocks_other_document_identity_takeover(
        self,
    ) -> None:
        bucket = _MediaRuntime()
        registry = _CallRegistry("document-a")
        first = MediaWebSocketOwner(user_id="user-a", client_id="document-a")
        await async_claim_call_media_owner(
            bucket,
            registry,
            "call-1",
            "kitchen",
            first,
            channel="audio",
            timeout=1.0,
        )

        with self.assertRaises(WebSocketOwnerBusyError):
            await async_claim_call_media_owner(
                bucket,
                registry,
                "call-1",
                "kitchen",
                MediaWebSocketOwner(
                    user_id="user-a",
                    client_id="document-b",
                ),
                channel="video",
                timeout=1.0,
            )

        self.assertEqual(
            registry.sessions["call-1"].metadata["media_client_id"],
            "document-a",
        )
        self.assertEqual(bucket.get("video_ws_owners"), {})

    async def test_disconnected_local_leg_rebinds_before_owner_claim(self) -> None:
        bucket = _MediaRuntime()
        registry = _CallRegistry()
        snapshot = types.SimpleNamespace(
            caller_endpoint_id="office",
            callee_endpoint_id="kitchen",
            caller_media_owner_id="office-document",
            callee_media_owner_id="closed-kitchen-document",
        )
        rebinds: list[tuple[str, str, str]] = []
        bridge = types.SimpleNamespace(
            get_call=lambda _call_id: snapshot,
            rebind_media_owner=lambda call_id, endpoint_id, owner_id: rebinds.append(
                (call_id, endpoint_id, owner_id)
            ),
        )
        replacement = MediaWebSocketOwner(
            user_id="user-a",
            client_id="new-kitchen-document",
        )

        owners, _lock, owner_key = await async_claim_call_media_owner(
            bucket,
            registry,
            "call-1",
            "kitchen",
            replacement,
            channel="audio",
            timeout=1.0,
            pin_client_identity=False,
            local_bridge=bridge,
        )

        self.assertEqual(
            rebinds,
            [("call-1", "kitchen", "new-kitchen-document")],
        )
        self.assertIs(owners[owner_key], replacement)

    async def test_local_rebind_race_is_reported_as_busy_without_owner_leak(
        self,
    ) -> None:
        bucket = _MediaRuntime()
        registry = _CallRegistry()
        snapshot = types.SimpleNamespace(
            caller_endpoint_id="office",
            callee_endpoint_id="kitchen",
            caller_media_owner_id="office-document",
            callee_media_owner_id="closed-kitchen-document",
        )

        def _raise_ended(*_args) -> None:
            raise RuntimeError("call ended during local lease rebind")

        bridge = types.SimpleNamespace(
            get_call=lambda _call_id: snapshot,
            rebind_media_owner=_raise_ended,
        )

        with self.assertRaises(WebSocketOwnerBusyError):
            await async_claim_call_media_owner(
                bucket,
                registry,
                "call-1",
                "kitchen",
                MediaWebSocketOwner(
                    user_id="user-a",
                    client_id="replacement-document",
                ),
                channel="audio",
                timeout=1.0,
                pin_client_identity=False,
                local_bridge=bridge,
            )

        self.assertEqual(bucket.get("audio_ws_owners"), {})
        self.assertEqual(bucket.get("video_ws_owners"), {})

    async def test_local_lease_releases_only_after_last_media_channel(self) -> None:
        key = "kitchen|call-1"
        bucket = _MediaRuntime(
            {
                "audio_ws_owners": {key: object()},
                "video_ws_owners": {key: object()},
            }
        )
        releases: list[tuple[str, str, str]] = []
        bridge = types.SimpleNamespace(
            release_media=lambda call_id, endpoint_id, token: (
                releases.append((call_id, endpoint_id, token)) or True
            )
        )
        lease = types.SimpleNamespace(
            call_id="call-1",
            endpoint_id="kitchen",
            token="lease",
        )

        self.assertFalse(
            await async_release_local_media_if_unowned(bucket, bridge, lease)
        )
        bucket["audio_ws_owners"].clear()
        self.assertFalse(
            await async_release_local_media_if_unowned(bucket, bridge, lease)
        )
        bucket["video_ws_owners"].clear()
        self.assertTrue(
            await async_release_local_media_if_unowned(bucket, bridge, lease)
        )
        self.assertEqual(releases, [("call-1", "kitchen", "lease")])

    async def test_local_lease_release_failure_is_visible_in_debug_log(
        self,
    ) -> None:
        def fail_release(_call_id: str, _endpoint_id: str, _token: str) -> None:
            raise RuntimeError("call ended before release")

        bridge = types.SimpleNamespace(release_media=fail_release)
        lease = types.SimpleNamespace(
            call_id="call-1",
            endpoint_id="kitchen",
            token="lease",
        )

        with self.assertLogs(OWNER_MODULE._LOGGER.name, level="DEBUG") as logs:
            released = await async_release_local_media_if_unowned(
                _MediaRuntime(),
                bridge,
                lease,
            )

        self.assertFalse(released)
        self.assertIn("call_id=call-1 endpoint_id=kitchen", "\n".join(logs.output))

    async def test_reconnect_revokes_then_waits_for_previous_teardown(self) -> None:
        owners: dict[str, object] = {}
        lock = asyncio.Lock()
        previous_ws = _FakeWebSocket()
        previous_transport = _FakeTransport()
        previous = MediaWebSocketOwner(
            websocket=previous_ws,
            transport=previous_transport,  # type: ignore[arg-type]
            user_id="user-a",
            client_id="tab-a",
        )
        owners["call-1"] = previous
        replacement = MediaWebSocketOwner(user_id="user-a", client_id="tab-a")

        claim = asyncio.create_task(
            async_claim_media_owner(
                owners,
                lock,
                "call-1",
                replacement,
                timeout=1.0,
            )
        )
        await asyncio.sleep(0)

        self.assertEqual(previous_ws.force_close_calls, 1)
        self.assertEqual(previous_transport.close_calls, 1)
        self.assertFalse(claim.done())
        await async_release_media_owner(owners, lock, "call-1", previous)
        await claim
        self.assertIs(owners["call-1"], replacement)

    async def test_different_user_cannot_revoke_or_claim_active_media(self) -> None:
        owners: dict[str, object] = {}
        lock = asyncio.Lock()
        previous_ws = _FakeWebSocket()
        previous_transport = _FakeTransport()
        previous = MediaWebSocketOwner(
            websocket=previous_ws,
            transport=previous_transport,  # type: ignore[arg-type]
            user_id="user-a",
            client_id="tab-a",
        )
        owners["private-call"] = previous

        with self.assertRaises(WebSocketOwnerBusyError):
            await async_claim_media_owner(
                owners,
                lock,
                "private-call",
                MediaWebSocketOwner(user_id="user-b", client_id="tab-b"),
                timeout=1.0,
            )

        self.assertIs(owners["private-call"], previous)
        self.assertEqual(previous_ws.force_close_calls, 0)
        self.assertEqual(previous_transport.close_calls, 0)
        self.assertFalse(previous.handoff_requested.is_set())

    async def test_second_tab_of_same_user_cannot_preempt_active_media(self) -> None:
        owners: dict[str, object] = {}
        lock = asyncio.Lock()
        previous_ws = _FakeWebSocket()
        previous = MediaWebSocketOwner(
            websocket=previous_ws,
            user_id="user-a",
            client_id="tab-a",
        )
        owners["private-call"] = previous

        with self.assertRaises(WebSocketOwnerBusyError):
            await async_claim_media_owner(
                owners,
                lock,
                "private-call",
                MediaWebSocketOwner(user_id="user-a", client_id="tab-b"),
                timeout=1.0,
            )

        self.assertIs(owners["private-call"], previous)
        self.assertEqual(previous_ws.force_close_calls, 0)
        self.assertFalse(previous.handoff_requested.is_set())

    async def test_late_old_teardown_cannot_remove_replacement(self) -> None:
        owners: dict[str, object] = {}
        lock = asyncio.Lock()
        replacement = MediaWebSocketOwner()
        owners["call-2"] = replacement
        stale = MediaWebSocketOwner()

        await async_release_media_owner(owners, lock, "call-2", stale)

        self.assertIs(owners["call-2"], replacement)
        self.assertTrue(stale.released.is_set())

    async def test_repeated_cancellation_cannot_interrupt_owner_release(self) -> None:
        owners: dict[str, object] = {}
        lock = asyncio.Lock()
        owner = MediaWebSocketOwner()
        owners["call-cancel"] = owner

        await lock.acquire()
        release = asyncio.create_task(
            async_release_media_owner(owners, lock, "call-cancel", owner)
        )
        await asyncio.sleep(0)
        release.cancel()
        await asyncio.sleep(0)
        release.cancel()
        await asyncio.sleep(0)
        self.assertFalse(release.done())
        self.assertIs(owners["call-cancel"], owner)
        self.assertFalse(owner.released.is_set())

        lock.release()
        with self.assertRaises(asyncio.CancelledError):
            await release

        self.assertEqual(owners, {})
        self.assertTrue(owner.released.is_set())

    async def test_timeout_preserves_previous_owner(self) -> None:
        owners: dict[str, object] = {}
        lock = asyncio.Lock()
        previous = MediaWebSocketOwner(
            websocket=_FakeWebSocket(),
            transport=_FakeTransport(),  # type: ignore[arg-type]
        )
        owners["call-3"] = previous

        with self.assertRaises(WebSocketOwnerBusyError):
            await async_claim_media_owner(
                owners,
                lock,
                "call-3",
                MediaWebSocketOwner(),
                timeout=0.001,
            )

        self.assertIs(owners["call-3"], previous)

    async def test_teardown_failures_do_not_prevent_handoff(self) -> None:
        owners: dict[str, object] = {}
        lock = asyncio.Lock()
        previous = MediaWebSocketOwner(
            websocket=_FakeWebSocket(fail=True),
            transport=_FakeTransport(fail=True),  # type: ignore[arg-type]
        )
        owners["call-4"] = previous
        replacement = MediaWebSocketOwner()
        claim = asyncio.create_task(
            async_claim_media_owner(
                owners,
                lock,
                "call-4",
                replacement,
                timeout=1.0,
            )
        )
        await asyncio.sleep(0)
        await async_release_media_owner(owners, lock, "call-4", previous)
        await claim

        self.assertIs(owners["call-4"], replacement)

    async def test_shutdown_revokes_all_owners_and_waits_for_release(self) -> None:
        owners: dict[str, object] = {}
        lock = asyncio.Lock()
        first = MediaWebSocketOwner(websocket=_FakeWebSocket())
        second = MediaWebSocketOwner(websocket=_FakeWebSocket())
        owners.update({"call-a": first, "call-b": second})

        shutdown = asyncio.create_task(
            async_revoke_media_owners(owners, lock, timeout=1.0)
        )
        await asyncio.sleep(0)

        self.assertTrue(first.handoff_requested.is_set())
        self.assertTrue(second.handoff_requested.is_set())
        self.assertFalse(shutdown.done())
        await async_release_media_owner(owners, lock, "call-a", first)
        await async_release_media_owner(owners, lock, "call-b", second)
        self.assertEqual(await shutdown, set())
        self.assertEqual(owners, {})

    async def test_shutdown_timeout_keeps_owner_mapping_fail_closed(self) -> None:
        owners: dict[str, object] = {}
        lock = asyncio.Lock()
        owner = MediaWebSocketOwner(websocket=_FakeWebSocket())
        owners["stuck"] = owner

        pending = await async_revoke_media_owners(owners, lock, timeout=0.001)

        self.assertEqual(pending, {"stuck"})
        self.assertIs(owners["stuck"], owner)

    async def test_shutdown_gate_prevents_waiting_reconnect_from_claiming(self) -> None:
        owners: dict[str, object] = {}
        lock = asyncio.Lock()
        shutdown = asyncio.Event()
        previous = MediaWebSocketOwner(websocket=_FakeWebSocket())
        owners["call-race"] = previous
        replacement = MediaWebSocketOwner()
        claim = asyncio.create_task(
            async_claim_media_owner(
                owners,
                lock,
                "call-race",
                replacement,
                timeout=1.0,
                shutdown_event=shutdown,
            )
        )
        await asyncio.sleep(0)
        self.assertTrue(previous.handoff_requested.is_set())

        shutdown.set()
        await async_release_media_owner(owners, lock, "call-race", previous)
        with self.assertRaises(WebSocketOwnerBusyError):
            await claim

        self.assertEqual(owners, {})
        self.assertFalse(replacement.released.is_set())

    async def test_shutdown_gate_rejects_new_owner_before_lookup(self) -> None:
        owners: dict[str, object] = {}
        shutdown = asyncio.Event()
        shutdown.set()

        with self.assertRaises(WebSocketOwnerBusyError):
            await async_claim_media_owner(
                owners,
                asyncio.Lock(),
                "call-new",
                MediaWebSocketOwner(),
                timeout=1.0,
                shutdown_event=shutdown,
            )

        self.assertEqual(owners, {})


if __name__ == "__main__":
    unittest.main()
