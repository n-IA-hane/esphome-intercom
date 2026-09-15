"""Established-call transfer through the session-owned SIP dialog."""

from __future__ import annotations

from dataclasses import dataclass

from .core import sip, sip_transfer
from .phone_endpoint import PhoneEndpoint
from .runtime_data import VoipStackRuntime
from .roster import find_entry, parse_roster_json
from .sip_client import SipCallClient, SipTransferResult


@dataclass(frozen=True, slots=True)
class CallTransferRequest:
    """One blind or attended transfer request."""

    call_id: str
    destination: str
    replaces_call_id: str = ""


def _client_for_call(runtime: VoipStackRuntime, call_id: str) -> SipCallClient | None:
    calls = runtime.sip
    if calls is None:
        return None
    clients = calls.sip_clients_snapshot()
    if client := clients.get(call_id):
        return client if isinstance(client, SipCallClient) else None
    session = calls.get_session(call_id)
    if session is None:
        return None
    candidates = tuple(
        leg.dialog
        for leg in session.legs.values()
        if isinstance(leg.dialog, SipCallClient) and leg.dialog.dialog is not None
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
    entry = find_entry(parse_roster_json(roster_json), raw) if roster_json else None
    if entry is not None and entry.sip_uri:
        return sip_transfer.SipReferTarget(str(sip.parse_sip_uri(entry.sip_uri)))
    endpoint = runtime.endpoints.resolve(raw)
    if not remote_uri:
        raise sip.SipError("call dialog is unavailable")
    remote = sip.parse_sip_uri(remote_uri)
    user = _endpoint_user(endpoint, entry.number if entry is not None and entry.number else raw)
    if not user:
        raise sip.SipError("transfer destination is empty")
    return sip_transfer.SipReferTarget(
        str(sip.SipUri(user, remote.host, remote.port, remote.params))
    )


def _inbound_server(runtime: VoipStackRuntime, call_id: str):
    if runtime.sip is None:
        return None, ""
    server = runtime.sip.component("udp_listener")
    trunk = runtime.sip.component("trunk")
    for candidate in (server, getattr(trunk, "inbound_endpoint", None)):
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
    return sip_transfer.SipReferTarget(
        dialog.remote_uri,
        sip_transfer.SipReplaces(
            consultation.dialog_ids.call_id,
            to_tag=consultation.dialog_ids.remote_tag,
            from_tag=consultation.dialog_ids.local_tag,
        ),
    )


async def async_transfer_call(
    runtime: VoipStackRuntime,
    request: CallTransferRequest,
) -> SipTransferResult:
    """Transfer one established call without creating another lifecycle owner."""

    client = _client_for_call(runtime, request.call_id)
    server, remote_uri = (
        (None, client.dialog.remote_uri)
        if client is not None and client.dialog is not None
        else _inbound_server(runtime, request.call_id)
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
    if server is not None:
        return await server.async_refer(request.call_id, target) or SipTransferResult(False, 0, "call_not_found")
    return await client.refer(target)


async def async_transfer_target(
    runtime: VoipStackRuntime,
    call_id: str,
    target: sip_transfer.SipReferTarget,
) -> SipTransferResult:
    """Relay an inbound REFER to the unique opposite SIP leg."""

    client = _client_for_call(runtime, call_id)
    if client is None or client.dialog is None:
        return SipTransferResult(False, 0, "call_not_found")
    return await client.refer(target)
