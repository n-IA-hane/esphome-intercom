"""Committed media updates for outbound SIP bridge dialogs."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .endpoint_lifecycle import call_registry
from .core.sdp import video_formats_passthrough_compatible
from .sip_bridge import dialog_rtp_peer, dialog_video_rtp_peer

if TYPE_CHECKING:
    from .sip_client import SipCallClient


_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class BridgeMediaUpdateBinder:
    """Bind outbound dialog re-offers to the current relay generation."""

    hass: HomeAssistant

    def attach(
        self,
        client: SipCallClient,
        relay: Any,
        *,
        source_call_id: str,
    ) -> None:
        """Install the staged media-update callback on one SIP client."""

        async def prepare(previous: Any, updated: Any, method: str):
            registry = call_registry(self.hass)
            session = registry.sessions.get(registry.resolve_session_id(source_call_id))
            if session is None:
                return None
            call_generation = session.generation
            try:
                previous_audio_peer = relay.right
                commit_audio = relay.prepare_peer_reconfiguration(
                    "right",
                    dialog_rtp_peer(updated),
                )
            except (TypeError, ValueError):
                return None

            video_relay = getattr(relay, "video_relay", None)
            previous_video_peer = video_relay.right if video_relay is not None else None
            previous_video = previous.video_format
            updated_video = updated.video_format
            if (previous_video is None) != (updated_video is None):
                return None
            commit_video = None
            if updated_video is not None:
                next_video_peer = dialog_video_rtp_peer(updated)
                if (
                    video_relay is None
                    or not video_formats_passthrough_compatible(
                        video_relay.left.recv_format,
                        next_video_peer.send_format,
                    )
                    or not video_formats_passthrough_compatible(
                        next_video_peer.recv_format,
                        video_relay.left.send_format,
                    )
                    or updated.remote_video_rtp_port <= 0
                ):
                    return None
                commit_video = video_relay.prepare_peer_reconfiguration(
                    "right",
                    next_video_peer,
                )

            async def commit() -> None:
                if not registry.is_generation_current(
                    source_call_id,
                    call_generation,
                ):
                    raise RuntimeError(
                        "SIP bridge media update belongs to a terminated call"
                    )
                if relay.right is not previous_audio_peer or (
                    video_relay is not None
                    and previous_video_peer is not None
                    and video_relay.right is not previous_video_peer
                ):
                    raise RuntimeError("SIP bridge media owner changed before commit")
                commit_audio()
                if commit_video is not None:
                    commit_video()
                _LOGGER.info(
                    "SIP bridge outbound %s committed source_call_id=%s "
                    "dest_call_id=%s remote_rtp=%s:%s audio_direction=%s "
                    "video_direction=%s",
                    method,
                    source_call_id,
                    client.dialog_ids.call_id,
                    updated.remote_rtp_host,
                    updated.remote_rtp_port,
                    updated.remote_audio_direction,
                    (
                        updated_video.direction
                        if updated_video is not None
                        else "inactive"
                    ),
                )

            return commit

        client.on_media_update = prepare
