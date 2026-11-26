"""Audio frame decoding, source tracking and browser capture playout."""

from collections import deque
from dataclasses import dataclass
import logging

import numpy as np

from .core import rtp
from .core.audio_format import AudioFormat
from .core.audio_pcm import _decode_frame, _encode_frame

_LOGGER = logging.getLogger(f"{__package__}.audio_ws_view")


_BROWSER_PLAYOUT_TARGET_MS = 60
_BROWSER_PLAYOUT_MAX_MS = 200


@dataclass(frozen=True, slots=True)
class DeferredAudioFrames:
    receiver: object
    frames: tuple[bytes, ...]
    gap_ms: float


def _conceal_pcm_frame(last_pcm: bytes, frame_bytes: int, audio_format=None) -> bytes:
    """Decay each channel's last signed sample to silence in the actual PCM."""
    audio_format = audio_format or AudioFormat()
    stride = audio_format.container_bytes_per_sample * audio_format.channels
    if frame_bytes % stride:
        raise ValueError("concealed PCM must contain complete channel frames")
    if frame_bytes < stride or not last_pcm:
        return bytes(frame_bytes)
    last = _decode_frame(last_pcm[-stride:], audio_format)
    samples = frame_bytes // stride
    gain = (samples - np.arange(samples) - 1) / samples
    return _encode_frame(last * gain, audio_format)


def _fade_in_pcm_frame(pcm: bytes, fade_samples: int, audio_format=None) -> bytes:
    """Fade recovered channel frames together using their signed PCM format."""
    audio_format = audio_format or AudioFormat()
    samples = min(len(pcm) // (audio_format.container_bytes_per_sample * audio_format.channels), max(0, fade_samples))
    if samples == 0:
        return pcm
    output = _decode_frame(pcm, audio_format)
    output[:, :samples] *= (np.arange(samples) + 1) / samples
    return _encode_frame(output, audio_format)


class BrowserPcmPlayout:
    """Bound one connection's captured PCM while its RTP clock keeps ticking."""

    def __init__(self, audio_format, counters):
        self.frames: deque[bytes] = deque()
        self.counters = counters
        self.reconfigure(audio_format)
        self.restart_prefill()

    def reconfigure(self, audio_format):
        self.frames.clear()
        self.audio_format = audio_format
        self.frame_ms = max(1.0, float(audio_format.frame_ms))
        self.target_frames = max(1, int(_BROWSER_PLAYOUT_TARGET_MS // self.frame_ms))
        self.max_frames = max(
            self.target_frames, int(_BROWSER_PLAYOUT_MAX_MS // self.frame_ms)
        )
        self.silence_pcm = bytes(int(audio_format.nominal_frame_bytes))
        self.last_pcm = b""
        self.plc_active = False

    def restart_prefill(self):
        self.started = False
        self.prefill_frames = 0

    def append(self, pcm):
        self.frames.append(pcm)
        limit = self.target_frames if self.plc_active else self.max_frames
        while len(self.frames) > limit:
            self.frames.popleft()
            self.counters["tx_playout_late_discard"] += 1
        self.counters["tx_playout_depth"] = len(self.frames)
        self.counters["tx_playout_peak"] = max(
            self.counters["tx_playout_peak"], len(self.frames)
        )

    def next_frame(self, sample_rate):
        if not self.started and self.frames:
            self.prefill_frames += 1
            self.started = (
                len(self.frames) >= self.target_frames
                or self.prefill_frames >= self.target_frames
            )
        if not self.started:
            return self.silence_pcm
        if self.frames:
            pcm = self.frames.popleft()
            if self.plc_active:
                pcm = _fade_in_pcm_frame(pcm, self.audio_format.sample_rate * 2 // 1000, self.audio_format)
            self.plc_active = False
        else:
            pcm = (
                self.silence_pcm
                if self.plc_active
                else _conceal_pcm_frame(self.last_pcm, len(self.silence_pcm), self.audio_format)
            )
            self.plc_active = True
            self.counters["tx_playout_plc"] += 1
        self.last_pcm = pcm
        return pcm


class BrowserAudioReceiver:
    """Track the RTP peer and continuity of audio delivered to the browser."""

    def __init__(self, remote_host, remote_port):
        self.reset(remote_host, remote_port)

    def reset(self, remote_host, remote_port):
        self.remote_host = str(remote_host)
        self.remote_port = int(remote_port)
        self.source = None
        self.ssrc = None
        self.received_format = None
        self.logged_first = False

    def decode(
        self,
        data,
        addr,
        session,
        *,
        decoder,
        encoder,
        dtmf_decoder,
        dtmf_send_lock,
        counters,
    ):
        """Return the decoded frames and silence-resume marker for an accepted packet."""
        if str(addr[0]) not in {self.remote_host, str(getattr(session, "remote_rtp_host", "")),
                                str(getattr(session, "signaling_host", ""))}:
            counters["drop_addr"] += 1
            return None
        packet = rtp.parse_packet(data)
        if (
            dtmf_decoder is not None
            and dtmf_decoder.accepts(packet.payload_type)
        ):
            report = dtmf_decoder.decode_report(data)
            from .audio_rtcp import observe_validated_event
            observe_validated_event(getattr(session, "audio_rtcp", None), dtmf_decoder)
            digit = report.digit if report is not None and report.first else ""
            if report is not None and report.shared_audio:
                if report.source_changed:
                    decoder.reset_source()
                decoder.observe_non_audio_sequence(packet.sequence)
                self.ssrc = report.ssrc
                self.source = (str(addr[0]), int(addr[1]))
                self.remote_host, self.remote_port = self.source
            if (
                digit
                and session.on_dtmf is not None
            ):
                session.on_dtmf(digit)
                counters["dtmf_rx_events"] += 1
            return None
        if not self.logged_first:
            _LOGGER.info(
                "HA softphone RTP RX first packet call_id=%s from=%s:%s payload_type=%s expected=%s bytes=%d",
                session.call_id,
                addr[0],
                addr[1],
                packet.payload_type,
                session.recv_format.payload_type,
                len(data),
            )
            self.logged_first = True
        received_format = decoder.format_for(packet.payload_type)
        if received_format is None:
            counters["drop_payload_type"] += 1
            return None
        if session.local_audio_direction not in {"recvonly", "sendrecv"}:
            counters["drop_direction"] += 1
            return None
        try:
            frames = decoder.decode_rtp(packet,
                normalize_frames=decoder.requires_fixed_input(received_format))
        except rtp.RtpError as err:
            counters["drop_payload_size"] += 1
            _LOGGER.debug("HA softphone RTP RX oversized audio drop: %s", err)
            return None
        if not decoder.last_packet_accepted:
            return None
        self.source = (str(addr[0]), int(addr[1]))
        self.ssrc = decoder.source_ssrc
        self.received_format = received_format
        self.remote_host, self.remote_port = self.source
        remote_silence_resume = decoder.last_gap_ms > received_format.frame_ms * 2
        if not dtmf_send_lock.locked() and encoder.follow_received(received_format):
            _LOGGER.info(
                "SIP audio TX follows negotiated RX codec call_id=%s tx=%s",
                session.call_id,
                encoder.fmt.wire_token(),
            )
        counters["rtp_rx"] += 1
        counters["rtp_rx_bytes"] += len(data)
        return frames, remote_silence_resume
