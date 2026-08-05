"""VoIP Stack integration for Home Assistant.

HA is a SIP softphone and SIP B2BUA/router for ESPHome SIP phones. Public call
control is expressed in SIP/SDP/RTP terms only; logical targets are resolved by
the central phonebook and routed through HA as SIP dialogs when needed.
"""

import logging

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import HomeAssistant, CoreState, Event, ServiceCall
from homeassistant.exceptions import ConfigEntryError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .config import (
    entry_assist_config as _entry_assist_config,
    entry_transport_config as _entry_transport_config,
    entry_trunk_config as _entry_trunk_config,
    transport_config as _get_transport_config,
)
from .config_entry_runtime import (
    async_config_entry_updated as _async_config_entry_updated,
    async_deferred_phonebook_sync as _deferred_phonebook_sync,
    async_refresh_and_push_phonebook as _refresh_and_push_phonebook,
    entry_phone_signature as _entry_phone_signature,
    entry_runtime_signature as _entry_runtime_signature,
    register_phonebook_service_event_sync as _register_phonebook_service_event_sync,
)
from .const import (
    CONF_ASSIST_ENDPOINT_ENABLED,
    CONF_ASSIST_PIPELINE,
    CONF_ASSIST_INTENTS,
    CONF_DEBUG_MODE,
    CONF_INITIAL_PHONE_CREATED,
    CONF_MEDIA_CAPTURE,
    CONF_PREFERRED_PHONE_DEVICE_ID,
    CONF_SIP_VIDEO,
    CONF_PHONEBOOK_CONTACTS,
    CONF_AUTOMATION_ROUTING_ENABLED,
    CONF_TRUNK_DTMF_ENABLED,
    CONF_TRUNK_DTMF_TIMEOUT_MS,
    CONF_TRUNK_INBOUND_MODE,
    DOMAIN,
    TRUNK_INBOUND_MODE_DIRECT,
    TRUNK_INBOUND_MODE_DTMF,
)
from .endpoint_lifecycle import call_registry as _call_registry, create_runtime_task
from .esphome_state_bridge import (
    register_state_event_bridge as _register_esp_state_event_bridge,
)
from .peer_snapshot import (
    async_advertise_host as _ha_advertise_host,
)
from .phone_endpoint import (
    EndpointKind,
)
from .service_endpoints import (
    async_require_phone_service_control as _require_phone_service_control,
    service_browser_endpoint as _service_browser_endpoint,
    service_configured_endpoint as _service_configured_endpoint,
)
from .phone_control import (
    CallControlRequest,
    OriginateRequest,
    PhoneAdapterRegistry,
    PhoneOperation,
)
from .softphone_forward import async_forward_browser_call as _forward_browser_call
from .phone_config import (
    async_ensure_phone_subentries,
    async_bootstrap_browser_phone,
    async_load_legacy_default_phone_overrides,
    async_setup_endpoint_registry,
    phone_subentries,
)
from .endpoint_device import async_ensure_endpoint_device
from .route_decisions import set_pending_route_decision as _set_pending_route_decision
from .runtime_data import (
    VoipStackConfigEntry,
    VoipStackRuntime,
    registration_data,
    runtime_data as _runtime_data,
)
from .store import manual_roster_entries as _manual_roster_entries
from .websocket_api import (
    async_register_websocket_api,
    _async_load_ha_softphone_store,
    async_set_ha_softphone_settings,
)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_migrate_entry(
    hass: HomeAssistant, config_entry: VoipStackConfigEntry
) -> bool:
    """Migrate inbound routing options without changing existing behavior."""
    if config_entry.version < 2:
        data = dict(config_entry.data)
        raw_timeout = data.get(CONF_TRUNK_DTMF_TIMEOUT_MS, 3000)
        timeout_ms = int(raw_timeout or 0)
        if 0 <= timeout_ms <= 10:
            timeout_ms *= 1000
        legacy_dtmf = bool(data.get(CONF_TRUNK_DTMF_ENABLED, False)) and timeout_ms > 0
        mode = TRUNK_INBOUND_MODE_DTMF if legacy_dtmf else TRUNK_INBOUND_MODE_DIRECT
        data[CONF_TRUNK_INBOUND_MODE] = mode
        data[CONF_AUTOMATION_ROUTING_ENABLED] = False
        data[CONF_TRUNK_DTMF_ENABLED] = legacy_dtmf
        data[CONF_TRUNK_DTMF_TIMEOUT_MS] = timeout_ms
        hass.config_entries.async_update_entry(config_entry, data=data, version=2)
        _LOGGER.info("Migrated VoIP Stack inbound routing mode to %s", mode)
    if config_entry.version < 3:
        legacy_phone_data = await async_load_legacy_default_phone_overrides(
            hass,
            config_entry,
        )
        async_ensure_phone_subentries(
            hass,
            config_entry,
            default_overrides=legacy_phone_data,
        )
        hass.config_entries.async_update_entry(config_entry, version=3)
        _LOGGER.info("Migrated VoIP Stack phones to config subentries")
    if config_entry.version < 4:
        hass.config_entries.async_update_entry(config_entry, version=4)
        _LOGGER.info("Prepared VoIP Stack phones for Device-based selection")
    if config_entry.version < 5:
        data = dict(config_entry.data)
        data.setdefault(CONF_INITIAL_PHONE_CREATED, True)
        hass.config_entries.async_update_entry(config_entry, data=data, version=5)
        _LOGGER.info("Migrated VoIP Stack preferred-phone bootstrap state")
    return True


