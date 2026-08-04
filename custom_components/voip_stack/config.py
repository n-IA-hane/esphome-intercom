"""Configuration normalization for VoIP Stack."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ASSIST_ADVANCED_CALL_CONTEXT,
    CONF_ASSIST_ENDPOINT_ENABLED,
    CONF_ASSIST_EXTENSION,
    CONF_ASSIST_PIPELINE,
    CONF_AUTOMATION_ROUTING_ENABLED,
    CONF_SIP_VIDEO,
    CONF_VIDEO_CAMERA_SEND,
    CONF_VIDEO_TRANSCODING,
    CONF_REGISTRAR_ENABLED,
    CONF_TRUNK_AUTH_USERNAME,
    CONF_TRUNK_DOMAIN,
    CONF_TRUNK_DTMF_ENABLED,
    CONF_TRUNK_DTMF_TERMINATOR,
    CONF_TRUNK_DTMF_TIMEOUT_MS,
    CONF_TRUNK_ENABLED,
    CONF_TRUNK_EXPIRES,
    CONF_TRUNK_INBOUND_DEFAULT_TARGET,
    CONF_TRUNK_INBOUND_MODE,
    CONF_TRUNK_OUTBOUND_PROXY,
    CONF_TRUNK_PASSWORD,
    CONF_TRUNK_PORT,
    CONF_TRUNK_SERVER,
    CONF_TRUNK_TRANSPORT,
    CONF_TRUNK_USERNAME,
    DOMAIN,
    TRUNK_INBOUND_MODE_DIRECT,
    TRUNK_INBOUND_MODE_DTMF,
    VOIP_STACK_RTP_PORT,
    VOIP_STACK_SIP_PORT,
)


def entry_assist_config(entry: ConfigEntry | None = None) -> dict:
    """Return the optional local Assist pipeline endpoint configuration."""
    data = entry.data if entry is not None else {}
    return {
        CONF_ASSIST_ADVANCED_CALL_CONTEXT: bool(
            data.get(CONF_ASSIST_ADVANCED_CALL_CONTEXT, False)
        ),
        CONF_ASSIST_ENDPOINT_ENABLED: bool(
            data.get(CONF_ASSIST_ENDPOINT_ENABLED, False)
        ),
        CONF_ASSIST_EXTENSION: str(data.get(CONF_ASSIST_EXTENSION) or "").strip(),
        CONF_ASSIST_PIPELINE: str(data.get(CONF_ASSIST_PIPELINE) or "").strip(),
    }


def entry_transport_config(entry: ConfigEntry | None = None) -> dict:
    data = entry.data if entry is not None else {}
    return {
        CONF_REGISTRAR_ENABLED: bool(data.get(CONF_REGISTRAR_ENABLED, False)),
        "sip_port": int(data.get("sip_port", VOIP_STACK_SIP_PORT)),
        "rtp_port": int(data.get("rtp_port", VOIP_STACK_RTP_PORT)),
        "advertise_host": (data.get("advertise_host") or "").strip(),
        CONF_SIP_VIDEO: bool(data.get(CONF_SIP_VIDEO, False)),
        CONF_VIDEO_TRANSCODING: bool(data.get(CONF_VIDEO_TRANSCODING, False)),
        CONF_VIDEO_CAMERA_SEND: bool(data.get(CONF_VIDEO_CAMERA_SEND, False)),
    }


def entry_trunk_config(entry: ConfigEntry | None = None) -> dict:
    data = entry.data if entry is not None else {}
    raw_dtmf_value = data.get(CONF_TRUNK_DTMF_TIMEOUT_MS)
    raw_dtmf_timeout = 3000 if raw_dtmf_value in (None, "") else int(raw_dtmf_value)
    dtmf_timeout_ms = (
        raw_dtmf_timeout * 1000 if 0 <= raw_dtmf_timeout <= 10 else raw_dtmf_timeout
    )
    legacy_dtmf = bool(data.get(CONF_TRUNK_DTMF_ENABLED, False)) and dtmf_timeout_ms > 0
    inbound_mode = str(data.get(CONF_TRUNK_INBOUND_MODE) or "").strip().lower()
    if inbound_mode not in {TRUNK_INBOUND_MODE_DIRECT, TRUNK_INBOUND_MODE_DTMF}:
        inbound_mode = (
            TRUNK_INBOUND_MODE_DTMF if legacy_dtmf else TRUNK_INBOUND_MODE_DIRECT
        )
    dtmf_enabled = inbound_mode == TRUNK_INBOUND_MODE_DTMF and dtmf_timeout_ms > 0
    return {
        CONF_TRUNK_ENABLED: bool(data.get(CONF_TRUNK_ENABLED, False)),
        CONF_TRUNK_TRANSPORT: str(data.get(CONF_TRUNK_TRANSPORT) or "udp")
        .strip()
        .lower(),
        CONF_TRUNK_SERVER: str(data.get(CONF_TRUNK_SERVER) or "").strip(),
        CONF_TRUNK_PORT: int(data.get(CONF_TRUNK_PORT) or VOIP_STACK_SIP_PORT),
        CONF_TRUNK_DOMAIN: str(data.get(CONF_TRUNK_DOMAIN) or "").strip(),
        CONF_TRUNK_USERNAME: str(data.get(CONF_TRUNK_USERNAME) or "").strip(),
        CONF_TRUNK_AUTH_USERNAME: str(data.get(CONF_TRUNK_AUTH_USERNAME) or "").strip(),
        CONF_TRUNK_PASSWORD: str(data.get(CONF_TRUNK_PASSWORD) or ""),
        CONF_TRUNK_EXPIRES: int(data.get(CONF_TRUNK_EXPIRES) or 300),
        CONF_TRUNK_OUTBOUND_PROXY: str(
            data.get(CONF_TRUNK_OUTBOUND_PROXY) or ""
        ).strip(),
        CONF_TRUNK_INBOUND_DEFAULT_TARGET: str(
            data.get(CONF_TRUNK_INBOUND_DEFAULT_TARGET) or "HA"
        ).strip()
        or "HA",
        CONF_TRUNK_INBOUND_MODE: inbound_mode,
        CONF_AUTOMATION_ROUTING_ENABLED: bool(
            data.get(CONF_AUTOMATION_ROUTING_ENABLED, False)
        ),
        CONF_TRUNK_DTMF_ENABLED: dtmf_enabled,
        CONF_TRUNK_DTMF_TIMEOUT_MS: max(0, min(10000, dtmf_timeout_ms)),
        CONF_TRUNK_DTMF_TERMINATOR: str(
            data.get(CONF_TRUNK_DTMF_TERMINATOR) or ""
        ).strip(),
    }


def transport_config(hass: HomeAssistant) -> dict:
    return hass.data.get(DOMAIN, {}).get(
        "transport_config",
        {
            "sip_port": VOIP_STACK_SIP_PORT,
            "rtp_port": VOIP_STACK_RTP_PORT,
            "advertise_host": "",
        },
    )


def trunk_config(hass: HomeAssistant) -> dict:
    return hass.data.get(DOMAIN, {}).get("trunk_config", entry_trunk_config(None))


def debug_mode(hass: HomeAssistant) -> bool:
    from .const import CONF_DEBUG_MODE

    return bool(hass.data.get(DOMAIN, {}).get(CONF_DEBUG_MODE, False))


def media_capture_enabled(hass: HomeAssistant) -> bool:
    """Return whether the user explicitly enabled private media captures."""

    from .const import CONF_MEDIA_CAPTURE

    return bool(hass.data.get(DOMAIN, {}).get(CONF_MEDIA_CAPTURE, False))


def trunk_enabled(cfg: dict) -> bool:
    return bool(
        cfg.get(CONF_TRUNK_ENABLED)
        and cfg.get(CONF_TRUNK_SERVER)
        and cfg.get(CONF_TRUNK_USERNAME)
        and cfg.get(CONF_TRUNK_PASSWORD)
    )
