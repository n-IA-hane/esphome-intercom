"""Runtime capability probes for optional HA-side SIP codecs."""

from __future__ import annotations

from functools import lru_cache
import logging

_LOGGER = logging.getLogger(__name__)


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
