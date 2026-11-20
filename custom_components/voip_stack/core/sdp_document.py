"""Immutable SDP input shared by the audio and media-section interpreters."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SdpDocument:
    """Own the decoded text and its ordered, nonempty normalized lines.

    Interpretation remains with each consumer: capabilities may use port zero,
    while an offer/answer consumer must preserve rejected media sections.
    Raw text is retained for extensions whose existing scope rules are stricter
    than the whitespace-tolerant media parsers.
    """

    text: str
    lines: tuple[str, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        normalized = (
            line.strip() for line in self.text.replace("\r\n", "\n").split("\n")
        )
        object.__setattr__(self, "lines", tuple(line for line in normalized if line))

    def __bool__(self) -> bool:
        return bool(self.text)

    @classmethod
    def parse(cls, value: str | bytes | SdpDocument) -> SdpDocument:
        if isinstance(value, cls):
            return value
        text = (
            value.decode("utf-8", errors="strict")
            if isinstance(value, bytes)
            else value
        )
        return cls(text)
