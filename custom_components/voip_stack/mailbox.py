"""Private mailbox configuration and the call-owned media archive boundary."""

from pathlib import Path
import asyncio
import hashlib
import hmac
import secrets
from urllib.parse import quote

from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .core.media_archive import ArchiveError, MediaArchive
from .phone_config import phone_subentry_by_endpoint_id, update_phone_subentry
from .phone_endpoint import EndpointKind
from .runtime_data import require_runtime_data
from .service_endpoints import async_require_phone_service_control
from .store import config_entry

CONF_ESPHOME_MAILBOXES = "esphome_mailboxes"
EVENT_VOICEMAIL_RECEIVED = "voip_stack_voicemail_received"
EVENT_VOICEMAIL_CHANGED = "voip_stack_voicemail_changed"


def archive_path(hass):
    directories = hass.config.media_dirs
    directory = directories.get("local") or next(iter(directories.values()), None)
    return Path(directory) / "voip_stack" if directory else None


def mailbox_settings(hass, endpoint_id: str) -> dict:
    entry = config_entry(hass)
    if entry is None:
        return {}
    record = phone_subentry_by_endpoint_id(entry, endpoint_id)
    if record is not None:
        return dict(record.data.get("voicemail", {}))
    return dict(entry.data.get(CONF_ESPHOME_MAILBOXES, {}).get(endpoint_id, {}))


def _pin_hash(pin: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode("ascii"), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"scrypt-v1:{salt.hex()}:{digest.hex()}"


def verify_pin(pin: str, encoded: str) -> bool:
    try:
        version, salt, expected = encoded.split(":")
        if version != "scrypt-v1" or len(salt) != 32 or len(expected) != 64 or not pin.isascii() or not pin.isdigit():
            return False
        digest = hashlib.scrypt(pin.encode("ascii"), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32)
        return hmac.compare_digest(digest, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


async def get_archive(hass) -> MediaArchive:
    runtime = require_runtime_data(hass)
    async with runtime.archive_lock:
        owner = runtime.sip
        if owner is None:
            raise ServiceValidationError("VoIP Stack is not ready")
        archive = owner.component("media_archive")
        if archive is None:
            path = archive_path(hass)
            if path is None:
                raise ServiceValidationError("Configure a Home Assistant media directory before recording audio")
            archive = MediaArchive(path, hass.async_add_executor_job)
            await archive.load()
            if runtime.sip is not owner or owner.phase.value != "active":
                await archive.close()
                raise ServiceValidationError("VoIP Stack stopped while opening the media archive")
            owner.adopt_component("media_archive", archive, closer=archive.close)
        return archive


def message_audio_reference(hass, archive, message):
    """Expose the existing message file through HA's existing media source."""
    directory = next((key for key, value in hass.config.media_dirs.items()
                      if Path(value) / "voip_stack" == archive.root), None)
    if directory is None or len(message["segments"]) != 1:
        raise ServiceValidationError("The message's media directory is unavailable")
    path = archive.segment_path(message["id"], message["mailbox_id"])
    return {"file_path": str(path), "media_source_id":
            f"media-source://media_source/{quote(directory, safe='')}/voip_stack/{path.name}"}


async def selected_mailbox(call: ServiceCall, selector: str):
    runtime = require_runtime_data(call.hass)
    endpoint = runtime.endpoints.by_device_id(selector) or runtime.endpoints.resolve(selector)
    if endpoint is None:
        raise ServiceValidationError("Select an existing phone mailbox")
    await async_require_phone_service_control(call.hass, call, endpoint=endpoint)
    return endpoint


async def async_set_voicemail_settings(call: ServiceCall) -> None:
    endpoint = await selected_mailbox(call, call.data["device_id"])
    entry = config_entry(call.hass)
    if entry is None:
        raise ServiceValidationError("VoIP Stack configuration is unavailable")
    if call.data.get("enabled") is False:
        await async_remove_mailbox(call.hass, endpoint.endpoint_id)
        return
    settings = mailbox_settings(call.hass, endpoint.endpoint_id)
    for key in ("enabled", "max_seconds"):
        if key in call.data:
            settings[key] = call.data[key]
    if call.data.get("clear_greeting"):
        settings["greeting_media"] = ""
    elif "greeting_media" in call.data:
        settings["greeting_media"] = str(call.data["greeting_media"]["media_content_id"])
    media = str(settings.get("greeting_media") or "")
    if media and not media.startswith("media-source://"):
        raise ServiceValidationError("Select a Home Assistant media source for the mailbox greeting")
    if "pin" in call.data:
        pin = str(call.data["pin"])
        if pin and (not pin.isascii() or not pin.isdigit() or not 4 <= len(pin) <= 10):
            raise ServiceValidationError("Use a PIN of 4 to 10 digits, or leave it empty to disable other-mailbox access")
        settings["pin_hash"] = await call.hass.async_add_executor_job(_pin_hash, pin) if pin else ""
    if settings.get("enabled"):
        from .config_validation import mailbox_route_aliases, route_namespace_conflicts
        from .contact_config import contact_dicts
        from .phonebook_services import _runtime_route_mappings

        mappings = _runtime_route_mappings(call.hass)
        # Retain this phone's normal aliases, excluding only its old mailbox aliases.
        existing = [{**item, "voicemail": {}} if item.get("id") == endpoint.endpoint_id else item
                    for item in mappings]
        candidate = {"endpoint_id": endpoint.endpoint_id, "name": endpoint.name,
                     "extension": endpoint.extension, "username": endpoint.username,
                     "voicemail": settings}
        if route_namespace_conflicts(candidate_mailbox_routes=mailbox_route_aliases(candidate),
                                     existing=(*existing, *contact_dicts(entry))):
            raise ServiceValidationError("A mailbox address conflicts with an existing phone, contact or group")
    if endpoint.kind is EndpointKind.ESPHOME:
        mailboxes = dict(entry.data.get(CONF_ESPHOME_MAILBOXES, {}))
        mailboxes[endpoint.endpoint_id] = settings
        call.hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_ESPHOME_MAILBOXES: mailboxes})
    elif not update_phone_subentry(call.hass, entry, endpoint.endpoint_id, {"voicemail": settings}):
        if phone_subentry_by_endpoint_id(entry, endpoint.endpoint_id) is None:
            raise ServiceValidationError("Phone configuration is unavailable")
    if settings.get("enabled"):
        await get_archive(call.hass)
    call.hass.bus.async_fire(EVENT_VOICEMAIL_CHANGED, {"endpoint_id": endpoint.endpoint_id})


