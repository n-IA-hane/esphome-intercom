"""Runtime helpers shared by SIP service and endpoint orchestration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from .runtime_data import (
    call_projection,
    endpoint_directory,
    sip_endpoint_manager,
    sip_trunk,
)
from .endpoint_session import SipTerminationDisposition, TerminationIntent
from .fsm import sip_failure_response

_LOGGER = logging.getLogger(__name__)


def _connected_identity_for_call(
    hass: HomeAssistant,
    call_id: str,
) -> tuple[str, str]:
    """Resolve one human name and stable SIP user from canonical call ownership."""

    from .core.sip import parse_sip_uri

    registry = call_projection(hass)
    if registry is None:
        return "", ""
    session_id = registry.resolve_session_id(call_id)
    session = registry.sessions.get(session_id)
    if session is None:
        return "", ""
    metadata = session.metadata
    endpoints = endpoint_directory(hass)
    for key in ("dest_endpoint_id", "target_endpoint_id", "endpoint_id"):
        endpoint_id = str(metadata.get(key) or "").strip()
        endpoint = endpoints.get(endpoint_id) if endpoint_id else None
        if endpoint is not None:
            return str(endpoint.name), str(endpoint.sip_uri_user)
    name = str(
        metadata.get("connected_party")
        or metadata.get("answered_by")
        or session.callee
        or ""
    ).strip()
    user = ""
    try:
        user = parse_sip_uri(str(metadata.get("sip_uri") or "")).user
    except (TypeError, ValueError):
        pass
    return name, str(user or name).strip()


def sip_servers(hass: HomeAssistant) -> list[object]:
    """Return every signaling endpoint that may own an inbound dialog."""
    servers: list[object] = []
    endpoint = sip_endpoint_manager(hass)
    if endpoint is not None:
        servers.append(endpoint)
    trunk_endpoint = getattr(sip_trunk(hass), "inbound_endpoint", None)
    if trunk_endpoint is not None:
        servers.append(trunk_endpoint)
    return servers


def send_final_response(
    hass: HomeAssistant,
    call_id: str,
    status: int,
    reason: str,
    *,
    answer_sdp: str = "",
    decline_reason: str = "",
    connected_identity_name: str = "",
    connected_identity_user: str = "",
    expected_generation: int | None = None,
) -> bool:
    """Send a final response through the endpoint owning ``call_id``."""
    if 200 <= int(status) < 300:
        calls = call_projection(hass)
        session_id = calls.resolve_session_id(call_id) if calls is not None else ""
        session = calls.sessions.get(session_id) if calls is not None else None
        if (
            session is None
            or expected_generation is None
            or session.generation != expected_generation
            or not session.answer_committed
        ):
            _LOGGER.error(
                "Rejected unowned SIP answer call_id=%s generation=%s",
                call_id,
                expected_generation,
            )
            return False
    if 200 <= int(status) < 300 and not (
        connected_identity_name and connected_identity_user
    ):
        resolved_name, resolved_user = _connected_identity_for_call(hass, call_id)
        connected_identity_name = connected_identity_name or resolved_name
        connected_identity_user = connected_identity_user or resolved_user
    for server in sip_servers(hass):
        send = getattr(server, "send_final_response", None)
        if callable(send) and send(
            call_id,
            status,
            reason,
            answer_sdp=answer_sdp,
            decline_reason=decline_reason,
            connected_identity_name=connected_identity_name,
            connected_identity_user=connected_identity_user,
        ):
            return True
    return False


def send_bye(hass: HomeAssistant, call_id: str = "") -> bool:
    """Send BYE through the endpoint owning ``call_id``."""
    for server in sip_servers(hass):
        send_bye_for_dialog = getattr(server, "send_bye", None)
        if callable(send_bye_for_dialog) and send_bye_for_dialog(call_id):
            return True
    return False


async def async_signal_termination(
    hass: HomeAssistant,
    call_id: str,
    intent: TerminationIntent,
) -> None:
    """Perform the one SIP terminal action selected by the call owner."""

    disposition = intent.sip_disposition
    if disposition is SipTerminationDisposition.NONE:
        return
    if disposition in {SipTerminationDisposition.AUTO, SipTerminationDisposition.BYE}:
        if send_bye(hass, call_id):
            return
        if disposition is SipTerminationDisposition.BYE:
            return
    if disposition is SipTerminationDisposition.CANCEL:
        # Outbound client legs own CANCEL and are closed by their leg closer.
        return
    status, reason, _, _ = sip_failure_response(intent.reason)
    if intent.response_status:
        status = intent.response_status
    send_final_response(
        hass,
        call_id,
        status,
        reason,
        decline_reason=intent.reason,
    )


def uri_transport(uri) -> str:
    """Return the SIP signaling transport declared by a parsed URI."""
    for key, value in getattr(uri, "params", ()) or ():
        if str(key).lower() == "transport" and str(value or "").lower() in {
            "tcp",
            "tls",
            "udp",
        }:
            return str(value).upper()
    return "TLS" if str(getattr(uri, "scheme", "sip")).lower() == "sips" else "UDP"


def enable_reused_tcp_connection(
    hass: HomeAssistant,
    client,
    uri,
    *,
    target: str,
    default_sip_port: int,
) -> bool:
    """Use the REGISTER TCP connection when a client Contact points at it.

    A registered endpoint behind NAT commonly advertises an unreachable
    Contact.  The registrar therefore normalizes TCP contacts to the observed
    source flow.  Keep the failure path observable: falling back to a new TCP
    connection is valid for an ordinary URI, but is the main interoperability
    clue when a registered door station never sees HA's outbound INVITE.
    """
    if uri_transport(uri).upper() != "TCP":
        return False
    endpoint = sip_endpoint_manager(hass)
    tcp_server = getattr(endpoint, "tcp_server", None)
    if tcp_server is None:
        _LOGGER.debug(
            "SIP TCP flow reuse unavailable for %s: listener is not running",
            target,
        )
        return False
    remote_addr = (uri.host, int(uri.port or default_sip_port))
    reuse = tcp_server.open_reused_dialog(remote_addr, client.dialog_ids.call_id)
    if reuse is None:
        _LOGGER.debug(
            "SIP TCP flow reuse unavailable for %s: no live registered flow "
            "at %s:%s; the client may open a new connection",
            target,
            remote_addr[0],
            remote_addr[1],
        )
        return False
    send, responses = reuse
    client.use_reused_tcp_connection(
        send=send,
        responses=responses,
        close=lambda addr=remote_addr, call_id=client.dialog_ids.call_id: (
            tcp_server.close_reused_dialog(addr, call_id)
        ),
    )
    _LOGGER.info(
        "SIP TCP connection reuse enabled for %s via %s:%s",
        target,
        remote_addr[0],
        remote_addr[1],
    )
    return True
