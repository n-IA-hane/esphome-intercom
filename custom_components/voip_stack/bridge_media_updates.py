"""Committed media updates for outbound SIP bridge dialogs."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .endpoint_lifecycle import call_registry
from .media_offer_answer import validate_bridged_video_reoffer
from .runtime_data import sip_endpoint_manager
from .core import sdp
from .sip_bridge import (
    dialog_rtp_peer,
    dialog_video_rtp_peer,
    invite_video_rtp_peer,
)
from .sip_client import PreparedDialogMediaUpdate

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
            commit_video = None
            source_reinvite = None
            answer_video_format = None
            source_video_formats = ()
            if updated_video is not None:
                next_video_peer = dialog_video_rtp_peer(updated)
                endpoint = sip_endpoint_manager(self.hass)
                if endpoint is None or video_relay is None:
                    return None
                source_video_formats = tuple(
                    dict.fromkeys(
                        (
                            video_relay.left.recv_format,
                            video_relay.left.send_format,
                        )
                    )
                )
                source_reinvite = await endpoint.async_prepare_video_reinvite(
                    source_call_id,
                    local_video_rtp_port=int(video_relay.left_port),
                    video_formats=source_video_formats,
                    video_direction=updated_video.direction,
                )
                if source_reinvite is None:
                    return None
                source_video_peer = invite_video_rtp_peer(
                    source_reinvite.candidate
                )
                decision = (
                    validate_bridged_video_reoffer(
                        previous_video,
                        updated_video,
                        updated.recv_video_format,
                        peer_send=source_video_peer.send_format,
                        peer_recv=source_video_peer.recv_format,
                        peer_direction=source_video_peer.video_format,
                        peer_held=source_video_peer.connection_held,
                        updated_held=updated.remote_video_connection_held,
                        caller_to_peer_transcoding=video_relay.transcodes_from(
                            "right"
                        ),
                        peer_to_caller_transcoding=video_relay.transcodes_from(
                            "left"
                        ),
                    )
                    if video_relay is not None
                    else None
                )
                if (
                    decision is None
                    or not decision.accepted
                    or updated.remote_video_rtp_port <= 0
                ):
                    restored = await source_reinvite.restore(
                        local_video_rtp_port=int(video_relay.left_port),
                        video_formats=source_video_formats,
                    )
                    _LOGGER.warning(
                        "SIP bridge outbound %s rejected video contract change "
                        "source_call_id=%s dest_call_id=%s reason=%s relay=%s "
                        "left_tx=%s left_rx=%s remote_tx=%s remote_rx=%s "
                        "source_tx=%s source_rx=%s source_direction=%s "
                        "remote_port=%s",
                        method,
                        source_call_id,
                        client.dialog_ids.call_id,
                        decision.reason if decision is not None else "missing_relay",
                        video_relay is not None,
                        (
                            video_relay.left.send_format.wire_token()
                            if video_relay is not None
                            else "none"
                        ),
                        (
                            video_relay.left.recv_format.wire_token()
                            if video_relay is not None
                            else "none"
                        ),
                        next_video_peer.send_format.wire_token(),
                        next_video_peer.recv_format.wire_token(),
                        source_video_peer.send_format.wire_token(),
                        source_video_peer.recv_format.wire_token(),
                        source_video_peer.video_format,
                        updated.remote_video_rtp_port,
                    )
                    if not restored:
                        raise RuntimeError(
                            "SIP bridge could not compensate rejected source offer"
                        )
                    return None
                answer_video_format = sdp.video_answer_contract(
                    updated_video,
                    source_video_peer.recv_format,
                )
                if answer_video_format is None:
                    if not await source_reinvite.restore(
                        local_video_rtp_port=int(video_relay.left_port),
                        video_formats=source_video_formats,
                    ):
                        raise RuntimeError(
                            "SIP bridge could not compensate invalid video answer"
                        )
                    return None
                commit_video = video_relay.prepare_peer_reconfiguration(
                    "right",
                    next_video_peer,
                )
                commit_source_video = video_relay.prepare_peer_reconfiguration(
                    "left",
                    source_video_peer,
                )
            else:
                commit_source_video = None

            # The source peer has already accepted and ACKed this offer on the
            # wire.  It cannot be rolled back by returning an error on the
            # other dialog.  Publish that accepted dialog generation now, then
            # commit both relay peers only after the origin 2xx is sent.
            if source_reinvite is not None and not source_reinvite.commit():
                return None

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
                if commit_source_video is not None:
                    commit_source_video()
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

            async def rollback() -> None:
                if source_reinvite is not None and not await source_reinvite.restore(
                    local_video_rtp_port=int(video_relay.left_port),
                    video_formats=source_video_formats,
                ):
                    raise RuntimeError(
                        "SIP bridge could not compensate unsent media answer"
                    )

            return PreparedDialogMediaUpdate(
                commit,
                rollback if source_reinvite is not None else None,
                answer_video_format=answer_video_format,
            )

        client.on_media_update = prepare
