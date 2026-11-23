"""Decode negotiated RTP payloads into one stable receive PCM contract."""

from __future__ import annotations

from copy import deepcopy
from time import monotonic
import asyncio
import logging

from .core import rtp, sdp
from .core.audio_pcm import PcmFrameConverter
from .core.rtp_payload import RtpPayloadDecoder
from .core.media.codecs import CODECS

_LOGGER = logging.getLogger(__name__)
_REORDER_PACKETS = 4
_REORDER_DELAY_S = 0.06


class RtpAudioReceiver:
    """Keep wire codecs separate from the PCM contract consumed by a call leg."""

    def __init__(
        self,
        selected_format: sdp.RtpPcmFormat,
        recv_formats: tuple[sdp.RtpPcmFormat, ...] = (),
        *,
        previous: RtpAudioReceiver | None = None,
        receive_state: rtp.RtpReceiveState | None = None,
        output_pcm=None,
    ) -> None:
        self.selected_format = selected_format
        self.output_pcm = output_pcm or selected_format.audio_format
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
        self._receive_state = receive_state if receive_state is not None else rtp.RtpReceiveState()
        self.source_clock_rate = 0
        self._ordered: dict[int, tuple[rtp.RtpPacket, bool, bool]] = {}
        self._next_sequence: int | None = None
        self._next_timestamp: int | None = None
        self._synthetic_ms = 0.0
        self._timestamp_rate = 0
        self.last_gap_ms = 0.0
        self.last_packet_accepted = False
        self.last_source_changed = False
        self._reorder_deadline = None
        self._reorder_timer = None
        self._reorder_epoch = 0
        self._on_reorder_ready = None
        self._on_reorder_error = None

    @property
    def receive_state(self):
        """Share RTP sequence admission with same-SSRC telephone events."""
        return self._receive_state

    @property
    def source_ssrc(self):
        return self._receive_state.ssrc

    def requires_fixed_input(self, received_format):
        return not PcmFrameConverter.same_pcm_format(received_format.audio_format, self.output_pcm)

    def retarget_output(self, output_pcm, *, pending_output=()):
        """Change the consumer PCM once, preserving codecs and RTP continuity.

        Retire partial samples with their original types before changing the
        converter. Only this transition converts the old consumer's PCM; future
        native frames go directly to the new consumer's PCM contract.
        """
        ready, commit = self.prepare_retarget_output(output_pcm, pending_output=pending_output)
        commit()
        return ready

    def prepare_retarget_output(self, output_pcm, *, pending_output=()):
        """Prepare typed PCM retirement without consuming the live DSP state."""
        if self.output_pcm == output_pcm:
            return list(pending_output), lambda: None
        retired, fragment = list(pending_output), b""
        previous = deepcopy(self._converter)
        if previous is not None:
            if self._pending:
                retired.extend(previous.convert(bytes(self._pending), final_fragment=True))
            fragment = previous.drain_pending_pcm()
        elif self._pending:
            fragment = bytes(self._pending)
        converter = PcmFrameConverter(self.output_pcm, output_pcm)
        ready = []
        for pcm in (*retired, fragment):
            # A native fast-path packet may contain more than the preferred
            # ptime, up to maxptime. Retire every complete processing quantum
            # and its aligned final fragment without padding or truncation.
            quantum = self.output_pcm.nominal_frame_bytes
            for offset in range(0, len(pcm), quantum):
                ready.extend(converter.convert(pcm[offset:offset + quantum], final_fragment=True))
        carry = converter.drain_pending_pcm()
        native = self._active_format.audio_format if self._active_format is not None else None
        # When native PCM already was the old consumer contract, the retiring
        # converter is also the new native converter. Keep its filter history.
        successor = (converter if native == self.output_pcm else PcmFrameConverter(native, output_pcm)
                     if native is not None else None)
        if carry:
            if successor is None:
                raise RuntimeError("Partial RTP PCM has no native format")
            successor.prepend_pending_pcm(carry)
        def commit():
            self._pending.clear()
            self.output_pcm = output_pcm
            self._converter = successor
        return ready, commit

    def reset_source(self):
        """Retire decode and partial PCM state on an admitted source transition."""
        callback, on_error = self._on_reorder_ready, self._on_reorder_error
        self.close()
        self.set_reorder_callback(callback, on_error)
        self._ordered.clear()
        self._next_sequence = self._next_timestamp = None
        self._timestamp_rate = 0
        self._synthetic_ms = 0.0

    def observe_non_audio_sequence(self, sequence):
        """An admitted shared-SSRC event consumes an RTP sequence number."""
        if self._next_sequence == sequence:
            self._next_sequence = rtp.next_sequence(sequence)

    @property
    def pending_reorder_deadline(self):
        return self._reorder_deadline

    @property
    def has_pending_media(self):
        return any(deliver for _packet, _normalize, deliver in self._ordered.values())

    def set_reorder_callback(self, callback, on_error=None):
        """Deliver deferred PCM through the same existing media consumer."""
        self._on_reorder_ready, self._on_reorder_error = callback, on_error

    def _cancel_reorder_timer(self):
        if self._reorder_timer is not None:
            self._reorder_timer.cancel()
            self._reorder_timer = None

    def _update_reorder_timer(self):
        if not self._ordered:
            self._cancel_reorder_timer()
            self._reorder_deadline = None
            return
        if self._reorder_deadline is None:
            self._reorder_deadline = monotonic() + _REORDER_DELAY_S
        if self._reorder_timer is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        epoch, source = self._reorder_epoch, self.source_ssrc
        self._reorder_timer = loop.call_later(max(0.0, self._reorder_deadline - monotonic()),
            self._reorder_expired, epoch, source)

    def _reorder_expired(self, epoch, source):
        if epoch != self._reorder_epoch or source != self.source_ssrc:
            return
        self._reorder_timer = None
        try:
            frames = self.flush_pending()
            if frames and self._on_reorder_ready is not None:
                self._on_reorder_ready(frames)
        except Exception as error:
            if self._on_reorder_error is not None:
                self._on_reorder_error(error)
            else:
                _LOGGER.debug("Deferred RTP audio decode failed", exc_info=True)
        finally:
            self._update_reorder_timer()

    def flush_pending(self, now=None):
        """Release a bounded gap at its original wall-clock deadline."""
        now = monotonic() if now is None else now
        if self._reorder_deadline is None or now < self._reorder_deadline:
            return []
        return self._drain_ordered(force=True)

    def decode_rtp(self, packet: rtp.RtpPacket, *, normalize_frames=False, admitted=False,
                   deliver_frames=True) -> list[bytes]:
        """Decode each RTP packet once with bounded reordering and one-shot expiry."""
        self.last_packet_accepted = self.last_source_changed = False
        fmt = self.format_for(packet.payload_type)
        if fmt is None:
            raise ValueError("unnegotiated RTP audio payload type")
        rtp.validate_audio_payload_size(packet.payload, fmt)
        self.last_gap_ms = 0.0
        if not admitted:
            if not self._receive_state.admit(packet, clock_rate=fmt.rtp_clock_rate):
                return []
            if self._receive_state.changed:
                self.reset_source()
                self.last_source_changed = True
        if self._next_sequence is None:
            self._next_sequence = packet.sequence
        delta = (packet.sequence - self._next_sequence + 32768) % 65536 - 32768
        if delta < 0:
            return []
        self.last_packet_accepted = True
        self.source_clock_rate = fmt.rtp_clock_rate
        self._ordered[packet.sequence] = (packet, normalize_frames, deliver_frames)
        return self._drain_ordered(force=len(self._ordered) >= _REORDER_PACKETS)

    def _drain_ordered(self, *, force=False):
        self.last_gap_ms = 0.0
        frames = []
        processed = False
        try:
            while self._ordered:
                if self._next_sequence not in self._ordered:
                    if not force or processed:
                        break
                    self._next_sequence = min(self._ordered,
                        key=lambda seq: (seq - self._next_sequence) & 65535)
                current, normalize_frames, deliver_frames = self._ordered[self._next_sequence]
                current_format = self.format_for(current.payload_type)
                if current_format is None:
                    self._ordered.pop(self._next_sequence)
                    self._next_sequence = rtp.next_sequence(current.sequence)
                    processed = True
                    continue
                if processed and self._next_timestamp is not None and self._timestamp_rate == current_format.rtp_clock_rate:
                    gap = (current.timestamp - self._next_timestamp + 0x80000000) % 0x100000000 - 0x80000000
                    if gap > 0:
                        # Keep every discontinuity before its own output batch.
                        # This packet is already ordered and needs no gap wait.
                        self._reorder_deadline = monotonic()
                        self._cancel_reorder_timer()
                        break
                self._ordered.pop(self._next_sequence)
                self._next_sequence = rtp.next_sequence(current.sequence)
                processed = True
                if self._timestamp_rate != current_format.rtp_clock_rate:
                    self._next_timestamp = None
                    self._timestamp_rate = current_format.rtp_clock_rate
                if self._next_timestamp is not None:
                    gap = (current.timestamp - self._next_timestamp + 0x80000000) % 0x100000000 - 0x80000000
                    if gap > 0:
                        gap_ms = gap * 1000 / current_format.rtp_clock_rate
                        if deliver_frames:
                            self.last_gap_ms += max(0.0, gap_ms - self._synthetic_ms)
                        self._pending.clear()
                        self._converter = None
                        self._active_format = None
                self._synthetic_ms = 0.0
                native, selected = current_format.audio_format, self.output_pcm
                if not deliver_frames:
                    decoded = [self.decode_packet(current.payload_type, current.payload)]
                elif (not normalize_frames and not self.has_pending_pcm and
                        (self._active_format is None or PcmFrameConverter.same_pcm_format(
                            native, self._active_format.audio_format)) and
                        (native.sample_rate, native.pcm_format, native.channels) ==
                        (selected.sample_rate, selected.pcm_format, selected.channels)):
                    decoded = [self.decode_packet(current.payload_type, current.payload)]
                    if self._active_format is None:
                        self._active_format = current_format
                else:
                    decoded = self.decode(current.payload_type, current.payload, normalize_frames=normalize_frames)
                if deliver_frames:
                    frames.extend(decoded)
                duration = rtp.audio_payload_duration_ms(current.payload, current_format)
                self._next_timestamp = rtp.next_timestamp(current.timestamp,
                    round(duration * current_format.rtp_clock_rate / 1000))
            return frames
        finally:
            self._update_reorder_timer()

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

    def observe_payload(self, payload_type: int, *, preserve_pcm=False) -> None:
        """Observe a validated packet, including packets relayed without decode."""
        fmt = self.format_for(payload_type)
        if fmt is None:
            raise ValueError(f"unnegotiated RTP audio payload type {payload_type}")
        self.source_clock_rate = fmt.rtp_clock_rate
        if fmt.encoding != "CN":
            self.end_comfort_noise()
        if payload_type in self._new_payload_types:
            # A new mapping proves that the peer applied the new description.
            self._retired.clear()
        if (self._active_format is not None
                and not preserve_pcm
                and not PcmFrameConverter.same_pcm_format(fmt.audio_format, self._active_format.audio_format)):
            self._pending.clear()
            self._converter = None
            self._active_format = None

    def observe_rtp(self, packet):
        """Advance the timeline for validated encoded copies without decoding."""
        if self.has_pending_pcm or self.has_pending_media:
            raise ValueError("encoded copy cannot bypass pending PCM or reordered media")
        fmt = self.format_for(packet.payload_type)
        if fmt is None:
            raise ValueError("unnegotiated RTP payload mapping")
        if self._next_sequence is None or (packet.sequence - self._next_sequence + 32768) % 65536 - 32768 >= 0:
            self._next_sequence = rtp.next_sequence(packet.sequence)
            self._timestamp_rate = fmt.rtp_clock_rate
            self._next_timestamp = rtp.next_timestamp(packet.timestamp,
                round(rtp.audio_payload_duration_ms(packet.payload, fmt) * fmt.rtp_clock_rate / 1000))
            self._synthetic_ms = 0.0
        self.observe_payload(packet.payload_type)

    def unrendered_gap_ms(self, packet):
        """Return only source time not already rendered by comfort-noise output."""
        fmt = self.format_for(packet.payload_type)
        if fmt is None or self._next_timestamp is None or self._timestamp_rate != fmt.rtp_clock_rate:
            return 0.0
        gap = (packet.timestamp - self._next_timestamp + 0x80000000) % 0x100000000 - 0x80000000
        return max(0.0, gap * 1000 / fmt.rtp_clock_rate - self._synthetic_ms)

    def resume_pcm(self):
        """Retire stale filter history after encoded copies, preserving owed PCM."""
        if not self.has_pending_pcm:
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

    def decode_packet(self, payload_type: int, payload: bytes) -> bytes:
        """Validate and decode one whole packet to its native PCM without reframing."""
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
        self._observe_decoded(fmt, pcm)
        return pcm

    def decode(self, payload_type: int, payload: bytes, *, normalize_frames: bool = False) -> list[bytes]:
        """Decode once, then convert to the stable receive PCM contract."""
        pcm = self.decode_packet(payload_type, payload)
        return self._reframe_decoded(self.format_for(payload_type), pcm, normalize_frames=normalize_frames)

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
        self._synthetic_ms += len(pcm) * 1000 / (
            fmt.audio_format.sample_rate * fmt.audio_format.channels * fmt.audio_format.container_bytes_per_sample)
        return self._accept_decoded(fmt, pcm, normalize_frames=normalize_frames)

    def close(self):
        self._reorder_epoch += 1
        self._cancel_reorder_timer()
        self._reorder_deadline = None
        self._ordered.clear()
        self._on_reorder_ready = self._on_reorder_error = None
        self.end_comfort_noise()
        for decoder in self._decoders.values():
            decoder.close()
        self._decoders.clear()
        self._pending.clear()
        self._converter = None
        self._active_format = None

    def _observe_decoded(self, fmt, pcm):
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
        self.observe_payload(fmt.payload_type, preserve_pcm=True)

    def _accept_decoded(self, fmt, pcm, *, normalize_frames):
        self._observe_decoded(fmt, pcm)
        return self._reframe_decoded(fmt, pcm, normalize_frames=normalize_frames)

    def _reframe_decoded(self, fmt, pcm, *, normalize_frames):
        source = fmt.audio_format
        output = []
        if (self._active_format is None
                or not PcmFrameConverter.same_pcm_format(source, self._active_format.audio_format)):
            carry = b""
            if self._converter is not None:
                if self._pending:
                    output.extend(self._converter.convert(bytes(self._pending), final_fragment=True))
                carry = self._converter.drain_pending_pcm()
            elif self._pending:
                carry = bytes(self._pending)
            self._pending.clear()
            self._converter = (
                    PcmFrameConverter(source, self.output_pcm)
                    if source != self.output_pcm or carry
                else None
            )
            if carry:
                self._converter.prepend_pending_pcm(carry)
            self._active_format = fmt
        else:
            # Payload aliases and packet quanta do not replace the processing
            # quantum or discard a partially assembled frame of the same PCM.
            source = self._active_format.audio_format
        if (self._converter is None and not self._pending
                and len(pcm) <= source.nominal_frame_bytes
                and (not normalize_frames or len(pcm) == source.nominal_frame_bytes)):
            return [*output, pcm]
        if self._converter is not None and not self._converter.requires_fixed_input:
            return [*output, *self._converter.convert(pcm)]

        # A valid RTP packet can be shorter than nominal ptime. The converter's
        # resampling filter consumes nominal input frames; retain at most one
        # incomplete frame, and emit as many destination frames as are ready.
        self._pending.extend(pcm)
        frame_bytes = source.nominal_frame_bytes
        while len(self._pending) >= frame_bytes:
            frame = bytes(self._pending[:frame_bytes])
            del self._pending[:frame_bytes]
            if self._converter is None:
                output.append(frame)
            else:
                output.extend(self._converter.convert(frame))
        return output
