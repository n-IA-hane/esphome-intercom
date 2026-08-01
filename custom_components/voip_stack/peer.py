"""Typed peer model for roster/routing decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


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
    conference_group: str = ""
    conference_ring: bool = False
    ring_group: str = ""
    audio_mode: Literal["full_duplex", "mic_only", "speaker_only"] = "full_duplex"
    tx_formats: list[str] | None = None
    rx_formats: list[str] | None = None
    device: dict[str, Any] | None = None

    @property
    def is_ha(self) -> bool:
        return self.local_ha

    @property
    def device_id(self) -> str | None:
        if self.device is None:
            return None
        return self.device.get("device_id")
