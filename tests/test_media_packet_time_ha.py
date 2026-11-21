"""Transmit packet time, rules T1 to T5 of the media negotiation contract."""

import pytest

from custom_components.voip_stack.core.media.codecs import CODECS
from custom_components.voip_stack.core.media.packet_time import (
    PacketTimes,
    ReceiverBounds,
    TransmitChoice,
    choose_transmit_packet_ms,
    payload_octets,
)

pytestmark = pytest.mark.ha

ANY = PacketTimes()
OPUS_FRAMES = PacketTimes(explicit=(2.5, 5, 10, 20, 40, 60))


def choose(name, clock, channels=1, *, producible=ANY, default=20, budget=1200, **bounds):
    return choose_transmit_packet_ms(
        CODECS[name], clock, channels, producible=producible, default_ms=default,
        receiver=ReceiverBounds(**bounds), budget=budget)


@pytest.mark.parametrize("name,clock,ptime,expected", [
    ("PCMA", 8000, 60, 60), ("PCMU", 8000, 30, 30), ("G722", 8000, 40, 40),
    ("L16", 8000, 60, 60), ("L16", 16000, 20, 20), ("L16", 48000, 10, 10),
])
def test_t1_the_receivers_packet_time_is_used_when_it_fits(name, clock, ptime, expected):
    assert choose(name, clock, ptime=ptime) == TransmitChoice(expected, "T1")


@pytest.mark.parametrize("clock,channels,ptime,expected", [
    (16000, 1, 60, 20),   # 60 ms would be 1920 octets: the default fits
    (48000, 1, 0, 10),    # 20 ms is 1920 octets: first ladder value that fits
    (32000, 1, 0, 10),    # CC-11: 10 ms, not the 16 ms of the old format list
    (44100, 1, 20, 10),
    (48000, 2, 0, 5),     # CC-11: 5 ms, not 6.25 ms
    (11025, 1, 0, 40),    # 441 samples: the only standard value with whole samples that fits
])
def test_t2_default_then_ladder(clock, channels, ptime, expected):
    assert choose("L16", clock, channels, ptime=ptime).packet_ms == expected


def test_t2_opus_uses_its_default_when_the_receiver_states_an_impossible_value():
    assert choose("OPUS", 48000, 2, producible=OPUS_FRAMES, ptime=30).packet_ms == 20
    assert choose("OPUS", 48000, 2, producible=OPUS_FRAMES, ptime=60).packet_ms == 60


def test_t3_maxptime_is_hard():
    assert choose("L16", 48000, ptime=20, maxptime=10).packet_ms == 10
    assert choose("L16", 16000, maxptime=5).packet_ms == 5
    # A fixed set that cannot go below the receiver's maximum makes the codec unusable.
    assert choose("L16", 16000, producible=PacketTimes(explicit=(20, 32)), maxptime=10) is None


def test_t3_a_fixed_set_is_hard():
    fixed = PacketTimes(explicit=(16, 32))
    assert choose("L16", 16000, producible=fixed, ptime=20).packet_ms == 32
    assert choose("L16", 16000, producible=fixed, ptime=16).packet_ms == 16


def test_t4_a_minimum_that_is_already_met_changes_nothing():
    """The preferred 20 ms stays when the minimum is 10; it is not shortened to the minimum."""
    choice = choose("PCMA", 8000, ptime=20, minimum=10)
    assert (choice.packet_ms, choice.rule, choice.below_minimum) == (20, "T1", False)


def test_t4_another_feasible_duration_that_meets_the_minimum_is_tried_first():
    choice = choose("L16", 8000, ptime=10, minimum=30)
    assert (choice.packet_ms, choice.rule, choice.below_minimum) == (60, "T4", False)


@pytest.mark.parametrize("clock,channels,expected", [(32000, 1, 10), (44100, 1, 10), (48000, 2, 5)])
def test_t4_cc1_a_minimum_that_cannot_fit_one_datagram_is_reported_not_refused(clock, channels, expected):
    """baresip sends minptime 20 with L16 at 32 and 44.1 kHz: 20 ms exceeds 1200 octets."""
    choice = choose("L16", clock, channels, ptime=20, minimum=20)
    assert (choice.packet_ms, choice.below_minimum) == (expected, True)


def test_payload_size_needs_whole_samples():
    assert payload_octets(CODECS["L16"], 44100, 1, 10) == 882
    assert payload_octets(CODECS["L16"], 11025, 1, 20) is None
    assert payload_octets(CODECS["G722"], 8000, 1, 20) == 160
    assert payload_octets(CODECS["OPUS"], 48000, 2, 2.5) == 0
