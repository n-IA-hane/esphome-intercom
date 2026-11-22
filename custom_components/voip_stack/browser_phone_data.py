"""Low-level access to the existing entry-owned browser phone presentation data."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .runtime_data import (
    endpoint_directory,
    preferred_browser_phone,
    require_runtime_data,
)


_MEDIA_COUNTER_KEYS = (
    "rtp_tx_packets",
    "rtp_rx_packets",
    "rtp_tx_bytes",
    "rtp_rx_bytes",
    "video_rtp_tx_packets",
    "video_rtp_rx_packets",
    "video_rtp_tx_bytes",
    "video_rtp_rx_bytes",
    "video_rtp_tx_payload_bytes",
    "video_rtp_dropped_packets",
    "video_access_units_tx",
    "video_access_units_rx",
    "video_drop_addr",
    "video_drop_payload_type",
    "video_drop_error",
    "video_drop_direction",
    "video_drop_connection_hold",
    "video_reordered_packets",
    "video_lost_packets",
    "video_duplicate_packets",
    "video_keyframe_requests",
    "video_symmetric_rtp_keepalives",
    "video_symmetric_rtp_keepalive_payload_type",
    "video_access_unit_queue_max",
    "video_access_unit_queue_drops",
    "video_browser_keyframe_requests",
    "video_rtcp_rx_packets",
    "video_rtcp_rx_bytes",
    "video_rtcp_tx_packets",
    "video_rtcp_tx_bytes",
    "video_rtcp_drop_addr",
    "video_rtcp_drop_error",
    "video_rtcp_drop_queue",
    "video_rtcp_task_errors",
    "video_keepalive_task_errors",
    "video_rtcp_pli_rx",
    "video_rtcp_fir_rx",
    "video_rtcp_keyframe_requests_to_browser",
    "video_jpeg_normalized",
    "video_jpeg_normalizer_errors",
)

_CALL_SCOPED_SOFTPHONE_FIELDS = (
    "session_device_id",
    "caller",
    "callee",
    "local_name",
    "peer_name",
    "dialed_target",
    "connected_party",
    "connected_uri",
    "connected_display_name",
    "caller_name",
    "answered_by",
    "direction",
    "role",
    "call_id",
    "target_device_id",
    "selected_tx_format",
    "selected_rx_format",
    "selected_tx_rtp_format",
    "selected_rx_rtp_format",
    "audio_mode",
    "audio_direction",
    "audio_connection_held",
    "video_active",
    "video_offered",
    "video_requested",
    "video_negotiated",
    "video_status",
    "video_failure_reason",
    "video_format",
    "video_send_format",
    "video_receive_format",
    "video_direction",
    "connected_at",
    "route_kind",
    "sip_uri",
    "media_debug",
)


def _clean_group_token(value: object) -> str:
    return (
        str(value or "")
        .replace("|", " ")
        .replace(";", " ")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()[:32]
    )


def _clean_group_name(value: object) -> str:
    groups: list[str] = []
    for raw in str(value or "").split(","):
        group = _clean_group_token(raw)
        if group and group not in groups:
            groups.append(group)
    return ", ".join(groups)


def _clean_endpoint_field(value: object, *, max_len: int = 32) -> str:
    return (
        str(value or "")
        .replace("|", " ")
        .replace(",", " ")
        .replace(";", " ")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )[:max_len]


def _endpoint_store_id(value: object) -> str:
    """Require the real browser endpoint that owns one state store."""

    endpoint_id = str(value or "").strip()
    if not endpoint_id:
        raise ValueError("a Home Assistant phone endpoint is required")
    return endpoint_id


def _ha_softphone_store(hass: HomeAssistant, endpoint_id: str) -> dict[str, Any]:
    """Return one endpoint-scoped HA softphone runtime store.

    Runtime state has one authoritative endpoint-keyed mapping.
    """
    stores = require_runtime_data(hass).softphones
    key = _endpoint_store_id(endpoint_id)
    store = stores.setdefault(key, {"dnd": False})
    store.setdefault("endpoint_id", key)
    endpoint = endpoint_directory(hass).get(key)
    if endpoint is not None and endpoint.supports_ha_client:
        store.setdefault("device_id", endpoint.ha_device_id)
        store.setdefault("local_name", endpoint.name)
    return store


def _ha_softphone_stores(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """Return every logical browser softphone store."""
    return require_runtime_data(hass).softphones


def _endpoint_registry(hass: HomeAssistant):
    """Return the entry-owned logical endpoint registry."""
    return endpoint_directory(hass)


def _browser_endpoint(hass: HomeAssistant, endpoint_id: object):
    registry = _endpoint_registry(hass)
    selector = str(endpoint_id or "").strip()
    endpoint = registry.get(selector) if registry is not None and selector else None
    from .phone_endpoint import ha_client_endpoint
    return ha_client_endpoint(endpoint)


def _ha_softphone_groups(hass: HomeAssistant, endpoint_id: str) -> dict[str, Any]:
    store = _ha_softphone_store(hass, endpoint_id)
    groups = store.setdefault("groups", {})
    return {
        "ring_group": _clean_group_name(groups.get("ring_group")),
        "conference_group": _clean_group_name(groups.get("conference_group")),
        "conference_ring": bool(groups.get("conference_ring", False)),
    }


def _ha_softphone_extension(hass: HomeAssistant, endpoint_id: str) -> str:
    return _clean_endpoint_field(
        _ha_softphone_store(hass, endpoint_id).get("extension")
    )


def _ha_softphone_auto_answer(hass: HomeAssistant, endpoint_id: str) -> bool:
    return bool(_ha_softphone_store(hass, endpoint_id).get("auto_answer", False))


def _ha_softphone_send_video(hass: HomeAssistant, endpoint_id: str) -> bool:
    return bool(_ha_softphone_store(hass, endpoint_id).get("send_video", False))


def _ha_softphone_dnd(hass: HomeAssistant, endpoint_id: str) -> bool:
    endpoint = _endpoint_registry(hass).get(endpoint_id)
    return bool(endpoint is not None and endpoint.dnd)


def _endpoint_id_from_selector(
    hass: HomeAssistant,
    *,
    endpoint_id: object = "",
    device_id: object = "",
    entity_id: object = "",
) -> str:
    """Resolve endpoint, Device or Entity selectors to one browser phone."""

    def _values(raw: object) -> tuple[str, ...]:
        if isinstance(raw, (list, tuple, set, frozenset)):
            values = raw
        else:
            values = (raw,)
        return tuple(text for value in values if (text := str(value or "").strip()))

    explicit = str(endpoint_id or "").strip()
    registry = _endpoint_registry(hass)
    selected_ids: set[str] = set()
    unresolved: list[str] = []

    def _by_current_entity_id(entity_id: str):
        endpoint = registry.by_entity_id(entity_id) if registry is not None else None
        if endpoint is not None:
            return endpoint
        # Entity IDs are user-renamable. Resolve the live Entity Registry row
        # back through its Device instead of relying solely on the ID captured
        # when the entity was first added.
        try:
            from homeassistant.helpers import entity_registry as er

            entity_entry = er.async_get(hass).async_get(entity_id)
        except (AttributeError, ImportError):
            entity_entry = None
        device = str(getattr(entity_entry, "device_id", "") or "")
        return (
            registry.by_device_id(device) if registry is not None and device else None
        )

    for device in _values(device_id):
        endpoint = registry.by_device_id(device) if registry is not None else None
        if endpoint is None or not endpoint.supports_ha_client:
            unresolved.append(device)
            continue
        selected_ids.add(endpoint.endpoint_id)

    for entity in _values(entity_id):
        endpoint = _by_current_entity_id(entity)
        if endpoint is None or not endpoint.supports_ha_client:
            unresolved.append(entity)
            continue
        selected_ids.add(endpoint.endpoint_id)

    if explicit:
        resolved_id = _endpoint_store_id(explicit)
        if registry is not None:
            from .sip_account_identity import resolve_ha_endpoint
            from .store import config_entry
            endpoint = resolve_ha_endpoint(registry, resolved_id, entry=config_entry(hass))
            if endpoint is None or not endpoint.supports_ha_client:
                raise ValueError(f"Unknown Home Assistant phone endpoint: {explicit}")
            # EndpointRegistry lookup is case-insensitive; every downstream
            # store, event and presence key must nevertheless use the stable
            # canonical identity owned by the registry.
            resolved_id = endpoint.endpoint_id
        else:
            raise ValueError(f"Unknown Home Assistant phone endpoint: {explicit}")
        if unresolved:
            raise ValueError(
                "Unknown Home Assistant phone selector: " + ", ".join(unresolved)
            )
        if selected_ids and selected_ids != {resolved_id}:
            raise ValueError(
                f"Home Assistant phone endpoint {explicit} does not match the "
                "selected Device or Entity"
            )
        return resolved_id

    if unresolved:
        raise ValueError(
            "Unknown Home Assistant phone selector: " + ", ".join(unresolved)
        )
    if len(selected_ids) > 1:
        raise ValueError("Selected Devices or Entities belong to different HA phones")
    if not selected_ids:
        endpoint = preferred_browser_phone(hass)
        if endpoint is None:
            raise ValueError("Select a Home Assistant phone")
        return endpoint.endpoint_id
    return next(iter(selected_ids))
