"""Runtime helpers shared by SIP service and endpoint orchestration."""

from __future__ import annotations

import logging
from ipaddress import ip_address
import socket
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from .runtime_data import (
    call_projection,
    endpoint_directory,
    sip_endpoint_manager,
    sip_trunks,
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
    source_endpoint_id = str(metadata.get("source_endpoint_id") or "").strip()
    for key in ("dest_endpoint_id", "target_endpoint_id", "endpoint_id"):
        endpoint_id = str(metadata.get(key) or "").strip()
        # A local application retains the caller's endpoint claim. That
        # ownership fallback is not the connected callee's SIP identity.
        if key == "endpoint_id" and endpoint_id == source_endpoint_id:
            continue
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
    for trunk in sip_trunks(hass).values():
        trunk_endpoint = getattr(trunk, "inbound_endpoint", None)
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
        for server in sip_servers(hass):
            async_send = getattr(server, "async_send_bye", None)
            if callable(async_send):
                if await async_send(call_id):
                    return
                continue
            send = getattr(server, "send_bye", None)
            if callable(send) and send(call_id):
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


def enable_reused_signaling_flow(
    hass: HomeAssistant,
    client,
    uri,
    *,
    target: str,
    default_sip_port: int,
) -> bool:
    """Borrow the local UDP listener or the client's registered TCP flow.

    A registered endpoint behind NAT commonly advertises an unreachable
    Contact.  The registrar therefore normalizes TCP contacts to the observed
    source flow.  Keep the failure path observable: falling back to a new TCP
    connection is valid for an ordinary URI, but is the main interoperability
    clue when a registered door station never sees HA's outbound INVITE.
    """
    transport = uri_transport(uri).upper()
    if transport not in {"UDP", "TCP"}:
        return False
    endpoint = sip_endpoint_manager(hass)
    server = (getattr(getattr(endpoint, "udp_server", None), "endpoint", None)
              if transport == "UDP" else getattr(endpoint, "tcp_server", None))
    if server is None:
        _LOGGER.debug(
            "SIP %s flow reuse unavailable for %s: listener is not running",
            transport,
            target,
        )
        return False
    if transport == "UDP":
        try:
            address = ip_address(uri.host)
        except ValueError:
            # A borrowed connection has an already selected remote tuple.
            # Leave unresolved URIs with the client's RFC3263 resolver.
            return False
        sock = server.transport.get_extra_info("socket") if server.transport is not None else None
        if sock is None or (address.version == 6 and sock.family != socket.AF_INET6):
            return False
    remote_addr = (uri.host, int(uri.port or default_sip_port))
    reuse = server.open_reused_dialog(remote_addr, client.dialog_ids.call_id)
    if reuse is None:
        _LOGGER.debug(
            "SIP %s flow reuse unavailable for %s: no live flow "
            "at %s:%s; the client may open a new connection",
            transport,
            target,
            remote_addr[0],
            remote_addr[1],
        )
        return False
    send, responses = reuse
    client.use_reused_signaling_flow(
        send=send,
        responses=responses,
        close=lambda addr=remote_addr, call_id=client.dialog_ids.call_id: (
            server.close_reused_dialog(addr, call_id)
        ),
        datagram_family=sock.family if transport == "UDP" else socket.AF_UNSPEC,
    )
    if transport == "UDP":
        client.local_sip_port = int(server.transport.get_extra_info("sockname")[1])
    bind_usage = getattr(server, "bind_reused_usage", None)
    if bind_usage is not None:
        call_id = client.dialog_ids.call_id
        client.bind_signaling_usage = lambda usage_id: bind_usage(remote_addr, call_id, usage_id)
    _LOGGER.info(
        "SIP %s flow reuse enabled for %s via %s:%s",
        transport,
        target,
        remote_addr[0],
        remote_addr[1],
    )
    return True


def configure_local_contact(hass: HomeAssistant, client) -> None:
    """Publish the HA user agent's stable Contact before opening its call leg."""
    from .core import sip
    registry = call_projection(hass)
    manager = sip_endpoint_manager(hass)
    application = registry.component("sip_application") if registry is not None else None
    if manager is None or application is None or not application.gruu_id:
        return
    client.local_contact_uri = str(sip.SipUri(client.local_uri_user, manager.local_ip, manager.port,
        params=(("transport", client.signaling_transport.lower()), ("gr", application.gruu_id))))
    client.resolve_local_target = application.resolve_local_target
    client.retain_signaling = application.retain_signaling
    client.observe_event_owner = application.observe_event_owner
    client.release_event_owner = application.release_event_owner
    client.archive_responses = application.archive_responses
    application.observe_event_owner(client)
