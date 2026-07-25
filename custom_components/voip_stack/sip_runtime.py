"""Runtime helpers shared by SIP service and endpoint orchestration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def sip_servers(hass: HomeAssistant) -> list[object]:
    """Return every signaling endpoint that may own an inbound dialog."""
    bucket = hass.data.get(DOMAIN, {})
    servers: list[object] = []
    endpoint = bucket.get("sip_endpoint")
    if endpoint is not None:
        servers.append(endpoint)
    else:
        servers.extend(
            server
            for server in (bucket.get("sip_server"), bucket.get("sip_tcp_server"))
            if server is not None
        )
    trunk_endpoint = getattr(bucket.get("sip_trunk"), "inbound_endpoint", None)
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
) -> bool:
    """Send a final response through the endpoint owning ``call_id``."""
    for server in sip_servers(hass):
        send = getattr(server, "send_final_response", None)
        if callable(send) and send(
            call_id,
            status,
            reason,
            answer_sdp=answer_sdp,
            decline_reason=decline_reason,
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


def uri_transport(uri) -> str:
    """Return the SIP signaling transport declared by a parsed URI."""
    for key, value in getattr(uri, "params", ()) or ():
        if str(key).lower() == "transport" and str(value or "").lower() in {
            "tcp",
            "udp",
        }:
            return str(value).upper()
    return "UDP"


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
    endpoint = hass.data.get(DOMAIN, {}).get("sip_endpoint")
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
