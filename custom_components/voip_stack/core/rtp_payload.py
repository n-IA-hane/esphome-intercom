"""Stateful RTP audio codecs shared by SIP, browser and mixer media."""

from __future__ import annotations

from . import g711
from .sdp_types import RtpPcmFormat
from .audio_format import AudioFormat, PcmFormat
from .g722_codec import G722Decoder, G722Encoder
from .opus_codec import OpusDecoder, OpusEncoder
from .g726_codec import G726Decoder, G726Encoder
from .g723_codec import G723Decoder, G723Encoder
from .comfort_noise import ComfortNoiseDecoder, ComfortNoiseEncoder
from .media.codecs import CODECS

_S24_SIGN_EXTENSION = bytes(0xFF if value & 0x80 else 0x00 for value in range(256))


def _rtp_encoding(fmt: AudioFormat | RtpPcmFormat) -> str:
    return getattr(fmt, "encoding", "")


def _audio_format(fmt: AudioFormat | RtpPcmFormat) -> AudioFormat:
    return fmt.audio_format if isinstance(fmt, RtpPcmFormat) else fmt


def pcm_to_rtp_payload(data: bytes, fmt: AudioFormat | RtpPcmFormat) -> bytes:
    encoding = _rtp_encoding(fmt)
    if encoding.startswith("G726-"):
        encoder = G726Encoder(encoding, _audio_format(fmt).frame_ms)
        try:
            return encoder.encode(data)
        finally:
            encoder.close()
    if encoding == "G723":
        return G723Encoder(_audio_format(fmt).frame_ms, fmtp=getattr(fmt, "fmtp", "")).encode(data)
    if encoding == "CN":
        return ComfortNoiseEncoder(_audio_format(fmt).sample_rate, _audio_format(fmt).frame_ms).encode(data)
    if encoding == "PCM":
        if len(data) % 2:
            raise ValueError("Dahua PCM frame length is not sample-aligned")
        return data
    if encoding == "PCMA":
        return g711.s16le_to_alaw(data)
    if encoding == "PCMU":
        return g711.s16le_to_ulaw(data)
    if encoding == "OPUS":
        return OpusEncoder(fmt.sample_rate, fmt.channels).encode(data)
    if encoding == "G722":
        return G722Encoder(frame_ms=_audio_format(fmt).frame_ms).encode(data)
    fmt = _audio_format(fmt)
    if fmt.pcm_format == PcmFormat.S16LE:
        if len(data) % 2:
            raise ValueError("s16le frame length is not sample-aligned")
        out = bytearray(len(data))
        out[0::2] = data[1::2]
        out[1::2] = data[0::2]
        return bytes(out)
    if fmt.pcm_format == PcmFormat.S24LE:
        if len(data) % 3:
            raise ValueError("s24le frame length is not sample-aligned")
        out = bytearray(len(data))
        out[0::3] = data[2::3]
        out[1::3] = data[1::3]
        out[2::3] = data[0::3]
        return bytes(out)
    if fmt.pcm_format == PcmFormat.S24LE_IN_S32:
        if len(data) % 4:
            raise ValueError("s24le_in_s32 frame length is not sample-aligned")
        samples = len(data) // 4
        out = bytearray(samples * 3)
        out[0::3] = data[2::4]
        out[1::3] = data[1::4]
        out[2::3] = data[0::4]
        return bytes(out)
    raise ValueError(f"{fmt.pcm_format.value} has no RTP mapping")


def rtp_payload_to_pcm(payload: bytes, fmt: AudioFormat | RtpPcmFormat) -> bytes:
    encoding = _rtp_encoding(fmt)
    if encoding.startswith("G726-"):
        return G726Decoder(encoding, _audio_format(fmt).frame_ms).decode(payload)
    if encoding == "G723":
        return G723Decoder(_audio_format(fmt).frame_ms).decode(payload)
    if encoding == "CN":
        return ComfortNoiseDecoder(_audio_format(fmt).sample_rate, _audio_format(fmt).frame_ms).decode(payload)
    if encoding == "PCM":
        if len(payload) % 2:
            raise ValueError("Dahua PCM payload length is not sample-aligned")
        return payload
    if encoding == "PCMA":
        return g711.alaw_to_s16le(payload)
    if encoding == "PCMU":
        return g711.ulaw_to_s16le(payload)
    if encoding == "OPUS":
        return OpusDecoder(fmt.sample_rate, fmt.channels).decode(payload)
    if encoding == "G722":
        return G722Decoder(frame_ms=_audio_format(fmt).frame_ms).decode(payload)
    fmt = _audio_format(fmt)
    if fmt.pcm_format == PcmFormat.S16LE:
        if len(payload) % 2:
            raise ValueError("L16 payload length is not sample-aligned")
        out = bytearray(len(payload))
        out[0::2] = payload[1::2]
        out[1::2] = payload[0::2]
        return bytes(out)
    if fmt.pcm_format == PcmFormat.S24LE:
        if len(payload) % 3:
            raise ValueError("L24 payload length is not sample-aligned")
        out = bytearray(len(payload))
        out[0::3] = payload[2::3]
        out[1::3] = payload[1::3]
        out[2::3] = payload[0::3]
        return bytes(out)
    if fmt.pcm_format == PcmFormat.S24LE_IN_S32:
        if len(payload) % 3:
            raise ValueError("L24 payload length is not sample-aligned")
        samples = len(payload) // 3
        out = bytearray(samples * 4)
        out[0::4] = payload[2::3]
        out[1::4] = payload[1::3]
        out[2::4] = payload[0::3]
        out[3::4] = payload[0::3].translate(_S24_SIGN_EXTENSION)
        return bytes(out)
    raise ValueError(f"{fmt.pcm_format.value} has no RTP mapping")


