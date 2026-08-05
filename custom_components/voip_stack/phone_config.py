"""Config-subentry persistence for logical VoIP Stack phone endpoints."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
import logging
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant

from .config_validation import route_namespace_conflicts
from .const import (
    CONF_ASSIST_ENDPOINT_ENABLED,
    CONF_ASSIST_EXTENSION,
    CONF_SIP_VIDEO,
    CONF_HA_SOFTPHONE_CONFERENCE_GROUP,
    CONF_HA_SOFTPHONE_CONFERENCE_RING,
    CONF_HA_SOFTPHONE_DND,
    CONF_HA_SOFTPHONE_EXTENSION,
    CONF_HA_SOFTPHONE_RING_GROUP,
    CONF_PHONEBOOK_CONTACTS,
    CONF_SIP_ACCOUNTS,
    DOMAIN,
    HA_PEER_FALLBACK_NAME,
)
from .endpoint_registry import EndpointRegistry
from .phone_endpoint import (
    DEFAULT_ENDPOINT_ID,
    EndpointAvailability,
    EndpointKind,
    OfflinePolicy,
    PhoneEndpoint,
)
from .sip_registrar import account_from_mapping, normalize_username


PHONE_SUBENTRY_TYPE = "phone"
CONF_PHONE_ENDPOINT_ID = "endpoint_id"
CONF_PHONE_KIND = "kind"
CONF_PHONE_NAME = "name"
CONF_PHONE_EXTENSION = "extension"
CONF_PHONE_USERNAME = "username"
CONF_PHONE_PASSWORD = "password"
CONF_PHONE_ENABLED = "enabled"
CONF_PHONE_DND = "dnd"
CONF_PHONE_AUTO_ANSWER = "auto_answer"
CONF_PHONE_SEND_VIDEO = "send_video"
CONF_PHONE_RING_GROUP = "ring_group"
CONF_PHONE_CONFERENCE_GROUP = "conference_group"
CONF_PHONE_CONFERENCE_RING = "conference_ring"
CONF_PHONE_VIDEO_ENABLED = "video_enabled"
CONF_PHONE_OFFLINE_POLICY = "offline_policy"
CONF_PHONE_OFFLINE_FORWARD_TARGET = "offline_forward_target"
LEGACY_HA_SOFTPHONE_STORE_KEY = f"{DOMAIN}_ha_softphone"
LEGACY_HA_SOFTPHONE_STORE_VERSION = 1

_LOGGER = logging.getLogger(__name__)


def phone_subentries(entry: ConfigEntry) -> list[ConfigSubentry]:
    """Return every logical-phone subentry owned by the integration entry."""
    return [
        subentry
        for subentry in entry.subentries.values()
        if subentry.subentry_type == PHONE_SUBENTRY_TYPE
    ]


def phone_subentry_by_endpoint_id(
    entry: ConfigEntry, endpoint_id: str
) -> ConfigSubentry | None:
    wanted = str(endpoint_id or "").strip().casefold()
    return next(
        (
            subentry
            for subentry in phone_subentries(entry)
            if str(subentry.data.get(CONF_PHONE_ENDPOINT_ID) or "").strip().casefold()
            == wanted
        ),
        None,
    )


def new_browser_endpoint_id() -> str:
    """Create a stable opaque identity independent from the user-facing name."""
    return f"browser:{uuid4().hex}"


def new_sip_account_endpoint_id() -> str:
    """Create a SIP-account identity independent from its mutable username."""
    return f"sip:{uuid4().hex}"


def sip_account_endpoint_id(username: object) -> str:
    """Return the identity used by pre-v3 username-keyed SIP accounts."""
    return f"sip:{normalize_username(str(username or ''))}"


def default_phone_data(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Build the migrated master softphone definition."""
    options = entry.options
    return {
        CONF_PHONE_ENDPOINT_ID: DEFAULT_ENDPOINT_ID,
        CONF_PHONE_KIND: EndpointKind.BROWSER.value,
        CONF_PHONE_NAME: (hass.config.location_name or "").strip()
        or HA_PEER_FALLBACK_NAME,
        CONF_PHONE_EXTENSION: str(
            options.get(CONF_HA_SOFTPHONE_EXTENSION) or ""
        ).strip(),
        CONF_PHONE_DND: bool(options.get(CONF_HA_SOFTPHONE_DND, False)),
        CONF_PHONE_AUTO_ANSWER: False,
        CONF_PHONE_SEND_VIDEO: False,
        CONF_PHONE_RING_GROUP: str(
            options.get(CONF_HA_SOFTPHONE_RING_GROUP) or ""
        ).strip(),
        CONF_PHONE_CONFERENCE_GROUP: str(
            options.get(CONF_HA_SOFTPHONE_CONFERENCE_GROUP) or ""
        ).strip(),
        CONF_PHONE_CONFERENCE_RING: bool(
            options.get(CONF_HA_SOFTPHONE_CONFERENCE_RING, False)
        ),
        CONF_PHONE_VIDEO_ENABLED: True,
        CONF_PHONE_ENABLED: True,
    }


