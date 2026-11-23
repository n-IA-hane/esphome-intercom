"""The production leg negotiator over normative cases and a frozen historical reference.

Two guarantees for every step of every case:
- it meets the contract;
- where it differs from the historical reference, the ledger declares that field.
"""

import json

import pytest

from custom_components.voip_stack.core import sdp
from custom_components.voip_stack.core.audio_capabilities import HA_AUDIO_CAPABILITIES
from custom_components.voip_stack.core.media import leg, negotiator
from tests.media_corpus_support import CORPUS, GOLDEN, POLICIES, _describe, _value, load_steps, mismatches

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
                  for field in COMPARED
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
    """Audio only offers with no ledger row must produce the same bytes as production."""
    changes = [row for row in LEDGER if row["step"] == step.name]
    if any(row["field"] != "answer.rtcp_bandwidth" for row in changes) or b"m=video" in step.sdp:
        return
    actual, expected = negotiate(step), LEGACY[step.name]["answer_bytes"]
    if changes:
        assert len(changes) == 1 and changes[0]["cc"] == "RFC3556"
        assert _value(actual, "answer.rtcp_bandwidth") == {"RS": 0, "RR": 0}
        media = next(line for line in expected.split("\r\n") if line.startswith("m=audio "))
        # RFC 3556: advertise the existing lack of audio RTCP. Every other
        # byte, including payloads, clocks, timing and fmtp, remains frozen.
        expected = expected.replace(media + "\r\n", media + "\r\nb=RS:0\r\nb=RR:0\r\n", 1)
    assert actual["answer_bytes"] == expected