class RtpPayloadDecoder:
    def __init__(self, fmt: RtpPcmFormat) -> None:
        self.fmt = fmt
        pcm_format = fmt.audio_format
        self._codec = (
            OpusDecoder(pcm_format.sample_rate, pcm_format.channels, pcm_format.frame_ms)
            if fmt.encoding == "OPUS"
            else G722Decoder(frame_ms=pcm_format.frame_ms)
            if fmt.encoding == "G722"
            else G726Decoder(fmt.encoding, pcm_format.frame_ms)
            if fmt.encoding.startswith("G726-")
            else G723Decoder(pcm_format.frame_ms)
            if fmt.encoding == "G723"
            else ComfortNoiseDecoder(pcm_format.sample_rate, pcm_format.frame_ms)
            if fmt.encoding == "CN"
            else None
        )

    def decode(self, payload: bytes) -> bytes:
        if self._codec is not None:
            return self._codec.decode(payload)
        return rtp_payload_to_pcm(payload, self.fmt)

    def close(self):
        close = getattr(self._codec, "close", None)
        if close is not None:
            close()

    def generate_comfort_noise(self, samples=None):
        """Continue the current SID period without creating another decoder."""
        if self.fmt.encoding != "CN":
            raise ValueError("The decoder does not own a comfort-noise mapping")
        return self._codec.generate(samples)

    def end_comfort_noise(self):
        if self.fmt.encoding == "CN":
            self._codec.reset()


class RtpPayloadEncoder:
    def __init__(self, fmt: RtpPcmFormat, *, send_formats=(), match_received_codec=False,
                 input_format: AudioFormat | None = None) -> None:
        self.input_format = input_format or fmt.audio_format
        if self.input_format.frame_ms != fmt.audio_format.frame_ms:
            raise ValueError("Encoder input must preserve the RTP packet duration")
        self.send_formats = tuple(send_formats) or (fmt,)
        self.match_received_codec = bool(match_received_codec)
        self._converter = None
        self._set_format(fmt)
        if self.input_format != fmt.audio_format:
            from .audio_pcm import PcmFrameConverter

            self._converter = PcmFrameConverter(self.input_format, fmt.audio_format)

    def _set_format(self, fmt: RtpPcmFormat) -> None:
        pcm_format = fmt.audio_format
        codec = (
            OpusEncoder(pcm_format.sample_rate, pcm_format.channels, pcm_format.frame_ms, fmtp=fmt.fmtp)
            if fmt.encoding == "OPUS"
            else G722Encoder(frame_ms=pcm_format.frame_ms)
            if fmt.encoding == "G722"
            else G726Encoder(fmt.encoding, pcm_format.frame_ms)
            if fmt.encoding.startswith("G726-")
            else G723Encoder(pcm_format.frame_ms, fmtp=fmt.fmtp)
            if fmt.encoding == "G723"
            else ComfortNoiseEncoder(pcm_format.sample_rate, pcm_format.frame_ms)
            if fmt.encoding == "CN"
            else None
        )
        previous = getattr(self, "_codec", None)
        self.fmt = fmt
        self._codec = codec
        close = getattr(previous, "close", None)
        if close is not None:
            close()

    @property
    def has_pending_pcm(self) -> bool:
        """Keep a retired input fragment ahead of any direct payload relay."""
        return self._converter is not None and self._converter.has_pending_pcm

    def retarget_input(self, input_format):
        """Change PCM input while retaining the effective native encoder state."""
        self.prepare_retarget_input(input_format)()

    def prepare_retarget_input(self, input_format):
        """Prepare input conversion without draining the current encoder FIFO."""
        from copy import deepcopy
        from .audio_pcm import PcmFrameConverter

        if input_format == self.input_format:
            return lambda: None
        converter = PcmFrameConverter(input_format, self.fmt.audio_format)
        previous = deepcopy(self._converter)
        pending = previous.drain_pending_pcm() if previous is not None else b""
        converter.prepend_pending_pcm(pending)
        def commit():
            self.input_format = input_format
            self._converter = converter if input_format != self.fmt.audio_format or pending else None
        return commit

    def follow_received(self, received: RtpPcmFormat) -> bool:
        """Follow a valid received codec only through the negotiated TX mapping.

        Like Asterisk's symmetric-codec policy, this never assumes that a
        received payload number is also the peer's receive payload number.
        Keep the input PCM contract and packet cadence stable for consumers.
        """
        if not self.match_received_codec:
            return False
        if (spec := CODECS.get(received.encoding)) is not None and spec.auxiliary:
            return False
        selected = next((fmt for fmt in self.send_formats if (
            fmt.encoding == received.encoding and fmt.sample_rate == received.sample_rate
            and fmt.channels == received.channels
            and fmt.audio_format.frame_ms == self.input_format.frame_ms
        )), None)
        if selected is None or selected == self.fmt:
            return False
        if self._converter is not None and self._converter.has_pending_pcm:
            return False
        from .audio_pcm import PcmFrameConverter

        converter = PcmFrameConverter(self.input_format, selected.audio_format)
        self._set_format(selected)
        self._converter = converter if self.input_format != selected.audio_format else None
        return True

    def encode(self, pcm: bytes) -> bytes:
        if self._converter is not None:
            frames = self._converter.convert(pcm)
            if len(frames) != 1:
                raise ValueError("Symmetric codec conversion must preserve packet duration")
            pcm = frames[0]
        if self._codec is not None:
            return self._codec.encode(pcm)
        return pcm_to_rtp_payload(pcm, self.fmt)

    def close(self):
        close = getattr(self._codec, "close", None)
        if close is not None:
            close()
