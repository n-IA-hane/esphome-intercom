"""SIP phone device resolver.

Registry structure is stable enough to cache, but endpoint availability is not:
phonebook rebuilds must parse the current HA states every time so reconnects
cannot keep an ESP out of the roster with a stale cached device list.
"""

from __future__ import annotations

import logging
from typing import Optional

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .audio_format import parse_audio_format_list
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
_CACHE_KEY = "device_resolver"


def _format_tokens(formats: list) -> list[str]:
    return [fmt.wire_token() for fmt in formats]


def slugify_route_id(raw: str) -> str:
    """Match the slug ESPHome uses for `esphome.{slug}_start_call` services."""
    return "".join(c if c.isalnum() else "_" for c in (raw or "").lower()).strip("_")


def _esphome_entry_for_host(hass: HomeAssistant, host: str):
    for entry in hass.config_entries.async_entries("esphome"):
        if entry.data.get("host") == host:
            return entry
    return None


def _valid_port(value: str) -> int | None:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def _valid_audio_mode(value: str | None) -> str | None:
    mode = (value or "full_duplex").strip().lower()
    if mode in ("full_duplex", "mic_only", "speaker_only"):
        return mode
    return None


def _parse_bool(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _sip_video_codec(extras: list[str]) -> str:
    """Return the compile-time SIP video codec advertised by an ESP endpoint."""
    for token in extras:
        key, separator, value = token.partition("=")
        if separator and key.strip().casefold() == "video":
            codec = value.strip().casefold()
            return codec if codec in {"jpeg", "h264"} else ""
    return ""


def parse_voip_endpoint(value: str | None) -> dict | None:
    """Parse the project endpoint standard published by ESP voip_stack.

    Name|host|sip_port|rtp_port|audio_mode|tx_formats|rx_formats|sip_tcp|extension[|extras...]

    Group membership is intentionally not carried in this state payload. ESP
    devices publish group membership through sibling voip_stack text/switch
    entities so the endpoint state stays below Home Assistant's state limit.
    """
    if not value:
        return None
    text = value.strip()
    if not text or text.lower() in ("unknown", "unavailable"):
        return None
    parts = [part.strip() for part in text.split("|")]
    if len(parts) < 5:
        return None

    name, host = parts[0], parts[1]
    if not name or not host:
        return None

    def parse_formats(first: int) -> tuple[str, list, list] | None:
        mode = _valid_audio_mode(parts[first] if len(parts) > first else None)
        if mode is None:
            _LOGGER.warning("Ignoring voip endpoint with unsupported audio role: %r", text)
            return None
        try:
            tx_formats = parse_audio_format_list(parts[first + 1] if len(parts) > first + 1 else None)
            rx_formats = parse_audio_format_list(parts[first + 2] if len(parts) > first + 2 else None)
        except ValueError as err:
            _LOGGER.warning("Invalid voip endpoint audio formats in %r: %s", text, err)
            return None
        if not tx_formats or not rx_formats:
            _LOGGER.warning("Ignoring voip endpoint without explicit SIP PCM formats: %r", text)
            return None
        return mode, tx_formats, rx_formats

    primary_port = _valid_port(parts[2])
    secondary_port = _valid_port(parts[3])
    if primary_port is None or secondary_port is None:
        return None
    if len(parts) == 5:
        _LOGGER.warning("Ignoring voip endpoint using obsolete no-format shape: %r", text)
        return None
    if len(parts) < 8:
        return None
    parsed_tail = parse_formats(4)
    if parsed_tail is None:
        return None
    mode, tx_formats, rx_formats = parsed_tail
    transport_token = parts[7].lower()
    if transport_token not in ("sip_tcp", "sip_udp"):
        return None
    sip_transport = "tcp" if transport_token == "sip_tcp" else "udp"
    extras = parts[9:] if len(parts) > 9 else []
    return {
        "name": name,
        "sip_transport": sip_transport,
        "host": host,
        "sip_port": primary_port,
        "rtp_port": secondary_port,
        "audio_mode": mode,
        "tx_formats": tx_formats,
        "rx_formats": rx_formats,
        "extension": parts[8] if len(parts) >= 9 else "",
        "extras": extras,
        "sip_video_codec": _sip_video_codec(extras),
    }


class VoipDeviceResolver:
    """Single source of truth for "which VoIP devices exist"."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def install_listeners(self) -> None:
        """Compatibility hook; device resolution reads live state on demand."""

    def route_id_for_host(self, host: str) -> str:
        """ESPHome node_name slug for `host`, used as ESPHome service prefix."""
        entry = _esphome_entry_for_host(self.hass, host)
        if entry is None:
            # Fallback: hostname (.local) in entry.data["host"] vs IP from
            # device_registry. Walk device_registry for any device whose
            # connections include this IP, then walk its config_entries.
            entry = self._esphome_entry_via_device(host)
            if entry is None:
                return ""
        raw = entry.data.get("device_name") or entry.title or ""
        return slugify_route_id(raw)

    def sip_uri_user_for_host(self, host: str, device=None) -> str:
        """Return the stable ESPHome node name used as the SIP URI user."""

        entry = _esphome_entry_for_host(self.hass, host)
        if entry is None and device is not None:
            for entry_id in device.config_entries:
                candidate = self.hass.config_entries.async_get_entry(entry_id)
                if candidate is not None and candidate.domain == "esphome":
                    entry = candidate
                    break
        if entry is None:
            return ""
        return str(entry.data.get("device_name") or "").strip()

    def _esphome_entry_via_device(self, host: str):
        device_registry = dr.async_get(self.hass)
        for device in device_registry.devices.values():
            for conn_type, conn_value in device.connections:
                if conn_value != host:
                    continue
                if 'ip' not in conn_type.lower() and conn_type != 'network_ip':
                    continue
                for entry_id in device.config_entries:
                    entry = self.hass.config_entries.async_get_entry(entry_id)
                    if entry and entry.domain == "esphome":
                        return entry
        return None

    async def list_devices(self) -> list[dict]:
        """Return current VoIP devices by parsing live endpoint states."""
        entity_registry = er.async_get(self.hass)
        device_registry = dr.async_get(self.hass)

        # First pass: device IDs owning an voip_endpoint entity, plus
        # an entities-by-device bucket for the second pass.
        voip_device_ids: set[str] = set()
        entities_by_device: dict[str, list] = {}
        for entity in entity_registry.entities.values():
            if entity.device_id is None:
                continue
            # This resolver is the legacy ESPHome transport inventory.  The
            # integration also exposes logical browser-phone entities whose
            # object ids intentionally contain ``voip_endpoint``; treating
            # those as ESP devices produces a misleading "missing endpoint"
            # warning and can hide the canonical logical endpoint path.
            if str(getattr(entity, "platform", "") or "") == DOMAIN:
                continue
            entities_by_device.setdefault(entity.device_id, []).append(entity)
            if "voip_endpoint" in entity.entity_id:
                voip_device_ids.add(entity.device_id)

        out: list[dict] = []
        for device_id in sorted(voip_device_ids):
            device = device_registry.async_get(device_id)
            if not device:
                continue

            esphome_id = self._device_esphome_id(device)
            entities = self._collect_entities(entities_by_device.get(device_id, []))
            endpoint_entity_id = entities.get("voip_endpoint")
            endpoint_state = self.hass.states.get(endpoint_entity_id) if endpoint_entity_id else None
            endpoint = parse_voip_endpoint(endpoint_state.state if endpoint_state else None)
            if endpoint is None:
                _LOGGER.debug(
                    "Skipping VoIP device %s: missing/invalid voip_endpoint",
                    device.name or esphome_id or device_id,
                )
                continue
            route_id = self.route_id_for_host(endpoint["host"])
            sip_uri_user = self.sip_uri_user_for_host(endpoint["host"], device)
            if not route_id:
                route_id = self._route_id_from_device(device)
            if route_id:
                entities["start_call_service"] = f"esphome.{route_id}_start_call"
            ring_group = self._state_value(entities.get("voip_ring_groups"))
            conference_group = self._state_value(entities.get("voip_conference_groups"))
            extension = self._state_value(entities.get("voip_extension")) or endpoint.get("extension") or ""
            camera_entity_id = entities.get("camera", "")
            capabilities = {"audio", "dtmf"}
            if endpoint.get("sip_video_codec"):
                capabilities.add("video")
            if camera_entity_id:
                capabilities.add("camera_preview")

            out.append({
                "device_id": device_id,
                "name": endpoint["name"],
                "sip_uri_user": sip_uri_user,
                "route_id": route_id,
                "host": endpoint["host"],
                "sip_port": endpoint.get("sip_port"),
                "rtp_port": endpoint.get("rtp_port"),
                "sip_transport": endpoint.get("sip_transport") or "",
                "extension": extension,
                "conference_group": conference_group,
                "conference_ring": _parse_bool(self._state_value(entities.get("voip_conference_ring"))),
                "ring_group": ring_group,
                "audio_mode": endpoint["audio_mode"],
                "tx_formats": _format_tokens(endpoint["tx_formats"]),
                "rx_formats": _format_tokens(endpoint["rx_formats"]),
                "sip_video_codec": endpoint.get("sip_video_codec") or "",
                "camera_entity_id": camera_entity_id,
                "capabilities": sorted(capabilities),
                "esphome_id": esphome_id,
                "entities": entities,
            })

        return out

    async def resolve_target(self, call: ServiceCall) -> Optional[dict]:
        """Match a service call's target selector to one of our devices."""
        device_ids: set[str] = set()
        entity_registry = er.async_get(self.hass)
        for source in [call.data, getattr(call, "target", None) or {}]:
            ids = source.get("device_id")
            if isinstance(ids, str):
                device_ids.add(ids)
            elif isinstance(ids, list):
                device_ids.update(ids)
            eids = source.get("entity_id")
            if isinstance(eids, str):
                eids = [eids]
            if eids:
                for eid in eids:
                    entry = entity_registry.async_get(eid)
                    if entry and entry.device_id:
                        device_ids.add(entry.device_id)
        if not device_ids:
            return None
        for dev in await self.list_devices():
            if dev["device_id"] in device_ids:
                return dev
        return None

    def _route_id_from_device(self, device) -> str:
        """Walk device.config_entries; slugify the linked esphome entry."""
        for entry_id in device.config_entries:
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry and entry.domain == "esphome":
                raw = entry.data.get("device_name") or entry.title or ""
                return slugify_route_id(raw)
        return ""

    @staticmethod
    def _device_esphome_id(device) -> Optional[str]:
        for domain, identifier in device.identifiers:
            if domain == "esphome":
                return identifier
        return None

    @staticmethod
    def _collect_entities(entities) -> dict[str, str]:
        out: dict[str, str] = {}
        for entity in entities:
            eid = entity.entity_id
            if eid.startswith("camera.") and "camera" not in out:
                out["camera"] = eid
            elif "voip_state" in eid and "voip_state" not in out:
                out["voip_state"] = eid
            elif "voip_endpoint" in eid and "voip_endpoint" not in out:
                out["voip_endpoint"] = eid
            elif "voip_extension" in eid and "voip_extension" not in out:
                out["voip_extension"] = eid
            elif "voip_transport" in eid and "voip_transport" not in out:
                # source of truth for "udp"/"tcp".
                out["voip_transport"] = eid
            elif "voip_ring_groups" in eid and "voip_ring_groups" not in out:
                out["voip_ring_groups"] = eid
            elif "voip_conference_groups" in eid and "voip_conference_groups" not in out:
                out["voip_conference_groups"] = eid
            elif "voip_ring_on_conference" in eid and "voip_conference_ring" not in out:
                out["voip_conference_ring"] = eid
            elif eid.startswith("switch.") and "auto_answer" in eid and "auto_answer" not in out:
                out["auto_answer"] = eid
            elif (
                eid.startswith("switch.")
                and ("do_not_disturb" in eid or eid.endswith("_dnd") or "_dnd_" in eid)
                and "dnd" not in out
            ):
                out["dnd"] = eid
            elif ("incoming_caller" in eid or eid.endswith("_caller")) and "incoming_caller" not in out:
                out["incoming_caller"] = eid
            elif "destination" in eid and "destination" not in out:
                out["destination"] = eid
            elif (
                ("voip_last_reason" in eid or "last_reason" in eid or "end_reason" in eid)
                and "last_reason" not in out
            ):
                out["last_reason"] = eid
            elif eid.startswith("button.") and "previous" in eid and "previous" not in out:
                out["previous"] = eid
            elif eid.startswith("button.") and "next" in eid and "next" not in out:
                out["next"] = eid
            elif eid.startswith("button.") and "decline" in eid and "decline" not in out:
                out["decline"] = eid
            elif eid.startswith("button.") and "call" in eid and "decline" not in eid and "call" not in out:
                out["call"] = eid
        return out

    def _state_value(self, entity_id: str | None) -> str:
        if not entity_id:
            return ""
        state = self.hass.states.get(entity_id)
        if state is None or str(state.state or "").strip().lower() in ("unknown", "unavailable"):
            return ""
        return str(state.state or "").strip()


def get_resolver(hass: HomeAssistant) -> VoipDeviceResolver:
    bucket = hass.data.setdefault(DOMAIN, {})
    resolver = bucket.get(_CACHE_KEY)
    if resolver is None:
        resolver = VoipDeviceResolver(hass)
        resolver.install_listeners()
        bucket[_CACHE_KEY] = resolver
    return resolver
