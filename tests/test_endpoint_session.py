#!/usr/bin/env python3
"""Executable lifecycle contract for the explicit PBX call session."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


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


_load_module("session_cleanup")
endpoint_session = _load_module("endpoint_session")


class EndpointCallSessionTest(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_signaling_precedes_resource_cleanup_once(self) -> None:
        events: list[str] = []

        async def signal(call_id, intent) -> None:
            events.append(f"signal:{call_id}:{intent.reason}")

        session = endpoint_session.EndpointCallSession(
            "call-1",
            1,
            termination_signaler=signal,
        )
        session.add_resource(
            "relay",
            object(),
            lambda reason: events.append(f"relay:{reason}"),
        )

        first = session.start_termination(
            endpoint_session.TerminationIntent("local_hangup")
        )
        second = session.start_termination(
            endpoint_session.TerminationIntent("duplicate")
        )
        self.assertIs(first, second)
        await first

        self.assertEqual(
            events,
            ["signal:call-1:local_hangup", "relay:local_hangup"],
        )

    async def test_termination_intent_derives_one_public_state_policy(self) -> None:
        self.assertEqual(
            endpoint_session.TerminationIntent("remote_hangup").public_state,
            "idle",
        )
        self.assertEqual(
            endpoint_session.TerminationIntent("busy").public_state,
            "busy",
        )
        self.assertEqual(
            endpoint_session.TerminationIntent("protocol_error").public_state,
            "transport_unreachable",
        )

    async def test_termination_claim_precedes_legacy_handoff_and_cleanup(self) -> None:
        events: list[str] = []
        session = endpoint_session.EndpointCallSession("call-1", 1)
        session.add_resource(
            "relay",
            object(),
            lambda reason: events.append(f"relay:{reason}"),
            stage=endpoint_session.CleanupStage.MEDIA,
        )

        self.assertTrue(
            session.claim_termination(
                endpoint_session.TerminationIntent("remote_hangup")
            )
        )
        self.assertFalse(
            session.claim_termination(endpoint_session.TerminationIntent("duplicate"))
        )
        self.assertIs(session.phase, endpoint_session.SessionPhase.TERMINATING)
        self.assertEqual(session.terminal_reason, "remote_hangup")
        self.assertEqual(events, [])

        result = await session.start_termination(
            endpoint_session.TerminationIntent("duplicate")
        )

        self.assertEqual(result.reason, "remote_hangup")
        self.assertEqual(events, ["relay:remote_hangup"])

    async def test_start_termination_is_synchronous_before_cleanup_runs(self) -> None:
        gate = asyncio.Event()
        session = endpoint_session.EndpointCallSession("call-1", 1)
        session.add_resource("blocked", object(), lambda _reason: gate.wait())

        cleanup = session.start_termination(
            endpoint_session.TerminationIntent("cancelled")
        )

        self.assertIs(session.phase, endpoint_session.SessionPhase.TERMINATING)
        self.assertEqual(session.terminal_reason, "cancelled")
        self.assertTrue(session.termination_started.is_set())
        self.assertFalse(cleanup.done())
        gate.set()
        await cleanup

    async def test_public_idle_is_published_only_after_cleanup_barrier(self) -> None:
        gate = asyncio.Event()
        session = endpoint_session.EndpointCallSession(
            "call-1",
            1,
            phase=endpoint_session.SessionPhase.ESTABLISHED,
        )
        session.apply_observation("in_call", endpoint_session.SessionPhase.ESTABLISHED)
        session.add_resource("blocked", object(), lambda _reason: gate.wait())

        cleanup = session.start_termination(
            endpoint_session.TerminationIntent(
                "local_hangup",
                initiator=endpoint_session.TerminationInitiator.LOCAL_USER,
            )
        )
        await asyncio.sleep(0)

        self.assertEqual(session.state, "in_call")
        self.assertIs(session.phase, endpoint_session.SessionPhase.TERMINATING)
        gate.set()
        await cleanup
        self.assertEqual(session.state, "idle")
        self.assertIs(session.phase, endpoint_session.SessionPhase.TERMINATED)

    async def test_terminal_projection_failure_cannot_break_cleanup_barrier(self) -> None:
        events: list[str] = []

        def retire(_session, _result) -> None:
            events.append("retired")

        async def broken_projection(_session, _intent) -> None:
            events.append("project")
            raise RuntimeError("projection failed")

        session = endpoint_session.EndpointCallSession(
            "call-1",
            1,
            termination_observer=broken_projection,
            on_terminated=retire,
        )
        session.add_resource(
            "relay",
            object(),
            lambda reason: events.append(f"relay:{reason}"),
        )

        result = await session.terminate(
            endpoint_session.TerminationIntent("remote_hangup")
        )

        self.assertEqual(
            events,
            ["relay:remote_hangup", "retired", "project"],
        )
        self.assertTrue(session.terminated.is_set())
        self.assertEqual(result.errors, ("observer:RuntimeError",))

    async def test_teardown_order_is_media_legs_then_reservations(self) -> None:
        events: list[str] = []
        session = endpoint_session.EndpointCallSession("call-1", 1)
        session.add_resource(
            "reservation",
            object(),
            lambda reason: events.append(f"reservation:{reason}"),
            stage=endpoint_session.CleanupStage.RESERVATION,
        )
        session.add_leg(
            endpoint_session.CallLeg(
                "callee",
                endpoint_session.LegKind.SIP,
                closer=lambda reason: events.append(f"leg:{reason}"),
            )
        )
        session.add_resource(
            "relay",
            object(),
            lambda reason: events.append(f"relay:{reason}"),
            stage=endpoint_session.CleanupStage.MEDIA,
        )

        result = await session.terminate(
            endpoint_session.TerminationIntent("cancelled")
        )

        self.assertEqual(
            events,
            ["relay:cancelled", "leg:cancelled", "reservation:cancelled"],
        )
        self.assertEqual(result.closed_legs, ("callee",))
        self.assertEqual(result.closed_resources, ("relay", "reservation"))
        self.assertEqual(session.phase, endpoint_session.SessionPhase.TERMINATED)

    async def test_terminate_is_idempotent_for_concurrent_observers(self) -> None:
        calls = 0
        entered = asyncio.Event()
        release = asyncio.Event()

        async def close(_reason: str) -> None:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()

        session = endpoint_session.EndpointCallSession("call-1", 1)
        session.add_leg(
            endpoint_session.CallLeg(
                "callee", endpoint_session.LegKind.SIP, closer=close
            )
        )
        first = asyncio.create_task(
            session.terminate(endpoint_session.TerminationIntent("remote_hangup"))
        )
        await entered.wait()
        second = asyncio.create_task(
            session.terminate(endpoint_session.TerminationIntent("duplicate"))
        )
        release.set()

        first_result, second_result = await asyncio.gather(first, second)

        self.assertIs(first_result, second_result)
        self.assertEqual(first_result.reason, "remote_hangup")
        self.assertEqual(calls, 1)

    async def test_repeated_caller_cancellation_cannot_break_cleanup(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()

        async def close(_reason: str) -> None:
            entered.set()
            await release.wait()
            finished.set()

        session = endpoint_session.EndpointCallSession("call-1", 1)
        session.add_resource(
            "relay",
            object(),
            close,
            stage=endpoint_session.CleanupStage.MEDIA,
        )
        waiter = asyncio.create_task(
            session.terminate(endpoint_session.TerminationIntent("cancelled"))
        )
        await entered.wait()
        waiter.cancel()
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())

        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertTrue(finished.is_set())
        self.assertEqual(session.phase, endpoint_session.SessionPhase.TERMINATED)

    async def test_owned_tasks_are_cancelled_before_media_cleanup(self) -> None:
        events: list[str] = []
        started = asyncio.Event()

        async def watcher() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append("watcher.cancelled")
                raise

        session = endpoint_session.EndpointCallSession("call-1", 1)
        session.create_task(watcher(), name="watcher")
        session.add_resource(
            "relay",
            object(),
            lambda _reason: events.append("relay.closed"),
            stage=endpoint_session.CleanupStage.MEDIA,
        )
        await started.wait()

        await session.terminate(endpoint_session.TerminationIntent("local_hangup"))

        self.assertEqual(events, ["watcher.cancelled", "relay.closed"])

    async def test_cleanup_failure_does_not_skip_remaining_owners(self) -> None:
        events: list[str] = []

        async def broken(_reason: str) -> None:
            events.append("broken")
            raise OSError("boom")

        session = endpoint_session.EndpointCallSession("call-1", 1)
        session.add_resource(
            "broken",
            object(),
            broken,
            stage=endpoint_session.CleanupStage.MEDIA,
        )
        session.add_leg(
            endpoint_session.CallLeg(
                "callee",
                endpoint_session.LegKind.SIP,
                closer=lambda _reason: events.append("leg"),
            )
        )

        result = await session.terminate(
            endpoint_session.TerminationIntent("protocol_error")
        )

        self.assertEqual(events, ["broken", "leg"])
        self.assertEqual(result.errors, ("resource:broken:OSError",))

    async def test_leg_stage_resource_is_closed_with_the_leg_barrier(self) -> None:
        events: list[str] = []
        session = endpoint_session.EndpointCallSession("call-1", 1)
        session.add_resource(
            "leg-adjacent",
            object(),
            lambda reason: events.append(f"resource:{reason}"),
            stage=endpoint_session.CleanupStage.LEG,
        )

        result = await session.terminate(
            endpoint_session.TerminationIntent("remote_hangup")
        )

        self.assertEqual(events, ["resource:remote_hangup"])
        self.assertEqual(result.closed_resources, ("leg-adjacent",))

    async def test_terminal_adapter_can_transfer_before_cleanup_barrier(self) -> None:
        session = endpoint_session.EndpointCallSession("call-1", 1)
        relay = object()
        session.add_resource("relay", relay, lambda _reason: None)

        self.assertTrue(
            session.claim_termination(
                endpoint_session.TerminationIntent("remote_hangup")
            )
        )
        transferred = session.release_resource("relay", value=relay)

        self.assertIs(transferred.value, relay)
        cleanup = session.start_termination(
            endpoint_session.TerminationIntent("remote_hangup")
        )
        with self.assertRaisesRegex(RuntimeError, "cleanup has started"):
            session.release_resource("relay")
        await cleanup

    def test_generation_token_rejects_stale_owner(self) -> None:
        session = endpoint_session.EndpointCallSession("call-1", 3)

        self.assertTrue(session.owns(endpoint_session.CallToken("call-1", 3)))
        self.assertFalse(session.owns(endpoint_session.CallToken("call-1", 2)))


if __name__ == "__main__":
    unittest.main()
