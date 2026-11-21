"""Offers toward peers that declared their formats: rules X2, X4 and R3 of the contract."""

import json
import re

import pytest

from custom_components.voip_stack.core.media import leg
from custom_components.voip_stack.core.media.capability import PeerEvidence
from tests.media_corpus_support import CORPUS

pytestmark = pytest.mark.ha

# What the two phones of the captures declare (firmware YAML: transmit and receive formats).
DECLARED = {
    "esp-p4": (["L16/16000/1/20", "L16/16000/1/10"], ["L16/48000/1/10", "L16/16000/1/10"]),
    "esp-box-opus": (["OPUS/48000/2/20"], ["OPUS/48000/2/20"]),
}
CASES = [(path.parent, device) for path in sorted((CORPUS / "cases").glob("esp-*-answers-*/case.json"))
         for device in DECLARED if path.parent.name.startswith(device)]


def evidence(device: str, *, complete: bool = True) -> PeerEvidence:
    sends, receives = DECLARED[device]
    return PeerEvidence.from_tokens(sends, receives, complete=complete)


def test_a_p4_is_offered_its_two_formats_instead_of_nine():
    plan = leg.create_offer_for_declared_peer(leg.PHONE_POLICY, evidence("esp-p4"))
    assert plan.payloads == ((96, ("L16", 48000, 1)), (97, ("L16", 16000, 1)), (101, ("TELEPHONE-EVENT", 8000, 1)))
    assert plan.packet_ms == 20, "the phone sends 16 kHz at 20 ms: that is what is asked (R3)"


def test_an_opus_box_is_offered_opus_at_the_time_it_can_send():
    plan = leg.create_offer_for_declared_peer(leg.PHONE_POLICY, evidence("esp-box-opus"))
    assert plan.audio == (("OPUS", 48000, 2),) and plan.packet_ms == 20


def test_incomplete_evidence_never_narrows_an_offer():
    """X4: a stale or partial declaration may rank, it may not remove candidates."""
    assert leg.create_offer_for_declared_peer(leg.PHONE_POLICY, evidence("esp-p4", complete=False)) is None
    assert leg.create_offer_for_declared_peer(leg.PHONE_POLICY, PeerEvidence(complete=True)) is None


def test_declared_packet_times_stay_with_their_mapping():
    """Principle 3: 10 ms declared for 48 kHz does not become a 48 kHz capability at 20 ms."""
    declared = evidence("esp-p4")
    assert {item.mapping: item.times.explicit for item in declared.receives} == {
        ("L16", 48000, 1): (10,), ("L16", 16000, 1): (10,)}
    assert {item.mapping: item.times.explicit for item in declared.sends} == {("L16", 16000, 1): (20, 10)}


@pytest.mark.parametrize("case,device", CASES, ids=lambda value: getattr(value, "name", value))
def test_the_reduced_offer_keeps_everything_the_real_device_answered_with(case, device):
    """On 21 captured dialogs the phone answered a nine codec offer. Every audio mapping it
    chose must still be in the reduced offer, or the reduction would change a working call."""
    step = json.loads((case / "case.json").read_text())["steps"][0]
    answer = (case / step["sdp"]).read_text().split("m=video")[0]
    answered = set()
    for name, clock, channels in re.findall(r"a=rtpmap:\d+ ([A-Za-z0-9]+)/(\d+)(?:/(\d+))?", answer):
        if name.lower() != "telephone-event":
            answered.add((name.upper(), int(clock), int(channels or 1)))
    plan = leg.create_offer_for_declared_peer(leg.PHONE_POLICY, evidence(device))
    assert answered and answered <= set(plan.audio), (answered, plan.audio)
