"""Capabilities: what one peer can send and receive, as stated by the peer itself.

A capability lists only combinations that exist. Packet times stay attached to the mapping they
were declared for and are never combined across rates or codecs. It carries no preference.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from math import isfinite

from .packet_time import PacketTimes
from .payload_map import RtpMapping
from .codecs import CODECS


@dataclass(frozen=True, slots=True)
class DirectionCapability:
    """One mapping in one direction with the packet times declared for it."""

    mapping: RtpMapping
    times: PacketTimes


@dataclass(frozen=True, slots=True)
class PeerEvidence:
    """Capabilities declared on one current SIP Contact and transport flow.

    ``sends`` is what the peer transmits, ``receives`` what it accepts. ``complete`` is true only
    when the declaration is known to be whole, current and specific to the contact binding in
    use; only an explicitly exhaustive declaration may narrow an offer. OPTIONS can rank.
    """

    sends: tuple[DirectionCapability, ...] = ()
    receives: tuple[DirectionCapability, ...] = ()
    complete: bool = False
    send_geometry_complete: bool = False
    receive_geometry_complete: bool = False

    @classmethod
    def from_tokens(cls, sends: list[str], receives: list[str], *, complete: bool) -> PeerEvidence:
        """Build from ``ENCODING/clock/channels/packet_ms`` tokens, one list per direction."""
        try:
            return cls(sends=_group(sends), receives=_group(receives), complete=complete)
        except (TypeError, ValueError):
            # A malformed declaration cannot remove formats from an offer.
            return cls(complete=False)

    @classmethod
    def from_options(cls, capabilities) -> PeerEvidence:
        """Separate standard codec geometry from unknown transmitter quanta."""
        try:
            sends = _group(capabilities.get("sip_audio_tx_formats", ()))
            receives = _group(capabilities.get("sip_audio_rx_formats", ()))
            maximum = float(capabilities.get("sip_audio_receiver_maxptime", 0) or 0)
            if not isfinite(maximum) or maximum < 0 or maximum > 1000:
                return cls()
            return cls(
                sends=tuple(replace(item, times=PacketTimes()) for item in sends),
                receives=tuple(replace(item, times=PacketTimes(maximum_ms=maximum or 1000)) for item in receives),
            )
        except (TypeError, ValueError):
            return cls()


def _group(tokens: list[str]) -> tuple[DirectionCapability, ...]:
    """Group tokens by mapping, keeping the first appearance order and every declared time."""
    times: dict[RtpMapping, list[float]] = {}
    for token in tokens:
        parts = [part.strip() for part in token.split("/")]
        if len(parts) not in {3, 4}:
            raise ValueError("Invalid directional audio declaration")
        try:
            mapping = (parts[0].upper(), int(parts[1]), int(parts[2]))
            packet_ms = float(parts[3]) if len(parts) == 4 else None
        except ValueError:
            raise ValueError("Invalid directional audio declaration") from None
        if (mapping[0] not in CODECS or mapping[1] <= 0 or mapping[2] not in (1, 2)
                or (packet_ms is not None and (not isfinite(packet_ms) or not 0 < packet_ms <= 1000))):
            raise ValueError("Invalid directional audio declaration")
        declared = times.setdefault(mapping, [])
        if packet_ms is not None and packet_ms not in declared:
            declared.append(packet_ms)
    return tuple(
        DirectionCapability(mapping, PacketTimes(explicit=tuple(int(ms) if ms.is_integer() else ms for ms in declared)))
        for mapping, declared in times.items()
    )


def constrain_formats(formats, declaration):
    """Keep only exact mapping and packet-time combinations a fixed peer declared."""
    allowed = {item.mapping: item.times for item in declaration}
    return tuple(fmt for fmt in formats
        if (times := allowed.get((fmt.encoding, fmt.sample_rate, fmt.channels))) is not None
        and times.contains(fmt.frame_ms))


class AnswerOrder(StrEnum):
    """Precedence for the first outbound mapping of a bidirectional answer."""

    LOCAL = "local"
    PEER = "peer"
    PEER_IF_UNCONFIGURED = "peer_if_unconfigured"


@dataclass(frozen=True, slots=True)
class MediaCompatibility:
    """Normalized interoperation inputs; the engine never identifies a device.

    Optional mappings still require a supported codec and feasible geometry.
    Payload preferences cannot replace a live or retired dialog mapping.
    """

    optional_mappings: tuple[RtpMapping, ...] = ()
    payload_preferences: tuple[tuple[RtpMapping, int], ...] = ()
    answer_order: AnswerOrder = AnswerOrder.LOCAL
