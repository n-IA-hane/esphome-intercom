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

    async def test_confirmed_call_transfers_new_media_without_second_sip_answer(self):
        session = endpoint_session.EndpointCallSession("call-1", 1)
        self.assertTrue(session.claim_answer(session.token, lambda: True))
        sent = []
        transaction = inbound_answer.AnswerTransaction(session, lambda *args: sent.append(args))
        transaction.add_resource("new-media", object(), lambda _reason: None)
        result = await transaction.commit("answer", claim=lambda: True, response_already_sent=True)
        self.assertTrue(result.committed)
        self.assertFalse(result.response_sent)
        self.assertEqual(sent, [])
        self.assertEqual([r.name for r in session.resources], ["new-media"])
        await session.terminate(endpoint_session.TerminationIntent("test_done"))

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


class AnswerStateRollbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_failed_claim_preserves_original_state_and_publishes_no_answer(self):
        runtime_type = _load_module('pbx_runtime').SipEndpointRuntime
        for raises in (False, True):
            with self.subTest(raises=raises):
                runtime = runtime_type(allow_dark_sessions=True)
                session = runtime.upsert('claim-failure', state='ringing', owner='router', callee='Original', source_endpoint_id='source')
                runtime.observe_leg(session.call_id, session.call_id, role='caller', state='connecting',
                    sip_call_id=session.call_id, endpoint_id='source', generation=session.generation)
                source_leg = session.legs[session.call_id]
                invite = object()
                runtime.set_pending_invite(session.call_id, invite)
                seen = []
                runtime.subscribe_state(seen.append)
                transaction = inbound_answer.AnswerTransaction(session, lambda *_args: self.fail('No SIP answer'))
                closed = []
                transaction.add_resource('prepared', object(), lambda reason: closed.append(reason))
                def claim():
                    runtime.transition(session.call_id, state='in_call', owner='bridge', callee='Candidate')
                    inbound_answer.confirm_existing_caller_leg(runtime, session, session.call_id)
                    self.assertEqual(source_leg.phase, endpoint_session.LegPhase.ANSWERED)
                    runtime.take_pending_invite(session.call_id)
                    if raises:
                        raise RuntimeError('injected claim failure')
                    return False
                result = await transaction.commit('answer', claim=claim)
                self.assertFalse(result.committed)
                self.assertTrue(session.live)
                self.assertEqual((session.state, session.owner, session.callee), ('ringing', 'router', 'Original'))
                self.assertFalse(session.answer_committed)
                self.assertEqual((source_leg.state, source_leg.phase), ('connecting', endpoint_session.LegPhase.INVITING))
                self.assertEqual(seen, [])
                self.assertIs(runtime.artifact_for(session.call_id, 'pending_invite'), invite)
                self.assertEqual(len(closed), 1)
                await session.terminate(endpoint_session.TerminationIntent('test_done'))


    async def test_failed_final_response_publishes_no_confirmed_caller(self):
        runtime = _load_module('pbx_runtime').SipEndpointRuntime(allow_dark_sessions=True)
        session = runtime.upsert('failed-source-answer', state='ringing', owner='router', source_endpoint_id='source')
        runtime.observe_leg(session.call_id, session.call_id, role='caller', state='connecting',
            sip_call_id=session.call_id, endpoint_id='source', generation=session.generation)
        seen = []
        runtime.subscribe_state(seen.append)
        result = await inbound_answer.async_commit_runtime_answer(runtime, session.call_id, 'answer',
            send_final_response=lambda *_args: False, response_context=object(), owner='bridge')
        self.assertFalse(result.committed)
        self.assertEqual(result.reason, 'final_response_failed')
        self.assertFalse(session.live)
        self.assertTrue(all(item.state != 'in_call' for item in seen))
        self.assertEqual(session.legs[session.call_id].phase, endpoint_session.LegPhase.CLOSED)

    async def test_existing_caller_confirmation_preserves_held_closed_and_other_legs(self):
        runtime = _load_module('pbx_runtime').SipEndpointRuntime(allow_dark_sessions=True)
        session = runtime.upsert('source-guard', state='ringing', owner='router', source_endpoint_id='source')
        runtime.observe_leg(session.call_id, 'sibling', role='callee', state='ringing',
            sip_call_id='sibling', endpoint_id='destination', generation=session.generation)
        before = dict(session.legs)
        inbound_answer.confirm_existing_caller_leg(runtime, session, session.call_id)
        self.assertEqual(session.legs, before)
        runtime.observe_leg(session.call_id, session.call_id, role='caller', state='connecting',
            sip_call_id=session.call_id, endpoint_id='source', generation=session.generation)
        source = session.legs[session.call_id]
        for phase in (endpoint_session.LegPhase.HELD, endpoint_session.LegPhase.CLOSING, endpoint_session.LegPhase.CLOSED):
            with self.subTest(phase=phase):
                source.phase, source.state = phase, phase.value
                inbound_answer.confirm_existing_caller_leg(runtime, session, session.call_id)
                self.assertEqual((source.phase, source.state), (phase, phase.value))
                self.assertEqual(session.legs['sibling'].state, 'ringing')
        source.phase, source.state, source.sip_call_id = endpoint_session.LegPhase.INVITING, 'connecting', 'another-wire'
        inbound_answer.confirm_existing_caller_leg(runtime, session, session.call_id)
        self.assertEqual(source.state, 'connecting')
        source.sip_call_id, source.endpoint_id = session.call_id, 'another-endpoint'
        inbound_answer.confirm_existing_caller_leg(runtime, session, session.call_id)
        self.assertEqual(source.state, 'connecting')
        await session.terminate(endpoint_session.TerminationIntent('test_done'))
