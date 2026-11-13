"""Established-call transfer through the session-owned SIP dialog."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .core import sip, sip_transfer
from .phone_endpoint import PhoneEndpoint
from .runtime_data import VoipStackRuntime
from .roster import find_entry, parse_roster_json
from .sip_client import SipCallClient, SipTransferResult
from .router import RouteAction, ha_uri_for, resolve_ha_router
from .trunk_policy import call_external_call_denied
from .endpoint_session import LegPhase


@dataclass(frozen=True, slots=True)
class CallTransferRequest:
    """One blind or attended transfer request."""

    call_id: str
    destination: str
    replaces_call_id: str = ""
    endpoint_id: str = ""


def _client_for_call(runtime: VoipStackRuntime, call_id: str) -> SipCallClient | None:
    calls = runtime.sip
    if calls is None:
        return None
    if client := calls.sip_client_for(call_id):
        return client if isinstance(client, SipCallClient) else None
    session = calls.get_session(call_id)
    if session is None:
        return None
    destination = str(session.metadata.get("bridge_dest_call_id") or "")
    if destination and isinstance(client := calls.sip_client_for(destination), SipCallClient):
        return client
    candidates = tuple(
        leg.dialog
        for leg in session.legs.values()
        if isinstance(leg.dialog, SipCallClient) and leg.dialog.dialog is not None
        and leg.phase not in {LegPhase.CLOSING, LegPhase.CLOSED}
    )
    return candidates[0] if len(candidates) == 1 else None


def _endpoint_user(endpoint: PhoneEndpoint | None, fallback: str) -> str:
    return str(
        (endpoint.extension if endpoint is not None else "")
        or (endpoint.username if endpoint is not None else "")
        or fallback
    ).strip()


def _blind_target(
    runtime: VoipStackRuntime,
    remote_uri: str,
    destination: str,
) -> sip_transfer.SipReferTarget:
    raw = str(destination or "").strip()
    if raw.lower().startswith(("sip:", "sips:")):
        return sip_transfer.SipReferTarget(str(sip.parse_sip_uri(raw)))
    if "@" in raw:
        return sip_transfer.SipReferTarget(str(sip.parse_sip_uri(f"sip:{raw}")))
    phonebook = getattr(runtime, "phonebook_sensor", None)
    attributes = phonebook.extra_state_attributes if phonebook is not None else {}
    roster_json = str((attributes or {}).get("roster_json") or "")
    entries = parse_roster_json(roster_json) if roster_json else []
    entry = find_entry(entries, raw)
    if entry is not None and entry.metadata.get("virtual_endpoint") == "automation":
        if not entry.enabled:
            raise sip.SipError("transfer destination is disabled")
        target = entry.extension or entry.id
        return _local_transfer_target(runtime, target, entries)
    endpoint = runtime.endpoints.resolve(raw)
    if endpoint is not None:
        # A logical phone belongs to this PBX's dial plan. Its current
        # transport address does not carry routing policy, registration flow
        # ownership or the media negotiation performed by the PBX.
        return _local_transfer_target(runtime, _endpoint_user(endpoint, raw), entries)
    if entry is not None and entry.address:
        route = resolve_ha_router(raw, entries)
        if route.action is RouteAction.REJECT:
            raise sip.SipError("transfer destination is unavailable")
        if route.sip_uri:
            return sip_transfer.SipReferTarget(str(sip.parse_sip_uri(route.sip_uri)))
    if entry is not None and entry.sip_uri:
        return sip_transfer.SipReferTarget(str(sip.parse_sip_uri(entry.sip_uri)))
    if not remote_uri:
        raise sip.SipError("call dialog is unavailable")
    remote = sip.parse_sip_uri(remote_uri)
    user = _endpoint_user(endpoint, entry.number if entry is not None and entry.number else raw)
    if not user:
        raise sip.SipError("transfer destination is empty")
    return sip_transfer.SipReferTarget(
        str(sip.SipUri(user, remote.host, remote.port, remote.params))
    )


def _local_transfer_target(runtime, target, entries):
    """Address a local extension through the PBX that owns its dial plan."""
    uri = ha_uri_for(target, entries)
    if not uri:
        config = runtime.transport_config
        server = runtime.sip.component("udp_listener") if runtime.sip is not None else None
        host = str(config.get("advertise_host") or getattr(server, "local_ip", "") or "")
        if not host or host in {"0.0.0.0", "::"}:
            raise sip.SipError("Home Assistant SIP address is unavailable for transfer")
        port = int(config.get("sip_port") or getattr(server, "local_sip_port", 5060))
        uri = ha_uri_for(target, entries, str(sip.SipUri("HA", host, port)))
    return sip_transfer.SipReferTarget(str(sip.parse_sip_uri(uri)))


def _inbound_server(runtime: VoipStackRuntime, call_id: str):
    if runtime.sip is None:
        return None, ""
    server = runtime.sip.component("udp_listener")
    identities = runtime.trunk_configs or {"": runtime.trunk_config}
    trunks = [runtime.sip.component(f"trunk:{identity}" if identity else "trunk") for identity in identities]
    for candidate in (server, *(getattr(trunk, "inbound_endpoint", None) for trunk in trunks)):
        if candidate is not None:
            remote_uri = candidate.remote_uri_for_call(call_id)
            if remote_uri:
                return candidate, remote_uri
    return None, ""


def _attended_target(
    consultation: SipCallClient,
) -> sip_transfer.SipReferTarget:
    dialog = consultation.dialog
    if dialog is None or not consultation.dialog_ids.remote_tag:
        raise sip.SipError("replacement dialog is unavailable")
    target_uri = dialog.remote_uri
    if dialog.remote_target_uri:
        contact = sip.parse_sip_uri(dialog.remote_target_uri)
        if any(key.casefold() == "gr" for key, _value in contact.params):
            target_uri = str(contact)
    return sip_transfer.SipReferTarget(
        target_uri,
        sip_transfer.SipReplaces(
            consultation.dialog_ids.call_id,
            to_tag=consultation.dialog_ids.remote_tag,
            from_tag=consultation.dialog_ids.local_tag,
        ),
    )


def _server_local_uri(server) -> str:
    port = getattr(server, "local_sip_port", None) or getattr(server, "port", 5060)
    return str(sip.SipUri("HA", server.local_ip, port))


def _external_transfer_denied(runtime, call_id, target, remote_uri, local_uri="") -> bool:
    """Apply the existing call's access policy before delegating a REFER."""
    if not call_external_call_denied(runtime, call_id):
        return False
    from .trunk_policy import is_any_trunk_uri

    if is_any_trunk_uri(runtime, target.uri):
        return True
    uri = sip.parse_sip_uri(target.uri)
    phonebook = getattr(runtime, "phonebook_sensor", None)
    attributes = phonebook.extra_state_attributes if phonebook is not None else {}
    roster_json = str((attributes or {}).get("roster_json") or "")
    entries = parse_roster_json(roster_json) if roster_json else []
    local = sip.parse_sip_uri(local_uri) if local_uri else None
    local_target = bool(local and sip.sip_endpoints_equal(
        uri.host, uri.port, local.host, local.port
    ))
    if local_target:
        # Follow HA's own dial plan; an extension is not necessarily external.
        route = resolve_ha_router(uri.user, entries, trunk_ready=True)
        # REFER delegates a new call to the peer, losing this session's
        # restricted origin. Groups and automation can create later trunk legs;
        # only HA-owned forwarding can retain the origin across those routes.
        return route.action in {
            RouteAction.TRUNK, RouteAction.GROUP, RouteAction.AUTOMATION,
        } or is_any_trunk_uri(runtime, route.sip_uri)
    if not is_any_trunk_uri(runtime, remote_uri):
        return False
    # A provider executes REFER itself. Only a known direct local destination
    # is safe here; an unknown remote target could create another trunk call.
    entry = find_entry(entries, uri.user)
    if entry is not None:
        known_uri = sip.parse_sip_uri(entry.sip_uri) if entry.sip_uri else None
        if known_uri is not None and sip.sip_endpoints_equal(
            uri.host, uri.port, known_uri.host, known_uri.port
        ):
            return False
        if entry.address and sip.sip_endpoints_equal(
            uri.host, uri.port, entry.address, entry.port
        ):
            return False
    return True


