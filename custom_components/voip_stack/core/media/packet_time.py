"""Packet time: the single owner of how long one RTP audio packet is.

Implements rules T1 to T5 of docs/MEDIA_NEGOTIATION_CONTRACT.md for the transmit direction.
Pure functions: no SDP, no sockets, no global tables of formats.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .codecs import CodecSpec

# Longest first: the standard durations tried when the default of a codec is not feasible (T2).
STANDARD_LADDER_MS: tuple[int, ...] = (60, 40, 30, 20, 10, 5)


@dataclass(frozen=True, slots=True)
class PacketTimes:
    """The packet times one sender can produce for one codec and PCM geometry.

    Either an explicit set (a fixed set, hard: an ESP configured in YAML, the Opus frame
    durations) or every whole sample duration up to ``maximum_ms``.
    """

    explicit: tuple[float, ...] = ()
    maximum_ms: float = 1000

    @property
    def fixed(self) -> bool:
        return bool(self.explicit)

    def contains(self, packet_ms: float) -> bool:
        if self.explicit:
            return packet_ms in self.explicit
        return 0 < packet_ms <= self.maximum_ms


@dataclass(frozen=True, slots=True)
class ReceiverBounds:
    """What the receiver of a stream stated about packet time."""

    ptime: float = 0
    maxptime: float = 0
    minimum: float = 0


@dataclass(frozen=True, slots=True)
class TransmitChoice:
    """The packet time to send with, and which rule chose it."""

    packet_ms: float
    rule: str
    # True when the receiver stated a minimum that no feasible duration of this codec meets.
    # The choice stands: a bare minimum never demotes the codec (T4). The trace records it.
    below_minimum: bool = False


# The sender schedules packets no shorter than this for codecs with a fixed sample size.
MINIMUM_SEND_MS = 1


def _samples(clock_rate: int, packet_ms: float) -> Fraction:
    """Exact number of samples in a packet. No rounding: 20.0001 ms is not 20 ms."""
    return Fraction(clock_rate) * Fraction(packet_ms) / 1000


def _plain(value: Fraction) -> float:
    return int(value) if value.denominator == 1 else float(value)


def payload_octets(spec: CodecSpec, clock_rate: int, channels: int, packet_ms: float) -> int | None:
    """Return the payload size of one packet, or None when it is not a whole number of samples.

    A codec without a fixed sample size (Opus) has no size to check and returns 0.
    """
    if spec.octets_per_sample is None:
        return 0
    samples = _samples(clock_rate, packet_ms)
    if samples.denominator != 1 or samples <= 0:
        return None
    return int(samples) * channels * spec.octets_per_sample


def _decimal_quantum(clock_rate: int) -> int:
    """Smallest sample count whose duration is a finite decimal number of milliseconds.

    SDP writes a packet time in decimal. At 11025 Hz only multiples of 441 samples (40 ms) can
    be written exactly; at 48000 Hz multiples of 3 samples.
    """
    quantum = clock_rate
    for factor in (2, 5):
        while quantum % factor == 0:
            quantum //= factor
    return quantum


def choose_transmit_packet_ms(
    spec: CodecSpec,
    clock_rate: int,
    channels: int,
    *,
    producible: PacketTimes,
    default_ms: float,
    receiver: ReceiverBounds,
    budget: int,
) -> TransmitChoice | None:
    """Choose the transmit packet time, or None when the codec is unusable on this leg (T3)."""
    sized = spec.octets_per_sample is not None

    def feasible(packet_ms: float) -> bool:
        if not packet_ms or not producible.contains(packet_ms):
            return False
        if sized and packet_ms < MINIMUM_SEND_MS:
            return False
        if receiver.maxptime and packet_ms > receiver.maxptime:
            return False
        octets = payload_octets(spec, clock_rate, channels, packet_ms)
        return octets is not None and octets <= budget

    def longest_writable(at_least: float = 0) -> float | None:
        """The longest whole sample duration SDP can state exactly, within every bound."""
        if producible.fixed or not sized:
            return None
        limit_ms = min(producible.maximum_ms, receiver.maxptime or producible.maximum_ms)
        samples = min(budget // (channels * spec.octets_per_sample),
                      int(Fraction(clock_rate) * Fraction(limit_ms) / 1000))
        quantum = _decimal_quantum(clock_rate)
        samples -= samples % quantum
        if samples <= 0:
            return None
        packet_ms = _plain(Fraction(samples * 1000, clock_rate))
        return packet_ms if packet_ms >= max(at_least, MINIMUM_SEND_MS) else None

    def candidates(at_least: float = 0):
        """Feasible durations in the order of T1 and T2, produced only as far as needed."""
        seen: set[float] = set()
        ordered = ((receiver.ptime, "T1"), (default_ms, "T2"),
                   *((value, "T2") for value in STANDARD_LADDER_MS),
                   *((value, "T2") for value in sorted(producible.explicit, reverse=True)))
        for packet_ms, rule in ordered:
            if packet_ms and packet_ms not in seen and packet_ms >= at_least and feasible(packet_ms):
                seen.add(packet_ms)
                yield packet_ms, rule
        longest = longest_writable(at_least)
        if longest is not None and longest not in seen:
            yield longest, "T2"

    chosen = next(candidates(), None)
    if chosen is None:
        return None
    packet_ms, rule = chosen
    if not receiver.minimum or packet_ms >= receiver.minimum:
        return TransmitChoice(packet_ms, rule)
    meeting = next(candidates(receiver.minimum), None)
    if meeting is not None:
        return TransmitChoice(meeting[0], "T4")
    return TransmitChoice(packet_ms, rule, below_minimum=True)
