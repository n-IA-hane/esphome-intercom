"""Resolve explicit contact choices separately from observed SIP identity."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .core.codec_capabilities import supports_dahua_pcm


@dataclass(frozen=True, slots=True)
class PeerMediaProfile:
    is_dahua: bool
    include_dahua_pcm: bool
    explicit_dahua: bool


def resolve_peer_media_profile(
    metadata: Mapping[str, Any] | None,
    user_agent_override: str | None = None,
) -> PeerMediaProfile:
    """Keep PCM opt-in independent of the Dahua video compatibility profile.

    Registered contact metadata historically stores a detected profile for the
    primary binding. A chosen binding's actual User-Agent takes precedence over
    that derived value. Static contacts can explicitly select their profile.
    """
    metadata = metadata or {}
    user_agent = str(
        metadata.get("user_agent") or ""
        if user_agent_override is None else user_agent_override
    ).strip()
    detected = supports_dahua_pcm(user_agent)
    configured = str(metadata.get("sip_profile") or "auto").strip().casefold() == "dahua"
    explicit = configured and not metadata.get("registered", False)
    legacy = configured and user_agent_override is None and not user_agent
    is_dahua = explicit or detected or legacy
    choice = str(metadata.get("dahua_audio") or "auto").strip().casefold()
    include_pcm = choice == "pcm" or (choice == "auto" and detected)
    return PeerMediaProfile(is_dahua, include_pcm, explicit)
