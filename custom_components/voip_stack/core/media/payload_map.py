"""Payload numbers: the single owner of which RTP payload number means which codec.

Implements rules P1 to P4 of docs/MEDIA_NEGOTIATION_CONTRACT.md. One map belongs to one media
stream of one dialog. It is immutable: every operation returns a new map, so a renegotiation can
be prepared beside the active one and dropped if the exchange fails.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .codecs import PREFERRED_PAYLOADS

# (encoding in upper case, RTP clock rate, channels)
RtpMapping = tuple[str, int, int]

TELEPHONE_EVENT = "TELEPHONE-EVENT"
FIRST_DYNAMIC = 96
LAST_DYNAMIC = 127
# P2: the number most phones expect for telephone events at 8000 Hz.
TELEPHONE_EVENT_8000 = 101


class PayloadConflict(ValueError):
    """A payload number would change its meaning inside one session (RFC 3264 8.3.2)."""


class PayloadsExhausted(ValueError):
    """No dynamic payload number is left without evicting a live or retired meaning."""


@dataclass(frozen=True, slots=True)
class PayloadMap:
    """Send and receive payload numbers of one stream, with their history.

    ``receive`` holds the numbers this side declared: packets arrive with them. ``send`` holds
    the numbers the peer declared: packets leave with them. ``receive_history`` keeps every
    meaning a receive number ever had in this session, retired ones included.
    """

    receive: Mapping[int, RtpMapping] = field(default_factory=dict)
    send: Mapping[int, RtpMapping] = field(default_factory=dict)
    receive_history: Mapping[int, RtpMapping] = field(default_factory=dict)

    @classmethod
    def for_offer(cls, mappings: Iterable[RtpMapping], previous: PayloadMap | None = None) -> PayloadMap:
        """Number the mappings of a local offer (P2)."""
        history = dict(previous.receive_history) if previous else {}
        used = dict(history)
        by_mapping = {mapping: number for number, mapping in used.items()}
        receive: dict[int, RtpMapping] = {}
        wanted = list(dict.fromkeys(mappings))
        # First pass: numbers that are not negotiable (already used in this session, static).
        for mapping in wanted:
            number = by_mapping.get(mapping, PREFERRED_PAYLOADS.get(mapping))
            if number is None and mapping == (TELEPHONE_EVENT, 8000, 1):
                number = TELEPHONE_EVENT_8000
            if number is not None and used.get(number, mapping) == mapping:
                receive[number] = used[number] = mapping
                by_mapping[mapping] = number
        # Second pass: dynamic numbers for everything else, never one that meant something else.
        for mapping in wanted:
            if mapping in by_mapping:
                continue
            number = next((n for n in range(FIRST_DYNAMIC, LAST_DYNAMIC + 1) if n not in used), None)
            if number is None:
                raise PayloadsExhausted(f"no dynamic payload number left for {mapping}")
            receive[number] = used[number] = mapping
            by_mapping[mapping] = number
        ordered = {by_mapping[mapping]: mapping for mapping in wanted}
        return cls(receive=ordered, send=dict(previous.send) if previous else {}, receive_history=used)

    def answering(self, offer: Mapping[int, RtpMapping], accepted: Iterable[RtpMapping]) -> PayloadMap:
        """This side answers ``offer`` and accepts ``accepted``: both directions use the numbers
        of the offer (P1). A number that meant something else before is a conflict (P4)."""
        chosen = set(accepted)
        numbers = {number: mapping for number, mapping in offer.items() if mapping in chosen}
        return PayloadMap(receive=numbers, send=numbers,
                          receive_history=self._history_with(numbers))

    def with_answer(self, answer: Mapping[int, RtpMapping]) -> PayloadMap:
        """The peer answered the local offer. It may have renumbered a codec (P3): its number is
        used to send, the number of the offer stays the one to receive on. Mappings that were
        not offered are ignored."""
        offered = set(self.receive.values())
        send = {number: mapping for number, mapping in answer.items() if mapping in offered}
        receive = {number: mapping for number, mapping in self.receive.items() if mapping in set(send.values())}
        return PayloadMap(receive=receive, send=send, receive_history=dict(self.receive_history))

    def send_payload(self, mapping: RtpMapping) -> int | None:
        return next((number for number, known in self.send.items() if known == mapping), None)

    def receive_mapping(self, number: int) -> RtpMapping | None:
        """Never guess a codec from a number: only what was negotiated is received."""
        return self.receive.get(number)

    def _history_with(self, numbers: Mapping[int, RtpMapping]) -> dict[int, RtpMapping]:
        history = dict(self.receive_history)
        for number, mapping in numbers.items():
            if history.get(number, mapping) != mapping:
                raise PayloadConflict(
                    f"payload {number} meant {history[number]} and cannot become {mapping}")
            history[number] = mapping
        return history
