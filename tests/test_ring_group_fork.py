"""Behavioral tests for ring-group fork adapters."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "ring_group_fork.py"
PACKAGE = "voip_stack_ring_group_fork_test"


def _load_module():
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(MODULE.parent)]
    sys.modules[PACKAGE] = package

    # Use the actual pure fork types and reducer; only transport adapters below
    # remain controlled boundaries for this software test.
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.dial_fork", MODULE.with_name("dial_fork.py"))
    assert spec is not None and spec.loader is not None
    dial_fork = importlib.util.module_from_spec(spec)
    sys.modules[dial_fork.__name__] = dial_fork
    spec.loader.exec_module(dial_fork)

    close_calls: list[tuple[object, bool]] = []

    async def close_outbound_leg(leg, *, bye_or_cancel=False):
        close_calls.append((leg, bye_or_cancel))

    outbound = types.ModuleType(f"{PACKAGE}.outbound_attempts")
    outbound.BrowserLeg = object
    outbound.OutboundLeg = object
    outbound.async_close_outbound_leg = close_outbound_leg
    sys.modules[outbound.__name__] = outbound

    candidates = types.ModuleType(f"{PACKAGE}.group_candidates")
    candidates.PreflightFailure = tuple
    sys.modules[candidates.__name__] = candidates

    module_name = f"{PACKAGE}.ring_group_fork"
    spec = importlib.util.spec_from_file_location(module_name, MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module, close_calls


ring_group_fork, CLOSE_CALLS = _load_module()
_Disposition = ring_group_fork.DialDisposition
_Outcome = ring_group_fork.DialOutcome
_CloseMode = ring_group_fork.LegCloseMode
_Candidate = sys.modules[f"{PACKAGE}.dial_fork"].DialCandidate


class RingGroupForkTest(unittest.IsolatedAsyncioTestCase):
    def test_account_offer_keeps_pending_and_timeout_semantics(self):
        for results, pending, expected in [
            ([], True, "timeout"), (["busy"], True, "timeout"),
            (["busy", "timeout"], False, "timeout"), (["dnd", "timeout"], False, "timeout"),
            (["busy", "dnd"], False, "busy"), (["declined"], False, "declined"),
            (["media_incompatible"], False, "media_incompatible"),
            ([], False, "transport_unreachable"),
        ]:
            with self.subTest(results=results, pending=pending):
                self.assertEqual(ring_group_fork.account_offer_outcome(results, pending=pending), expected)

    async def test_child_timeout_or_cancellation_preserves_the_call_owned_decision(self) -> None:
        for timeout in (.01, None):
            with self.subTest(timeout=timeout):
                future = asyncio.get_running_loop().create_future()
                fork, _payloads, _decision = ring_group_fork.build_ring_group_fork(
                    sip_port=5060, route_future=future, attempts=[],
                    browser_legs=[SimpleNamespace(endpoint_id="kitchen")], preflight_failures=[],
                    ring_timeout=timeout,
                )
                task = asyncio.create_task(fork[0].dial())
                if timeout is None:
                    await asyncio.sleep(0)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    self.assertIs((await task).disposition, _Disposition.TIMEOUT)
                self.assertFalse(future.done())
                future.set_result({"action": "answer_ha", "endpoint_id": "kitchen"})
                self.assertIs((await fork[0].dial()).disposition, _Disposition.ANSWERED)

    async def test_browser_answer_selects_the_requested_endpoint(self) -> None:
        future = asyncio.get_running_loop().create_future()
        browser_leg = SimpleNamespace(endpoint_id="kitchen")
        fork, payloads, decision = ring_group_fork.build_ring_group_fork(
            sip_port=5060,
            route_future=future,
            attempts=[],
            browser_legs=[browser_leg],
            preflight_failures=[],
        )

        future.set_result(
            {"action": "answer_ha", "endpoint_id": "kitchen"}
        )
        outcome = await fork[0].dial()

        self.assertEqual(fork[0].candidate_id, "browser:route-control")
        self.assertIs(outcome.disposition, _Disposition.ANSWERED)
        self.assertIs(payloads["browser:route-control"], browser_leg)
        self.assertEqual(decision["endpoint_id"], "kitchen")

    async def test_source_cancel_remains_a_control_outcome(self) -> None:
        future = asyncio.get_running_loop().create_future()
        fork, _payloads, _decision = ring_group_fork.build_ring_group_fork(
            sip_port=5060,
            route_future=future,
            attempts=[],
            browser_legs=[],
            preflight_failures=[
                ("busy", "ws3", _Disposition.BUSY, 0, 0)
            ],
        )

        busy = await fork[0].dial()
        future.set_result({"action": "cancel"})
        cancelled = await fork[1].dial()

        self.assertIs(busy.disposition, _Disposition.BUSY)
        self.assertEqual(fork[1].candidate_id, "caller:route-control")
        self.assertIs(
            cancelled.disposition,
            _Disposition.SOURCE_CANCELLED,
        )

    async def test_sip_answer_and_loser_cleanup_preserve_signaling(self) -> None:
        CLOSE_CALLS.clear()
        future = asyncio.get_running_loop().create_future()

        class Client:
            dialog = object()
            dialog_ids = SimpleNamespace(call_id="branch-1")

            async def invite(self, **kwargs):
                self.invite_kwargs = kwargs
                return "ringing"

            async def wait_for_final(self, *, timeout):
                self.final_timeout = timeout
                return "in_call"

        client = Client()
        leg = SimpleNamespace(
            candidate_id="sip:ws3",
            client=client,
            uri=SimpleNamespace(
                user="427",
                host="phone.local",
                port=0,
                scheme="sip",
                __str__=lambda _self: "sip:427@phone.local",
            ),
            member="WS3",
            tier=1,
            order=2,
            endpoint_id="ws3",
        )
        fork, payloads, _decision = ring_group_fork.build_ring_group_fork(
            sip_port=5070,
            route_future=future,
            attempts=[leg],
            browser_legs=[],
            preflight_failures=[],
        )

        outcome = await fork[0].dial()
        await fork[0].close(_CloseMode.CANCEL_OR_BYE)

        self.assertIs(outcome.disposition, _Disposition.ANSWERED)
        self.assertEqual(client.invite_kwargs["remote_sip_port"], 5060)
        self.assertIsNone(client.final_timeout)
        self.assertIs(payloads["sip:ws3"], leg)
        self.assertEqual(CLOSE_CALLS, [(leg, True)])

    async def test_parallel_sip_ringing_is_published_once(self) -> None:
        future = asyncio.get_running_loop().create_future()
        published: list[str] = []

        class Client:
            dialog = None

            def __init__(self, call_id: str) -> None:
                self.dialog_ids = SimpleNamespace(call_id=call_id)

            async def invite(self, **_kwargs):
                return "ringing"

            async def wait_for_final(self, *, timeout):
                self.final_timeout = timeout
                return "busy"

        def leg(call_id: str):
            return SimpleNamespace(
                candidate_id=f"sip:{call_id}",
                client=Client(call_id),
                uri=SimpleNamespace(
                    user=call_id,
                    host="phone.local",
                    port=5060,
                ),
                member=call_id,
                tier=0,
                order=0,
                endpoint_id=call_id,
            )

        fork, _payloads, _decision = ring_group_fork.build_ring_group_fork(
            sip_port=5060,
            route_future=future,
            attempts=[leg("one"), leg("two")],
            browser_legs=[],
            preflight_failures=[],
            on_ringing=lambda: published.append("ringing"),
        )

        first, second = await asyncio.gather(fork[0].dial(), fork[1].dial())

        self.assertIs(first.disposition, _Disposition.BUSY)
        self.assertIs(second.disposition, _Disposition.BUSY)
        self.assertEqual(published, ["ringing"])
