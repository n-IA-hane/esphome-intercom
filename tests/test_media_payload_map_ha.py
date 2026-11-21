"""Payload numbers, rules P1 to P4 of the media negotiation contract."""

import pytest

from custom_components.voip_stack.core.media.payload_map import (
    PayloadConflict,
    PayloadMap,
    PayloadsExhausted,
)

pytestmark = pytest.mark.ha

PCMA, PCMU, G722 = ("PCMA", 8000, 1), ("PCMU", 8000, 1), ("G722", 8000, 1)
OPUS, L16_48, L16_16 = ("OPUS", 48000, 2), ("L16", 48000, 1), ("L16", 16000, 1)
DTMF_8, DTMF_48 = ("TELEPHONE-EVENT", 8000, 1), ("TELEPHONE-EVENT", 48000, 1)
PCM_DAHUA = ("PCM", 16000, 1)


def test_p2_offer_uses_static_numbers_then_dynamic_from_96_and_101_for_dtmf():
    offer = PayloadMap.for_offer([OPUS, G722, PCMA, PCMU, L16_48, L16_16, DTMF_8])
    assert offer.receive == {98: OPUS, 9: G722, 8: PCMA, 0: PCMU, 96: L16_48, 97: L16_16, 101: DTMF_8}
    assert list(offer.receive) == [98, 9, 8, 0, 96, 97, 101], "the order of the offer is kept"


def test_p2_a_second_offer_in_the_same_dialog_keeps_every_number():
    first = PayloadMap.for_offer([L16_48, L16_16, DTMF_8])
    second = PayloadMap.for_offer([L16_16, OPUS, L16_48, DTMF_8, DTMF_48], previous=first)
    assert {mapping: number for number, mapping in second.receive.items()} == {
        L16_16: 97, OPUS: 98, L16_48: 96, DTMF_8: 101, DTMF_48: 99}


def test_p2_a_retired_number_is_never_given_another_meaning():
    first = PayloadMap.for_offer([L16_48, L16_16])
    second = PayloadMap.for_offer([L16_16], previous=first)
    third = PayloadMap.for_offer([L16_16, PCM_DAHUA], previous=second)
    assert third.receive == {97: L16_16, 98: PCM_DAHUA}, "96 still means L16 at 48 kHz"


def test_p2_exhaustion_is_an_error_not_an_eviction():
    many = [("L16", rate, 1) for rate in range(8000, 8000 + 32)]
    PayloadMap.for_offer(many)
    with pytest.raises(PayloadsExhausted):
        PayloadMap.for_offer([*many, ("L16", 9000, 2)])


def test_p1_an_answer_uses_the_numbers_of_the_offer_in_both_directions():
    offer = {111: OPUS, 8: PCMA, 0: PCMU, 126: DTMF_48, 102: DTMF_8}
    answer = PayloadMap().answering(offer, [PCMA, DTMF_8])
    assert answer.receive == answer.send == {8: PCMA, 102: DTMF_8}
    assert answer.send_payload(PCMA) == 8 and answer.receive_mapping(111) is None


def test_p3_a_renumbered_answer_sends_on_its_number_and_receives_on_the_offered_one():
    """Zoiper and others answer an offered codec on a number of their own."""
    offer = PayloadMap.for_offer([L16_48, PCMA, DTMF_8])
    negotiated = offer.with_answer({8: PCMA, 120: DTMF_8})
    assert negotiated.send == {8: PCMA, 120: DTMF_8}
    assert negotiated.receive == {8: PCMA, 101: DTMF_8}
    assert negotiated.receive_mapping(120) is None, "no receive alias for the peer's number"


def test_s5_an_answered_codec_that_was_not_offered_is_not_received():
    offer = PayloadMap.for_offer([PCMA, DTMF_8])
    negotiated = offer.with_answer({8: PCMA, 97: PCM_DAHUA, 101: DTMF_8})
    assert 97 not in negotiated.send and PCM_DAHUA not in negotiated.receive.values()


def test_p4_a_remote_reoffer_cannot_remap_a_used_number():
    """Dahua PCM on 97, later L16 on 97: a late packet could not be told apart."""
    first = PayloadMap().answering({97: PCM_DAHUA, 0: PCMU}, [PCM_DAHUA])
    with pytest.raises(PayloadConflict):
        first.answering({97: L16_16, 0: PCMU}, [L16_16])
    assert first.answering({97: PCM_DAHUA, 0: PCMU}, [PCMU]).receive == {0: PCMU}


def test_p4_the_peers_receive_numbers_keep_their_history_across_three_exchanges():
    """Peer receives PCM/16000 on 120, retires it for PCMU, then answers 120 as L16/16000."""
    offer = PayloadMap.for_offer([PCM_DAHUA, PCMU, L16_16])
    first = offer.with_answer({120: PCM_DAHUA})
    second = PayloadMap.for_offer([PCM_DAHUA, PCMU, L16_16], previous=first).with_answer({0: PCMU})
    assert second.send == {0: PCMU} and second.send_history[120] == PCM_DAHUA
    third = PayloadMap.for_offer([PCM_DAHUA, PCMU, L16_16], previous=second)
    with pytest.raises(PayloadConflict):
        third.with_answer({120: L16_16})
    assert third.with_answer({120: PCM_DAHUA}).send == {120: PCM_DAHUA}, "the old meaning may return"


def test_p3_different_numbers_for_the_two_directions_stay_valid():
    negotiated = PayloadMap.for_offer([L16_16]).with_answer({110: L16_16})
    assert (negotiated.send_payload(L16_16), list(negotiated.receive)) == (110, [96])


def test_an_accepted_map_cannot_change_afterwards():
    source = {8: PCMA, 102: DTMF_8}
    answer = PayloadMap().answering(source, [PCMA, DTMF_8])
    source[8] = PCMU
    assert answer.receive[8] == PCMA and answer.send[8] == PCMA
    for view in (answer.receive, answer.send, answer.receive_history, answer.send_history):
        with pytest.raises(TypeError):
            view[8] = PCMU
    negotiated = PayloadMap.for_offer([PCMA]).with_answer({8: PCMA})
    with pytest.raises(TypeError):
        negotiated.send[8] = PCMU


def test_numbers_out_of_range_or_against_a_static_assignment_are_refused():
    with pytest.raises(PayloadConflict):
        PayloadMap().answering({128: PCMA}, [PCMA])
    with pytest.raises(PayloadConflict):
        PayloadMap().answering({0: PCMA}, [PCMA])
    with pytest.raises(PayloadConflict):
        PayloadMap.for_offer([PCMA]).with_answer({9: PCMA})


def test_direct_snapshot_has_one_consistent_active_and_retired_meaning():
    initial = PayloadMap(receive={96: L16_16}, send={120: L16_16})
    assert initial.receive_history == {96: L16_16} and initial.send_history == {120: L16_16}
    retired = initial.with_mappings(receive={0: PCMU}, send={0: PCMU})
    assert 96 not in retired.receive and retired.receive_history[96] == L16_16
    assert 120 not in retired.send and retired.send_history[120] == L16_16
    assert retired.with_mappings(receive=retired.receive, send=retired.send) is retired
    with pytest.raises(PayloadConflict):
        PayloadMap(send={120: PCM_DAHUA}, send_history={120: L16_16})


def test_static_video_identity_uses_the_same_parser_registry_fact():
    assert PayloadMap(receive={26: ("JPEG", 90000, 1)}).receive_mapping(26) == ("JPEG", 90000, 1)
    with pytest.raises(PayloadConflict):
        PayloadMap(send={26: ("H264", 90000, 1)})