_LOGGER = logging.getLogger(__name__)
SIP_ROUTE_DECISION_TIMEOUT = 1.5


async def _handle_purge_devices_service(call: ServiceCall) -> None:
    """Reject unsafe removal of devices owned by ESPHome."""

    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="purge_devices_disabled",
    )


async def _handle_sip_answer_service(call: ServiceCall) -> None:
    await _control_phone(call, PhoneOperation.ANSWER)


async def _handle_sip_decline_service(call: ServiceCall) -> None:
    await _control_phone(call, PhoneOperation.DECLINE)


async def _handle_sip_hangup_service(call: ServiceCall) -> None:
    await _control_phone(call, PhoneOperation.HANGUP)


async def _control_phone(call: ServiceCall, operation: PhoneOperation) -> None:
    runtime = _runtime_data(call.hass)
    if runtime is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="phone_unavailable",
            translation_placeholders={"phone": "VoIP Stack"},
        )
    reason = str(
        call.data.get("reason")
        or call.data.get("decline_reason")
        or ("local_hangup" if operation is PhoneOperation.HANGUP else "")
    ).strip()
    await runtime.phones.control(
        call,
        operation,
        CallControlRequest(
            call_id=str(call.data.get("call_id") or "").strip(),
            reason=reason,
            context=call.context,
        ),
    )


async def _handle_set_dnd_service(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    endpoint_id, _endpoint = _service_configured_endpoint(hass, call)
    dnd_entities = tuple(
        entity_id
        for entity_id in getattr(_endpoint, "entity_ids", ())
        if str(entity_id).startswith("switch.")
    )
    await _require_phone_service_control(
        hass,
        call,
        endpoint=_endpoint,
        action_entity_ids=dnd_entities,
    )
    enabled = bool(call.data.get("dnd"))
    from .switch import async_set_endpoint_dnd

    await async_set_endpoint_dnd(hass, endpoint_id, enabled)
    _LOGGER.info(
        "HA softphone endpoint=%s DND set to %s via service",
        endpoint_id,
        enabled,
    )


async def _handle_browser_preference_service(
    call: ServiceCall,
    *,
    preference: str,
) -> None:
    hass = call.hass
    endpoint_id, endpoint = _service_browser_endpoint(hass, call, strict=True)
    switch_entities = tuple(
        entity_id
        for entity_id in getattr(endpoint, "entity_ids", ())
        if str(entity_id).startswith("switch.")
    )
    await _require_phone_service_control(
        hass,
        call,
        endpoint=endpoint,
        action_entity_ids=switch_entities,
    )
    enabled = bool(call.data.get(preference))
    await async_set_ha_softphone_settings(
        hass,
        endpoint_id=endpoint_id,
        **{preference: enabled},
    )
    _LOGGER.info(
        "HA softphone endpoint=%s %s set to %s via service",
        endpoint_id,
        preference,
        enabled,
    )


async def _handle_set_auto_answer_service(call: ServiceCall) -> None:
    await _handle_browser_preference_service(call, preference="auto_answer")


async def _handle_set_send_video_service(call: ServiceCall) -> None:
    await _handle_browser_preference_service(call, preference="send_video")


async def _handle_set_ha_softphone_settings_service(call: ServiceCall) -> None:
    hass = call.hass
    endpoint_id, _endpoint = _service_browser_endpoint(hass, call, strict=True)
    await async_set_ha_softphone_settings(
        hass,
        endpoint_id=endpoint_id,
        extension=call.data.get("extension"),
        ring_group=call.data.get("ring_group"),
        conference_group=call.data.get("conference_group"),
        conference_ring=call.data.get("conference_ring"),
        auto_answer=call.data.get("auto_answer"),
        send_video=call.data.get("send_video"),
    )
    await _refresh_and_push_phonebook(hass)


async def _handle_sip_call_target_service(
    call: ServiceCall,
    *,
    force_ha_bridge: bool = False,
) -> dict[str, object] | None:
    result = await _originate_phone_action(
        call,
        force_ha_bridge=force_ha_bridge,
    )
    if not call.return_response:
        return None
    return result.as_service_response()


async def _originate_phone_action(
    call: ServiceCall,
    *,
    force_ha_bridge: bool = False,
):
    """Dispatch one originate action through the entry-owned phone registry."""

    runtime = _runtime_data(call.hass)
    if runtime is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="phone_unavailable",
            translation_placeholders={"phone": "VoIP Stack"},
        )
    result = await runtime.phones.originate(
        call,
        OriginateRequest(
            destination=str(call.data.get("destination") or "").strip(),
            send_video=bool(call.data.get("send_video", False)),
            force_ha_bridge=bool(force_ha_bridge or call.data.get("ha_bridge", False)),
            context=call.context,
        ),
    )
    return result


