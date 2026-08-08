#!/usr/bin/env python3
"""Transactional inbound-answer tests."""

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
inbound_answer = _load_module("inbound_answer")


class AnswerTransactionTest(unittest.IsolatedAsyncioTestCase):
    async def test_success_claims_before_response_and_transfers_resources(self) -> None:
        events: list[str] = []
        session = endpoint_session.EndpointCallSession("call-1", 1)
        transaction = inbound_answer.AnswerTransaction(
            session,
            lambda status, reason, sdp: (
                not events.append(f"response:{status}:{reason}:{sdp}")
            ),
        )
        transaction.add_resource(
            "ports",
            object(),
            lambda reason: events.append(f"ports:{reason}"),
        )

        result = await transaction.commit(
            "answer",
            claim=lambda: not events.append("claim"),
        )

        self.assertTrue(result.committed)
        self.assertEqual(events, ["claim", "response:200:OK:answer"])
        self.assertEqual([resource.name for resource in session.resources], ["ports"])
        await session.terminate(endpoint_session.TerminationIntent("remote_hangup"))
        self.assertEqual(events[-1], "ports:remote_hangup")

    async def test_stale_session_rolls_back_without_sending_response(self) -> None:
        events: list[str] = []
        session = endpoint_session.EndpointCallSession("call-1", 1)
        transaction = inbound_answer.AnswerTransaction(
            session,
            lambda *_args: not events.append("response"),
        )
        transaction.add_resource(
            "ports",
            object(),
            lambda reason: events.append(f"ports:{reason}"),
        )
        await session.terminate(endpoint_session.TerminationIntent("cancelled"))

        result = await transaction.commit("answer", claim=lambda: True)

        self.assertFalse(result.committed)
        self.assertEqual(result.reason, "stale_call")
        self.assertEqual(events, ["ports:stale_call"])

    async def test_failed_state_claim_rolls_back_prepared_resources(self) -> None:
        events: list[str] = []
        session = endpoint_session.EndpointCallSession("call-1", 1)
        transaction = inbound_answer.AnswerTransaction(
            session,
            lambda *_args: not events.append("response"),
        )
        transaction.add_resource(
            "socket",
            object(),
            lambda reason: events.append(f"socket:{reason}"),
        )

        result = await transaction.commit("answer", claim=lambda: False)

        self.assertFalse(result.committed)
        self.assertEqual(events, ["socket:stale_call"])

    async def test_only_one_answer_can_commit_for_a_generation(self) -> None:
        events: list[str] = []
        session = endpoint_session.EndpointCallSession("call-1", 1)

        first = inbound_answer.AnswerTransaction(
            session,
            lambda *_args: not events.append("first-response"),
        )
        second = inbound_answer.AnswerTransaction(
            session,
            lambda *_args: not events.append("second-response"),
        )

        self.assertTrue((await first.commit("answer", claim=lambda: True)).committed)
        result = await second.commit("answer", claim=lambda: True)

        self.assertFalse(result.committed)
        self.assertEqual(result.reason, "stale_call")
        self.assertEqual(events, ["first-response"])

    async def test_resource_conflict_rolls_back_without_claim_or_response(self) -> None:
        events: list[str] = []
        session = endpoint_session.EndpointCallSession("call-1", 1)
        session.add_resource("socket", object(), lambda _reason: None)
        transaction = inbound_answer.AnswerTransaction(
            session,
            lambda *_args: not events.append("response"),
        )
        transaction.add_resource(
            "socket",
            object(),
            lambda reason: events.append(f"socket:{reason}"),
        )

        result = await transaction.commit(
            "answer",
            claim=lambda: not events.append("claim"),
        )

        self.assertFalse(result.committed)
        self.assertEqual(result.reason, "resource_conflict")
        self.assertEqual(events, ["socket:resource_conflict"])

    async def test_failed_final_response_terminates_transferred_resources(self) -> None:
        events: list[str] = []
        session = endpoint_session.EndpointCallSession("call-1", 1)
        transaction = inbound_answer.AnswerTransaction(
            session,
            lambda *_args: False,
        )
        transaction.add_resource(
            "relay",
            object(),
            lambda reason: events.append(f"relay:{reason}"),
            stage=endpoint_session.CleanupStage.MEDIA,
        )

        result = await transaction.commit("answer", claim=lambda: True)

        self.assertFalse(result.committed)
        self.assertEqual(result.reason, "final_response_failed")
        self.assertEqual(events, ["relay:final_response_failed"])
        self.assertEqual(session.phase, endpoint_session.SessionPhase.TERMINATED)

    async def test_rollback_survives_repeated_waiter_cancellation(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()

        async def close(_reason: str) -> None:
            entered.set()
            await release.wait()
            finished.set()

        session = endpoint_session.EndpointCallSession("call-1", 1)
        transaction = inbound_answer.AnswerTransaction(session, lambda *_args: True)
        transaction.add_resource("socket", object(), close)
        waiter = asyncio.create_task(transaction.rollback("cancelled"))
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


if __name__ == "__main__":
    unittest.main()
