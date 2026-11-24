"""Best-effort display information on the current ESPHome API connection.

The call session owns delivery tasks and their small delivery cache. SIP legs
and committed relay resources remain the authorities for identity and media.
"""

from __future__ import annotations

import asyncio
import logging

from .core.sdp import remote_can_receive, remote_can_send
from .endpoint_session import LegKind, LegPhase
from .esphome_native import ProvisioningActions, connected, runtime_for_entry
from .phone_endpoint import EndpointKind
from .sip_rtp_bridge import SipRtpRelay

_LOGGER = logging.getLogger(__name__)
_ACTION = "set_pbx_media_diagnostics"
_OWNER = "pbx_media_diagnostics"


def _video_operations(relay):
    video = relay.video_relay
    if video is None:
        return "inactive", "inactive"
    result = []
    for side, source, target in (
        ("left", video.left, video.right),
        ("right", video.right, video.left),
    ):
        if not remote_can_send(source.video_format) or not remote_can_receive(
            target.video_format, connection_held=target.connection_held
        ):
            result.append("inactive")
        elif not video.processing_known_from(side):
            result.append("unknown")
        else:
            result.append("converted" if video.transcodes_from(side) else "forwarded")
    return tuple(result)


def _leg_call_id(session, endpoint_id):
    """Use one live SIP leg, including its actual outbound client dialog."""
    candidates = []
    for leg in session.legs.values():
        if (leg.endpoint_id != endpoint_id or leg.kind != LegKind.SIP
                or leg.phase not in {LegPhase.ANSWERED, LegPhase.HELD}):
            continue
        call_id = leg.sip_call_id
        if leg.dialog is not None:
            dialog = getattr(leg.dialog, "dialog", leg.dialog)
            actual = getattr(dialog, "call_id", "")
            if actual != call_id:
                continue
        if call_id:
            candidates.append(call_id)
    return candidates[0] if len(candidates) == 1 else ""


def _targets(data, session, relay):
    audio, video = relay.audio_operations, _video_operations(relay)
    result = []
    for index, key in enumerate(("source_endpoint_id", "dest_endpoint_id")):
        endpoint_id = session.metadata.get(key, "")
        endpoint = data.endpoints.get(endpoint_id) if endpoint_id else None
        if (endpoint is None or endpoint.kind is not EndpointKind.SIP_ACCOUNT
                or not endpoint.device_id or not endpoint.account_device_id
                or endpoint.device_id == endpoint.account_device_id
                or not (call_id := _leg_call_id(session, endpoint_id))):
            continue
        reverse = 1 - index
        result.append((endpoint_id, endpoint.device_id, call_id,
                       f"{audio[index]},{audio[reverse]}",
                       f"{video[index]},{video[reverse]}"))
    return tuple(result)


def _managed_hardware_matches(hass, data, endpoint_id, device_id, origin_id):
    """Follow the canonical account's configured native hardware association."""
    from .managed_phones import managed_account_subentries
    from .phone_config import phone_endpoint_id
    from .store import config_entry

    entry = config_entry(hass)
    row = managed_account_subentries(entry).get(device_id)
    endpoint = data.endpoints.get(endpoint_id)
    return (row is not None and endpoint is not None
            and phone_endpoint_id(row) == endpoint_id
            and row.data.get("username") == endpoint.username
            and row.data.get("origin_entry_id") == origin_id)


def publish_pbx_media_diagnostics(hass, data, session):
    """Schedule optional information after commit without blocking signaling."""
    try:
        _publish(hass, data, session)
    except Exception:
        _LOGGER.debug("Optional ESP media diagnostics unavailable", exc_info=True)


def _publish(hass, data, session):
    if not session.live or session.state != "in_call" or session.metadata.get("event_only"):
        return
    resources = [item for item in session.resources
                 if item.name.startswith("relay:") and isinstance(item.value, SipRtpRelay)]
    if len(resources) != 1:
        return
    resource = resources[0]
    relay = resource.value
    targets = _targets(data, session, relay)
    if not targets:
        return
    state = session.metadata.setdefault(_OWNER, {"delivered": {}})
    request = (id(resource), targets)
    task = session.named_tasks.get(_OWNER)
    if state.get("requested") == request and task is not None and not task.done():
        return
    data.sip.cancel_task(session.call_id, _OWNER)
    state["requested"] = request
    token = session.token

    def current():
        return (session.owns(token) and session.state == "in_call"
                and data.sip.get_session(token.call_id, generation=token.generation) is session
                and any(item is resource and item.value is relay for item in session.resources)
                and tuple(item[:3] for item in _targets(data, session, relay))
                == tuple(item[:3] for item in targets))

    async def deliver():
        try:
            from .device_resolver import get_resolver

            devices = await get_resolver(hass).list_devices()
            for endpoint_id, device_id, call_id, _audio, _video in targets:
                if not current():
                    return
                device = next((item for item in devices if item["device_id"] == device_id), None)
                if device is None:
                    continue
                origin_id = device.get("origin_entry_id", "")
                if not _managed_hardware_matches(hass, data, endpoint_id, device_id, origin_id):
                    continue
                origin = hass.config_entries.async_get_entry(origin_id)
                native = runtime_for_entry(origin)
                if origin is None or origin.domain != "esphome" or not connected(native):
                    continue
                actions = ProvisioningActions.capture(native)
                service = actions.by_name().get(_ACTION)
                if service is None or not actions.current(origin):
                    continue
                # Re-read the committed owner immediately before sending. The
                # queued request is only a delivery trigger, not a media snapshot.
                target = next(item for item in _targets(data, session, relay)
                              if item[:3] == (endpoint_id, device_id, call_id))
                payload = {"call_id": call_id, "audio": target[3], "video": target[4]}
                previous = state["delivered"].get(endpoint_id)
                if previous is not None and actions.same_owner(previous[0]) and payload == previous[1]:
                    continue
                try:
                    # A disconnected or stalled information channel cannot retain a task forever.
                    async with asyncio.timeout(2):
                        await actions.execute(service, payload)
                except Exception:
                    _LOGGER.debug("Optional ESP media diagnostics delivery failed", exc_info=True)
                    continue
                if (current() and actions.current(origin)
                        and hass.config_entries.async_get_entry(origin_id) is origin
                        and _managed_hardware_matches(hass, data, endpoint_id, device_id, origin_id)):
                    state["delivered"][endpoint_id] = (actions, payload)
        except Exception:
            _LOGGER.debug("Optional ESP media diagnostics discovery failed", exc_info=True)

    session.create_task(deliver(), name=_OWNER)