def sip_account_phone_data(
    raw: Mapping[str, Any],
    *,
    endpoint_id: str = "",
) -> dict[str, Any]:
    """Translate one legacy registrar account to a phone subentry."""
    account = account_from_mapping(raw)
    return {
        CONF_PHONE_ENDPOINT_ID: str(endpoint_id or new_sip_account_endpoint_id()),
        CONF_PHONE_KIND: EndpointKind.SIP_ACCOUNT.value,
        CONF_PHONE_NAME: account.display_name,
        CONF_PHONE_EXTENSION: account.extension,
        CONF_PHONE_USERNAME: account.username,
        CONF_PHONE_PASSWORD: account.password,
        CONF_PHONE_ENABLED: account.enabled,
        CONF_PHONE_DND: False,
        CONF_PHONE_RING_GROUP: account.ring_group,
        CONF_PHONE_CONFERENCE_GROUP: account.conference_group,
        CONF_PHONE_CONFERENCE_RING: account.conference_ring,
        CONF_PHONE_VIDEO_ENABLED: True,
        CONF_PHONE_OFFLINE_POLICY: OfflinePolicy.UNAVAILABLE.value,
        CONF_PHONE_OFFLINE_FORWARD_TARGET: "",
    }


def _add_phone_subentry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    data: Mapping[str, Any],
    title: str,
) -> ConfigSubentry:
    endpoint_id = str(data[CONF_PHONE_ENDPOINT_ID]).strip()
    subentry = ConfigSubentry(
        data=MappingProxyType(dict(data)),
        subentry_type=PHONE_SUBENTRY_TYPE,
        title=title,
        unique_id=f"phone:{endpoint_id}",
    )
    hass.config_entries.async_add_subentry(entry, subentry)
    return subentry


def legacy_default_phone_overrides(
    raw: Mapping[str, Any] | None,
    *,
    options: Mapping[str, Any],
) -> dict[str, Any]:
    """Translate the historical Store without overriding newer HA options."""
    data = raw if isinstance(raw, Mapping) else {}
    groups = data.get("groups") if isinstance(data.get("groups"), Mapping) else {}
    candidates = {
        CONF_HA_SOFTPHONE_DND: (
            CONF_PHONE_DND,
            bool(data.get("dnd", False)),
        ),
        CONF_HA_SOFTPHONE_EXTENSION: (
            CONF_PHONE_EXTENSION,
            str(data.get("extension") or "").strip(),
        ),
        CONF_HA_SOFTPHONE_RING_GROUP: (
            CONF_PHONE_RING_GROUP,
            str(groups.get("ring_group") or "").strip(),
        ),
        CONF_HA_SOFTPHONE_CONFERENCE_GROUP: (
            CONF_PHONE_CONFERENCE_GROUP,
            str(groups.get("conference_group") or "").strip(),
        ),
        CONF_HA_SOFTPHONE_CONFERENCE_RING: (
            CONF_PHONE_CONFERENCE_RING,
            bool(groups.get("conference_ring", False)),
        ),
    }
    return {
        subentry_key: value
        for option_key, (subentry_key, value) in candidates.items()
        if option_key not in options
    }


