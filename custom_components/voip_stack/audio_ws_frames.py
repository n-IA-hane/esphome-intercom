"""Audio frame decoding, source tracking and browser capture playout."""

from collections import deque
import logging

from .core import rtp
from .dtmf import telephone_event_code

_LOGGER = logging.getLogger(f"{__package__}.audio_ws_view")


_BROWSER_PLAYOUT_TARGET_MS = 60
_BROWSER_PLAYOUT_MAX_MS = 200


def _conceal_pcm_frame(last_pcm: bytes, frame_bytes: int) -> bytes:
    """Return one click-free s16le PLC frame which decays to silence."""
    if frame_bytes < 2 or not last_pcm:
        return bytes(frame_bytes)
    last_sample = int.from_bytes(last_pcm[-2:], "little", signed=True)
    samples = frame_bytes // 2
    output = bytearray(frame_bytes)
    for index in range(samples):
        sample = round(last_sample * (samples - index - 1) / samples)
        output[index * 2 : index * 2 + 2] = int(sample).to_bytes(
            2, "little", signed=True
        )
    return bytes(output)


def _fade_in_pcm_frame(pcm: bytes, fade_samples: int) -> bytes:
    """Fade in recovered s16le PCM so PLC recovery cannot click."""
    samples = min(len(pcm) // 2, max(0, fade_samples))
    if samples == 0:
        return pcm
    output = bytearray(pcm)
    for index in range(samples):
        offset = index * 2
        sample = int.from_bytes(pcm[offset : offset + 2], "little", signed=True)
        sample = round(sample * (index + 1) / samples)
        output[offset : offset + 2] = int(sample).to_bytes(2, "little", signed=True)
    return bytes(output)


class BrowserPcmPlayout:
    """Bound one connection's captured PCM while its RTP clock keeps ticking."""

    def __init__(self, audio_format, counters):
        self.frames: deque[bytes] = deque()
        self.counters = counters
        self.reconfigure(audio_format)
        self.restart_prefill()

    def reconfigure(self, audio_format):
        self.frames.clear()
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
                pcm = _fade_in_pcm_frame(pcm, int(sample_rate) * 2 // 1000)
            self.plc_active = False
        else:
            pcm = (
                self.silence_pcm
                if self.plc_active
                else _conceal_pcm_frame(self.last_pcm, len(self.silence_pcm))
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
        self.logged_first = False
        self.last_sequence = None
        self.last_timestamp = None
        self.last_format = None

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
        packet = rtp.parse_packet(data)
        if (
            dtmf_decoder is not None
            and packet.payload_type == dtmf_decoder.payload_type
        ):
            digit = dtmf_decoder.decode(data)
            event = telephone_event_code(digit)
            if (
                digit
                and event is not None
                and event in session.dtmf_events
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
        if self.ssrc is not None and packet.ssrc != self.ssrc:
            counters["drop_addr"] += 1
            return None
        try:
            frames = decoder.decode(packet.payload_type, packet.payload)
        except rtp.RtpError as err:
            counters["drop_payload_size"] += 1
            _LOGGER.debug("HA softphone RTP RX oversized audio drop: %s", err)
            return None
        source = (str(addr[0]), int(addr[1]))
        if self.source is None:
            self.source = source
            self.ssrc = packet.ssrc
            self.remote_host = source[0]
            self.remote_port = source[1]
        elif source[0] != self.source[0]:
            counters["drop_addr"] += 1
            return None
        elif source[1] != self.source[1]:
            # Preserve the SSRC latch while allowing a NAT mapping to
            # change its source port during a long-lived call.
            self.source = source
            self.remote_port = source[1]
        sequence_delta = (
            (packet.sequence - self.last_sequence) & 0xFFFF
            if self.last_sequence is not None
            else 0
        )
        timestamp_delta = (
            (packet.timestamp - self.last_timestamp) & 0xFFFFFFFF
            if self.last_timestamp is not None
            else 0
        )
        expected_timestamp_delta = int(received_format.rtp_timestamp_step)
        remote_silence_resume = (
            self.last_sequence is not None
            and self.last_format == received_format
            and sequence_delta == 1
            and timestamp_delta > expected_timestamp_delta * 2
        )
        self.last_sequence = packet.sequence
        self.last_timestamp = packet.timestamp
        self.last_format = received_format
        if not dtmf_send_lock.locked() and encoder.follow_received(received_format):
            _LOGGER.info(
                "SIP audio TX follows negotiated RX codec call_id=%s tx=%s",
                session.call_id,
                encoder.fmt.wire_token(),
            )
        counters["rtp_rx"] += 1
        counters["rtp_rx_bytes"] += len(data)
        return frames, remote_silence_resume
