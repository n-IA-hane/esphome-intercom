"""HA media capabilities, independent of peer discovery and offer preferences.

Preset AudioFormats rank offers. Codec rules and bounded wire budgets decide
whether a format is usable, including packet times absent from those presets.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import TYPE_CHECKING

from .audio_format import AudioFormat, PcmFormat, UDP_SAFE_PAYLOAD_BYTES
from .codec_capabilities import codec_available
from .media.codecs import CODECS, OCTETS_PER_SAMPLE
from .media.packet_time import payload_octets

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
            spec = CODECS.get(fmt.encoding)
            if spec is None:
                return False
            if spec.frame_duration_ms and fmt.frame_ms % spec.frame_duration_ms:
                return False
            if spec.pyav_decoder == "g726le" or fmt.encoding == "G723":
                if not codec_available(fmt.encoding, receive=receive) or (fmt.sample_rate, fmt.channels) != (8000, 1):
                    return False
                samples = fmt.sample_rate * fmt.frame_ms / 1000
                if not _whole(samples) or round(samples) % spec.sample_quantum:
                    return False
                if fmt.encoding == "G723" and not receive:
                    from .g723_codec import g723_encoder_supports_fmtp

                    if not g723_encoder_supports_fmtp(fmt.fmtp):
                        return False
            elif spec.auxiliary:
                return fmt.encoding == "CN" and fmt.channels == 1 and _whole(pcm.sample_rate * fmt.frame_ms / 1000)
            elif fmt.encoding == "OPUS":
                if (
                    not codec_available("OPUS", receive=receive)
                    or fmt.sample_rate != 48000
                    or fmt.channels != 2
                ):
                    return False
                return (
                    (fmt.frame_ms <= 120 and _whole(fmt.frame_ms / 2.5))
                    if receive
                    else fmt.frame_ms in OPUS_ENCODER_FRAME_MS
                )
            elif fmt.encoding == "G722":
                if (
                    not codec_available("G722", receive=receive)
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
                if fmt.sample_rate != 16000 or fmt.channels != 1:
                    return False
            elif fmt.encoding not in {"L16", "L24"}:
                return False
            if not _whole(pcm.sample_rate * fmt.frame_ms / 1000):
                return False
            budget = (
                self.receive_payload_bytes if receive else self.transmit_payload_bytes
            )
            size = payload_octets(spec, fmt.sample_rate, fmt.channels, fmt.frame_ms)
            return size is not None and size <= budget
        except (ValueError, TypeError, OverflowError):
            return False

    def max_receive_packet_ms(self, fmt: RtpPcmFormat) -> int | float:
        if fmt.encoding == "OPUS":
            return 120
        spec = CODECS.get(fmt.encoding)
        if spec is not None and spec.frame_duration_ms and spec.max_frame_octets:
            return min(1000 // spec.frame_duration_ms, self.receive_payload_bytes // spec.max_frame_octets) * spec.frame_duration_ms
        width = OCTETS_PER_SAMPLE.get(
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
            width = OCTETS_PER_SAMPLE.get(
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
                quantum = math.lcm(quantum, CODECS[remote.encoding].sample_quantum)
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
            spec = CODECS.get(remote.encoding)
            if spec is not None and spec.frame_duration_ms:
                durations.extend((spec.frame_duration_ms, spec.frame_duration_ms * 2))
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


def local_rtp_audio_candidates(
    preferred: list[AudioFormat], *, hints=(), receive: bool = False
) -> tuple[RtpPcmFormat, ...]:
    """List feasible candidates, using hints only for ranking.

    Payload numbers here are placeholders. The complete offer's PayloadMap
    assigns them after all audio, event and historical mappings are known.
    """
    from .sdp import RtpPcmFormat

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
            if pcm.sample_rate == 16000 and codec_available("G722", receive=receive):
                candidates.append(RtpPcmFormat(9, "G722", 8000, 1, pcm.frame_ms))
            if pcm.sample_rate == 48000 and codec_available("OPUS", receive=receive):
                candidates.append(RtpPcmFormat(98, "OPUS", 48000, 2, pcm.frame_ms))
            if pcm.sample_rate == 8000:
                for name, spec in CODECS.items():
                    if name.startswith("G726-") or name == "G723":
                        if codec_available(name, receive=receive):
                            candidates.append(RtpPcmFormat(96, name, 8000, 1,
                                spec.frame_duration_ms or pcm.frame_ms))
    output = []
    seen = set()
    for fmt in candidates:
        key = (fmt.encoding, fmt.sample_rate, fmt.channels, fmt.frame_ms)
        if key in seen or not HA_AUDIO_CAPABILITIES.supports_rtp(fmt, receive=receive):
            continue
        seen.add(key)
        output.append(
            replace(
                fmt,
                payload_type=96,
                max_frame_ms=HA_AUDIO_CAPABILITIES.max_receive_packet_ms(fmt)
                if receive
                else fmt.max_frame_ms,
            )
        )
    return tuple(output)
