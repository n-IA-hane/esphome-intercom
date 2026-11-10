"""Re-offers for a browser connected to an existing outgoing SIP dialog."""

from .endpoint_lifecycle import call_registry
from .media_offer_answer import validate_direct_video_reoffer
from .media_session_updates import (
    commit_audio_session_update,
    commit_video_session_update,
    committed_video_format_state,
)
from .runtime_data import require_runtime_data
from .sip_client import PreparedDialogMediaUpdate
from .websocket_api import _ha_softphone_store, _fire_call_event


def attach_browser_dialog_updates(hass, client, endpoint_id, source_device_id, *, source_call_id=""):
    registry = call_registry(hass)
    async def _prepare_softphone_media_update(previous, updated, method):
        """Stage one peer re-offer for the live HA browser media owner."""

        call_id = source_call_id or client.dialog_ids.call_id
        session = registry.sessions.get(registry.resolve_session_id(call_id))
        if session is None:
            return None
        call_generation = session.generation
        browser_media = require_runtime_data(hass).media
        audio_session = browser_media.sessions_for("audio").get((call_id, endpoint_id))
        previous_video = previous.video_format
        updated_video = updated.video_format
        video_offer = validate_direct_video_reoffer(
            previous_video,
            previous.recv_video_format,
            updated_video,
            updated.recv_video_format,
        )
        if not video_offer.accepted:
            return None
        video_session = browser_media.sessions_for("video").get((call_id, endpoint_id))

        payload = None
        async def _commit() -> None:
            nonlocal payload
            if not registry.is_generation_current(call_id, call_generation):
                raise RuntimeError(
                    "SIP softphone media update belongs to a terminated call"
                )
            descriptor = registry.resource_for(call_id, "softphone_media")
            if descriptor is not None:
                from .local_call_media import ApplicationCallSource
                if isinstance(descriptor.get("invite"), ApplicationCallSource):
                    descriptor["invite"] = ApplicationCallSource.from_peer_dialog(updated,
                        call_id=call_id, caller=session.caller, target=session.callee,
                        trunk_id=str(session.metadata.get("inbound_trunk_id") or ""))
            if audio_session is not None:
                commit_audio_session_update(
                    audio_session,
                    updated,
                    dtmf_payload_type=updated.dtmf_payload_type,
                    dtmf_events=updated.dtmf_events,
                )
            if video_session is not None and updated_video is not None:
                registry.clear_video_parameter_sets(call_id)
                commit_video_session_update(
                    video_session,
                    updated,
                    local_direction=updated.local_video_direction,
                )
            elif video_session is not None:
                video_session.removed = True
                video_session.media_generation += 1
                video_session.update_event.set()
            store = _ha_softphone_store(hass, endpoint_id)
            if str(store.get("call_id") or "") == call_id:
                store.update(
                    {
                        "audio_direction": updated.local_audio_direction,
                        "audio_connection_held": updated.remote_audio_connection_held,
                        "video_active": bool(
                            updated_video is not None
                            and updated.local_video_direction != "inactive"
                        ),
                        "video_requested": bool(updated_video is not None),
                        "video_negotiated": bool(updated_video is not None),
                        "video_status": (
                            "active"
                            if updated_video is not None
                            and updated.local_video_direction != "inactive"
                            else "inactive"
                        ),
                        "video_failure_reason": "",
                        **committed_video_format_state(updated),
                        "video_direction": updated.local_video_direction,
                        "video_connection_held": updated.remote_video_connection_held,
                        "last_sip_event": method,
                        "media_renegotiations": int(
                            store.get("media_renegotiations") or 0
                        )
                        + 1,
                    }
                )
                payload = dict(store, endpoint_id=endpoint_id, device_id=source_device_id,
                               event_type="state_changed")

        def published(actual):
            from .call_scope import call_belongs_to_endpoint
            if (payload is not None and registry.is_generation_current(call_id, call_generation)
                    and client.on_media_update is _prepare_softphone_media_update and client.dialog is actual
                    and call_belongs_to_endpoint(registry, call_id, endpoint_id)):
                _fire_call_event(hass, payload, "session")

        return PreparedDialogMediaUpdate(_commit, on_committed=published)

    client.on_media_update = _prepare_softphone_media_update
