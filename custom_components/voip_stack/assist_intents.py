"""Optional Home Assistant Assist intent handlers for VoIP Stack."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import intent
from homeassistant.helpers.intent import Intent, IntentHandler, IntentResponse

from .const import DOMAIN
from .device_registry_compat import device_config_entry_ids

_LOGGER = logging.getLogger(__name__)

INTENT_CALL = "VoipCall"
INTENT_HANGUP = "VoipHangup"
INTENT_ANSWER = "VoipAnswer"
INTENT_DECLINE = "VoipDecline"
INTENT_TYPES = (INTENT_CALL, INTENT_HANGUP, INTENT_ANSWER, INTENT_DECLINE)


@dataclass(frozen=True)
class ContactResolution:
    """Result of resolving a spoken contact name against the live phonebook."""

    canonical: str = ""
    error: str = ""
    matches: tuple[str, ...] = ()
    source: str = "contact"


@dataclass(frozen=True)
class ContactCandidate:
    """Searchable phonebook entry for Assist target resolution."""

    canonical: str
    tokens: tuple[str, ...]
    source: str = "contact"


def _slot_value(intent_obj: Intent, name: str) -> str:
    slot = intent_obj.slots.get(name) or {}
    value = slot.get("value", "") if isinstance(slot, dict) else ""
    return str(value or "").strip()


def _normalize_contact(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _compact_contact(value: str) -> str:
    return "".join(ch for ch in _normalize_contact(value) if ch.isalnum())


def _metadata_tokens(metadata: dict[str, Any]) -> list[str]:
    tokens: list[str] = []
    for key in ("alias", "aliases", "area", "room", "zone", "friendly_name"):
        value = metadata.get(key)
        if isinstance(value, str):
            tokens.append(value)
        elif isinstance(value, (list, tuple, set)):
            tokens.extend(str(item) for item in value)
    return tokens


def _resolve_contact_name(raw_name: str, contacts: list[ContactCandidate]) -> ContactResolution:
    """Resolve a spoken contact to exactly one canonical phonebook contact."""
    normalized = _normalize_contact(raw_name)
    compact = _compact_contact(raw_name)
    if not normalized:
        return ContactResolution(error="missing")

    matches: list[ContactCandidate] = []
    seen: set[str] = set()
    for contact in contacts:
        canonical = str(contact.canonical or "").strip()
        if not canonical:
            continue
        token_set = {_normalize_contact(token) for token in contact.tokens if str(token or "").strip()}
        compact_set = {_compact_contact(token) for token in contact.tokens if str(token or "").strip()}
        if normalized not in token_set and compact not in compact_set:
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        matches.append(contact)

    if not matches:
        return ContactResolution(error="not_found")
    if len(matches) > 1:
        return ContactResolution(error="ambiguous", matches=tuple(item.canonical for item in matches))
    match = matches[0]
    return ContactResolution(canonical=match.canonical, matches=(match.canonical,), source=match.source)


def _response(intent_obj: Intent, speech: str) -> IntentResponse:
    response = intent_obj.create_response()
    response.async_set_speech(speech)
    return response


async def _voip_devices(hass: HomeAssistant) -> list[dict[str, Any]]:
    from .websocket_api import _get_voip_devices

    return await _get_voip_devices(hass)


async def _origin_device(intent_obj: Intent) -> dict[str, Any] | None:
    from homeassistant.helpers import device_registry as dr

    device_id = str(intent_obj.device_id or "").strip()
    if not device_id:
        _LOGGER.info("Assist VoIP command has no source device_id")
        return None
    devices = await _voip_devices(intent_obj.hass)
    for device in devices:
        if str(device.get("device_id") or "") == device_id:
            return device

    device_registry = dr.async_get(intent_obj.hass)
    source_device = device_registry.async_get(device_id)
    if source_device is None:
        _LOGGER.info("Assist VoIP command source device_id=%s is not in HA device registry", device_id)
        return None

    source_entries = set(device_config_entry_ids(source_device))
    if source_entries:
        entry_matches: list[dict[str, Any]] = []
        for device in devices:
            registry_device = device_registry.async_get(str(device.get("device_id") or ""))
            if registry_device is None:
                continue
            if source_entries.intersection(
                set(device_config_entry_ids(registry_device))
            ):
                entry_matches.append(device)
        if len(entry_matches) == 1:
            _LOGGER.info(
                "Assist VoIP source %s mapped to %s by shared config entry",
                source_device.name or device_id,
                entry_matches[0].get("name"),
            )
            return entry_matches[0]

    if source_device.area_id:
        area_matches: list[dict[str, Any]] = []
        for device in devices:
            registry_device = device_registry.async_get(str(device.get("device_id") or ""))
            if registry_device is not None and registry_device.area_id == source_device.area_id:
                area_matches.append(device)
        if len(area_matches) == 1:
            _LOGGER.info(
                "Assist VoIP source %s mapped to %s by HA area",
                source_device.name or device_id,
                area_matches[0].get("name"),
            )
            return area_matches[0]
        if area_matches:
            _LOGGER.info(
                "Assist VoIP source %s area has multiple VoIP devices: %s",
                source_device.name or device_id,
                ", ".join(str(item.get("name") or "") for item in area_matches),
            )

    _LOGGER.info(
        "Assist VoIP command source %s is not a VoIP device and could not be mapped",
        source_device.name or device_id,
    )
    return None


async def _phonebook_contacts(hass: HomeAssistant) -> list[ContactCandidate]:
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import device_registry as dr

    from .roster import parse_roster_json

    devices = await _voip_devices(hass)
    area_registry = ar.async_get(hass)
    device_registry = dr.async_get(hass)
    contacts: list[ContactCandidate] = []

    def area_name_for_device(device_id: str) -> str:
        registry_device = device_registry.async_get(device_id)
        if registry_device is None or not registry_device.area_id:
            return ""
        area = area_registry.async_get_area(registry_device.area_id)
        return "" if area is None else str(area.name or "")

    for device in devices:
        name = str(device.get("name") or "").strip()
        if not name:
            continue
        tokens = [
            name,
            str(device.get("route_id") or ""),
            str(device.get("esphome_id") or ""),
            area_name_for_device(str(device.get("device_id") or "")),
        ]
        contacts.append(ContactCandidate(canonical=name, tokens=tuple(tokens), source="device"))

    ha_phonebook = hass.states.get("sensor.voip_phonebook")
    if ha_phonebook is not None:
        roster_json = str(ha_phonebook.attributes.get("roster_json") or "")
        if roster_json:
            try:
                for entry in parse_roster_json(roster_json):
                    canonical = entry.display_name
                    tokens = [
                        entry.id,
                        entry.name,
                        entry.number,
                        entry.address,
                        *_metadata_tokens(entry.metadata or {}),
                    ]
                    contacts.append(ContactCandidate(canonical=canonical, tokens=tuple(tokens), source="phonebook"))
            except Exception as err:
                _LOGGER.warning("Assist could not parse VoIP phonebook roster_json: %s", err)
        else:
            raw = str(ha_phonebook.attributes.get("phonebook") or "")
            for row in raw.split(","):
                name = row.split("|", 1)[0].strip()
                if name:
                    contacts.append(ContactCandidate(canonical=name, tokens=(name,), source="phonebook"))

    out: list[ContactCandidate] = []
    seen: set[str] = set()
    for contact in contacts:
        key = _compact_contact(contact.canonical)
        if key and key not in seen:
            seen.add(key)
            out.append(contact)
    return out


async def _resolve_area_contact(hass: HomeAssistant, raw_name: str) -> ContactResolution:
    """Resolve a spoken area name to one VoIP device in that HA area."""
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import device_registry as dr

    normalized = _normalize_contact(raw_name)
    if not normalized:
        return ContactResolution(error="missing", source="area")

    area_registry = ar.async_get(hass)
    matching_areas = [
        area
        for area in area_registry.async_list_areas()
        if _normalize_contact(area.name) == normalized
    ]
    if not matching_areas:
        return ContactResolution(error="not_found", source="area")
    if len(matching_areas) > 1:
        return ContactResolution(
            error="ambiguous_area",
            matches=tuple(area.name for area in matching_areas),
            source="area",
        )

    device_registry = dr.async_get(hass)
    area_id = matching_areas[0].id
    devices: list[str] = []
    for device in await _voip_devices(hass):
        registry_device = device_registry.async_get(str(device.get("device_id") or ""))
        if registry_device is None or registry_device.area_id != area_id:
            continue
        devices.append(str(device.get("name") or "").strip())

    devices = [name for name in devices if name]
    if not devices:
        return ContactResolution(error="area_empty", source="area")
    if len(devices) > 1:
        return ContactResolution(
            error="ambiguous_area_device",
            matches=tuple(devices),
            source="area",
        )
    return ContactResolution(canonical=devices[0], matches=(devices[0],), source="area")


async def _resolve_contact_or_area(hass: HomeAssistant, raw_name: str) -> ContactResolution:
    """Resolve target first as a phonebook contact, then as a HA area."""
    contacts = await _phonebook_contacts(hass)
    contact = _resolve_contact_name(raw_name, contacts)
    if contact.error != "not_found":
        return contact
    area = await _resolve_area_contact(hass, raw_name)
    if area.error == "not_found":
        return contact
    return area


def _ha_peer_name(hass: HomeAssistant) -> str:
    from .websocket_api import _ha_peer_name as resolve_ha_peer_name

    return resolve_ha_peer_name(hass)


async def _start_call_to_ha_peer(
    hass: HomeAssistant,
    origin: dict[str, Any],
    dest_name: str,
    *,
    context: Context | None = None,
) -> None:
    """Ask the originating ESP to call HA by its phonebook peer name."""
    from .phonebook_runtime import available_esphome_services

    route_id = str(origin.get("route_id") or "").strip()
    if not route_id:
        raise ValueError(f"No route_id for VoIP device {origin.get('name')}")

    service_name = f"{route_id}_start_call"
    if service_name not in available_esphome_services(hass):
        raise ValueError(f"ESPHome service esphome.{service_name} is not registered")

    await hass.services.async_call(
        "esphome",
        service_name,
        {"dest": dest_name},
        blocking=True,
        context=context,
    )


class _VoipIntentHandler(IntentHandler):
    """Base handler for local-satellite VoIP commands."""

    intent_type: str = ""

    async def _require_origin(self, intent_obj: Intent) -> dict[str, Any] | IntentResponse:
        origin = await _origin_device(intent_obj)
        if origin is None:
            _LOGGER.info("Assist VoIP command rejected: unknown source device_id=%s", intent_obj.device_id)
            return _response(intent_obj, "I do not know which VoIP device heard that.")
        return origin


class VoipCallIntentHandler(_VoipIntentHandler):
    """Start an VoIP call from the satellite that heard the command."""

    intent_type = INTENT_CALL
    description = (
        "Start a real VoIP phone call from the voice satellite that heard the command. "
        "Use this for requests like 'call kitchen', 'call home', 'call the phone', "
        "or calling an ESPHome VoIP endpoint, Home Assistant softphone, registered "
        "softphone, contact name, area name, or phone number. Do not use broadcast "
        "or announcement tools for call requests."
    )

    @property
    def slot_schema(self) -> dict:
        return {"target": cv.string}

    async def async_handle(self, intent_obj: Intent) -> IntentResponse:
        origin = await self._require_origin(intent_obj)
        if isinstance(origin, IntentResponse):
            return origin

        spoken_target = _slot_value(intent_obj, "target")
        resolved = await _resolve_contact_or_area(intent_obj.hass, spoken_target)
        if resolved.error == "missing":
            _LOGGER.info("Assist VoIP call rejected: missing target source=%s", origin.get("name"))
            return _response(intent_obj, "Which VoIP contact should I call?")
        if resolved.error == "not_found":
            _LOGGER.info(
                "Assist VoIP call rejected: target not found source=%s spoken=%r",
                origin.get("name"),
                spoken_target,
            )
            return _response(intent_obj, f"I cannot find an VoIP contact named {spoken_target}.")
        if resolved.error == "ambiguous":
            _LOGGER.info(
                "Assist VoIP call rejected: ambiguous contact source=%s spoken=%r matches=%s",
                origin.get("name"),
                spoken_target,
                ", ".join(resolved.matches),
            )
            return _response(intent_obj, f"The VoIP contact {spoken_target} is ambiguous.")
        if resolved.error == "ambiguous_area":
            _LOGGER.info(
                "Assist VoIP call rejected: ambiguous area source=%s spoken=%r matches=%s",
                origin.get("name"),
                spoken_target,
                ", ".join(resolved.matches),
            )
            return _response(intent_obj, f"The Home Assistant area {spoken_target} is ambiguous.")
        if resolved.error == "area_empty":
            _LOGGER.info(
                "Assist VoIP call rejected: empty area source=%s spoken=%r",
                origin.get("name"),
                spoken_target,
            )
            return _response(intent_obj, f"The Home Assistant area {spoken_target} has no VoIP device.")
        if resolved.error == "ambiguous_area_device":
            _LOGGER.info(
                "Assist VoIP call rejected: area has multiple VoIP devices source=%s spoken=%r matches=%s",
                origin.get("name"),
                spoken_target,
                ", ".join(resolved.matches),
            )
            return _response(
                intent_obj,
                f"The Home Assistant area {spoken_target} has more than one VoIP device.",
            )

        try:
            if _normalize_contact(resolved.canonical) == _normalize_contact(_ha_peer_name(intent_obj.hass)):
                await _start_call_to_ha_peer(
                    intent_obj.hass,
                    origin,
                    resolved.canonical,
                    context=intent_obj.context,
                )
            else:
                await intent_obj.hass.services.async_call(
                    DOMAIN,
                    "call",
                    {
                        "destination": resolved.canonical,
                        "device_id": origin["device_id"],
                    },
                    blocking=True,
                    context=intent_obj.context,
                )
        except Exception as err:
            _LOGGER.error(
                "Assist VoIP call failed: %s -> %s (spoken=%r): %s",
                origin.get("name"),
                resolved.canonical,
                spoken_target,
                err,
            )
            return _response(intent_obj, f"I could not call {resolved.canonical}.")
        _LOGGER.info(
            "Assist VoIP call: %s -> %s (spoken=%r source=%s)",
            origin.get("name"),
            resolved.canonical,
            spoken_target,
            resolved.source,
        )
        return _response(intent_obj, f"Calling {resolved.canonical}.")


class VoipHangupIntentHandler(_VoipIntentHandler):
    """Hang up the call owned by the satellite that heard the command."""

    intent_type = INTENT_HANGUP
    description = "Hang up the active VoIP call on the voice satellite that heard the command."

    async def async_handle(self, intent_obj: Intent) -> IntentResponse:
        origin = await self._require_origin(intent_obj)
        if isinstance(origin, IntentResponse):
            return origin
        try:
            await intent_obj.hass.services.async_call(
                DOMAIN,
                "hangup",
                {"device_id": origin["device_id"]},
                blocking=True,
                context=intent_obj.context,
            )
        except Exception as err:
            _LOGGER.error("Assist VoIP hangup failed for %s: %s", origin.get("name"), err)
            return _response(intent_obj, "I could not hang up the VoIP call.")
        return _response(intent_obj, "OK.")


class VoipAnswerIntentHandler(_VoipIntentHandler):
    """Answer the call on the satellite that heard the command."""

    intent_type = INTENT_ANSWER
    description = "Answer the ringing VoIP call on the voice satellite that heard the command."

    async def async_handle(self, intent_obj: Intent) -> IntentResponse:
        origin = await self._require_origin(intent_obj)
        if isinstance(origin, IntentResponse):
            return origin
        try:
            await intent_obj.hass.services.async_call(
                DOMAIN,
                "answer",
                {"device_id": origin["device_id"]},
                blocking=True,
                context=intent_obj.context,
            )
        except Exception as err:
            _LOGGER.error("Assist VoIP answer failed for %s: %s", origin.get("name"), err)
            return _response(intent_obj, "I could not answer the VoIP call.")
        return _response(intent_obj, "Answering.")


class VoipDeclineIntentHandler(_VoipIntentHandler):
    """Decline the call on the satellite that heard the command."""

    intent_type = INTENT_DECLINE
    description = "Decline the ringing VoIP call on the voice satellite that heard the command."

    async def async_handle(self, intent_obj: Intent) -> IntentResponse:
        origin = await self._require_origin(intent_obj)
        if isinstance(origin, IntentResponse):
            return origin
        try:
            await intent_obj.hass.services.async_call(
                DOMAIN,
                "decline",
                {
                    "device_id": origin["device_id"],
                    "reason": "declined by voice command",
                },
                blocking=True,
                context=intent_obj.context,
            )
        except Exception as err:
            _LOGGER.error("Assist VoIP decline failed for %s: %s", origin.get("name"), err)
            return _response(intent_obj, "I could not decline the VoIP call.")
        return _response(intent_obj, "Declining.")


def async_register_assist_intents(hass: HomeAssistant) -> None:
    """Register optional Assist handlers."""
    for handler in (
        VoipCallIntentHandler(),
        VoipHangupIntentHandler(),
        VoipAnswerIntentHandler(),
        VoipDeclineIntentHandler(),
    ):
        intent.async_register(hass, handler)
    hass.data.setdefault(DOMAIN, {})["assist_intents_registered"] = True
    _LOGGER.info("VoIP Stack Assist intents registered")


def async_unregister_assist_intents(hass: HomeAssistant) -> None:
    """Unregister optional Assist handlers."""
    for intent_type in INTENT_TYPES:
        intent.async_remove(hass, intent_type)
    hass.data.setdefault(DOMAIN, {}).pop("assist_intents_registered", None)
    _LOGGER.info("VoIP Stack Assist intents unregistered")
