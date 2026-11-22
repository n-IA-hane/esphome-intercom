"""Project browser phone views from the existing endpoint and call owners."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .config import debug_mode as current_debug_mode
from .config import transport_config as current_transport_config
from .const import (
    CONF_VIDEO_CAMERA_SEND,
    CONF_VIDEO_TRANSCODING,
)
from .endpoint_lifecycle import call_registry
from .counterpart_display import counterpart_display
from .fsm import (
    CallState,
    sip_phone_state,
)
from .phone_endpoint import (
    EndpointAvailability,
    EndpointKind,
)
from .runtime_diagnostics import runtime_resource_snapshot
from .runtime_data import (
    require_runtime_data,
    runtime_data,
    sip_endpoint_manager,
    sip_registrar,
    sip_trunk,
)
from .call_events import (
    home_phone_name as _ha_peer_name,
    phone_projection_state as _phone_projection_state,
)
from .browser_phone_data import (
    _endpoint_store_id,
    _ha_softphone_store,
    _endpoint_registry,
    _ha_softphone_groups,
    _ha_softphone_extension,
    _ha_softphone_auto_answer,
    _ha_softphone_send_video,
    _ha_softphone_dnd,
    _MEDIA_COUNTER_KEYS,
)


def _runtime_counter(store: dict[str, Any], runtime: dict[str, Any], key: str) -> int:
    del runtime
    store_value = int(store.get(key, 0) or 0)
    # Runtime totals aggregate every relay in the integration, so they cannot
    # safely represent one HA softphone call even while its call-id is active.
    # Browser audio/video reporters persist call-scoped counters in ``store``;
    # aggregate relay counters remain available under debug topology maps.
    return store_value


def _sip_runtime_snapshot(
    hass: HomeAssistant,
    *,
    detailed: bool = False,
    call_ids: frozenset[str] | None = None,
) -> dict[str, Any]:
    registry = call_registry(hass)
    identities = None if call_ids is None else call_ids | frozenset(
        identity for call_id in call_ids if (session := registry.sessions.get(call_id)) is not None
        for leg in session.legs.values() for identity in (leg.leg_id, leg.sip_call_id) if identity
    )
    def visible(identity):
        return identities is None or identity in identities or registry.resolve_session_id(str(identity)) in call_ids
    endpoint = sip_endpoint_manager(hass)
    data: dict[str, Any] = {
        "sip_udp_ready": False,
        "sip_tcp_ready": False,
        "pending_transactions": 0,
        "active_dialogs": 0,
        "pending_call_ids": [],
        "active_call_ids": [],
        "last_sip_event": "",
        "last_sip_status_code": 0,
        "last_sip_reason": "",
        "rtp_tx_packets": 0,
        "rtp_rx_packets": 0,
        "rtp_tx_bytes": 0,
        "rtp_rx_bytes": 0,
        "rtp_dropped_packets": 0,
        "rtp_relays": {},
        "sip_client_dialogs": {},
        "sip_trunk": {},
        "sip_trunks": {},
        "sip_registrar": {},
        "media_debug": {},
        "runtime_resources": {},
    }
    snapshot = getattr(endpoint, "snapshot", None)
    if callable(snapshot):
        snap = snapshot()
        data.update(
            {
                "sip_udp_ready": bool(getattr(snap, "udp_ready", False)),
                "sip_tcp_ready": bool(getattr(snap, "tcp_ready", False)),
                "pending_transactions": int(
                    getattr(
                        snap,
                        "pending_transactions",
                        getattr(snap, "pending_invites", 0),
                    )
                ),
                "active_dialogs": int(getattr(snap, "active_dialogs", 0)),
                "pending_call_ids": list(getattr(snap, "pending_call_ids", ()) or ()),
                "active_call_ids": list(getattr(snap, "active_call_ids", ()) or ()),
                "last_sip_event": str(getattr(snap, "last_sip_event", "") or ""),
                "last_sip_status_code": int(
                    getattr(snap, "last_sip_status_code", 0) or 0
                ),
                "last_sip_reason": str(getattr(snap, "last_sip_reason", "") or ""),
            }
        )
        if call_ids is not None:
            for key in ("pending_call_ids", "active_call_ids"):
                data[key] = [identity for identity in data[key] if visible(identity)]
            data["pending_transactions"] = len(data["pending_call_ids"])
            data["active_dialogs"] = len(data["active_call_ids"])
    for call_id, relay in registry.relays_snapshot().items():
        if not visible(call_id):
            continue
        if detailed:
            snap = getattr(relay, "snapshot", None)
            if not callable(snap):
                continue
            relay_data = dict(snap())
            data["rtp_relays"][call_id] = relay_data
        else:
            relay_data = {
                "left_rx_packets": getattr(relay, "left_rx_packets", 0),
                "left_rx_bytes": getattr(relay, "left_rx_bytes", 0),
                "left_tx_packets": getattr(relay, "left_tx_packets", 0),
                "left_tx_bytes": getattr(relay, "left_tx_bytes", 0),
                "right_rx_packets": getattr(relay, "right_rx_packets", 0),
                "right_rx_bytes": getattr(relay, "right_rx_bytes", 0),
                "right_tx_packets": getattr(relay, "right_tx_packets", 0),
                "right_tx_bytes": getattr(relay, "right_tx_bytes", 0),
                "dropped_packets": getattr(relay, "dropped", 0),
            }
        data["rtp_rx_packets"] += int(relay_data.get("left_rx_packets", 0)) + int(
            relay_data.get("right_rx_packets", 0)
        )
        data["rtp_rx_bytes"] += int(relay_data.get("left_rx_bytes", 0)) + int(
            relay_data.get("right_rx_bytes", 0)
        )
        data["rtp_tx_packets"] += int(relay_data.get("left_tx_packets", 0)) + int(
            relay_data.get("right_tx_packets", 0)
        )
        data["rtp_tx_bytes"] += int(relay_data.get("left_tx_bytes", 0)) + int(
            relay_data.get("right_tx_bytes", 0)
        )
        data["rtp_dropped_packets"] += int(relay_data.get("dropped_packets", 0))
    for key, client in registry.sip_clients_snapshot().items():
        if not visible(key):
            continue
        snap = getattr(client, "snapshot", None)
        if not callable(snap):
            continue
        client_data = dict(snap())
        if detailed:
            data["sip_client_dialogs"][key] = client_data
        if client_data.get("dialog_active"):
            data["active_dialogs"] += 1
            call_id = str(client_data.get("call_id") or "")
            if call_id and call_id not in data["active_call_ids"]:
                data["active_call_ids"].append(call_id)
        elif client_data.get("pending_invite"):
            data["pending_transactions"] += 1
            call_id = str(client_data.get("call_id") or "")
            if call_id and call_id not in data["pending_call_ids"]:
                data["pending_call_ids"].append(call_id)
        if client_data.get("last_sip_event"):
            data["last_sip_event"] = str(
                client_data.get("last_sip_event") or data["last_sip_event"]
            )
            data["last_sip_status_code"] = int(
                client_data.get("last_sip_status_code")
                or data["last_sip_status_code"]
                or 0
            )
            data["last_sip_reason"] = str(
                client_data.get("last_sip_reason") or data["last_sip_reason"]
            )
    trunk = sip_trunk(hass)
    if detailed and call_ids is None:
        from .runtime_data import sip_trunks
        data["sip_trunks"] = {identity: dict(client.snapshot()) for identity, client in sip_trunks(hass).items()}
    trunk_snapshot = getattr(trunk, "snapshot", None)
    if callable(trunk_snapshot) and call_ids is None:
        trunk_data = dict(trunk_snapshot())
        if detailed:
            data["sip_trunk"] = trunk_data
        if trunk_data.get("trunk_last_sip_event"):
            data["last_sip_event"] = str(
                trunk_data.get("trunk_last_sip_event") or data["last_sip_event"]
            )
        if trunk_data.get("trunk_status_code"):
            data["last_sip_status_code"] = int(trunk_data.get("trunk_status_code") or 0)
            data["last_sip_reason"] = str(trunk_data.get("trunk_status_reason") or "")
    registrar = sip_registrar(hass)
    registrar_snapshot = getattr(registrar, "snapshot", None)
    if detailed and callable(registrar_snapshot) and call_ids is None:
        data["sip_registrar"] = dict(registrar_snapshot())
        if data["sip_registrar"].get("registrar_last_sip_event"):
            data["last_sip_event"] = str(
                data["sip_registrar"].get("registrar_last_sip_event")
                or data["last_sip_event"]
            )
        if data["sip_registrar"].get("registrar_last_sip_status_code"):
            data["last_sip_status_code"] = int(
                data["sip_registrar"].get("registrar_last_sip_status_code") or 0
            )
            data["last_sip_reason"] = str(
                data["sip_registrar"].get("registrar_last_sip_reason") or ""
            )
    elif registrar is not None and call_ids is None:
        if getattr(registrar, "last_sip_event", ""):
            data["last_sip_event"] = str(registrar.last_sip_event)
        if getattr(registrar, "last_sip_status_code", 0):
            data["last_sip_status_code"] = int(registrar.last_sip_status_code)
            data["last_sip_reason"] = str(
                getattr(registrar, "last_sip_reason", "") or ""
            )
    data["pending_call_ids"] = sorted(set(data["pending_call_ids"]))
    data["active_call_ids"] = sorted(set(data["active_call_ids"]))
    if call_ids is not None:
        data["pending_transactions"] = len(data["pending_call_ids"])
        data["active_dialogs"] = len(data["active_call_ids"])
    runtime = runtime_data(hass)
    data["runtime_resources"] = runtime_resource_snapshot(
        registry,
        detailed=detailed,
        rtp_port_pool=runtime.rtp_port_pool if runtime is not None else None,
        call_artifacts=runtime.sip if runtime is not None else None,
        browser_media=runtime.media if runtime is not None else None,
        runtime_tasks=runtime.tasks if runtime is not None else (),
    ) if call_ids is None else {"resource_counts": registry.snapshot(call_ids=call_ids)["resource_counts"]}
    return data


def _account_browser_media(runtime, registry, endpoint_id, call_id):
    """Read an existing media capability; a native SIP dialog is not one."""
    if not call_id:
        return False
    from .endpoint_session import LegPhase
    session = registry.get_session(registry.resolve_session_id(call_id))
    if session is None or not session.live:
        return False
    # A fork can retire its browser leg while the same participant continues
    # through the committed local bridge. That current owner takes precedence.
    local_bridge = getattr(runtime, "local_bridge", None)
    local = local_bridge.get_call(session.call_id) if local_bridge is not None else None
    if local is not None and endpoint_id in {local.caller_endpoint_id, local.callee_endpoint_id}:
        return True
    browser_legs = [leg for leg in session.legs.values() if leg.role == "ha_softphone" and leg.endpoint_id == endpoint_id]
    live_browser_legs = [leg for leg in browser_legs if leg.phase not in {LegPhase.CLOSING, LegPhase.CLOSED}]
    if browser_legs and not live_browser_legs:
        return False
    media = (registry.resource_for(call_id, f"softphone_media_endpoint:{endpoint_id}")
             or registry.resource_for(call_id, "softphone_media"))
    if isinstance(media, dict) and media.get("endpoint_id") == endpoint_id:
        return True
    return bool(live_browser_legs)


def _account_shared_projection(hass, endpoint):
    if endpoint is None or endpoint.kind is not EndpointKind.SIP_ACCOUNT:
        return None
    from .phone_state import phone_states
    from homeassistant.exceptions import ServiceValidationError
    try:
        # A commit may precede the observer's queued refresh. Read its same
        # pure projection from current legs so a losing browser cannot linger.
        return phone_states(hass).get(endpoint.endpoint_id)
    except ServiceValidationError:
        return None


def _ha_softphone_state(hass: HomeAssistant, endpoint_id: str) -> dict[str, Any]:
    from .paging_config import paging_group_memberships
    from .store import config_entry

    entry = config_entry(hass)
    endpoint_id = _endpoint_store_id(endpoint_id)
    paging_groups = paging_group_memberships(entry).get(endpoint_id, []) if entry is not None else None
    store = dict(_ha_softphone_store(hass, endpoint_id))
    endpoint = _endpoint_registry(hass).get(endpoint_id)
    projection = _account_shared_projection(hass, endpoint)
    if projection is not None and projection["calls"]:
        selected = next((call for call in projection["calls"] if call["call_id"] == store.get("call_id")), projection["calls"][0])
        identity = selected["call_id"]
        selected_state = (
            "held" if selected["state"] == "confirmed" and selected["held"]
            else "in_call" if selected["state"] == "confirmed"
            else "ringing" if selected["state"] == "early"
            else "connecting" if selected["state"] == "proceeding"
            else "calling"
        )
        if identity != store.get("call_id") or not _account_browser_media(require_runtime_data(hass), call_registry(hass), endpoint_id, identity):
            current = call_registry(hass).get_session(identity)
            incoming = selected["direction"] == "recipient"
            store.update(call_id=identity, state=selected_state, sip_state=selected_state,
                direction="incoming" if incoming else "outgoing", peer_name=selected["peer_name"],
                caller=current.caller if current is not None else "", callee=current.callee if current is not None else "")
        elif store.get("state") in {"in_call", "held"} and selected_state in {"in_call", "held"}:
            store.update(state=selected_state, sip_state=selected_state)
    elif projection is not None and store.get("state") in {"calling", "connecting", "ringing", "remote_ringing", "answering", "in_call", "held", "terminating"}:
        from .endpoint_session import LegPhase
        registry = call_registry(hass)
        current = registry.get_session(registry.resolve_session_id(str(store.get("call_id") or "")))
        participates = current is not None and current.live and (
            endpoint_id in current.endpoint_claims or any(leg.endpoint_id == endpoint_id
                and leg.phase not in {LegPhase.CLOSING, LegPhase.CLOSED} for leg in current.legs.values()))
        if not participates and not _account_browser_media(require_runtime_data(hass), registry, endpoint_id, store.get("call_id")):
            # The registry has retired this account's call. A stale presentation
            # cache cannot resurrect it; retain the old ID for terminal ordering.
            store.update(state=projection["state"], sip_state=projection["state"])
    entry_runtime = runtime_data(hass)
    from .parking_config import parking_settings
    parking = parking_settings(hass)
    connected_cards = int(
        (entry_runtime.softphone_presence if entry_runtime is not None else {}).get(
            endpoint_id, 0
        )
    )
    debug_mode = current_debug_mode(hass)
    from .call_scope import call_belongs_to_endpoint
    registry = call_registry(hass)
    visible_call_ids = frozenset(
        identity for identity, session in registry.sessions.items()
        if endpoint_id in session.endpoint_claims
        or call_belongs_to_endpoint(registry, identity, endpoint_id)
        or any(leg.endpoint_id == endpoint_id for leg in session.legs.values())
    )
    runtime = _sip_runtime_snapshot(hass, detailed=debug_mode, call_ids=visible_call_ids)
    transport_config = current_transport_config(hass)
    active_softphone = store.get("state") in {
        CallState.CALLING.value,
        CallState.REMOTE_RINGING.value,
        CallState.RINGING.value,
        CallState.CONNECTING.value,
        CallState.IN_CALL.value,
        CallState.HELD.value,
        CallState.TERMINATING.value,
    }
    # Integration-wide registrar/trunk diagnostics never describe an unrelated
    # account. An idle account with no call event has no SIP outcome to report.
    last_status = store.get("sip_status_code", "") or ""
    last_event = store.get("last_sip_event", "")
    caller = store.get("caller", "") or store.get("last_terminal_caller", "")
    callee = store.get("callee", "") or store.get("last_terminal_callee", "")
    connected_party = str(
        store.get("connected_party", "") or store.get("answered_by", "")
    )
    peer_name = store.get("peer_name", "") or store.get("last_terminal_peer_name", "")
    direction = store.get("direction", "") or store.get("last_terminal_direction", "")
    if direction == "incoming" and caller:
        peer_name = caller
    elif store.get("state") == CallState.IN_CALL.value and connected_party:
        peer_name = connected_party
    peer_name = counterpart_display(peer_name, fallback=callee if direction == "outgoing" else caller,
                                    name=store.get("peer_name", ""), directory=require_runtime_data(hass).directory,
                                    updated=bool(store.get("connected_uri") or store.get("last_terminal_peer_name")),
                                    display_name=store.get("connected_display_name" if direction == "outgoing" else "caller_name", ""),
                                    direction=direction, caller=caller, callee=callee, connected_party=connected_party)
    dialed_target = store.get("dialed_target", "") or store.get(
        "last_terminal_dialed_target", ""
    )
    call_id = store.get("call_id", "") or store.get("last_terminal_call_id", "")
    recording = entry_runtime.sip.resource_for(call_id, "recording") if entry_runtime is not None and entry_runtime.sip is not None else None
    recording_active = recording is not None and not recording.stopped.is_set()
    registry = call_registry(hass)
    media_debug = dict(store.get("media_debug") or {}) if debug_mode else {}
    if debug_mode:
        active_transcoder = (
            entry_runtime.media.transcoder if entry_runtime is not None else None
        )
        media_debug.update(
            {
                "call_registry": registry.snapshot(call_ids=visible_call_ids),
                "runtime_resources": runtime["runtime_resources"],
                "audio_ws_owner_call_ids": sorted(
                    (identity for identity in entry_runtime.media.owners_for("audio")
                     if registry.resolve_session_id(identity) in visible_call_ids)
                    if entry_runtime is not None
                    else ()
                ),
                "video_ws_owner_call_ids": sorted(
                    (identity for identity in entry_runtime.media.owners_for("video")
                     if registry.resolve_session_id(identity) in visible_call_ids)
                    if entry_runtime is not None
                    else ()
                ),
                "video_transcoder_call_id": str(
                    getattr(active_transcoder, "call_id", "") or ""
                    if registry.resolve_session_id(str(getattr(active_transcoder, "call_id", "") or "")) in visible_call_ids else ""
                ),
            }
        )
    session = registry.sessions.get(registry.resolve_session_id(str(call_id or "")))
    paging = bool(session is not None and session.live and session.owner == "paging"
                  and session.metadata.get("application") == "browser_paging"
                  and session.metadata.get("endpoint_id") == endpoint_id
                  and str(store.get("call_id") or "") == session.call_id)
    can_attach_media = _account_browser_media(entry_runtime, registry, endpoint_id, str(call_id or "")) if entry_runtime is not None else False
    descriptor = registry.resource_for(str(call_id or ""), "softphone_media") or {}
    handoff = descriptor.get("media_handoff") or {}
    media_handoff = dict(handoff) if (can_attach_media and session is not None
        and endpoint_id in session.endpoint_claims and descriptor.get("endpoint_id") == endpoint_id
        and handoff.get("endpoint_id") == endpoint_id and descriptor.get("media_client_id")) else None
    event_context = registry.event_context(str(call_id or ""))
    video_active = bool(store.get("video_active", False))
    video_offered = bool(store.get("video_offered", False))
    video_requested = bool(store.get("video_requested", video_offered))
    video_negotiated = bool(store.get("video_negotiated", video_active))
    video_status = str(store.get("video_status") or "").strip().lower()
    if not video_status:
        video_status = (
            "active"
            if video_active
            else "offered"
            if video_offered
            else "requested"
            if video_requested
            else "inactive"
        )
    view_state = _phone_projection_state(store.get("state", CallState.IDLE.value), direction)
    from .call_scope import application_owns_browser_answer
    can_answer = bool(session is not None and session.live
        and view_state in {CallState.RINGING.value, "incoming"} and direction == "incoming"
        and (paging or not application_owns_browser_answer(session)))
    phone = sip_phone_state(
        state=view_state,
        call_id=call_id,
        direction=direction,
        caller=caller,
        callee=callee,
        local_uri=store.get("local_uri", ""),
        remote_uri=store.get("remote_uri", ""),
        contact=peer_name,
        sip_transport=store.get("sip_transport", "udp+tcp"),
        sip_status_code=int(last_status or 0),
        terminal_reason=store.get("terminal_reason", ""),
        selected_tx_format=store.get("selected_tx_format", ""),
        selected_rx_format=store.get("selected_rx_format", ""),
        rtp_tx_packets=_runtime_counter(store, runtime, "rtp_tx_packets"),
        rtp_rx_packets=_runtime_counter(store, runtime, "rtp_rx_packets"),
        rtp_tx_bytes=_runtime_counter(store, runtime, "rtp_tx_bytes"),
        rtp_rx_bytes=_runtime_counter(store, runtime, "rtp_rx_bytes"),
        last_sip_event=last_event,
    )
    return {
        **phone,
        "endpoint_id": endpoint_id,
        "device_id": (
            endpoint.ha_device_id
            if endpoint is not None and endpoint.ha_device_id
            else store.get("device_id", "")
        ),
        "name": endpoint.name if endpoint is not None else _ha_peer_name(hass),
        "endpoint_type": endpoint.kind.value if endpoint is not None else EndpointKind.BROWSER.value,
        "available": bool(
            endpoint is not None
            and endpoint.availability is EndpointAvailability.AVAILABLE
        ),
        "enabled": bool(
            endpoint is not None
            and endpoint.availability is not EndpointAvailability.UNAVAILABLE
        ),
        "connected_cards": connected_cards,
        "ha_client_supported": bool(endpoint is not None and endpoint.supports_ha_client),
        "can_attach_media": can_attach_media,
        **({"media_handoff": media_handoff} if media_handoff is not None else {}),
        "can_answer": can_answer,
        "media_is_local": can_attach_media,
        "owns_media": False,
        "media_owner": "available" if can_attach_media else "other",
        "capabilities": (
            sorted(endpoint.capabilities) if endpoint is not None else ["audio", "dtmf"]
        ),
        "session_device_id": store.get("session_device_id", ""),
        "dnd": _ha_softphone_dnd(hass, endpoint_id),
        "voicemail": dict(store.get("voicemail") or {"enabled": False, "new": 0, "old": 0, "total": 0}),
        "parking": {"enabled": bool(parking.get("enabled")),
                    "unavailable_reason": parking.get("unavailable_reason", ""),
                    "video_supported": bool(session is not None and session.live
                                            and session.owner in {"ha_softphone", "bridge"})},
        "recording": {"active": recording_active,
                      "finishing": recording is not None and recording.stopped.is_set(),
                      "owned": recording_active and recording.endpoint.endpoint_id == endpoint_id},
        "extension": _ha_softphone_extension(hass, endpoint_id),
        "queues": list(endpoint.queues) if endpoint is not None else [],
        **({"paging_groups": list(paging_groups)} if paging_groups is not None else {}),
        "queue_states": (entry_runtime.queues.phone_summary(endpoint_id)
                         if entry_runtime is not None and getattr(entry_runtime, "queues", None) is not None else []),
        "groups": _ha_softphone_groups(hass, endpoint_id),
        "busy": bool(store.get("session_device_id") and active_softphone),
        "state": view_state,
        "sip_state": _phone_projection_state(store.get("sip_state", view_state), direction),
        "caller": caller,
        "callee": callee,
        "local_name": store.get(
            "local_name", endpoint.name if endpoint is not None else _ha_peer_name(hass)
        ),
        "peer_name": peer_name,
        "dialed_target": dialed_target,
        "connected_party": store.get("connected_party", ""),
        "answered_by": store.get("answered_by", ""),
        "direction": direction,
        "role": store.get("role", ""),
        "call_id": call_id,
        "sequence": event_context.sequence if event_context is not None else 0,
        "revision": session.revision if session is not None else 0,
        "owner": session.owner if session is not None else "",
        "target_device_id": store.get("target_device_id", ""),
        "selected_tx_format": store.get("selected_tx_format", ""),
        "selected_rx_format": store.get("selected_rx_format", ""),
        "selected_tx_rtp_format": store.get("selected_tx_rtp_format", ""),
        "selected_rx_rtp_format": store.get("selected_rx_rtp_format", ""),
        "audio_mode": store.get("audio_mode", ""),
        "audio_direction": "recvonly" if paging else store.get("audio_direction", "sendrecv"),
        "announcement_id": str(session.metadata.get("announcement_id") or "") if paging else "",
        "answer_mode": "auto" if paging else "",
        "generation": session.generation if session is not None else 0,
        "audio_connection_held": bool(store.get("audio_connection_held", False)),
        "video_active": video_active,
        "video_offered": video_offered,
        "video_requested": video_requested,
        "video_negotiated": video_negotiated,
        "video_status": video_status,
        "video_failure_reason": str(store.get("video_failure_reason") or ""),
        "video_format": store.get("video_format", ""),
        "video_send_format": store.get(
            "video_send_format", store.get("video_format", "")
        ),
        "video_receive_format": store.get(
            "video_receive_format", store.get("video_format", "")
        ),
        "video_direction": store.get("video_direction", "inactive"),
        "auto_answer": _ha_softphone_auto_answer(hass, endpoint_id),
        "send_video": _ha_softphone_send_video(hass, endpoint_id),
        "video_camera_send_enabled": bool(
            transport_config.get(CONF_VIDEO_CAMERA_SEND, False)
        ),
        "video_transcoding_enabled": bool(
            transport_config.get(CONF_VIDEO_TRANSCODING, False)
        ),
        "connected_at": (
            event_context.connected_at_epoch
            if session is not None and session.live and event_context is not None
            and store.get("state") in {"in_call", "held", "transferring"}
            else 0.0
        ),
        "sip_transport": store.get("sip_transport", "udp+tcp"),
        "sip_status_code": last_status,
        "terminal_reason": store.get("terminal_reason", ""),
        "last_sip_event": last_event,
        "last_sip_reason": store.get("last_sip_reason", ""),
        "sip_udp_ready": runtime["sip_udp_ready"],
        "sip_tcp_ready": runtime["sip_tcp_ready"],
        "pending_transactions": runtime["pending_transactions"],
        "active_dialogs": runtime["active_dialogs"],
        "pending_call_ids": runtime["pending_call_ids"],
        "active_call_ids": runtime["active_call_ids"],
        "rtp_tx_packets": _runtime_counter(store, runtime, "rtp_tx_packets"),
        "rtp_rx_packets": _runtime_counter(store, runtime, "rtp_rx_packets"),
        "rtp_tx_bytes": _runtime_counter(store, runtime, "rtp_tx_bytes"),
        "rtp_rx_bytes": _runtime_counter(store, runtime, "rtp_rx_bytes"),
        "rtp_dropped_packets": runtime["rtp_dropped_packets"],
        # Topology and trunk addresses belong to opt-in diagnostics. The
        # authenticated softphone state stream otherwise needs call state and
        # aggregate counters only.
        "rtp_relays": runtime["rtp_relays"] if debug_mode else {},
        "sip_client_dialogs": runtime["sip_client_dialogs"] if debug_mode else {},
        "sip_trunk": runtime["sip_trunk"] if debug_mode else {},
        "sip_trunks": runtime["sip_trunks"] if debug_mode else {},
        **{
            key: _runtime_counter(store, runtime, key)
            for key in _MEDIA_COUNTER_KEYS
            if key.startswith("video_")
        },
        "debug_mode": debug_mode,
        "media_debug": media_debug,
    }


def _ha_softphone_device(
    hass: HomeAssistant,
    endpoint_id: str,
) -> dict[str, Any]:
    endpoint_id = _endpoint_store_id(endpoint_id)
    state = _ha_softphone_state(hass, endpoint_id)
    return {
        "endpoint_id": endpoint_id,
        "endpoint_type": state.get("endpoint_type", EndpointKind.BROWSER.value),
        "device_id": state.get("device_id") or "",
        "name": state.get("name") or _ha_peer_name(hass),
        "extension": state.get("extension", ""),
        "availability": (
            EndpointAvailability.AVAILABLE.value
            if state.get("available")
            else EndpointAvailability.OFFLINE.value
        ),
        "capabilities": list(state.get("capabilities") or ()),
        "host": "",
        "sip_transport": "udp+tcp",
        "softphone": True,
        "ha_client_supported": True,
        "voicemail": dict(state.get("voicemail") or {}),
        "ha_softphone": state,
    }


def _ha_softphone_devices(hass: HomeAssistant) -> list[dict[str, Any]]:
    registry = _endpoint_registry(hass)
    if registry is None:
        return []
    devices = [
        _ha_softphone_device(hass, endpoint.endpoint_id)
        for endpoint in registry.endpoints
        if endpoint.supports_ha_client
    ]
    return devices
