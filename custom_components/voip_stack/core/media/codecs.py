"""Codec registry: the single owner of the facts about each audio codec.

Every module that needs to know how wide a sample is on the wire, which static
payload number a codec has, or which dynamic number it conventionally gets,
reads it from here. The registry describes exactly the codecs the RTP adapters
of this integration implement. Adding a codec means adding an entry and an
adapter, and is a declared change of behaviour, never a side effect.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_OPUS_RECEIVE_FMTP = "stereo=0;sprop-stereo=0;maxaveragebitrate=28000"


@dataclass(frozen=True, slots=True)
class CodecSpec:
    """Facts about one RTP audio encoding, keyed by its SDP name in upper case."""

    name: str
    # Octets one sample of one channel occupies in the payload. None for a
    # codec whose packets have no fixed size per sample (Opus).
    octets_per_sample: int | float | None
    # RFC 3551 static payload number with its clock rate and channel count.
    static_payload: int | None = None
    static_clock_rate: int = 0
    static_channels: int = 1
    # Dynamic number this integration proposes first for the codec, with the
    # clock rate and channel count it applies to.
    conventional_payload: int | None = None
    conventional_clock_rate: int = 0
    conventional_channels: int = 1
    # Decoded PCM this integration works with. A rate of None means the RTP
    # clock rate; channels of None means the channel count of the mapping.
    decoded_rate: int | None = None
    decoded_channels: int | None = None
    # True when decoded samples are 24 bit; every other codec decodes to 16 bit.
    decodes_to_24_bit: bool = False
    # Packet time assumed when a mapping states none. None: the mapping must state it.
    assumed_packet_ms: int | None = None
    # Additional static assignments when one codec has more than one geometry.
    # Each tuple is (payload number, RTP clock rate, channel count).
    static_mappings: tuple[tuple[int, int, int], ...] = ()
    # A packet must contain this many samples per channel in each quantum.
    sample_quantum: int = 1
    # Framed codecs contain an integral number of these codec frames.
    frame_duration_ms: int | None = None
    # Maximum octets of one variable-size codec frame, including its header.
    max_frame_octets: int | None = None
    # Auxiliary payloads accompany a primary audio codec and cannot select it.
    auxiliary: bool = False
    pyav_decoder: str | None = None
    pyav_encoder: str | None = None
    bit_rate: int = 0
    frame_octets_by_tag: tuple[int, ...] = ()

    @property
    def bits_per_sample(self) -> int | None:
        """Return the exact codeword width when the wire format has one."""
        return round(self.octets_per_sample * 8) if self.octets_per_sample is not None else None


CODECS: dict[str, CodecSpec] = {
    spec.name: spec
    for spec in (
        CodecSpec("PCMU", 1, static_payload=0, static_clock_rate=8000, assumed_packet_ms=20),
        CodecSpec("PCMA", 1, static_payload=8, static_clock_rate=8000, assumed_packet_ms=20),
        # G.722 carries 16 kHz audio on an 8 kHz RTP clock (RFC 3551 4.5.2).
        CodecSpec("G722", 1, static_payload=9, static_clock_rate=8000, decoded_rate=16000,
                  assumed_packet_ms=20, pyav_decoder="g722", pyav_encoder="g722", bit_rate=64000),
        CodecSpec("L16", 2, static_mappings=((10, 44100, 2), (11, 44100, 1))),
        CodecSpec("L24", 3, decodes_to_24_bit=True),
        # Dahua door stations label signed 16 bit little endian samples as
        # PCM/16000. It is gated by a profile during negotiation; on the wire
        # it has the same two octet sample envelope as L16.
        CodecSpec("PCM", 2),
        # Always opus/48000/2 on the wire (RFC 7587 7); decoded here to mono.
        CodecSpec("OPUS", None, conventional_payload=98, conventional_clock_rate=48000,
                  conventional_channels=2, decoded_rate=48000, decoded_channels=1,
                  assumed_packet_ms=20, pyav_decoder="opus", pyav_encoder="libopus", bit_rate=64000),
        # RFC 3551 4.5.4 uses least-significant-codeword-bit first. FFmpeg's
        # g726le implements this RTP packing; g726 instead uses AAL2 order.
        CodecSpec("G726-16", .25, assumed_packet_ms=20, sample_quantum=4,
                  decoded_rate=8000, decoded_channels=1, pyav_decoder="g726le", pyav_encoder="g726le", bit_rate=16000),
        CodecSpec("G726-24", .375, assumed_packet_ms=20, sample_quantum=8,
                  decoded_rate=8000, decoded_channels=1, pyav_decoder="g726le", pyav_encoder="g726le", bit_rate=24000),
        CodecSpec("G726-32", .5, assumed_packet_ms=20, sample_quantum=2,
                  decoded_rate=8000, decoded_channels=1, pyav_decoder="g726le", pyav_encoder="g726le", bit_rate=32000),
        CodecSpec("G726-40", .625, assumed_packet_ms=20, sample_quantum=8,
                  decoded_rate=8000, decoded_channels=1, pyav_decoder="g726le", pyav_encoder="g726le", bit_rate=40000),
        # G723 is the SDP name for G.723.1, not the older G.726 ADPCM name.
        # RFC 3551 4.5.3 frames have 24, 20 or 4 octets and last 30 ms.
        CodecSpec("G723", None, static_payload=4, static_clock_rate=8000,
                  decoded_rate=8000, decoded_channels=1, assumed_packet_ms=30,
                  frame_duration_ms=30, max_frame_octets=24,
                  pyav_decoder="g723_1", pyav_encoder="g723_1", bit_rate=6300,
                  frame_octets_by_tag=(24, 20, 4)),
        CodecSpec("CN", None, static_payload=13, static_clock_rate=8000,
                  decoded_channels=1, assumed_packet_ms=20, auxiliary=True),
    )
}

# Octets per sample and channel for every codec with a fixed sample size.
OCTETS_PER_SAMPLE: dict[str, int | float] = {
    name: spec.octets_per_sample
    for name, spec in CODECS.items()
    if spec.octets_per_sample is not None
}

# Static payload number -> (encoding, clock rate, channels), for descriptions
# that list a static number without an rtpmap line.
STATIC_RTPMAP: dict[int, tuple[str, int, int]] = {
    number: (spec.name, clock, channels)
    for spec in CODECS.values()
    for number, clock, channels in (
        *(((spec.static_payload, spec.static_clock_rate, spec.static_channels),)
          if spec.static_payload is not None else ()),
        *spec.static_mappings,
    )
}

# (encoding, clock rate, channels) -> payload number proposed in local offers.
PREFERRED_PAYLOADS: dict[tuple[str, int, int], int] = {
    **{mapping: payload for payload, mapping in STATIC_RTPMAP.items()},
    **{
        (spec.name, spec.conventional_clock_rate, spec.conventional_channels): spec.conventional_payload
        for spec in CODECS.values()
        if spec.conventional_payload is not None
    },
}
