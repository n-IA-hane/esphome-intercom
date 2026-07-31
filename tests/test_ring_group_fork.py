"""Behavioral tests for ring-group fork adapters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "ring_group_fork.py"
PACKAGE = "voip_stack_ring_group_fork_test"


class _Disposition(StrEnum):
    ANSWERED = "answered"
    BUSY = "busy"
    DND = "dnd"
    DECLINED = "declined"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    MEDIA_INCOMPATIBLE = "media_incompatible"
    AUTH_FAILED = "auth_failed"
    CANCELLED = "cancelled"
    SOURCE_CANCELLED = "source_cancelled"
    REROUTE = "reroute"
    PROTOCOL_ERROR = "protocol_error"


class _CloseMode(StrEnum):
    CANCEL_OR_BYE = "cancel_or_bye"
    BYE = "bye"
    CLOSE = "close"


@dataclass(frozen=True, slots=True)
class _Outcome:
    disposition: _Disposition
    status: int = 0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class _Candidate:
    candidate_id: str
    dial: object
    close: object
    tier: int = 0
    order: int = 0
    endpoint_id: str = ""
    control: bool = False


def _load_module():
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(MODULE.parent)]
    sys.modules[PACKAGE] = package

    dial_fork = types.ModuleType(f"{PACKAGE}.dial_fork")
    dial_fork.DialCandidate = _Candidate
    dial_fork.DialDisposition = _Disposition
    dial_fork.DialOutcome = _Outcome
    dial_fork.LegCloseMode = _CloseMode
    sys.modules[dial_fork.__name__] = dial_fork

    close_calls: list[tuple[object, bool]] = []

    async def close_outbound_leg(leg, *, bye_or_cancel=False):
        close_calls.append((leg, bye_or_cancel))

    outbound = types.ModuleType(f"{PACKAGE}.outbound_attempts")
    outbound.BrowserLeg = object
    outbound.OutboundLeg = object
    outbound.async_close_outbound_leg = close_outbound_leg
    sys.modules[outbound.__name__] = outbound

    candidates = types.ModuleType(f"{PACKAGE}.ring_group_candidates")
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


class RingGroupForkTest(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(client.invite_kwargs["remote_sip_port"], 5070)
        self.assertEqual(client.final_timeout, 30.0)
        self.assertIs(payloads["sip:ws3"], leg)
        self.assertEqual(CLOSE_CALLS, [(leg, True)])
