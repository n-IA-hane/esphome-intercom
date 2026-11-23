"""The production leg negotiator over normative cases and a frozen historical reference.

Two guarantees for every step of every case:
- it meets the contract;
- where it differs from the historical reference, the ledger declares that field.
"""

import copy
from dataclasses import replace
import json

import pytest

from custom_components.voip_stack.core import sdp
from custom_components.voip_stack.core.audio_capabilities import HA_AUDIO_CAPABILITIES
from custom_components.voip_stack.core.media import leg, negotiator
from tests.media_corpus_support import CORPUS, GOLDEN, POLICIES, _describe, _value, assert_declared_answer_bytes, declared_answer_bytes, load_steps, mismatches

pytestmark = pytest.mark.ha

STEPS = load_steps()
LEDGER = json.loads((CORPUS / "ledger.json").read_text())
LEGACY = json.loads(GOLDEN.read_text())
COMPARED = ("outcome", "send.codec", "send.ptime", "send.payload", "recv.codec", "recv.ptime",
            "recv.payload", "dtmf.clock", "dtmf.payload")


def contract(step):
    """Use the production boundary's actual local formats and backend capability."""
    send, receive = POLICIES[step.policy]
    dahua = step.profile == "dahua_pcm"
    return (negotiator.answer_offer(step.sdp, send, receive,
                capabilities=HA_AUDIO_CAPABILITIES, dahua=dahua)
            if step.op == "answer_offer" else
            negotiator.apply_answer(step.local_offer, step.sdp, send, receive,
                capabilities=HA_AUDIO_CAPABILITIES, dahua=dahua))


def negotiate(step) -> dict:
    result = contract(step)
    if isinstance(result, leg.Refusal):
        return {"outcome": "reject", "reason": result.reason}
    written = ""
    if step.op == "answer_offer":
        # The existing writer serializes the contract: only the negotiation is new.
        written = sdp.build_answer_directional(
            "192.0.2.1", "192.0.2.1", 40000, result.send, result.receive,
            recv_formats=result.receive_accept, dtmf=result.dtmf,
            dtmf_direction=result.dtmf_direction, remote_sdp=step.sdp)
    return {
        "outcome": "accept",
        "answer": [line for line in written.split("\r\n") if line.startswith("a=")],
        "answer_bytes": written,
        "send": _describe(result.send),
        "recv": _describe(result.receive),
        # The corpus field describes sent digits, like the public entrypoint.
        "dtmf": {"clock": result.dtmf_direction.send.sample_rate,
                 "payload": result.dtmf_direction.send.payload_type}
                if result.dtmf_direction is not None and result.dtmf_direction.send is not None else None,
        "recv_accept": sorted(_describe(fmt)["codec"] for fmt in result.receive_accept) or None,
        "recv_payloads": [fmt.payload_type for fmt in result.receive_accept],
    }


@pytest.mark.parametrize("step", STEPS, ids=lambda step: step.name)
def test_leg_engine_meets_the_contract(step):
    actual = negotiate(step)
    found = mismatches(step.expect, actual)
    assert not found, "\n".join(found)
    if actual["outcome"] != "accept":
        return

    def payloads(body):
        media = next(line for line in body.decode().splitlines() if line.startswith("m=audio "))
        return {int(value) for value in media.split()[3:]}

    assert actual["send"]["payload"] in payloads(step.sdp)
    assert actual["recv"]["payload"] in payloads(step.local_offer if step.local_offer else step.sdp)
    direction = sdp.parse_sdp(step.sdp)["direction"]
    if step.op == "answer_offer" and direction in {"sendonly", "sendrecv"}:
        assert set(actual["recv_payloads"]) <= payloads(actual["answer_bytes"].encode())
    encoding, rate, channels = actual["send"]["codec"].split("/")
    if encoding in {"L16", "PCM", "L24"} and direction in {"recvonly", "sendrecv"}:
        width = 3 if encoding == "L24" else 2
        wire_bytes = int(rate) * int(channels) * width * actual["send"]["ptime"] / 1000
        assert wire_bytes.is_integer() and wire_bytes <= HA_AUDIO_CAPABILITIES.transmit_payload_bytes


