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


def codec_available(name: str, *, receive: bool = False) -> bool:
    """Share one directional result across default and keyword call forms."""
    return _probe_codec_available(name, receive)


@lru_cache(maxsize=64)
def _probe_codec_available(name: str, receive: bool) -> bool:
    """Probe the actual adapter in one direction, including two live packets."""
    from .media.codecs import CODECS

    spec = CODECS.get(name)
    if spec is None:
        return False
    if name in {"L16", "L24", "PCMA", "PCMU", "PCM"}:
        return True
    adapter = None
    try:
        from .rtp_payload import RtpPayloadDecoder, RtpPayloadEncoder
        from .sdp_types import RtpPcmFormat

        rate = spec.conventional_clock_rate or spec.static_clock_rate or spec.decoded_rate or 8000
        channels = spec.conventional_channels if spec.conventional_clock_rate else 1
        fmt = RtpPcmFormat(96, name, rate, channels, spec.assumed_packet_ms or 20)
        adapter = RtpPayloadDecoder(fmt) if receive else RtpPayloadEncoder(fmt)
        if receive:
            if name == "OPUS":
                payload = b"\xf8\xff\xfe"  # An independently specified Opus silence packet.
            elif name == "G723":
                payload = bytes([2, 0, 0, 0])  # RFC-tagged SID, independent of the encoder.
            elif name == "CN":
                payload = bytes([64])
            else:
                payload = bytes(round(rate * fmt.frame_ms / 1000 * spec.octets_per_sample))
            expected = fmt.audio_format.nominal_frame_bytes
            return all(len(adapter.decode(payload)) == expected for _ in range(2))
        pcm = bytes(fmt.audio_format.nominal_frame_bytes)
        return all(bool(adapter.encode(pcm)) for _ in range(2))
    except Exception as err:
        _LOGGER.debug(
            "Optional SIP codec unavailable encoding=%s direction=%s (%s)",
            name, "receive" if receive else "send", type(err).__name__,
        )
        return False
    finally:
        if adapter is not None:
            adapter.close()


def common_sip_codecs() -> frozenset[str]:
    """Return optional codecs safe to advertise for both RTP directions."""
    from .media.codecs import CODECS

    return frozenset(name for name, spec in CODECS.items()
                     if (spec.pyav_encoder or spec.pyav_decoder or spec.auxiliary)
                     and codec_available(name) and codec_available(name, receive=True))


def codec_directions(name: str) -> dict[str, bool]:
    """Expose the two independently probed capabilities without another store."""
    return {"send": codec_available(name), "receive": codec_available(name, receive=True)}


def preload_codec_capabilities() -> None:
    """Fill the existing directional cache from an executor before loop users.

    Probe both directions independently: an absent encoder must not leave the
    decoder's first library lookup to a later SDP or configuration request.
    """
    from .media.codecs import CODECS

    for name in CODECS:
        codec_available(name)
        codec_available(name, receive=True)


# Keep the existing cache inspection/invalidation API on the public boundary.
codec_available.cache_clear = _probe_codec_available.cache_clear
codec_available.cache_info = _probe_codec_available.cache_info
codec_available.cache_parameters = _probe_codec_available.cache_parameters
common_sip_codecs.cache_clear = codec_available.cache_clear
