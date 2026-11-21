"""Re-offers inside a dialog: rule N1 of the media negotiation contract."""

import pytest

from custom_components.voip_stack.core.media import leg

pytestmark = pytest.mark.ha

HEAD = "v=0\r\no=- 1 {version} IN IP4 192.0.2.10\r\ns=-\r\nc=IN IP4 192.0.2.10\r\nt=0 0\r\n"


def offer(lines: str, version: int = 1) -> bytes:
    return (HEAD.format(version=version) + lines.replace("\n", "\r\n")).encode()


FIRST = offer("m=audio 46000 RTP/AVP 96 8 101\na=rtpmap:96 L16/48000\na=rtpmap:8 PCMA/8000\n"
              "a=rtpmap:101 telephone-event/8000\na=ptime:10\na=sendrecv\n")


def established(sdp: bytes = FIRST) -> leg.LegContract:
    contract = leg.answer_offer(sdp, leg.PHONE_POLICY)
    assert isinstance(contract, leg.LegContract)
    return contract


def test_the_mapping_in_use_survives_a_reoffer_that_lists_a_better_ranked_codec_first():
    """A phone that started on PCMA keeps PCMA when it re-offers with wideband added."""
    start = established(offer("m=audio 46000 RTP/AVP 8 101\na=rtpmap:8 PCMA/8000\n"
                              "a=rtpmap:101 telephone-event/8000\na=sendrecv\n"))
    again = leg.answer_offer(FIRST.replace(b" 1 IN IP4", b" 2 IN IP4"), leg.PHONE_POLICY, established=start)
    assert (again.send.encoding, again.send.payload_type) == ("PCMA", 8)
    assert (again.receive.encoding, again.receive.frame_ms) == ("PCMA", 20)
    assert any(line.startswith("N1") for line in again.trace)
    fresh = leg.answer_offer(FIRST, leg.PHONE_POLICY)
    assert fresh.send.encoding == "L16", "without an established call the policy order decides"


def test_a_new_maximum_reaches_the_sender_of_a_kept_mapping():
    """Reproduced on the old path: PCMA kept at 20 ms after the peer accepted at most 10 ms,
    so 160 byte packets went to a receiver that had asked for 80."""
    start = established(offer("m=audio 46000 RTP/AVP 8\na=rtpmap:8 PCMA/8000\na=sendrecv\n"))
    assert start.send.frame_ms == 20
    again = leg.answer_offer(offer("m=audio 46000 RTP/AVP 8\na=rtpmap:8 PCMA/8000\na=maxptime:10\na=sendrecv\n", 2),
                             leg.PHONE_POLICY, established=start)
    assert (again.send.encoding, again.send.frame_ms) == ("PCMA", 10)


def test_a_new_packet_time_of_the_peer_wins_over_the_retained_one():
    start = established()
    assert start.send.frame_ms == 10
    again = leg.answer_offer(FIRST.replace(b"a=ptime:10", b"a=ptime:5"), leg.PHONE_POLICY, established=start)
    assert (again.send.encoding, again.send.frame_ms) == ("L16", 5)


def test_the_retained_packet_time_stays_when_the_reoffer_states_none():
    start = established(offer("m=audio 46000 RTP/AVP 8\na=rtpmap:8 PCMA/8000\na=ptime:40\na=sendrecv\n"))
    assert start.send.frame_ms == 40
    again = leg.answer_offer(offer("m=audio 46000 RTP/AVP 8\na=rtpmap:8 PCMA/8000\na=sendrecv\n", 2),
                             leg.PHONE_POLICY, established=start)
    assert again.send.frame_ms == 40


def test_a_mapping_that_is_no_longer_offered_is_replaced():
    start = established()
    again = leg.answer_offer(offer("m=audio 46000 RTP/AVP 8\na=rtpmap:8 PCMA/8000\na=sendrecv\n", 2),
                             leg.PHONE_POLICY, established=start)
    assert again.send.encoding == "PCMA"
    assert not any(line.startswith("N1") for line in again.trace)


def test_the_same_codec_on_another_payload_number_is_not_the_mapping_in_use():
    """N1 keeps a mapping only on its payload number: a renumbered codec is a new mapping."""
    start = established(offer("m=audio 46000 RTP/AVP 8\na=rtpmap:8 PCMA/8000\na=sendrecv\n"))
    again = leg.answer_offer(offer("m=audio 46000 RTP/AVP 96 97\na=rtpmap:96 L16/16000\na=rtpmap:97 PCMA/8000\na=sendrecv\n", 2),
                             leg.PHONE_POLICY, established=start)
    assert again.send.encoding == "L16"
