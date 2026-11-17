"""HA media capabilities, independent of peer discovery and offer preferences.

Preset AudioFormats rank offers. Codec rules and bounded wire budgets decide
whether a format is usable, including packet times absent from those presets.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import TYPE_CHECKING

from .audio_format import AudioFormat, PcmFormat, UDP_SAFE_PAYLOAD_BYTES
from .codec_capabilities import common_sip_codecs

if TYPE_CHECKING:
    from .sdp import RtpPcmFormat

MAX_RECEIVE_AUDIO_BYTES = 12 * 1024
OPUS_ENCODER_FRAME_MS = (2.5, 5, 10, 20, 40, 60)


def _whole(value: float) -> bool:
    return (
        math.isfinite(value)
        and value >= 1
        and math.isclose(value, round(value), abs_tol=1e-8)
    )


def _wire_bytes(fmt: RtpPcmFormat) -> int:
    samples = fmt.sample_rate * fmt.frame_ms / 1000
    width = {"L16": 2, "L24": 3, "PCM": 2, "PCMA": 1, "PCMU": 1, "G722": 1}.get(
        fmt.encoding
    )
    return math.ceil(samples * fmt.channels * width) if width is not None else 0


@dataclass(frozen=True, slots=True)
class AudioCapabilities:
    """Capabilities of the existing HA codec/receiver implementation."""

    transmit_payload_bytes: int = UDP_SAFE_PAYLOAD_BYTES
    receive_payload_bytes: int = MAX_RECEIVE_AUDIO_BYTES

    def supports_rtp(self, fmt: RtpPcmFormat, *, receive: bool = False) -> bool:
        try:
            if not (0 < fmt.frame_ms <= 1000) or not math.isfinite(fmt.frame_ms):
                return False
            if not receive and fmt.frame_ms < 1:
                return False  # Existing HA sender schedules at millisecond resolution.
            pcm = fmt.audio_format
            if fmt.encoding == "OPUS":
                if (
                    "OPUS" not in common_sip_codecs()
                    or fmt.sample_rate != 48000
                    or fmt.channels != 2
                ):
                    return False
                return (
                    (fmt.frame_ms <= 120 and _whole(fmt.frame_ms / 2.5))
                    if receive
                    else fmt.frame_ms in OPUS_ENCODER_FRAME_MS
                )
            if fmt.encoding == "G722":
                if (
                    "G722" not in common_sip_codecs()
                    or fmt.sample_rate != 8000
                    or fmt.channels != 1
                ):
                    return False
                if not _whole(fmt.sample_rate * fmt.frame_ms / 1000):
                    return False
            elif fmt.encoding in {"PCMA", "PCMU"}:
                if fmt.sample_rate != 8000 or fmt.channels not in (1, 2):
                    return False
            elif fmt.encoding == "PCM":
                if fmt.sample_rate != 16000 or fmt.channels != 1 or fmt.frame_ms != 20:
                    return False
            elif fmt.encoding not in {"L16", "L24"}:
                return False
            if not _whole(pcm.sample_rate * fmt.frame_ms / 1000):
                return False
            budget = (
                self.receive_payload_bytes if receive else self.transmit_payload_bytes
            )
            return _wire_bytes(fmt) <= budget
        except (ValueError, TypeError, OverflowError):
            return False

    def max_receive_packet_ms(self, fmt: RtpPcmFormat) -> int | float:
        if fmt.encoding == "OPUS":
            return 120
        width = {"L16": 2, "L24": 3, "PCM": 2, "PCMA": 1, "PCMU": 1, "G722": 1}.get(
            fmt.encoding, 0
        )
        if not width or not fmt.sample_rate or not fmt.channels:
            return fmt.frame_ms
        # Millisecond envelope is conservative and always representable for the
        # selected PCM geometry. G722 additionally requires whole RTP ticks.
        samples_per_ms = fmt.sample_rate / 1000
        maximum = min(
            1000,
            math.floor(
                self.receive_payload_bytes / (samples_per_ms * fmt.channels * width)
            ),
        )
        while maximum > 0 and not _whole(samples_per_ms * maximum):
            maximum -= 1
        return max(fmt.frame_ms, maximum)

    def expand(
        self,
        preferred: list[AudioFormat],
        offered: list[RtpPcmFormat],
        *,
        receive: bool = False,
    ) -> list[AudioFormat]:
        """Rank existing preferences, adding valid peer shapes/packetization."""
        output = list(preferred)
        for remote in offered:
            durations = [remote.frame_ms] if remote.frame_ms else []
            durations.extend((fmt.frame_ms for fmt in preferred))
            durations.extend((remote.min_frame_ms, remote.max_frame_ms))
            width = {"L16": 2, "L24": 3, "PCM": 2, "PCMA": 1, "PCMU": 1, "G722": 1}.get(
                remote.encoding
            )
            if width and remote.sample_rate > 0 and remote.channels > 0:
                budget = (
                    self.receive_payload_bytes
                    if receive
                    else self.transmit_payload_bytes
                )
                upper = min(
                    1000, budget * 1000 / (remote.sample_rate * remote.channels * width)
                )
                if not receive and remote.max_frame_ms:
                    upper = min(upper, remote.max_frame_ms)
                wanted = min(remote.frame_ms or upper, upper)
                samples = math.floor(wanted * remote.sample_rate / 1000 + 1e-8)
                # A finite decimal millisecond duration can represent only
                # sample counts that cancel every prime factor except 2/5
                # from the rate. For 11025 Hz this quantum is 441 samples,
                # giving 40 ms instead of a repeating decimal at the budget.
                quantum = remote.sample_rate
                for factor in (2, 5):
                    while quantum % factor == 0:
                        quantum //= factor
                samples = samples // quantum * quantum
                minimum = remote.min_frame_ms if not receive else 0
                if minimum:
                    minimum_samples = math.ceil(minimum * remote.sample_rate / 1000 - 1e-8)
                    samples = max(
                        samples, (minimum_samples + quantum - 1) // quantum * quantum
                    )
                duration = samples * 1000 / remote.sample_rate
                if duration <= upper + 1e-8:
                    durations.append(
                        int(duration) if duration.is_integer() else duration
                    )
            if remote.encoding == "OPUS":
                durations.extend(OPUS_ENCODER_FRAME_MS)
            for duration in dict.fromkeys(durations):
                if not duration or (
                    not receive
                    and (
                        (remote.min_frame_ms and duration < remote.min_frame_ms)
                        or (remote.max_frame_ms and duration > remote.max_frame_ms)
                    )
                ):
                    continue
                candidate = replace(remote, frame_ms=duration)
                if (
                    self.supports_rtp(candidate, receive=receive)
                    and candidate.audio_format not in output
                ):
                    output.append(candidate.audio_format)
        return output


HA_AUDIO_CAPABILITIES = AudioCapabilities()


def local_rtp_audio_formats(
    preferred: list[AudioFormat], *, hints=(), receive: bool = False
) -> tuple[RtpPcmFormat, ...]:
    """Build local offers from real capabilities, using hints only for ranking."""
    from .sdp import RtpPcmFormat

    codec_set = common_sip_codecs()
    candidates = []
    for hint in hints:
        candidates.append(hint)
        if not HA_AUDIO_CAPABILITIES.supports_rtp(hint, receive=receive):
            for pcm in HA_AUDIO_CAPABILITIES.expand([], [hint], receive=receive):
                candidates.append(replace(hint, frame_ms=pcm.frame_ms))
    for pcm in preferred:
        encoding = (
            "L16"
            if pcm.pcm_format is PcmFormat.S16LE
            else "L24"
            if pcm.pcm_format in (PcmFormat.S24LE, PcmFormat.S24LE_IN_S32)
            else None
        )
        if encoding:
            candidates.append(
                RtpPcmFormat(96, encoding, pcm.sample_rate, pcm.channels, pcm.frame_ms)
            )
        if pcm.pcm_format is PcmFormat.S16LE and pcm.channels == 1:
            if pcm.sample_rate == 8000:
                candidates.extend(
                    RtpPcmFormat(0, name, 8000, 1, pcm.frame_ms)
                    for name in ("PCMA", "PCMU")
                )
            if pcm.sample_rate == 16000 and "G722" in codec_set:
                candidates.append(RtpPcmFormat(9, "G722", 8000, 1, pcm.frame_ms))
            if pcm.sample_rate == 48000 and "OPUS" in codec_set:
                candidates.append(RtpPcmFormat(98, "OPUS", 48000, 2, pcm.frame_ms))
    output = []
    seen = set()
    static = {
        ("PCMU", 8000, 1): 0,
        ("PCMA", 8000, 1): 8,
        ("G722", 8000, 1): 9,
        ("OPUS", 48000, 2): 98,
    }
    used = set(static.values())
    mappings = {}
    next_pt = 96
    for fmt in candidates:
        key = (fmt.encoding, fmt.sample_rate, fmt.channels, fmt.frame_ms)
        if key in seen or not HA_AUDIO_CAPABILITIES.supports_rtp(fmt, receive=receive):
            continue
        seen.add(key)
        shape = key[:3]
        payload = static.get(shape, mappings.get(shape))
        if payload is None:
            while next_pt in used:
                next_pt += 1
            if next_pt > 127:
                break
            payload = next_pt
            mappings[shape] = payload
            used.add(payload)
        output.append(
            replace(
                fmt,
                payload_type=payload,
                max_frame_ms=HA_AUDIO_CAPABILITIES.max_receive_packet_ms(fmt)
                if receive
                else fmt.max_frame_ms,
            )
        )
    return tuple(output)
