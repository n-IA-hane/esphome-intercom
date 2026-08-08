"""Generated lifecycle sequences for the authoritative call registry."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from hypothesis import given, settings, strategies as st
import pytest

from tests.support.module_loader import load_voip_stack_module


pytestmark = pytest.mark.unit
pbx_runtime = load_voip_stack_module("pbx_runtime")


def _registry():
    return pbx_runtime.SipEndpointRuntime(allow_dark_sessions=True)


CALL_IDS = ("call-a", "call-b", "physical:esp")
STATES = ("ringing", "connecting", "in_call", "busy", "cancelled", "idle")


@dataclass(frozen=True, slots=True)
class Operation:
    action: str
    call_id: str
    value: str


operations = st.lists(
    st.builds(
        Operation,
        action=st.sampled_from(
            (
                "upsert",
                "transition",
                "add_leg",
                "remove_leg",
                "begin_termination",
                "finish",
                "late_bridge",
            )
        ),
        call_id=st.sampled_from(CALL_IDS),
        value=st.sampled_from(STATES),
    ),
    min_size=1,
    max_size=60,
)


def _assert_indexes_are_consistent(registry) -> None:
    for leg_id, session_id in registry.leg_index.items():
        assert session_id in registry.sessions
        assert leg_id in registry.sessions[session_id].legs

    counts = registry.snapshot()["resource_counts"]
    assert counts["sessions"] == len(registry.sessions)
    assert counts["legs"] == sum(
        len(session.legs) for session in registry.sessions.values()
    )
    assert counts["sip_clients"] == len(registry.sip_clients)
    assert counts["client_watchers"] == len(registry.client_watchers)
    assert counts["relays"] == len(registry.relays)
    assert counts["bridges"] == len(registry.bridge_clients)


@given(operations)
@settings(max_examples=150, deadline=None)
def test_generated_lifecycle_sequences_preserve_registry_invariants(
    sequence: list[Operation],
) -> None:
    registry = _registry()
    generations: dict[str, int] = {}

    for index, operation in enumerate(sequence):
        call_id = operation.call_id
        if operation.action == "upsert":
            session = registry.upsert(
                call_id,
                state=operation.value,
                owner="router",
            )
            generations[call_id] = session.generation
        elif operation.action == "transition" and call_id in registry.sessions:
            registry.transition(call_id, state=operation.value)
        elif operation.action == "add_leg":
            session = registry.upsert(call_id, state="ringing", owner="router")
            generations[call_id] = session.generation
            registry.add_leg(
                call_id,
                f"{call_id}:leg:{index % 3}",
                role="callee",
                state=operation.value,
            )
        elif operation.action == "remove_leg":
            registry.remove_leg(call_id, f"{call_id}:leg:{index % 3}")
        elif operation.action == "begin_termination":
            registry.begin_termination(
                call_id, pbx_runtime.TerminationIntent("terminated")
            )
        elif operation.action == "finish":
            registry.terminate_call(
                call_id,
                reason="generated_terminal",
            )
        elif operation.action == "late_bridge" and call_id in generations:
            generation = generations[call_id]
            attached = registry.register_bridge(
                source_call_id=call_id,
                dest_call_id=f"{call_id}:late",
                client=object(),
                state="connecting",
                expected_generation=generation,
            )
            if registry.is_terminated(call_id, generation=generation):
                assert attached is None

        _assert_indexes_are_consistent(registry)

        if call_id not in registry.sessions and call_id in generations:
            assert not registry.is_generation_current(
                call_id,
                generations[call_id],
            )

    asyncio.run(registry.shutdown())
    registry.clear_runtime()
    assert all(count == 0 for count in registry.snapshot()["resource_counts"].values())


@pytest.mark.mutation
@pytest.mark.fault
@pytest.mark.parametrize(
    "fault",
    (
        "duplicate_terminal",
        "late_transition",
        "late_bridge",
        "reused_physical_id",
    ),
)
def test_terminal_faults_cannot_resurrect_or_duplicate_calls(fault: str) -> None:
    registry = _registry()
    session = registry.upsert("physical:esp", state="in_call", owner="bridge")
    generation = session.generation

    assert registry.begin_termination(
        "physical:esp", pbx_runtime.TerminationIntent("terminated")
    )
    registry.terminate_call(
        "physical:esp",
        reason="remote_hangup",
    )

    if fault == "duplicate_terminal":
        assert not registry.begin_termination(
            "physical:esp", pbx_runtime.TerminationIntent("terminated")
        )
    elif fault == "late_transition":
        assert registry.transition("physical:esp", state="in_call") is None
    elif fault == "late_bridge":
        assert (
            registry.register_bridge(
                source_call_id="physical:esp",
                dest_call_id="late-leg",
                client=object(),
                state="connecting",
                expected_generation=generation,
            )
            is None
        )
    else:
        replacement = registry.upsert(
            "physical:esp",
            state="ringing",
            owner="router",
        )
        assert replacement.generation > generation
        assert registry.is_generation_current(
            "physical:esp",
            replacement.generation,
        )

    _assert_indexes_are_consistent(registry)
