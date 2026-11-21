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


def payload_octets(spec: CodecSpec, clock_rate: int, channels: int, packet_ms: float) -> int | None:
    """Return the payload size of one packet, or None when it is not a whole number of samples.

    A codec without a fixed sample size (Opus) has no size to check and returns 0.
    """
    if spec.octets_per_sample is None:
        return 0
    samples = Fraction(clock_rate) * Fraction(packet_ms).limit_denominator(1000) / 1000
    if samples.denominator != 1 or samples <= 0:
        return None
    return int(samples) * channels * spec.octets_per_sample


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

    def feasible(packet_ms: float) -> bool:
        if not packet_ms or not producible.contains(packet_ms):
            return False
        if receiver.maxptime and packet_ms > receiver.maxptime:
            return False
        octets = payload_octets(spec, clock_rate, channels, packet_ms)
        return octets is not None and octets <= budget

    def ranked() -> list[tuple[float, str]]:
        """Every feasible duration in the order of T1 and T2, without duplicates."""
        order: list[tuple[float, str]] = [(receiver.ptime, "T1"), (default_ms, "T2")]
        order += [(value, "T2") for value in STANDARD_LADDER_MS]
        order += [(value, "T2") for value in sorted(producible.explicit, reverse=True)]
        if not producible.fixed and spec.octets_per_sample:
            # The longest whole millisecond duration that fits, for rates where no standard
            # value does (for example 11025 Hz).
            limit = int(min(producible.maximum_ms, receiver.maxptime or producible.maximum_ms))
            order += [(value, "T2") for value in range(limit, 0, -1)]
        seen: set[float] = set()
        result = []
        for packet_ms, rule in order:
            if packet_ms in seen or not feasible(packet_ms):
                continue
            seen.add(packet_ms)
            result.append((packet_ms, rule))
        return result

    candidates = ranked()
    if not candidates:
        return None
    packet_ms, rule = candidates[0]
    if not receiver.minimum or packet_ms >= receiver.minimum:
        return TransmitChoice(packet_ms, rule)
    for other_ms, _ in candidates:
        if other_ms >= receiver.minimum:
            return TransmitChoice(other_ms, "T4")
    return TransmitChoice(packet_ms, rule, below_minimum=True)
