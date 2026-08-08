#!/usr/bin/env python3
"""Behavioral tests for the shared PBX outbound fork."""

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
dial_fork = _load_module("dial_fork")


class FakeCandidate:
    def __init__(
        self,
        candidate_id: str,
        outcome,
        *,
        tier: int = 0,
        order: int = 0,
        dial_gate: asyncio.Event | None = None,
        close_gate: asyncio.Event | None = None,
    ) -> None:
        self.candidate_id = candidate_id
        self.outcome = outcome
        self.dial_gate = dial_gate
        self.close_gate = close_gate
        self.close_started = asyncio.Event()
        self.closes: list[object] = []
        self.candidate = dial_fork.DialCandidate(
            candidate_id,
            self.dial,
            self.close,
            tier=tier,
            order=order,
        )

    async def dial(self):
        if self.dial_gate is not None:
            await self.dial_gate.wait()
        return self.outcome

    async def close(self, mode) -> None:
        self.closes.append(mode)
        self.close_started.set()
        if self.close_gate is not None:
            await self.close_gate.wait()


class DialForkControllerTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_dnd_preserves_dnd_instead_of_unreachable(self) -> None:
        session = endpoint_session.EndpointCallSession("call-1", 1)
        first = FakeCandidate(
            "first",
            dial_fork.DialOutcome(dial_fork.DialDisposition.DND),
        )
        second = FakeCandidate(
            "second",
            dial_fork.DialOutcome(dial_fork.DialDisposition.DND),
            order=1,
        )

        result = await dial_fork.DialForkController(
            session,
            (first.candidate, second.candidate),
        ).run(lambda _candidate, _outcome: True)

        self.assertIs(result.outcome.disposition, dial_fork.DialDisposition.DND)
        self.assertEqual(result.outcome.status, 486)

    async def test_dnd_member_does_not_block_available_group_winner(self) -> None:
        session = endpoint_session.EndpointCallSession("call-1", 1)
        dnd = FakeCandidate(
            "dnd",
            dial_fork.DialOutcome(dial_fork.DialDisposition.DND),
        )
        available = FakeCandidate(
            "available",
            dial_fork.DialOutcome(dial_fork.DialDisposition.ANSWERED, 200),
            order=1,
        )

        result = await dial_fork.DialForkController(
            session,
            (dnd.candidate, available.candidate),
        ).run(lambda _candidate, _outcome: True)

        self.assertIs(result.winner, available.candidate)

    async def test_busy_takes_precedence_over_dnd_for_failed_group(self) -> None:
        session = endpoint_session.EndpointCallSession("call-1", 1)
        dnd = FakeCandidate(
            "dnd",
            dial_fork.DialOutcome(dial_fork.DialDisposition.DND),
        )
        busy = FakeCandidate(
            "busy",
            dial_fork.DialOutcome(dial_fork.DialDisposition.BUSY),
            order=1,
        )

        result = await dial_fork.DialForkController(
            session,
            (dnd.candidate, busy.candidate),
        ).run(lambda _candidate, _outcome: True)

        self.assertIs(result.outcome.disposition, dial_fork.DialDisposition.BUSY)

    async def test_parallel_first_answer_wins_and_losers_cancel(self) -> None:
        session = endpoint_session.EndpointCallSession("call-1", 1)
        answer = dial_fork.DialOutcome(dial_fork.DialDisposition.ANSWERED, 200)
        busy = dial_fork.DialOutcome(dial_fork.DialDisposition.BUSY, 486)
        winner = FakeCandidate("winner", answer, order=0)
        loser = FakeCandidate("loser", busy, order=1)
        committed: list[str] = []

        result = await dial_fork.DialForkController(
            session,
            (winner.candidate, loser.candidate),
        ).run(lambda candidate, _outcome: not committed.append(candidate.candidate_id))

        self.assertIs(result.winner, winner.candidate)
        self.assertEqual(committed, ["winner"])
        self.assertEqual(loser.closes, [dial_fork.LegCloseMode.CANCEL_OR_BYE])
        self.assertEqual(winner.closes, [])

    async def test_simultaneous_answers_use_configured_order(self) -> None:
        session = endpoint_session.EndpointCallSession("call-1", 1)
        answer = dial_fork.DialOutcome(dial_fork.DialDisposition.ANSWERED, 200)
        second = FakeCandidate("second", answer, order=2)
        first = FakeCandidate("first", answer, order=1)

        result = await dial_fork.DialForkController(
            session,
            (second.candidate, first.candidate),
        ).run(lambda _candidate, _outcome: True)

        self.assertEqual(result.winner.candidate_id, "first")

    async def test_source_cancel_wins_over_simultaneous_answer(self) -> None:
        session = endpoint_session.EndpointCallSession("call-1", 1)
        answer = FakeCandidate(
            "answer",
            dial_fork.DialOutcome(dial_fork.DialDisposition.ANSWERED, 200),
            order=0,
        )
        source_cancel = FakeCandidate(
            "source-control",
            dial_fork.DialOutcome(
                dial_fork.DialDisposition.SOURCE_CANCELLED,
                487,
            ),
            order=1,
        )

        result = await dial_fork.DialForkController(
            session,
            (answer.candidate, source_cancel.candidate),
        ).run(lambda _candidate, _outcome: True)

        self.assertIsNone(result.winner)
        self.assertIs(
            result.outcome.disposition,
            dial_fork.DialDisposition.SOURCE_CANCELLED,
        )
        self.assertEqual(
            answer.closes,
            [dial_fork.LegCloseMode.CANCEL_OR_BYE],
        )

    async def test_reroute_is_a_control_result_not_a_dial_failure(self) -> None:
        session = endpoint_session.EndpointCallSession("call-1", 1)
        reroute = FakeCandidate(
            "route-control",
            dial_fork.DialOutcome(dial_fork.DialDisposition.REROUTE),
        )

        result = await dial_fork.DialForkController(
            session,
            (reroute.candidate,),
        ).run(lambda _candidate, _outcome: True)

        self.assertIsNone(result.winner)
        self.assertIs(
            result.outcome.disposition,
            dial_fork.DialDisposition.REROUTE,
        )

    async def test_control_waiter_does_not_block_sequential_branches(self) -> None:
        session = endpoint_session.EndpointCallSession("call-1", 1)
        events: list[str] = []
        control_gate = asyncio.Event()

        async def dial(candidate_id: str, disposition):
            events.append(candidate_id)
            return dial_fork.DialOutcome(disposition)

        async def wait_control():
            await control_gate.wait()
            return dial_fork.DialOutcome(dial_fork.DialDisposition.SOURCE_CANCELLED)

        async def close(_mode) -> None:
            return None

        candidates = (
            dial_fork.DialCandidate(
                "first",
                lambda: dial("first", dial_fork.DialDisposition.BUSY),
                close,
                order=0,
            ),
            dial_fork.DialCandidate(
                "second",
                lambda: dial("second", dial_fork.DialDisposition.ANSWERED),
                close,
                order=1,
            ),
            dial_fork.DialCandidate(
                "control",
                wait_control,
                close,
                order=-1,
                control=True,
            ),
        )

        result = await dial_fork.DialForkController(
            session,
            candidates,
            strategy=dial_fork.ForkStrategy.SEQUENTIAL,
        ).run(lambda _candidate, _outcome: True)

        self.assertEqual(events, ["first", "second"])
        self.assertEqual(result.winner.candidate_id, "second")

    async def test_answered_control_wins_over_pending_media_branch(self) -> None:
        session = endpoint_session.EndpointCallSession("call-1", 1)
        branch_started = asyncio.Event()
        keep_branch_pending = asyncio.Event()
        closed: list[tuple[str, object]] = []
        committed: list[str] = []

        async def dial_branch():
            branch_started.set()
            await keep_branch_pending.wait()
            return dial_fork.DialOutcome(dial_fork.DialDisposition.BUSY)

        async def answer_browser():
            await branch_started.wait()
            return dial_fork.DialOutcome(
                dial_fork.DialDisposition.ANSWERED,
                200,
            )

        async def close(candidate_id: str, mode) -> None:
            closed.append((candidate_id, mode))

        branch = dial_fork.DialCandidate(
            "sip-branch",
            dial_branch,
            lambda mode: close("sip-branch", mode),
        )
        browser = dial_fork.DialCandidate(
            "browser-control",
            answer_browser,
            lambda mode: close("browser-control", mode),
            order=-1,
            control=True,
        )

        result = await dial_fork.DialForkController(
            session,
            (branch, browser),
        ).run(lambda candidate, _outcome: not committed.append(candidate.candidate_id))

        self.assertIs(result.winner, browser)
        self.assertEqual(committed, ["browser-control"])
        self.assertIn(
            ("sip-branch", dial_fork.LegCloseMode.CANCEL_OR_BYE),
            closed,
        )
        self.assertNotIn(
            ("browser-control", dial_fork.LegCloseMode.CLOSE),
            closed,
        )

    async def test_failed_media_branches_do_not_cancel_ringing_browser(self) -> None:
        session = endpoint_session.EndpointCallSession("call-1", 1)
        browser_answer = asyncio.Event()

        async def busy_branch():
            return dial_fork.DialOutcome(
                dial_fork.DialDisposition.BUSY,
                486,
            )

        async def answer_browser():
            await browser_answer.wait()
            return dial_fork.DialOutcome(
                dial_fork.DialDisposition.ANSWERED,
                200,
            )

        async def close(_mode) -> None:
            return None

        branch = dial_fork.DialCandidate(
            "busy-sip-phone",
            busy_branch,
            close,
        )
        browser = dial_fork.DialCandidate(
            "ringing-browser",
            answer_browser,
            close,
            control=True,
        )
        running = asyncio.create_task(
            dial_fork.DialForkController(
                session,
                (branch, browser),
            ).run(lambda _candidate, _outcome: True)
        )

        await asyncio.sleep(0.01)
        self.assertFalse(running.done())
        browser_answer.set()
        result = await running

        self.assertIs(result.winner, browser)
        self.assertIs(
            result.outcome.disposition,
            dial_fork.DialDisposition.ANSWERED,
        )

    async def test_sequential_source_cancel_arbitrates_before_winner_commit(
        self,
    ) -> None:
        session = endpoint_session.EndpointCallSession("call-1", 1)
        gate = asyncio.Event()
        committed: list[str] = []

        async def answer():
            await gate.wait()
            return dial_fork.DialOutcome(dial_fork.DialDisposition.ANSWERED)

        async def source_cancel():
            await gate.wait()
            return dial_fork.DialOutcome(dial_fork.DialDisposition.SOURCE_CANCELLED)

        closed: list[tuple[str, object]] = []

        async def close(candidate_id: str, mode) -> None:
            closed.append((candidate_id, mode))

        answer_candidate = dial_fork.DialCandidate(
            "answer",
            answer,
            lambda mode: close("answer", mode),
        )
        control_candidate = dial_fork.DialCandidate(
            "control",
            source_cancel,
            lambda mode: close("control", mode),
            control=True,
        )
        running = asyncio.create_task(
            dial_fork.DialForkController(
                session,
                (answer_candidate, control_candidate),
                strategy=dial_fork.ForkStrategy.SEQUENTIAL,
            ).run(
                lambda candidate, _outcome: not committed.append(candidate.candidate_id)
            )
        )
        await asyncio.sleep(0)
        gate.set()
        result = await running

        self.assertIs(
            result.outcome.disposition,
            dial_fork.DialDisposition.SOURCE_CANCELLED,
        )
        self.assertEqual(committed, [])
        self.assertTrue(
            any(
                candidate_id == "answer"
                and mode
                in {
                    dial_fork.LegCloseMode.BYE,
                    dial_fork.LegCloseMode.CANCEL_OR_BYE,
                }
                for candidate_id, mode in closed
            )
        )

    async def test_source_cancel_during_blocked_loser_cleanup_closes_winner(
        self,
    ) -> None:
        session = endpoint_session.EndpointCallSession("call-1", 1)
        answer = dial_fork.DialOutcome(dial_fork.DialDisposition.ANSWERED, 200)
        busy = dial_fork.DialOutcome(dial_fork.DialDisposition.BUSY, 486)
        release_cleanup = asyncio.Event()
        winner = FakeCandidate("winner", answer, order=0)
        loser = FakeCandidate(
            "loser",
            busy,
            order=1,
            close_gate=release_cleanup,
        )
        committed: list[str] = []
        controller = dial_fork.DialForkController(
            session,
            (winner.candidate, loser.candidate),
        )
        running = asyncio.create_task(
            controller.run(
                lambda candidate, _outcome: not committed.append(candidate.candidate_id)
            )
        )
        await loser.close_started.wait()

        termination = asyncio.create_task(
            session.terminate(endpoint_session.TerminationIntent("cancelled"))
        )
        await asyncio.sleep(0)
        release_cleanup.set()
        result = await running
        await termination

        self.assertEqual(
            result.outcome.disposition, dial_fork.DialDisposition.CANCELLED
        )
        self.assertEqual(committed, [])
        self.assertEqual(winner.closes, [dial_fork.LegCloseMode.BYE])

    async def test_lower_priority_tier_starts_only_after_higher_tier_fails(
        self,
    ) -> None:
        session = endpoint_session.EndpointCallSession("call-1", 1)
        events: list[str] = []

        async def dial(candidate_id: str, disposition):
            events.append(candidate_id)
            return dial_fork.DialOutcome(disposition)

        async def close(_mode) -> None:
            return None

        high = dial_fork.DialCandidate(
            "high",
            lambda: dial("high", dial_fork.DialDisposition.UNAVAILABLE),
            close,
            tier=0,
        )
        low = dial_fork.DialCandidate(
            "low",
            lambda: dial("low", dial_fork.DialDisposition.ANSWERED),
            close,
            tier=1,
        )

        result = await dial_fork.DialForkController(session, (low, high)).run(
            lambda _candidate, _outcome: True
        )

        self.assertEqual(events, ["high", "low"])
        self.assertIs(result.winner, low)

    async def test_sequential_strategy_dials_in_order(self) -> None:
        session = endpoint_session.EndpointCallSession("call-1", 1)
        events: list[str] = []

        def candidate(candidate_id: str, disposition, order: int):
            async def dial():
                events.append(candidate_id)
                return dial_fork.DialOutcome(disposition)

            async def close(_mode) -> None:
                return None

            return dial_fork.DialCandidate(
                candidate_id,
                dial,
                close,
                order=order,
            )

        first = candidate("first", dial_fork.DialDisposition.BUSY, 0)
        second = candidate("second", dial_fork.DialDisposition.ANSWERED, 1)

        result = await dial_fork.DialForkController(
            session,
            (second, first),
            strategy=dial_fork.ForkStrategy.SEQUENTIAL,
        ).run(lambda _candidate, _outcome: True)

        self.assertEqual(events, ["first", "second"])
        self.assertIs(result.winner, second)

    async def test_tier_strategy_overrides_group_default(self) -> None:
        session = endpoint_session.EndpointCallSession("call-1", 1)
        events: list[str] = []
        release = asyncio.Event()

        async def high_dial():
            events.append("high")
            return dial_fork.DialOutcome(dial_fork.DialDisposition.UNAVAILABLE)

        async def low_dial(candidate_id: str, disposition):
            events.append(candidate_id)
            await release.wait()
            return dial_fork.DialOutcome(disposition)

        async def close(_mode) -> None:
            return None

        candidates = (
            dial_fork.DialCandidate("high", high_dial, close, tier=0),
            dial_fork.DialCandidate(
                "low-a",
                lambda: low_dial("low-a", dial_fork.DialDisposition.ANSWERED),
                close,
                tier=1,
                order=0,
            ),
            dial_fork.DialCandidate(
                "low-b",
                lambda: low_dial("low-b", dial_fork.DialDisposition.BUSY),
                close,
                tier=1,
                order=1,
            ),
        )
        running = asyncio.create_task(
            dial_fork.DialForkController(
                session,
                candidates,
                strategy=dial_fork.ForkStrategy.SEQUENTIAL,
                tier_strategies={1: dial_fork.ForkStrategy.PARALLEL},
            ).run(lambda _candidate, _outcome: True)
        )
        while len(events) < 3:
            await asyncio.sleep(0)

        self.assertEqual(events, ["high", "low-a", "low-b"])
        release.set()
        result = await running
        self.assertEqual(result.winner.candidate_id, "low-a")

    async def test_failure_reducer_preserves_group_semantics(self) -> None:
        cases = (
            (
                [dial_fork.DialDisposition.DECLINED] * 2,
                dial_fork.DialDisposition.DECLINED,
                603,
            ),
            (
                [dial_fork.DialDisposition.DECLINED, dial_fork.DialDisposition.DND],
                dial_fork.DialDisposition.DND,
                486,
            ),
            (
                [dial_fork.DialDisposition.MEDIA_INCOMPATIBLE] * 2,
                dial_fork.DialDisposition.MEDIA_INCOMPATIBLE,
                488,
            ),
            (
                [dial_fork.DialDisposition.TIMEOUT] * 2,
                dial_fork.DialDisposition.TIMEOUT,
                408,
            ),
        )
        for dispositions, expected, status in cases:
            with self.subTest(dispositions=dispositions):
                outcome = dial_fork.DialForkController._reduce_failures(
                    [dial_fork.DialOutcome(item) for item in dispositions]
                )
                self.assertEqual(outcome.disposition, expected)
                self.assertEqual(outcome.status, status)


if __name__ == "__main__":
    unittest.main()
