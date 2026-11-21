"""Decode negotiated RTP payloads into one stable receive PCM contract."""

from __future__ import annotations

from time import monotonic
import logging

from .core import rtp, sdp
from .core.audio_pcm import PcmFrameConverter
from .core.rtp_payload import RtpPayloadDecoder
from .core.media.codecs import CODECS

_LOGGER = logging.getLogger(__name__)


class RtpAudioReceiver:
    """Keep wire codecs separate from the PCM contract consumed by a call leg."""

    def __init__(
        self,
        selected_format: sdp.RtpPcmFormat,
        recv_formats: tuple[sdp.RtpPcmFormat, ...] = (),
        *,
        previous: RtpAudioReceiver | None = None,
    ) -> None:
        self.selected_format = selected_format
        if CODECS.get(selected_format.encoding) is not None and CODECS[selected_format.encoding].auxiliary:
            raise ValueError("Comfort noise requires a negotiated primary audio format")
        # Share the observer collection even when it is empty: a recorder may
        # attach after offer preparation but before the new receiver commits.
        self._pcm_observers = previous._pcm_observers if previous is not None else {}
        self._formats = {fmt.payload_type: fmt for fmt in recv_formats}
        self._formats.setdefault(selected_format.payload_type, selected_format)
        if any(fmt.encoding == "CN" and (fmt.channels != 1 or fmt.sample_rate != selected_format.sample_rate)
               for fmt in self._formats.values()):
            raise ValueError("Comfort noise must use the primary audio RTP clock and one channel")
        if any(not 0 <= pt <= 127 for pt in self._formats):
            raise ValueError("RTP audio payload type out of range")
        self._retired: dict[int, sdp.RtpPcmFormat] = {}
        self._retire_at = 0.0
        self._new_payload_types: set[int] = set()
        if previous is not None:
            for old in previous.accepted_formats:
                current = self._formats.get(old.payload_type)
                if current is not None and (
                    current.encoding, current.sample_rate, current.channels
                ) != (old.encoding, old.sample_rate, old.channels):
                    raise ValueError("RTP payload type mapping changed within a session")
            # Only inherit the preceding generation's current mappings. Older
            # retired mappings never acquire another minute through chaining.
            self._retired = {
                pt: fmt for pt, fmt in previous._formats.items()
                if pt not in self._formats
            }
            self._new_payload_types = self._formats.keys() - previous._formats.keys()
            if self._retired:
                self._retire_at = monotonic() + 60.0
        self._decoders: dict[int, RtpPayloadDecoder] = {}
        self._active_format: sdp.RtpPcmFormat | None = None
        self._converter: PcmFrameConverter | None = None
        self._pending = bytearray()
        self._comfort_payload_type: int | None = None

    @property
    def accepted_formats(self) -> tuple[sdp.RtpPcmFormat, ...]:
        """Current mappings plus the bounded SDP transition overlap."""
        self._expire_retired()
        return (*self._formats.values(), *self._retired.values())

    def _expire_retired(self) -> None:
        if self._retired and monotonic() >= self._retire_at:
            self._retired.clear()

    def format_for(self, payload_type: int) -> sdp.RtpPcmFormat | None:
        """Look up the SDP mapping, never infer a codec from a dynamic PT."""
        self._expire_retired()
        return self._formats.get(payload_type) or self._retired.get(payload_type)

    def observe_payload(self, payload_type: int) -> None:
        """Observe a validated packet, including packets relayed without decode."""
        fmt = self.format_for(payload_type)
        if fmt is None:
            raise ValueError(f"unnegotiated RTP audio payload type {payload_type}")
        if fmt.encoding != "CN":
            self.end_comfort_noise()
        if payload_type in self._new_payload_types:
            # A new mapping proves that the peer applied the new description.
            self._retired.clear()
        if fmt != self._active_format:
            self._pending.clear()
            self._converter = None
            self._active_format = None

    @property
    def has_pending_pcm(self) -> bool:
        return bool(self._pending) or (self._converter is not None and self._converter.has_pending_pcm)

    @property
    def has_pcm_observers(self):
        return bool(self._pcm_observers)

    def add_pcm_observer(self, observer):
        """Tap decoded native PCM without another decoder or receive queue."""
        observers = self._pcm_observers
        observers[observer] = None
        return lambda: observers.pop(observer, None)

    def decode(self, payload_type: int, payload: bytes, *, normalize_frames: bool = False) -> list[bytes]:
        fmt = self.format_for(payload_type)
        if fmt is None:
            raise ValueError(f"unnegotiated RTP audio payload type {payload_type}")
        rtp.validate_audio_payload_size(payload, fmt)
        decoder = self._decoders.get(payload_type)
        if decoder is None:
            decoder = self._decoders[payload_type] = RtpPayloadDecoder(fmt)
        pcm = decoder.decode(payload)
        if fmt.encoding == "CN":
            self._comfort_payload_type = payload_type
        return self._accept_decoded(fmt, pcm, normalize_frames=normalize_frames)

    @property
    def comfort_noise_active(self) -> bool:
        return self._comfort_payload_type is not None

    @property
    def comfort_noise_packet_ms(self):
        fmt = self.format_for(self._comfort_payload_type) if self._comfort_payload_type is not None else None
        return fmt.audio_format.frame_ms if fmt is not None else 0

    def end_comfort_noise(self):
        if self._comfort_payload_type is not None:
            self._decoders[self._comfort_payload_type].end_comfort_noise()
            self._comfort_payload_type = None

    def take_comfort_noise(self, *, normalize_frames=True) -> list[bytes]:
        """Supply the next silence-period quantum from this receiver's SID."""
        payload_type = self._comfort_payload_type
        if payload_type is None:
            return []
        fmt = self.format_for(payload_type)
        if fmt is None:
            self.end_comfort_noise()
            return []
        pcm = self._decoders[payload_type].generate_comfort_noise()
        return self._accept_decoded(fmt, pcm, normalize_frames=normalize_frames)

    def close(self):
        self.end_comfort_noise()
        for decoder in self._decoders.values():
            decoder.close()
        self._decoders.clear()
        self._pending.clear()
        self._converter = None
        self._active_format = None

    def _accept_decoded(self, fmt, pcm, *, normalize_frames):
        source = fmt.audio_format
        stride = source.container_bytes_per_sample * source.channels
        if not pcm or len(pcm) % stride:
            raise ValueError("decoded RTP audio is empty or not sample-aligned")
        if self._pcm_observers:
            for observer in tuple(self._pcm_observers):
                try:
                    observer(pcm, source)
                except Exception:
                    self._pcm_observers.pop(observer, None)
                    _LOGGER.debug("Decoded audio observer failed; call audio continues", exc_info=True)
        self.observe_payload(fmt.payload_type)
        if fmt != self._active_format:
            self._pending.clear()
            self._converter = (
                PcmFrameConverter(source, self.selected_format.audio_format)
                if source != self.selected_format.audio_format
                else None
            )
            self._active_format = fmt
        if (self._converter is None and not self._pending
                and len(pcm) <= source.nominal_frame_bytes
                and (not normalize_frames or len(pcm) == source.nominal_frame_bytes)):
            return [pcm]

        # A valid RTP packet can be shorter than nominal ptime. The converter's
        # resampling filter consumes nominal input frames; retain at most one
        # incomplete frame, and emit as many destination frames as are ready.
        self._pending.extend(pcm)
        frame_bytes = source.nominal_frame_bytes
        output: list[bytes] = []
        while len(self._pending) >= frame_bytes:
            frame = bytes(self._pending[:frame_bytes])
            del self._pending[:frame_bytes]
            if self._converter is None:
                output.append(frame)
            else:
                output.extend(self._converter.convert(frame))
        return output