def resolve_mailbox_route(hass, target: str, entries, *, source_endpoint_id=""):
    """Resolve mailbox access/deposit through the canonical PBX dial plan."""
    from .router import RouteAction, RouteDecision, RouteReason
    from .roster import RosterEntry
    from .runtime_data import runtime_data
    from .voicemail_config import mailbox_routes_available
    from .mailbox_address import parse_mailbox_address

    raw = str(target or "").strip()
    address = parse_mailbox_address(raw)
    if address is None or not mailbox_routes_available(hass):
        return None
    if any(str(value or "").strip().casefold() == raw.casefold()
           for entry in entries for value in (entry.id, entry.name, entry.extension, entry.number)):
        return None
    runtime = runtime_data(hass)
    if runtime is None:
        return None
    mode, selector = address
    if mode == "own":
        selector = source_endpoint_id
    endpoint = runtime.endpoints.resolve(selector) if selector else None
    if mode == "deposit" and (endpoint is None or not mailbox_settings(hass, endpoint.endpoint_id).get("enabled", False)):
        return RouteDecision(RouteAction.REJECT, target=raw, status=480, reason=RouteReason.TARGET_UNREACHABLE)
    if mode == "access" and endpoint is None:
        return RouteDecision(RouteAction.REJECT, target=raw, status=404, reason=RouteReason.ROUTE_NOT_FOUND)
    entry = RosterEntry(raw, name=f"Voicemail: {endpoint.name}" if endpoint else "Voicemail",
                        metadata={"mailbox_endpoint_id": endpoint.endpoint_id if endpoint else "",
                                  "mailbox_mode": mode})
    return RouteDecision(RouteAction.VOICEMAIL, target=raw, entry=entry, reason=RouteReason.EXPLICIT_ROUTE)


def mailbox_snapshot(hass, endpoint_id):
    """One mailbox state projected to HA phones and standard SIP MWI."""
    from .runtime_data import runtime_data
    from .voicemail_config import mailbox_routes_available

    runtime = runtime_data(hass)
    owner = runtime.sip if runtime is not None else None
    archive = owner.component("media_archive") if owner is not None else None
    new, old = archive.counts(endpoint_id) if archive is not None else (0, 0)
    available = mailbox_routes_available(hass)
    return {"available": available, "enabled": bool(available and mailbox_settings(hass, endpoint_id).get("enabled")),
            "new": new, "old": old, "total": new + old}


async def async_remove_mailbox(hass, endpoint_id):
    """Permanently remove one phone's mailbox after its applications release it."""
    from .session_cleanup import async_wait_for_cleanup

    try:
        await async_wait_for_cleanup(asyncio.create_task(
            _remove_mailbox(hass, endpoint_id), name="voip-mailbox-remove",
        ))
    except (OSError, ArchiveError) as error:
        raise ServiceValidationError("The mailbox could not be deleted. Restore access to the media directory and try again.") from error