async def _handle_sip_route_service(call: ServiceCall) -> None:
    _set_pending_route_decision(call.hass, dict(call.data))


async def _handle_select_inbound_destination_service(call: ServiceCall) -> None:
    """Select the initial destination of one pending inbound route request."""
    from homeassistant.exceptions import ServiceValidationError

    from .automation_routing import resolve_pending_route_call_id

    data = dict(call.data)
    registry = _call_registry(call.hass)
    try:
        call_id = resolve_pending_route_call_id(
            str(data.get("call_id") or ""), registry.pending_routes
        )
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err
    data["call_id"] = call_id
    data["action"] = "forward"
    _set_pending_route_decision(call.hass, data)


async def _handle_sip_forward_service(call: ServiceCall) -> None:
    await _forward_browser_call(call)


async def _handle_sip_set_deadline_service(call: ServiceCall) -> None:
    from .call_deadlines import async_set_call_deadline

    await async_set_call_deadline(call.hass, dict(call.data))


async def _handle_sip_cancel_deadline_service(call: ServiceCall) -> None:
    from .call_deadlines import cancel_call_deadline

    cancel_call_deadline(call.hass, str(call.data.get("call_id") or ""))


async def _async_register_services(hass: HomeAssistant) -> None:
    """Register HA services for SIP phone control."""
    from .account_services import build_account_service_handlers
    from .phonebook_services import build_phonebook_service_handlers
    from .services import async_register_services

    account_handlers = build_account_service_handlers(_refresh_and_push_phonebook)
    phonebook_handlers = build_phonebook_service_handlers(_refresh_and_push_phonebook)

    await async_register_services(
        hass,
        {
            "purge_devices": _handle_purge_devices_service,
            "answer": _handle_sip_answer_service,
            "decline": _handle_sip_decline_service,
            "hangup": _handle_sip_hangup_service,
            **phonebook_handlers,
            "set_dnd": _handle_set_dnd_service,
            "set_auto_answer": _handle_set_auto_answer_service,
            "set_send_video": _handle_set_send_video_service,
            "set_ha_softphone_settings": _handle_set_ha_softphone_settings_service,
            "call": _handle_sip_call_target_service,
            "forward": _handle_sip_forward_service,
            "route": _handle_sip_route_service,
            "select_inbound_destination": _handle_select_inbound_destination_service,
            "set_deadline": _handle_sip_set_deadline_service,
            "cancel_deadline": _handle_sip_cancel_deadline_service,
            **account_handlers,
        },
    )


async def _async_apply_assist_intents(hass: HomeAssistant, enabled: bool) -> None:
    """Register optional Assist intent handlers only when explicitly enabled."""
    if enabled:
        from .assist_intents import async_register_assist_intents

        async_register_assist_intents(hass)
        return

    if registration_data(hass).assist_intents:
        from .assist_intents import async_unregister_assist_intents

        async_unregister_assist_intents(hass)