async def _transfer_local_source(
    hass, runtime, session, current, retained, destination
):
    """Route the existing browser source through the ordinary forward owner."""
    from .local_softphone_runtime import PendingLocalSource
    from .runtime_data import sip_endpoint_manager
    from .session_cleanup import async_wait_for_cleanup

    registry = runtime.sip
    manager = sip_endpoint_manager(hass)
    if manager is None or registry.forward_call is None or current.video_active:
        return SipTransferResult(False, 488, "local_transfer_unavailable")
    previous = registry.resource_for(session.call_id, "browser_source")
    if previous is not None and not previous.committed:
        return SipTransferResult(False, 491, "transfer_in_progress")
    source = PendingLocalSource(
        hass,
        current,
        manager.local_ip,
        int(runtime.transport_config["sip_port"]),
        retained_endpoint_id=retained,
    )
    if previous is not None:
        registry.release_resource(
            session.call_id, f"browser_source:{session.call_id}", value=previous
        )
    registry.own_resource(
        session.call_id, f"browser_source:{session.call_id}", source, None
    )
    registry.set_pending_invite(session.call_id, source.invite)
    task = None
    try:
        async with asyncio.timeout(30):
            await registry.forward_call(
                call_id=session.call_id,
                destination=destination,
                on_failure="resume",
                expected_state="in_call",
            )
            task = registry.task_for(session.call_id, "forward")
            if task is not None:
                await asyncio.shield(task)
        success = source.committed and session.live and session.state == "in_call"
        return SipTransferResult(
            success,
            200 if success else 480,
            "completed" if success else "transfer_failed",
            retained_call=success,
        )
    except BaseException:
        if task is not None and not task.done() and not source.committed:
            task.cancel()
            await async_wait_for_cleanup(asyncio.gather(task, return_exceptions=True))
        raise
    finally:
        if not source.committed and session.live:
            source.resume()
            registry.release_resource(session.call_id, f"browser_source:{session.call_id}", value=source)


