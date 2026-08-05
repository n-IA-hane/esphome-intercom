"""Runtime capability probes for optional HA-side SIP codecs."""

from __future__ import annotations

from functools import lru_cache
import logging

_LOGGER = logging.getLogger(__name__)


def supports_dahua_pcm(user_agent: str | None) -> bool:
    """Return whether a peer identifies the Dahua UAC media profile.

    Dahua door stations advertise an unregistered ``PCM/16000`` RTP encoding
    whose samples are signed 16-bit little-endian.  Keep this vendor profile
    narrow so standard ``L16`` peers retain RFC 3551 network byte order.
    """

    return str(user_agent or "").strip().casefold().startswith("dahua uac/")


def _pyav_codec_available(decoder: str, encoder: str) -> bool:
    """Return whether PyAV exposes both halves of one full-duplex codec."""

    try:
        import av

        av.CodecContext.create(decoder, "r")
        av.CodecContext.create(encoder, "w")
    except Exception as err:  # pragma: no cover - runtime build dependent.
        _LOGGER.debug(
            "Optional SIP codec unavailable decoder=%s encoder=%s: %s",
            decoder,
            encoder,
            err,
        )
        return False
    return True


@lru_cache(maxsize=1)
def common_sip_codecs() -> frozenset[str]:
    """Return optional codecs safe to advertise for both RTP directions."""

    available: set[str] = set()
    if _pyav_codec_available("opus", "libopus"):
        available.add("OPUS")
    if _pyav_codec_available("g722", "g722"):
        available.add("G722")
    return frozenset(available)