async def _async_setup_shared(hass: HomeAssistant, config: dict | None = None) -> None:
    """Shared setup logic for both YAML and config entry."""
    registration = registration_data(hass)
    if registration.initialized:
        # Services, websocket commands and HTTP views stay registered across a
        # config-entry reload. The event listeners are explicitly removed by
        # unload, so restore just those idempotent subscriptions here.
        _register_esp_state_event_bridge(hass)
        _register_phonebook_service_event_sync(hass)
        if _get_transport_config(hass).get(CONF_SIP_VIDEO, False):
            from .video_ws_view import async_register_video_ws_view

            async_register_video_ws_view(hass)
        return

    registration.initialized = True

    async_register_websocket_api(hass)
    from .audio_ws_view import async_register_audio_ws_view

    async_register_audio_ws_view(hass)
    if _get_transport_config(hass).get(CONF_SIP_VIDEO, False):
        from .video_ws_view import async_register_video_ws_view

        async_register_video_ws_view(hass)
    await _async_register_services(hass)
    _register_esp_state_event_bridge(hass)
    _register_phonebook_service_event_sync(hass)

    # Sensor platform is forwarded per config entry; YAML setup gets only
    # services + websocket API.

    async def _register_frontend(_event: Event | None = None) -> None:
        from .frontend import JSModuleRegistration

        registration = JSModuleRegistration(hass)
        await registration.async_register()

    if hass.state == CoreState.running:
        await _register_frontend(None)
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _register_frontend)

    _LOGGER.info("VoIP Stack loaded (SIP softphone + SIP B2BUA/router)")


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up VoIP Stack defaults from configuration.yaml."""
    await _async_setup_shared(hass, config)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: VoipStackConfigEntry) -> bool:
    """Set up VoIP Stack from a config entry (UI setup)."""
    previous_runtime = _runtime_data(hass)
    if CONF_INITIAL_PHONE_CREATED not in entry.data:
        async_bootstrap_browser_phone(hass, entry)
        data = dict(entry.data)
        data[CONF_INITIAL_PHONE_CREATED] = True
        hass.config_entries.async_update_entry(entry, data=data)
    endpoint_registry = async_setup_endpoint_registry(hass, entry)
    for configured_endpoint in tuple(endpoint_registry.endpoints):
        async_ensure_endpoint_device(
            hass,
            entry,
            configured_endpoint,
            endpoint_registry,
        )
    preferred_phone_device_id = str(
        entry.data.get(CONF_PREFERRED_PHONE_DEVICE_ID) or ""
    ).strip()
    preferred_endpoint = endpoint_registry.by_device_id(preferred_phone_device_id)
    if preferred_endpoint is None or preferred_endpoint.kind is not EndpointKind.BROWSER:
        browser_phones = tuple(
            endpoint
            for endpoint in endpoint_registry.endpoints
            if endpoint.kind is EndpointKind.BROWSER and endpoint.device_id
        )
        preferred_phone_device_id = (
            browser_phones[0].device_id if len(browser_phones) == 1 else ""
        )
    if preferred_phone_device_id != str(
        entry.data.get(CONF_PREFERRED_PHONE_DEVICE_ID) or ""
    ).strip():
        data = dict(entry.data)
        if preferred_phone_device_id:
            data[CONF_PREFERRED_PHONE_DEVICE_ID] = preferred_phone_device_id
        else:
            data.pop(CONF_PREFERRED_PHONE_DEVICE_ID, None)
        hass.config_entries.async_update_entry(entry, data=data)
    cfg = _entry_transport_config(entry)
    assist_cfg = _entry_assist_config(entry)
    trunk_cfg = _entry_trunk_config(entry)
    entry.runtime_data = VoipStackRuntime(
        transport_config=cfg,
        assist_config=assist_cfg,
        trunk_config=trunk_cfg,
        endpoints=endpoint_registry,
        phones=PhoneAdapterRegistry(
            hass,
            endpoint_registry,
            preferred_phone_device_id=preferred_phone_device_id,
        ),
        preferred_phone_device_id=preferred_phone_device_id,
        debug_mode=bool(entry.data.get(CONF_DEBUG_MODE, False)),
        media_capture=bool(entry.data.get(CONF_MEDIA_CAPTURE, False)),
        entry_runtime_signature=_entry_runtime_signature(entry),
        entry_phone_signature=_entry_phone_signature(entry),
        entry_phone_records={
            str(subentry.data.get("endpoint_id") or "").strip(): dict(subentry.data)
            for subentry in phone_subentries(entry)
        },
        entry_contacts_signature=tuple(
            dict(item)
            for item in entry.data.get(CONF_PHONEBOOK_CONTACTS, [])
            if isinstance(item, dict)
        ),
        softphones=(
            previous_runtime.softphones if previous_runtime is not None else {}
        ),
        softphone_presence=(
            previous_runtime.softphone_presence
            if previous_runtime is not None
            else {}
        ),
    )
    from .local_softphone_runtime import async_setup_local_softphone_bridge

    async_setup_local_softphone_bridge(hass)
    from .repairs import async_sync_runtime_issues

    async_sync_runtime_issues(hass)
    hass.data[DOMAIN]["manual_roster_entries"] = _manual_roster_entries(hass)
    await _async_setup_shared(hass)
    for subentry in phone_subentries(entry):
        endpoint = endpoint_registry.get(str(subentry.data.get("endpoint_id") or ""))
        if endpoint is None or endpoint.kind is not EndpointKind.BROWSER:
            continue
        await _async_load_ha_softphone_store(
            hass,
            entry,
            endpoint_id=endpoint.endpoint_id,
            endpoint_data=dict(subentry.data),
        )
    await _async_apply_assist_intents(
        hass,
        bool(entry.data.get(CONF_ASSIST_INTENTS, False)),
    )
    if assist_cfg[CONF_ASSIST_ENDPOINT_ENABLED]:
        from homeassistant.components.assist_pipeline.pipeline import async_get_pipeline

        pipeline_id = assist_cfg[CONF_ASSIST_PIPELINE]
        pipeline = async_get_pipeline(
            hass,
            pipeline_id=None if pipeline_id in {"", "preferred"} else pipeline_id,
        )
        assist_cfg["name"] = pipeline.name
    if not await _async_start_sip_endpoint(hass):
        raise ConfigEntryError(
            f"Failed to bind SIP port {cfg['sip_port']}. Another SIP "
            "endpoint may already be listening on that port."
        )
    await _async_start_sip_trunk(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_config_entry_updated))
    create_runtime_task(hass, _deferred_phonebook_sync(hass))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: VoipStackConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False
    await _async_apply_assist_intents(hass, False)

    # Stop sessions / bridges before tearing down listeners; otherwise
    # orphaned transports leak sockets across config-entry reload.
    from .websocket_api import _async_shutdown_all

    await _async_shutdown_all(hass)

    # The authoritative PBX runtime stops calls, trunk and listeners in one
    # ordered cleanup barrier.  Do not tear the trunk out from under live call
    # sessions before that owner begins shutdown.
    await _async_stop_sip_endpoint(hass)
    unsub = entry.runtime_data.esp_state_event_bridge_unsub
    entry.runtime_data.esp_state_event_bridge_unsub = None
    if unsub is not None:
        unsub()
    unsub = entry.runtime_data.phonebook_service_event_unsub
    entry.runtime_data.phonebook_service_event_unsub = None
    if unsub is not None:
        unsub()
    entry.runtime_data.endpoints.close()
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: VoipStackConfigEntry) -> None:
    """Forget all runtime state owned by a permanently removed entry.

    Services, websocket commands and HTTP views are process-wide Home
    Assistant registrations and intentionally survive. ``initialized`` and
    the view-registration sentinels therefore remain in the domain bucket so
    adding the integration again cannot attempt duplicate registrations.
    """
    bucket = hass.data.get(DOMAIN)
    if not isinstance(bucket, dict):
        return

    bucket.pop("manual_roster_entries", None)
    bucket.pop(entry.entry_id, None)


async def _async_start_sip_trunk(hass: HomeAssistant) -> bool:
    from .trunk_runtime import async_start_sip_trunk

    return await async_start_sip_trunk(hass, local_ip=await _ha_advertise_host(hass))


async def _async_stop_sip_trunk(hass: HomeAssistant) -> None:
    from .trunk_runtime import async_stop_sip_trunk

    await async_stop_sip_trunk(hass)


async def _async_start_sip_endpoint(hass: HomeAssistant) -> bool:
    from .endpoint_runtime import async_start_sip_endpoint

    return await async_start_sip_endpoint(hass)


async def _async_stop_sip_endpoint(hass: HomeAssistant) -> None:
    from .endpoint_lifecycle import async_stop_sip_endpoint

    await async_stop_sip_endpoint(hass)