async def _remove_mailbox(hass, endpoint_id):
    from .endpoint_session import TerminationIntent
    from .voicemail import VoicemailCall

    runtime = require_runtime_data(hass)
    owner = runtime.sip
    if owner is None:
        return
    barriers = []
    for session in tuple(owner.sessions.values()):
        application = owner.resource_for(session.call_id, "voicemail")
        if application is not None and application.mailbox_id == endpoint_id:
            # A remote BYE may already own termination while its writer is
            # finishing. Mark every affected deposit before awaiting any one.
            if isinstance(application, VoicemailCall):
                application.removed = True
            barrier = owner.request_termination(session.call_id, TerminationIntent("phone_removed"), generation=session.generation)
            if barrier is not None:
                barriers.append(barrier)
    if barriers:
        await asyncio.gather(*barriers)
    archive = owner.component("media_archive")
    path = archive_path(hass)
    if archive is None and path is not None:
        archive = await get_archive(hass)
    if archive is not None:
        await archive.remove_mailbox(endpoint_id)
        await archive.imap_forget(endpoint_id)
    entry = config_entry(hass)
    if entry is not None and endpoint_id in entry.data.get(CONF_ESPHOME_MAILBOXES, {}):
        settings = dict(entry.data[CONF_ESPHOME_MAILBOXES])
        settings.pop(endpoint_id)
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_ESPHOME_MAILBOXES: settings})
    if entry is not None and phone_subentry_by_endpoint_id(entry, endpoint_id) is not None:
        update_phone_subentry(hass, entry, endpoint_id, {"voicemail": {}})
    hass.bus.async_fire(EVENT_VOICEMAIL_CHANGED, {"endpoint_id": endpoint_id, "removed": True})


async def async_setup_mailboxes(hass, entry):
    """Observe persistent ownership and publish state without an idle polling task."""
    from homeassistant.core import callback
    from homeassistant.helpers.device_registry import EVENT_DEVICE_REGISTRY_UPDATED
    from .endpoint_lifecycle import create_runtime_task
    runtime = require_runtime_data(hass)

    @callback
    def changed(event):
        from .websocket_api import _publish_ha_softphone_state, _ha_softphone_store

        endpoint_id = str(event.data.get("endpoint_id") or "")
        if runtime.imap_server is not None:
            runtime.imap_server.notify_changed(endpoint_id)
        if runtime.phone_provisioning is not None:
            runtime.phone_provisioning.schedule()
        endpoint = runtime.endpoints.get(endpoint_id)
        if endpoint is not None and endpoint.kind.is_softphone:
            _ha_softphone_store(hass, endpoint.endpoint_id)["voicemail"] = mailbox_snapshot(hass, endpoint.endpoint_id)
            _publish_ha_softphone_state(hass, endpoint_id=endpoint.endpoint_id)

    @callback
    def device_removed(event):
        if event.data.get("action") != "remove":
            return
        if runtime.phone_provisioning is not None:
            # The phone owner coordinates revocation, protocol sessions and
            # files. This fallback serves standalone mailbox consumers only.
            return
        endpoint = runtime.endpoints.by_device_id(event.data["device_id"])
        if endpoint is not None and endpoint.kind is EndpointKind.ESPHOME:
            create_runtime_task(hass, async_remove_mailbox(hass, endpoint.endpoint_id))

    entry.async_on_unload(hass.bus.async_listen(EVENT_VOICEMAIL_CHANGED, changed))
    entry.async_on_unload(hass.bus.async_listen(EVENT_DEVICE_REGISTRY_UPDATED, device_removed))
    await async_refresh_mailbox_state(hass)


async def async_remove_entry_mailboxes(hass, entry):
    """Remove this entry's mailboxes after normal integration shutdown."""
    from .phone_config import phone_subentries
    from .session_cleanup import async_wait_for_cleanup

    path = archive_path(hass)
    identities = {str(item.data.get("endpoint_id") or "") for item in phone_subentries(entry)}
    identities.update(entry.data.get(CONF_ESPHOME_MAILBOXES, {}))
    identities.discard("")
    if path is None or not identities:
        return

    async def remove():
        owner = getattr(getattr(entry, "runtime_data", None), "sip", None)
        if owner is not None:
            from .voicemail import VoicemailCall

            for _call_id, application in owner.resource_items("voicemail"):
                if isinstance(application, VoicemailCall):
                    application.removed = True
            await owner.shutdown()
        archive = MediaArchive(path, hass.async_add_executor_job)
        await archive.load()
        for identity in identities:
            await archive.remove_mailbox(identity)

    await async_wait_for_cleanup(asyncio.create_task(remove(), name="voip-mailboxes-remove-entry"))


async def async_refresh_mailbox_state(hass):
    from .phone_config import phone_subentries
    from .websocket_api import _publish_ha_softphone_state, _ha_softphone_store

    runtime = require_runtime_data(hass)
    if runtime.sip is None:
        return
    entry = config_entry(hass)
    if entry is not None and (any(item.get("enabled") for item in entry.data.get(CONF_ESPHOME_MAILBOXES, {}).values())
            or any(child.data.get("voicemail", {}).get("enabled") for child in phone_subentries(entry))):
        await get_archive(hass)
    for endpoint in runtime.endpoints.endpoints:
        if endpoint.kind.is_softphone:
            _ha_softphone_store(hass, endpoint.endpoint_id)["voicemail"] = mailbox_snapshot(hass, endpoint.endpoint_id)
            _publish_ha_softphone_state(hass, endpoint_id=endpoint.endpoint_id)
    if runtime.phone_provisioning is not None:
        runtime.phone_provisioning.schedule()