@pytest.mark.parametrize("step", STEPS, ids=lambda step: step.name)
def test_leg_engine_differs_from_frozen_reference_only_as_declared(step):
    new, old = negotiate(step), LEGACY[step.name]
    declared = {row["field"] for row in LEDGER if row["step"] == step.name}
    if "outcome" in declared:
        return  # A declared change of outcome: the contract test above judges the new result.
    undeclared = [f"{field}: {_value(old, field)!r} -> {_value(new, field)!r}"
                  for field in (*COMPARED, *(
                      ("answer.payloads",) if "answer.payloads" in declared else ()))
                  if _value(old, field) != _value(new, field) and field not in declared]
    assert not undeclared, "; ".join(undeclared)


def test_every_decision_is_explained():
    """Principle 8: a contract or a refusal carries the rules that produced it."""
    for step in STEPS:
        result = contract(step)
        if isinstance(result, leg.LegContract):
            assert any(line.startswith(("S2", "S5")) for line in result.trace), step.name
        else:
            assert result.reason, step.name


@pytest.mark.parametrize("step", [step for step in STEPS if step.op == "answer_offer"], ids=lambda step: step.name)
def test_written_answer_is_byte_identical_where_nothing_was_declared(step):
    """Only declared serialization insertions may change frozen audio bytes."""
    changes = [row for row in LEDGER if row["step"] == step.name]
    if any(row["field"] not in {"answer.rtcp_bandwidth", "answer.payloads"} for row in changes) or b"m=video" in step.sdp:
        return
    actual = negotiate(step)
    assert_declared_answer_bytes(step, actual["answer_bytes"],
        LEGACY[step.name]["answer_bytes"], changes)


ALIAS_STEPS = [step for step in STEPS if "answer_payloads" in step.expect]


@pytest.mark.parametrize("step", ALIAS_STEPS, ids=lambda step: step.name)
@pytest.mark.parametrize("mutation", ["extra payload", "alias clock", "DTMF", "ptime", "origin", "line endings"])
def test_declared_alias_byte_oracle_rejects_every_unlisted_change(step, mutation):
    rows = [row for row in LEDGER if row["step"] == step.name]
    expected = declared_answer_bytes(step, LEGACY[step.name]["answer_bytes"], rows)
    altered = expected
    if mutation == "extra payload":
        media = next(line for line in expected.split("\r\n") if line.startswith("m=audio "))
        altered = expected.replace(media, media + " 120", 1)
    elif mutation == "alias clock":
        altered = expected.replace("a=rtpmap:8 PCMA/8000/1", "a=rtpmap:8 PCMA/16000/1", 1)
    elif mutation == "DTMF":
        altered = expected.replace("telephone-event/8000", "telephone-event/16000", 1)
    elif mutation == "ptime":
        altered = expected.replace("a=ptime:20", "a=ptime:10", 1)
    elif mutation == "origin":
        altered = expected.replace("o=- 0 0", "o=- 0 1", 1)
    else:
        altered = expected.replace("\r\n", "\n")
    assert altered != expected
    with pytest.raises(AssertionError):
        assert_declared_answer_bytes(step, altered, LEGACY[step.name]["answer_bytes"], rows)


@pytest.mark.parametrize("step", ALIAS_STEPS, ids=lambda step: step.name)
def test_alias_declaration_cannot_change_the_primary_or_admit_unoffered_mapping(step):
    rows = [row for row in LEDGER if row["step"] == step.name]
    for intended in ([101, *step.expect["answer_payloads"][:-1]], [*step.expect["answer_payloads"], 120]):
        changed = copy.deepcopy(rows)
        next(row for row in changed if row["field"] == "answer.payloads")["intended"] = intended
        altered_step = replace(step, expect={**step.expect, "answer_payloads": intended})
        with pytest.raises(AssertionError):
            declared_answer_bytes(altered_step, LEGACY[step.name]["answer_bytes"], changed)