async def async_transfer_call(
    runtime: VoipStackRuntime,
    request: CallTransferRequest,
    *, hass=None,
) -> SipTransferResult:
    """Transfer one established call without creating another lifecycle owner."""

    session = runtime.sip.get_session(request.call_id) if hass is not None and runtime.sip is not None else None
    if session is not None and session.owner == "local_bridge" and hass is not None:
        from .local_softphone_runtime import local_softphone_bridge
        from .pbx_routing import browser_endpoint_can_ring

        bridge = local_softphone_bridge(hass)
        current = bridge.get_call(session.call_id) if bridge is not None else None
        target = runtime.endpoints.resolve(request.destination)
        if current is None or request.endpoint_id not in {current.caller_endpoint_id, current.callee_endpoint_id}:
            return SipTransferResult(False, 403, "call_not_owned")
        if request.replaces_call_id:
            return SipTransferResult(False, 488, "local_transfer_unavailable")
        if target is not None and (target.dnd or target.active_call_id):
            return SipTransferResult(False, 486, "busy")
        retained = current.callee_endpoint_id if request.endpoint_id == current.caller_endpoint_id else current.caller_endpoint_id
        from .account_dialing import account_uses_ha_only
        if not account_uses_ha_only(hass, target):
            return await _transfer_local_source(hass, runtime, session, current, retained, request.destination)
        if not browser_endpoint_can_ring(target):
            return SipTransferResult(False, 480, "unavailable")
        bridge.redirect_ringing(session.call_id, target.endpoint_id, retained_endpoint_id=retained)
        return SipTransferResult(True, 200, "completed", retained_call=True)

    # REFER goes to the peer being retained, not necessarily the outgoing leg.
    # The callee can transfer the original caller just as the caller can
    # transfer the callee. The existing source owner covers UAC and UAS legs.
    from_callee = bool(
        session is not None
        and request.endpoint_id
        and request.endpoint_id == session.metadata.get("dest_endpoint_id")
    )
    client = (
        runtime.sip.source_sip_client_for(session.call_id)
        if from_callee else _client_for_call(runtime, request.call_id)
    )
    server, remote_uri = (
        (None, client.dialog.remote_uri)
        if client is not None and client.dialog is not None
        else _inbound_server(runtime, session.call_id if from_callee else request.call_id)
    )
    if not remote_uri:
        return SipTransferResult(False, 0, "call_not_found")
    if request.replaces_call_id:
        consultation = _client_for_call(runtime, request.replaces_call_id)
        if consultation is None or consultation is client:
            return SipTransferResult(False, 0, "replacement_not_found")
        target = _attended_target(consultation)
    else:
        target = _blind_target(runtime, remote_uri, request.destination)
    local_uri = (
        client.dialog.local_uri
        if client is not None and client.dialog is not None
        else _server_local_uri(server)
    )
    if _external_transfer_denied(runtime, request.call_id, target, remote_uri, local_uri):
        return SipTransferResult(False, 403, "external_calls_disabled")
    if server is not None:
        return await server.async_refer(request.call_id, target) or SipTransferResult(False, 0, "call_not_found")
    return await client.refer(target)


