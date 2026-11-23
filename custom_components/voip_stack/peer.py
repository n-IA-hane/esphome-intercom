"""Typed peer model for roster/routing decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .core.sip import SipUri, parse_sip_uri


@dataclass(frozen=True)
class Peer:
    """One routable voip endpoint known to Home Assistant."""

    name: str
    host: str
    endpoint_id: str = ""
    endpoint_kind: str = ""
    capabilities: tuple[str, ...] = ()
    local_ha: bool = False
    sip_port: int | None = None
    rtp_port: int | None = None
    extension: str = ""
    sip_uri_user: str = ""
    contact_uri: str = ""
    conference_group: str = ""
    conference_ring: bool = False
    ring_group: str = ""
    device: dict[str, Any] | None = None

    @property
    def is_ha(self) -> bool:
        return self.local_ha

    @property
    def device_id(self) -> str | None:
        if self.device is None:
            return None
        return self.device.get("device_id")

    @property
    def request_uri_user(self) -> str:
        """Return the stable SIP user used to address this peer."""
        return self.sip_uri_user or self.extension or self.name


def sip_uri_for_peer(
    peer: Peer,
    *,
    default_port: int,
    fallback_user: str = "",
) -> SipUri:
    """Preserve a registered Contact as the transport destination."""

    if peer.contact_uri:
        return parse_sip_uri(peer.contact_uri)

    transport = str((peer.device or {}).get("sip_transport") or "tcp").lower()
    if transport not in {"tcp", "udp", "tls"}:
        transport = "tcp"
    return SipUri(
        peer.request_uri_user or fallback_user,
        peer.host,
        peer.sip_port or int(default_port),
        (("transport", transport),),
        "sips" if transport == "tls" else "sip",
    )
