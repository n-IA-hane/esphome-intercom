"""VoIP PCM format contract shared by transports and browser audio."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from math import isfinite
from numbers import Integral, Real


class PcmFormat(StrEnum):
    S16LE = "s16le"
    S24LE = "s24le"
    S24LE_IN_S32 = "s24le_in_s32"
    S32LE = "s32le"


_CONTAINER_BYTES = {
    PcmFormat.S16LE: 2,
    PcmFormat.S24LE: 3,
    PcmFormat.S24LE_IN_S32: 4,
    PcmFormat.S32LE: 4,
}

# Historical preference catalogs, not exhaustive backend capability limits.
SUPPORTED_SAMPLE_RATES = frozenset({8000, 12000, 16000, 24000, 32000, 44100, 48000})
SUPPORTED_CHANNELS = frozenset({1, 2})
SUPPORTED_FRAME_MS = frozenset({10, 16, 20, 32})
MAX_SAMPLE_RATE = 192000
MAX_FRAME_MS = 1000
MAX_PCM_FRAME_BYTES = MAX_SAMPLE_RATE * 2 * 4
UDP_SAFE_PAYLOAD_BYTES = 1200


@dataclass(frozen=True, slots=True)
class AudioFormat:
    sample_rate: int = 16000
    pcm_format: PcmFormat = PcmFormat.S16LE
    channels: int = 1
    frame_ms: int | float = 16

    def __post_init__(self) -> None:
        object.__setattr__(self, "pcm_format", PcmFormat(self.pcm_format))
        if (
            isinstance(self.sample_rate, bool)
            or not isinstance(self.sample_rate, Integral)
            or not 1 <= self.sample_rate <= MAX_SAMPLE_RATE
        ):
            raise ValueError(f"unsupported sample_rate {self.sample_rate}")
        if (
            isinstance(self.channels, bool)
            or not isinstance(self.channels, Integral)
            or self.channels not in SUPPORTED_CHANNELS
        ):
            raise ValueError(f"unsupported channel count {self.channels}")
        if (
            isinstance(self.frame_ms, bool)
            or not isinstance(self.frame_ms, Real)
            or not 0 < self.frame_ms <= MAX_FRAME_MS
            or not isfinite(self.frame_ms)
        ):
            raise ValueError(f"unsupported frame_ms {self.frame_ms}")
        # Canonicalize integral durations so existing tokens remain ``20``,
        # while fractional codec quanta such as Opus 2.5 ms round trip.
        frame_ms = float(self.frame_ms)
        object.__setattr__(self, "sample_rate", int(self.sample_rate))
        object.__setattr__(self, "channels", int(self.channels))
        object.__setattr__(
            self, "frame_ms", int(frame_ms) if frame_ms.is_integer() else frame_ms
        )
        samples = self.sample_rate * Fraction(str(self.frame_ms)) / 1000
        if samples.denominator != 1 or samples < 1:
            raise ValueError(
                f"sample_rate {self.sample_rate} and frame_ms {self.frame_ms} do not form whole PCM frames"
            )

    @property
    def container_bytes_per_sample(self) -> int:
        return _CONTAINER_BYTES[self.pcm_format]

    @property
    def nominal_frame_samples(self) -> int:
        return int(self.sample_rate * Fraction(str(self.frame_ms)) / 1000)

    @property
    def nominal_frame_bytes(self) -> int:
        return self.nominal_frame_samples * self.channels * self.container_bytes_per_sample

    def fits_udp_payload(self, max_payload: int = UDP_SAFE_PAYLOAD_BYTES) -> bool:
        return self.nominal_frame_bytes <= max_payload

    def wire_token(self) -> str:
        return f"{self.sample_rate}:{self.pcm_format.value}:{self.channels}:{self.frame_ms}"


PREFERRED_FRAME_MS = (10, 16, 20, 32)
HA_SIP_PCM_FORMATS = (
    AudioFormat(48000, PcmFormat.S16LE, 2, 20),
    AudioFormat(48000, PcmFormat.S16LE, 1, 20),
    AudioFormat(48000, PcmFormat.S16LE, 1, 10),
    AudioFormat(32000, PcmFormat.S16LE, 1, 16),
    AudioFormat(32000, PcmFormat.S16LE, 1, 10),
    AudioFormat(16000, PcmFormat.S16LE, 1, 16),
    AudioFormat(16000, PcmFormat.S16LE, 1, 10),
    AudioFormat(16000, PcmFormat.S16LE, 1, 20),
    AudioFormat(16000, PcmFormat.S16LE, 1, 32),
    AudioFormat(8000, PcmFormat.S16LE, 1, 20),
    AudioFormat(8000, PcmFormat.S16LE, 1, 10),
)
_HA_PREFERRED_PCM_TX_FORMAT = AudioFormat(48000, PcmFormat.S16LE, 1, 10)
HA_SIP_PCM_TX_FORMATS = (
    _HA_PREFERRED_PCM_TX_FORMAT,
    *(fmt for fmt in HA_SIP_PCM_FORMATS if fmt != _HA_PREFERRED_PCM_TX_FORMAT),
)
# Receive preferences are independent of the outgoing codec/packet time.
# HA sends 48 kHz/10 ms and receives 16 kHz/10 ms from an ESP phone.
# Keep every existing quantum for explicitly configured/negotiated peers.
_HA_PREFERRED_PCM_RX_FORMATS = (
    AudioFormat(16000, PcmFormat.S16LE, 1, 10),
    AudioFormat(48000, PcmFormat.S16LE, 1, 10),
)
HA_SIP_PCM_RX_FORMATS = (
    *_HA_PREFERRED_PCM_RX_FORMATS,
    *(fmt for fmt in HA_SIP_PCM_FORMATS if fmt not in _HA_PREFERRED_PCM_RX_FORMATS),
)
HA_TRUNK_AUDIO_FORMATS = (
    AudioFormat(48000, PcmFormat.S16LE, 2, 20),
    AudioFormat(48000, PcmFormat.S16LE, 1, 20),
    AudioFormat(16000, PcmFormat.S16LE, 1, 20),
    AudioFormat(8000, PcmFormat.S16LE, 1, 20),
)


def parse_audio_format_token(token: str | None) -> AudioFormat:
    if not token:
        raise ValueError("audio format token is required")
    parts = [part.strip() for part in token.split(":")]
    if len(parts) != 4:
        raise ValueError(f"invalid audio format token '{token}'")
    sample_rate, pcm, channels, frame_ms = parts
    return AudioFormat(
        sample_rate=int(sample_rate),
        pcm_format=PcmFormat(pcm),
        channels=int(channels),
        frame_ms=float(frame_ms),
    )


def parse_audio_format_list(value: str | None) -> list[AudioFormat]:
    if not value:
        return []
    formats = [parse_audio_format_token(part.strip()) for part in value.split(";") if part.strip()]
    if not formats:
        return []
    if len(formats) > 8:
        raise ValueError("too many audio formats (max 8)")
    return formats


def choose_common_frame_ms(*format_lists: list[AudioFormat]) -> int | float | None:
    available: set[int | float] | None = None
    for formats in format_lists:
        frames = {fmt.frame_ms for fmt in formats}
        if not frames:
            return None
        available = frames if available is None else available & frames
    if not available:
        return None
    for frame_ms in PREFERRED_FRAME_MS:
        if frame_ms in available:
            return frame_ms
    return min(available)
