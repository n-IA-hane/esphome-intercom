"""Decode negotiated RTP payloads into one stable receive PCM contract."""

from __future__ import annotations

from time import monotonic

from .core import rtp, sdp
from .core.audio_pcm import PcmFrameConverter
from .sip_client import RtpPayloadDecoder


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
        self._formats = {fmt.payload_type: fmt for fmt in recv_formats}
        self._formats.setdefault(selected_format.payload_type, selected_format)
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
        if payload_type in self._new_payload_types:
            # A new mapping proves that the peer applied the new description.
            self._retired.clear()
        if fmt != self._active_format:
            self._pending.clear()
            self._converter = None
            self._active_format = None

    def decode(self, payload_type: int, payload: bytes) -> list[bytes]:
        fmt = self.format_for(payload_type)
        if fmt is None:
            raise ValueError(f"unnegotiated RTP audio payload type {payload_type}")
        rtp.validate_audio_payload_size(payload, fmt)
        decoder = self._decoders.get(payload_type)
        if decoder is None:
            decoder = self._decoders[payload_type] = RtpPayloadDecoder(fmt)
        pcm = decoder.decode(payload)
        source = fmt.audio_format
        stride = source.container_bytes_per_sample * source.channels
        if not pcm or len(pcm) % stride:
            raise ValueError("decoded RTP audio is empty or not sample-aligned")
        self.observe_payload(payload_type)
        if fmt != self._active_format:
            self._pending.clear()
            self._converter = (
                PcmFrameConverter(source, self.selected_format.audio_format)
                if source != self.selected_format.audio_format
                else None
            )
            self._active_format = fmt
        if self._converter is None:
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
            output.extend(self._converter.convert(frame))
        return output
