"""The new leg negotiator over the media corpus. It is not used by production yet.

Two guarantees for every step of every case:
- it meets the contract, except where a declared change is not implemented yet;
- where it differs from the engine in production, the differential ledger declares that field.
"""

import json

import pytest

from custom_components.voip_stack.core.media import leg
from tests.media_corpus_support import CORPUS, GOLDEN, _describe, _value, load_steps, mismatches

pytestmark = pytest.mark.ha

STEPS = load_steps()
LEDGER = json.loads((CORPUS / "ledger.json").read_text())
LEGACY = json.loads(GOLDEN.read_text())
# Declared changes this engine does not implement yet. Their cases keep failing on purpose.
NOT_YET = {
    "CC-12": "static payload numbers 10 and 11",
    "CC-5": "the engine does not write descriptions yet",
}
COMPARED = ("outcome", "send.codec", "send.ptime", "send.payload", "recv.codec", "recv.ptime",
            "recv.payload", "dtmf.clock", "dtmf.payload")


def negotiate(step) -> dict:
    policy = leg.TRUNK_POLICY if step.policy == "trunk" else leg.PHONE_POLICY
    dahua = step.profile == "dahua_pcm"
    result = (leg.answer_offer(step.sdp, policy, dahua=dahua) if step.op == "answer_offer"
              else leg.apply_answer(step.local_offer, step.sdp, policy, dahua=dahua))
    if isinstance(result, leg.Refusal):
        return {"outcome": "reject", "reason": result.reason}
    return {
        "outcome": "accept",
        "send": _describe(result.send),
        "recv": _describe(result.receive),
        "dtmf": {"clock": result.dtmf.sample_rate, "payload": result.dtmf.payload_type} if result.dtmf else None,
        "recv_accept": sorted(_describe(fmt)["codec"] for fmt in result.receive_accept) or None,
    }


@pytest.mark.parametrize("step", STEPS, ids=lambda step: step.name)
def test_new_engine_meets_the_contract(step):
    deferred = {row["field"] for row in LEDGER if row["step"] == step.name and row["cc"] in NOT_YET}
    found = [item for item in mismatches(step.expect, negotiate(step))
             if item.split(":", 1)[0] not in deferred]
    assert not found, "\n".join(found)


@pytest.mark.parametrize("step", STEPS, ids=lambda step: step.name)
def test_new_engine_differs_from_production_only_as_declared(step):
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