async def async_transfer_target(
    runtime: VoipStackRuntime,
    call_id: str,
    target: sip_transfer.SipReferTarget,
    *,
    source_client: SipCallClient | None = None,
    hass=None,
) -> SipTransferResult:
    """Relay an inbound REFER to the unique opposite SIP leg."""

    if hass is not None and target.replaces is None and runtime.sip is not None:
        from .parking import parking_address, park_established_sip_call, ParkingError
        from .endpoint_routing import is_local_listener_uri

        uri = sip.parse_sip_uri(target.uri)
        listener = runtime.sip.component("udp_listener")
        local_host = str(getattr(listener, "local_ip", ""))
        if is_local_listener_uri(uri, local_ip=local_host, sip_port=int(runtime.transport_config["sip_port"])):
            address = parking_address(hass, uri.user)
            if address is not None:
                session = runtime.sip.get_session(runtime.sip.resolve_session_id(call_id))
                if session is None:
                    return SipTransferResult(False, 481, "call_not_found")
                from_callee = (source_client.dialog_ids.call_id == session.metadata.get("bridge_dest_call_id")
                               if source_client is not None else call_id == session.metadata.get("bridge_dest_call_id"))
                parker = str(session.metadata.get("dest_endpoint_id" if from_callee else "source_endpoint_id") or "")
                try:
                    import asyncio
                    await park_established_sip_call(hass, call_id, parker, slot=address[1], referrer=source_client,
                                                   refer_transaction=asyncio.current_task())
                except ParkingError:
                    return SipTransferResult(False, 486, "parking_unavailable")
                return SipTransferResult(True, 200, "parked")

    client = _client_for_call(runtime, call_id)
    server = None
    if source_client is not None:
        calls = runtime.sip
        session = calls.get_session(calls.resolve_session_id(call_id)) if calls else None
        if session is None or not session.live:
            return SipTransferResult(False, 0, "call_not_found")
        candidates = {
            id(leg.dialog): leg.dialog for leg in session.legs.values()
            if isinstance(leg.dialog, SipCallClient) and leg.dialog is not source_client
            and leg.dialog.dialog is not None
            and leg.phase not in {LegPhase.CLOSING, LegPhase.CLOSED}
        }
        if len(candidates) > 1:
            return SipTransferResult(False, 603, "ambiguous_call")
        client = next(iter(candidates.values()), None)
        call_id = session.call_id
        if client is None:
            server, remote_uri = _inbound_server(runtime, call_id)
    if client is not None and client.dialog is not None:
        remote_uri, local_uri = client.dialog.remote_uri, client.dialog.local_uri
    elif server is not None:
        local_uri = _server_local_uri(server)
    else:
        return SipTransferResult(False, 0, "call_not_found")
    if _external_transfer_denied(
        runtime, call_id, target, remote_uri, local_uri
    ):
        return SipTransferResult(False, 403, "external_calls_disabled")
    from .call_replaces import prepare_replacement_grant, ReplacementRejected

    try:
        grant = prepare_replacement_grant(runtime, call_id, target, source_client)
    except ReplacementRejected as err:
        return SipTransferResult(False, err.status, "replacement_rejected")
    try:
        if grant is not None and not grant.endpoint_id:
            # A trunk or unrepresented SIP caller has no endpoint identity
            # for a replacement INVITE. Keep its existing dialog in HA.
            from .local_transfer import try_local_audio_transfer

            local = await try_local_audio_transfer(hass, runtime, grant, source_client) if hass is not None else None
            return local or SipTransferResult(False, 488, "local_transfer_unavailable")
        if server is not None:
            result = await server.async_refer(call_id, target) or SipTransferResult(False, 0, "call_not_found")
        else:
            result = await client.refer(target)
        if result.status in {405, 501} and grant is not None and hass is not None:
            from .local_transfer import try_local_audio_transfer

            local = await try_local_audio_transfer(hass, runtime, grant, source_client)
            if local is not None:
                return local
        return result
    finally:
        if grant is not None:
            grant.release()
