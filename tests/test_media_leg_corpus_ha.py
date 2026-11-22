"""The production leg negotiator over normative cases and a frozen historical reference.

Two guarantees for every step of every case:
- it meets the contract;
- where it differs from the historical reference, the ledger declares that field.
"""

import json

import pytest

from custom_components.voip_stack.core import sdp
from custom_components.voip_stack.core.media import leg
from tests.media_corpus_support import CORPUS, GOLDEN, _describe, _value, load_steps, mismatches

pytestmark = pytest.mark.ha

STEPS = load_steps()
LEDGER = json.loads((CORPUS / "ledger.json").read_text())
LEGACY = json.loads(GOLDEN.read_text())
COMPARED = ("outcome", "send.codec", "send.ptime", "send.payload", "recv.codec", "recv.ptime",
            "recv.payload", "dtmf.clock", "dtmf.payload")


def negotiate(step) -> dict:
    policy = leg.TRUNK_POLICY if step.policy == "trunk" else leg.PHONE_POLICY
    dahua = step.profile == "dahua_pcm"
    result = (leg.answer_offer(step.sdp, policy, dahua=dahua) if step.op == "answer_offer"
              else leg.apply_answer(step.local_offer, step.sdp, policy, dahua=dahua))
    if isinstance(result, leg.Refusal):
        return {"outcome": "reject", "reason": result.reason}
    written = ""
    if step.op == "answer_offer":
        # The existing writer serializes the contract: only the negotiation is new.
        written = sdp.build_answer_directional(
            "192.0.2.1", "192.0.2.1", 40000, result.send, result.receive,
            dtmf=result.dtmf, remote_sdp=step.sdp)
    return {
        "outcome": "accept",
        "answer": [line for line in written.split("\r\n") if line.startswith("a=")],
        "answer_bytes": written,
        "send": _describe(result.send),
        "recv": _describe(result.receive),
        "dtmf": {"clock": result.dtmf.sample_rate, "payload": result.dtmf.payload_type} if result.dtmf else None,
        "recv_accept": sorted(_describe(fmt)["codec"] for fmt in result.receive_accept) or None,
    }


@pytest.mark.parametrize("step", STEPS, ids=lambda step: step.name)
def test_leg_engine_meets_the_contract(step):
    found = mismatches(step.expect, negotiate(step))
    assert not found, "\n".join(found)


@pytest.mark.parametrize("step", STEPS, ids=lambda step: step.name)
def test_leg_engine_differs_from_frozen_reference_only_as_declared(step):
    new, old = negotiate(step), LEGACY[step.name]
    declared = {row["field"] for row in LEDGER if row["step"] == step.name}
    if "outcome" in declared:
        return  # A declared change of outcome: the contract test above judges the new result.
    undeclared = [f"{field}: {_value(old, field)!r} -> {_value(new, field)!r}"
                  for field in COMPARED
                  if _value(old, field) != _value(new, field) and field not in declared]
    assert not undeclared, "; ".join(undeclared)


def test_every_decision_is_explained():
    """Principle 8: a contract or a refusal carries the rules that produced it."""
    for step in STEPS:
        policy = leg.TRUNK_POLICY if step.policy == "trunk" else leg.PHONE_POLICY
        dahua = step.profile == "dahua_pcm"
        result = (leg.answer_offer(step.sdp, policy, dahua=dahua) if step.op == "answer_offer"
                  else leg.apply_answer(step.local_offer, step.sdp, policy, dahua=dahua))
        if isinstance(result, leg.LegContract):
            assert any(line.startswith(("S2", "S5")) for line in result.trace), step.name
        else:
            assert result.reason, step.name


@pytest.mark.parametrize("step", [step for step in STEPS if step.op == "answer_offer"], ids=lambda step: step.name)
def test_written_answer_is_byte_identical_where_nothing_was_declared(step):
    """Audio only offers with no ledger row must produce the same bytes as production."""
    if any(row["step"] == step.name for row in LEDGER) or b"m=video" in step.sdp:
        return
    assert negotiate(step)["answer_bytes"] == LEGACY[step.name]["answer_bytes"]