async def async_load_legacy_default_phone_overrides(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Read the v1 master-softphone Store before v3 makes subentries canonical."""
    from homeassistant.helpers.storage import Store

    try:
        raw = await Store(
            hass,
            LEGACY_HA_SOFTPHONE_STORE_VERSION,
            LEGACY_HA_SOFTPHONE_STORE_KEY,
        ).async_load()
    except (OSError, TypeError, ValueError) as err:
        _LOGGER.warning("Could not read legacy HA softphone settings: %s", err)
        raw = None
    return legacy_default_phone_overrides(raw, options=entry.options)


def async_ensure_phone_subentries(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    default_overrides: Mapping[str, Any] | None = None,
) -> None:
    """Create the master and migrate legacy registrar accounts once."""
    if phone_subentry_by_endpoint_id(entry, DEFAULT_ENDPOINT_ID) is None:
        data = default_phone_data(hass, entry)
        if default_overrides:
            data.update(
                {
                    key: value
                    for key, value in default_overrides.items()
                    if key
                    in {
                        CONF_PHONE_DND,
                        CONF_PHONE_EXTENSION,
                        CONF_PHONE_RING_GROUP,
                        CONF_PHONE_CONFERENCE_GROUP,
                        CONF_PHONE_CONFERENCE_RING,
                    }
                }
            )
        _add_phone_subentry(
            hass,
            entry,
            data=data,
            title=str(data[CONF_PHONE_NAME]),
        )

    existing_usernames = {
        str(item.data.get(CONF_PHONE_USERNAME) or "").strip().casefold()
        for item in phone_subentries(entry)
        if item.data.get(CONF_PHONE_KIND) == EndpointKind.SIP_ACCOUNT.value
    }
    legacy_accounts = [
        dict(item)
        for item in entry.data.get(CONF_SIP_ACCOUNTS, [])
        if isinstance(item, Mapping)
    ]
    for raw in legacy_accounts:
        data = sip_account_phone_data(raw)
        username = str(data[CONF_PHONE_USERNAME]).casefold()
        if username in existing_usernames:
            continue
        _add_phone_subentry(
            hass,
            entry,
            data=data,
            title=str(data[CONF_PHONE_NAME]),
        )
        existing_usernames.add(username)

    if legacy_accounts:
        data = dict(entry.data)
        data.pop(CONF_SIP_ACCOUNTS, None)
        hass.config_entries.async_update_entry(entry, data=data)


def _endpoint_capabilities(entry: ConfigEntry, data: Mapping[str, Any]) -> set[str]:
    capabilities = {"audio", "dtmf"}
    if bool(entry.data.get(CONF_SIP_VIDEO, False)) and bool(
        data.get(CONF_PHONE_VIDEO_ENABLED, True)
    ):
        capabilities.add("video")
    return capabilities


def _persisted_sip_offline_policy(data: Mapping[str, Any]) -> OfflinePolicy:
    """Load a SIP policy without stranding entries from older dev builds."""
    raw = str(
        data.get(CONF_PHONE_OFFLINE_POLICY) or OfflinePolicy.UNAVAILABLE.value
    )
    try:
        return OfflinePolicy(raw)
    except ValueError:
        _LOGGER.warning(
            "Unsupported persisted SIP account offline policy %r for endpoint=%s; "
            "using unavailable",
            raw,
            str(data.get(CONF_PHONE_ENDPOINT_ID) or "unknown"),
        )
        return OfflinePolicy.UNAVAILABLE


def endpoint_from_subentry(
    entry: ConfigEntry,
    subentry: ConfigSubentry,
    *,
    availability: EndpointAvailability = EndpointAvailability.OFFLINE,
) -> PhoneEndpoint:
    """Build an immutable endpoint snapshot from persisted configuration."""
    return endpoint_from_data(
        entry,
        subentry.data,
        fallback_endpoint_id=str(subentry.unique_id or ""),
        fallback_name=subentry.title,
        availability=availability,
    )


def endpoint_from_data(
    entry: ConfigEntry,
    data: Mapping[str, Any],
    *,
    fallback_endpoint_id: str = "",
    fallback_name: str = "",
    availability: EndpointAvailability = EndpointAvailability.OFFLINE,
) -> PhoneEndpoint:
    """Build and validate an endpoint before its config subentry is persisted."""
    kind = EndpointKind(str(data.get(CONF_PHONE_KIND) or EndpointKind.BROWSER.value))
    enabled = bool(data.get(CONF_PHONE_ENABLED, True))
    if not enabled:
        availability = EndpointAvailability.UNAVAILABLE
    return PhoneEndpoint(
        endpoint_id=str(data.get(CONF_PHONE_ENDPOINT_ID) or fallback_endpoint_id),
        name=str(data.get(CONF_PHONE_NAME) or fallback_name),
        kind=kind,
        extension=str(data.get(CONF_PHONE_EXTENSION) or ""),
        username=str(data.get(CONF_PHONE_USERNAME) or ""),
        availability=availability,
        capabilities=_endpoint_capabilities(entry, data),
        dnd=bool(data.get(CONF_PHONE_DND, False)),
        auto_answer=bool(data.get(CONF_PHONE_AUTO_ANSWER, False)),
        send_video=bool(data.get(CONF_PHONE_SEND_VIDEO, False)),
        offline_policy=(
            _persisted_sip_offline_policy(data)
            if kind is EndpointKind.SIP_ACCOUNT
            else OfflinePolicy.UNAVAILABLE.value
        ),
        ring_group=str(data.get(CONF_PHONE_RING_GROUP) or ""),
        conference_group=str(data.get(CONF_PHONE_CONFERENCE_GROUP) or ""),
        conference_ring=bool(data.get(CONF_PHONE_CONFERENCE_RING, False)),
        offline_forward_target=(
            str(data.get(CONF_PHONE_OFFLINE_FORWARD_TARGET) or "")
            if kind is EndpointKind.SIP_ACCOUNT
            else ""
        ),
    )


def _clear_browser_runtime(bucket: dict[str, Any], endpoint_id: str) -> None:
    """Forget transient state owned by a deleted browser phone."""
    bucket.setdefault("ha_softphone_presence", {}).pop(endpoint_id, None)
    if endpoint_id != DEFAULT_ENDPOINT_ID:
        bucket.setdefault("ha_softphones", {}).pop(endpoint_id, None)


def async_setup_endpoint_registry(
    hass: HomeAssistant, entry: ConfigEntry
) -> EndpointRegistry:
    """Create and publish the authoritative configured endpoint registry."""
    registry = EndpointRegistry()
    subentry_ids: dict[str, str] = {}
    presence = hass.data.setdefault(DOMAIN, {}).get("ha_softphone_presence", {})
    for subentry in phone_subentries(entry):
        endpoint = endpoint_from_subentry(entry, subentry)
        if (
            endpoint.kind is EndpointKind.BROWSER
            and endpoint.availability is not EndpointAvailability.UNAVAILABLE
            and int(presence.get(endpoint.endpoint_id, 0) or 0) > 0
        ):
            # Config-entry reloads do not close existing websocket
            # subscriptions. Reapply their live presence to the new registry
            # instead of making a connected kiosk appear offline until it
            # happens to reconnect.
            endpoint = replace(
                endpoint, availability=EndpointAvailability.AVAILABLE
            )
        registry.register(endpoint)
        subentry_ids[endpoint.endpoint_id] = subentry.subentry_id
    bucket = hass.data.setdefault(DOMAIN, {})
    previous_registry = bucket.get("endpoint_registry")
    if previous_registry is not None:
        previous_registry.close()
    bucket["endpoint_registry"] = registry
    registry.subentry_ids = subentry_ids
    pending_removals = registry.pending_removals

    def _remove_pending_endpoint(endpoint_id: str) -> None:
        if endpoint_id not in pending_removals:
            return
        current = registry.get(endpoint_id)
        if current is None:
            pending_removals.discard(endpoint_id)
            return
        if current.active_call_id:
            return
        pending_removals.discard(endpoint_id)
        removed = registry.remove(endpoint_id)
        if removed.kind is EndpointKind.BROWSER:
            # An endpoint removed while busy stays alive until its terminal
            # transition.  By then the config-entry update that initiated the
            # removal has already run, so clear page-presence bookkeeping here
            # instead of retaining a dead endpoint ID indefinitely.
            _clear_browser_runtime(bucket, endpoint_id)

    def _finish_deferred_removal(event) -> None:
        endpoint = event.endpoint
        if (
            endpoint.endpoint_id in pending_removals
            and not endpoint.active_call_id
        ):
            # Do not emit REMOVED recursively from inside the UPDATED
            # notification. Entity adapters must observe a complete ordered
            # event sequence.
            hass.loop.call_soon(_remove_pending_endpoint, endpoint.endpoint_id)

    registry.subscribe(_finish_deferred_removal)
    return registry


def sync_registry_from_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply config-subentry mutations without changing active runtime fields."""
    registry: EndpointRegistry | None = hass.data.get(DOMAIN, {}).get(
        "endpoint_registry"
    )
    if registry is None:
        return
    configured: set[str] = set()
    bucket = hass.data.setdefault(DOMAIN, {})
    subentries = phone_subentries(entry)
    subentry_ids = {
        str(subentry.data.get(CONF_PHONE_ENDPOINT_ID) or "").strip(): (
            subentry.subentry_id
        )
        for subentry in subentries
        if str(subentry.data.get(CONF_PHONE_ENDPOINT_ID) or "").strip()
    }
    # Entity/device listeners run synchronously from registry.upsert(). Publish
    # ownership before the first event so a newly added phone is immediately
    # associated with its native HA config subentry.
    registry.subentry_ids = subentry_ids
    pending_removals = registry.pending_removals
    presence = bucket.get("ha_softphone_presence", {})
    for subentry in subentries:
        candidate = endpoint_from_subentry(entry, subentry)
        configured.add(candidate.endpoint_id)
        pending_removals.discard(candidate.endpoint_id)
        previous = registry.get(candidate.endpoint_id)
        if previous is not None:
            if candidate.availability is EndpointAvailability.UNAVAILABLE:
                availability = EndpointAvailability.UNAVAILABLE
            elif (
                candidate.kind is EndpointKind.SIP_ACCOUNT
                and previous.kind is EndpointKind.SIP_ACCOUNT
                and candidate.username.casefold() != previous.username.casefold()
            ):
                # Registrar registrations are keyed by the mutable SIP
                # username. Renaming an account invalidates its old contact;
                # never preserve AVAILABLE until the new identity REGISTERs.
                availability = EndpointAvailability.OFFLINE
            elif previous.availability is EndpointAvailability.UNAVAILABLE:
                # A disabled endpoint is represented as unavailable.  Once it
                # is enabled again it must re-enter normal transport discovery
                # instead of inheriting the disabled runtime state forever.
                availability = (
                    EndpointAvailability.AVAILABLE
                    if candidate.kind is EndpointKind.BROWSER
                    and int(presence.get(candidate.endpoint_id, 0) or 0) > 0
                    else EndpointAvailability.OFFLINE
                )
            else:
                availability = previous.availability
            candidate = replace(
                candidate,
                device_id=previous.device_id,
                entity_ids=previous.entity_ids,
                availability=availability,
                active_call_id=previous.active_call_id,
            )
        elif (
            candidate.kind is EndpointKind.BROWSER
            and candidate.availability is not EndpointAvailability.UNAVAILABLE
            and int(presence.get(candidate.endpoint_id, 0) or 0) > 0
        ):
            candidate = replace(
                candidate, availability=EndpointAvailability.AVAILABLE
            )
        registry.upsert(candidate)
    for endpoint in tuple(registry.endpoints):
        if endpoint.kind in {EndpointKind.BROWSER, EndpointKind.SIP_ACCOUNT} and (
            endpoint.endpoint_id not in configured
        ):
            if endpoint.active_call_id:
                # Keep the runtime endpoint until teardown releases its call.
                # It is already absent from routing/config, so it cannot
                # receive a new call while draining.
                pending_removals.add(endpoint.endpoint_id)
                if endpoint.availability is not EndpointAvailability.UNAVAILABLE:
                    registry.update(
                        endpoint.endpoint_id,
                        availability=EndpointAvailability.UNAVAILABLE,
                    )
            else:
                pending_removals.discard(endpoint.endpoint_id)
                removed = registry.remove(endpoint.endpoint_id)
                if removed.kind is EndpointKind.BROWSER:
                    _clear_browser_runtime(bucket, removed.endpoint_id)


def sip_account_dicts_from_subentries(entry: ConfigEntry) -> list[dict[str, Any]]:
    """Expose registrar accounts through the legacy service/storage contract."""
    accounts: list[dict[str, Any]] = []
    for subentry in phone_subentries(entry):
        data = subentry.data
        if data.get(CONF_PHONE_KIND) != EndpointKind.SIP_ACCOUNT.value:
            continue
        accounts.append(
            {
                "username": data.get(CONF_PHONE_USERNAME, ""),
                "display_name": data.get(CONF_PHONE_NAME, subentry.title),
                "password": data.get(CONF_PHONE_PASSWORD, ""),
                "enabled": bool(data.get(CONF_PHONE_ENABLED, True)),
                "extension": data.get(CONF_PHONE_EXTENSION, ""),
                "conference_group": data.get(CONF_PHONE_CONFERENCE_GROUP, ""),
                "conference_ring": bool(
                    data.get(CONF_PHONE_CONFERENCE_RING, False)
                ),
                "ring_group": data.get(CONF_PHONE_RING_GROUP, ""),
            }
        )
    return accounts


def validate_sip_account_namespace(
    entry: ConfigEntry,
    account_data: Iterable[Mapping[str, Any]],
) -> None:
    """Validate a complete future SIP-account set against the dial plan.

    Native phone subentry flows already validate the shared namespace. The
    legacy account services replace the complete SIP-account set directly, so
    they must perform the same validation before the first Config Registry
    write. Group names may be reused by members, but no contact, phone or
    Assist route may be shadowed by a group (or vice versa).
    """
    existing: list[Mapping[str, Any]] = [
        item
        for item in entry.data.get(CONF_PHONEBOOK_CONTACTS, []) or []
        if isinstance(item, Mapping)
    ]
    existing.extend(
        subentry.data
        for subentry in phone_subentries(entry)
        if subentry.data.get(CONF_PHONE_KIND) != EndpointKind.SIP_ACCOUNT.value
    )
    assist_extension = str(entry.data.get(CONF_ASSIST_EXTENSION) or "").strip()
    if entry.data.get(CONF_ASSIST_ENDPOINT_ENABLED) and assist_extension:
        existing.append(
            {
                "id": "assist",
                "name": "Assist",
                "extension": assist_extension,
            }
        )

    accepted: list[Mapping[str, Any]] = []
    for data in account_data:
        groups = tuple(
            part.strip()
            for field in (
                CONF_PHONE_RING_GROUP,
                CONF_PHONE_CONFERENCE_GROUP,
            )
            for part in str(data.get(field) or "").split(",")
            if part.strip()
        )
        if route_namespace_conflicts(
            candidate_routes=(
                data.get(CONF_PHONE_NAME),
                data.get(CONF_PHONE_EXTENSION),
                data.get(CONF_PHONE_USERNAME),
            ),
            candidate_groups=groups,
            existing=(*existing, *accepted),
        ):
            label = str(
                data.get(CONF_PHONE_NAME)
                or data.get(CONF_PHONE_USERNAME)
                or "SIP account"
            )
            raise ValueError(
                f"SIP account route for {label!r} conflicts with an existing "
                "phone, contact, Assist extension, or group"
            )
        accepted.append(data)


def replace_sip_account_subentries(
    hass: HomeAssistant,
    entry: ConfigEntry,
    raw_accounts: Iterable[Mapping[str, Any]],
) -> None:
    """Synchronize service-managed registrar accounts with phone subentries."""
    existing = {
        str(subentry.data.get(CONF_PHONE_USERNAME) or "").casefold(): subentry
        for subentry in phone_subentries(entry)
        if subentry.data.get(CONF_PHONE_KIND) == EndpointKind.SIP_ACCOUNT.value
    }
    wanted: dict[str, dict[str, Any]] = {}
    for raw in raw_accounts:
        data = sip_account_phone_data(raw)
        username = str(data[CONF_PHONE_USERNAME]).casefold()
        previous = existing.get(username)
        if previous is not None:
            # A native subentry reconfigure may have changed the username
            # while retaining the opaque endpoint identity. Legacy account
            # services must never silently rename the HA device/entities.
            data[CONF_PHONE_ENDPOINT_ID] = str(
                previous.data.get(CONF_PHONE_ENDPOINT_ID)
                or data[CONF_PHONE_ENDPOINT_ID]
            )
            for key in (
                CONF_PHONE_DND,
                CONF_PHONE_VIDEO_ENABLED,
                CONF_PHONE_OFFLINE_POLICY,
                CONF_PHONE_OFFLINE_FORWARD_TARGET,
            ):
                if key in previous.data:
                    data[key] = previous.data[key]
        wanted[username] = data

    validate_sip_account_namespace(entry, wanted.values())

    # Validate the complete future configuration before making the first HA
    # registry write. Otherwise a collision discovered halfway through would
    # leave a persisted but unloadable set of subentries.
    future = EndpointRegistry()
    for subentry in phone_subentries(entry):
        if subentry.data.get(CONF_PHONE_KIND) == EndpointKind.SIP_ACCOUNT.value:
            continue
        future.register(endpoint_from_subentry(entry, subentry))
    for data in wanted.values():
        future.register(endpoint_from_data(entry, data))
    for username, data in wanted.items():
        subentry = existing.get(username)
        if subentry is None:
            _add_phone_subentry(
                hass,
                entry,
                data=data,
                title=str(data[CONF_PHONE_NAME]),
            )
            continue
        hass.config_entries.async_update_subentry(
            entry=entry,
            subentry=subentry,
            data=data,
            title=str(data[CONF_PHONE_NAME]),
        )
    for username, subentry in existing.items():
        if username not in wanted:
            hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
    sync_registry_from_entry(hass, entry)


def update_phone_subentry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    endpoint_id: str,
    updates: Mapping[str, Any],
) -> bool:
    """Persist mutable settings into their owning phone subentry."""
    subentry = phone_subentry_by_endpoint_id(entry, endpoint_id)
    if subentry is None:
        return False
    data = dict(subentry.data)
    data.update(updates)
    changed = hass.config_entries.async_update_subentry(
        entry=entry,
        subentry=subentry,
        data=data,
        title=str(data.get(CONF_PHONE_NAME) or subentry.title),
    )
    sync_registry_from_entry(hass, entry)
    return changed
