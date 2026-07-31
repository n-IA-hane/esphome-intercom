"""SIP in-dialog media renegotiation for active PBX calls.

Every accepted offer follows the same two-phase rule: validate and reserve
without touching live media, send the SIP answer, then commit only while the
original call generation and media owner are still current.  Rollback owns
every staged resource until commit.  This keeps a concurrent BYE or hangup
authoritative and preserves the previously negotiated media contract when a
re-INVITE fails.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .assist_runtime import AssistMediaSession
from .const import DOMAIN, HA_SOFTPHONE_DEVICE_ID
from .endpoint_lifecycle import call_registry as _call_registry
from .media_offer_answer import (
    validate_bridged_video_reoffer,
    validate_direct_video_reoffer,
)
from .media_ports import (
    release_video_media_reservation as _release_video_media_reservation,
    reserve_sip_video_media,
)
from .media_session_updates import (
    commit_audio_session_update,
    commit_video_session_update,
)
from .phone_endpoint import DEFAULT_ENDPOINT_ID
from .sdp import (
    build_answer_directional,
    constrained_media_direction,
    constrained_video_direction,
    first_offered_dtmf_format,
)
from .sip_bridge import invite_rtp_peer, invite_video_rtp_peer
from .sip_listener import SipInvite, SipInviteResult
from .sip_video_relay import remote_can_receive, remote_can_send
from .websocket_api import _fire_call_event, _ha_softphone_store


_LOGGER = logging.getLogger(__name__)


async def async_prepare_media_update(
    hass: HomeAssistant,
    local_ip: str,
    previous: SipInvite,
    updated: SipInvite,
    method: str,
) -> SipInviteResult:
    """Validate and stage one in-dialog offer without mutating live media."""

    registry = _call_registry(hass)
    call_id = updated.call_id
    if call_id != previous.call_id:
        return SipInviteResult(481, "Call/Transaction Does Not Exist")

    preanswered = registry.preanswered.get(call_id)
    if isinstance(preanswered, dict):
        # The trunk dialog is already established while DTMF and the
        # bounded automation decision select its destination.  It still
        # owns real RTP/video reservations even though no browser or relay
        # has won yet, so in-dialog offers must update this pending media
        # contract instead of being rejected as ownerless.
        session = registry.sessions.get(registry.resolve_session_id(call_id))
        if session is None:
            return SipInviteResult(481, "Call/Transaction Does Not Exist")
        call_generation = session.generation
        previous_video = previous.video_format
        updated_video = updated.video_format
        video_offer = validate_direct_video_reoffer(
            previous_video,
            previous.recv_video_format,
            updated_video,
            updated.recv_video_format,
        )
        if not video_offer.accepted:
            return SipInviteResult(488, "Not Acceptable Here")
        local_rtp_port = int(preanswered.get("local_rtp_port") or 0)
        if not local_rtp_port:
            return SipInviteResult(488, "Not Acceptable Here")
        local_video_rtp_port = int(
            preanswered.get("local_video_rtp_port") or 0
        )
        staged_video_reservation = None
        staged_video_rtp_socket = None
        staged_video_rtcp_socket = None
        staged_video_committed = False
        if updated_video is not None and not local_video_rtp_port:
            try:
                (
                    staged_video_reservation,
                    staged_video_rtp_socket,
                    staged_video_rtcp_socket,
                ) = reserve_sip_video_media(hass)
                local_video_rtp_port = int(staged_video_reservation.ports[1])
            except (OSError, RuntimeError) as err:
                _LOGGER.warning(
                    "SIP pre-answer video re-INVITE could not allocate RTP "
                    "call_id=%s: %s",
                    call_id,
                    err,
                )
                return SipInviteResult(488, "Not Acceptable Here")
        video_direction = (
            constrained_video_direction(
                updated_video.direction,
                allow_send=not updated.remote_video_connection_held,
            )
            if updated_video is not None and local_video_rtp_port
            else "inactive"
        )
        answer = build_answer_directional(
            local_ip,
            local_ip,
            local_rtp_port,
            updated.send_format,
            updated.recv_format,
            dtmf=first_offered_dtmf_format(updated.remote_sdp),
            remote_sdp=updated.remote_sdp,
            video_port=local_video_rtp_port,
            video_format=updated.answer_video_format,
            video_direction=video_direction,
        )

        def _release_staged_preanswer_video() -> None:
            nonlocal staged_video_reservation
            if staged_video_reservation is None or staged_video_committed:
                return
            for sock in (
                staged_video_rtp_socket,
                staged_video_rtcp_socket,
            ):
                if sock is not None:
                    sock.close()
            staged_video_reservation.release()
            staged_video_reservation = None

        async def _commit_preanswered_update() -> None:
            nonlocal staged_video_committed
            if not registry.is_generation_current(call_id, call_generation):
                raise RuntimeError(
                    "SIP pre-answer media update belongs to a terminated call"
                )
            current = registry.preanswered.get(call_id)
            if current is not preanswered:
                raise RuntimeError("SIP pre-answer media owner changed")
            if staged_video_reservation is not None:
                current["video_rtp_reservation"] = staged_video_reservation
                current["video_rtp_socket"] = staged_video_rtp_socket
                current["video_rtcp_socket"] = staged_video_rtcp_socket
                current["local_video_rtp_port"] = local_video_rtp_port
                staged_video_committed = True
            if updated_video is None:
                _release_video_media_reservation(current)
                current["local_video_rtp_port"] = 0
            current["video_direction"] = video_direction
            registry.pending_invites[call_id] = updated

        async def _rollback_preanswered_update() -> None:
            _release_staged_preanswer_video()

        return SipInviteResult(
            200,
            "OK",
            answer_sdp=answer,
            commit=_commit_preanswered_update,
            rollback=_rollback_preanswered_update,
        )

    media = registry.softphone_media.get(call_id)
    if isinstance(media, dict) and media.get("invite") is not None:
        session = registry.sessions.get(registry.resolve_session_id(call_id))
        if session is None:
            return SipInviteResult(481, "Call/Transaction Does Not Exist")
        call_generation = session.generation
        media_endpoint_id = str(
            media.get("endpoint_id")
            or (
                (session.metadata if session is not None else {}).get(
                    "endpoint_id"
                )
            )
            or DEFAULT_ENDPOINT_ID
        ).strip()
        media_endpoint = (
            hass.data.get(DOMAIN, {})
            .get("endpoint_registry")
            .get(media_endpoint_id)
            if hass.data.get(DOMAIN, {}).get("endpoint_registry") is not None
            else None
        )
        media_device_id = str(
            getattr(media_endpoint, "device_id", "") or HA_SOFTPHONE_DEVICE_ID
        )
        local_rtp_port = int(media.get("local_rtp_port") or 0)
        if not local_rtp_port:
            return SipInviteResult(488, "Not Acceptable Here")
        audio_session = (
            hass.data.setdefault(DOMAIN, {})
            .setdefault("active_audio_sessions", {})
            .get(call_id)
        )
        previous_video = previous.video_format
        updated_video = updated.video_format
        video_session = (
            hass.data.setdefault(DOMAIN, {})
            .setdefault("active_video_sessions", {})
            .get(call_id)
        )
        new_video_reservation = None
        new_video_rtp_socket = None
        new_video_rtcp_socket = None
        new_video_media_committed = False
        video_offer = validate_direct_video_reoffer(
            previous_video,
            previous.recv_video_format,
            updated_video,
            updated.recv_video_format,
        )
        if not video_offer.accepted:
            # A direction change can activate a media path which had no
            # live codec contract in the previous offer.  A common SIP
            # camera flow starts with ``recvonly`` and later sends a
            # sendrecv re-INVITE when the user enables their camera.  Do
            # not compare the previously *inactive* receive candidate
            # with the newly active receive format: RFC 3264 permits that
            # path to be negotiated by the new offer.
            previous_remote_direction = (
                str(previous_video.direction) if previous_video else "none"
            )
            updated_remote_direction = (
                str(updated_video.direction) if updated_video else "none"
            )
            _LOGGER.info(
                "SIP video re-INVITE rejected call_id=%s reason=%s "
                "old_direction=%s new_direction=%s old_tx=%s new_tx=%s "
                "old_rx=%s new_rx=%s",
                call_id,
                video_offer.reason,
                previous_remote_direction,
                updated_remote_direction,
                previous_video.wire_token() if previous_video else "none",
                updated_video.wire_token() if updated_video else "none",
                previous.recv_video_format.wire_token()
                if previous.recv_video_format is not None
                else "none",
                updated.recv_video_format.wire_token()
                if updated.recv_video_format is not None
                else "none",
            )
            return SipInviteResult(488, "Not Acceptable Here")
        local_video_rtp_port = int(media.get("local_video_rtp_port") or 0)
        if (
            previous_video is None
            and updated_video is not None
            and not local_video_rtp_port
        ):
            try:
                (
                    new_video_reservation,
                    new_video_rtp_socket,
                    new_video_rtcp_socket,
                ) = reserve_sip_video_media(hass)
                local_video_rtp_port = int(new_video_reservation.ports[1])
            except (OSError, RuntimeError) as err:
                _LOGGER.warning(
                    "SIP video re-INVITE could not allocate RTP call_id=%s: %s",
                    call_id,
                    err,
                )
                return SipInviteResult(488, "Not Acceptable Here")
        # Per-call camera consent is immutable across hold/resume.  The
        # current negotiated direction may temporarily be recvonly and
        # must not erase the user's original authorization to send when
        # the peer resumes with sendrecv/recvonly.
        allow_video_send = bool(media.get("camera_send_authorized", False))
        video_direction = (
            constrained_video_direction(
                updated_video.direction,
                allow_send=(
                    allow_video_send and not updated.remote_video_connection_held
                ),
            )
            if updated_video is not None and local_video_rtp_port
            else "inactive"
        )

        def _release_staged_video() -> None:
            nonlocal new_video_reservation
            if new_video_reservation is None or new_video_media_committed:
                return
            for sock in (new_video_rtp_socket, new_video_rtcp_socket):
                if sock is not None:
                    sock.close()
            new_video_reservation.release()
            new_video_reservation = None

        try:
            answer = build_answer_directional(
                local_ip,
                local_ip,
                local_rtp_port,
                updated.send_format,
                updated.recv_format,
                dtmf=first_offered_dtmf_format(updated.remote_sdp),
                remote_sdp=updated.remote_sdp,
                video_port=local_video_rtp_port,
                video_format=updated.answer_video_format,
                video_direction=video_direction,
            )
        except Exception:
            _release_staged_video()
            raise

        async def _commit_softphone_update() -> None:
            nonlocal new_video_media_committed
            if not registry.is_generation_current(call_id, call_generation):
                raise RuntimeError(
                    "SIP softphone media update belongs to a terminated call"
                )
            if new_video_reservation is not None:
                media["local_video_rtp_port"] = local_video_rtp_port
                media["video_rtp_reservation"] = new_video_reservation
                media["video_rtp_socket"] = new_video_rtp_socket
                media["video_rtcp_socket"] = new_video_rtcp_socket
                new_video_media_committed = True
            media["invite"] = updated
            media["video_direction"] = video_direction
            if audio_session is not None:
                dtmf_format = first_offered_dtmf_format(updated.remote_sdp)
                commit_audio_session_update(
                    audio_session,
                    updated,
                    dtmf_payload_type=(
                        dtmf_format.payload_type if dtmf_format is not None else None
                    ),
                    dtmf_events=(
                        dtmf_format.events if dtmf_format is not None else frozenset()
                    ),
                )
            if video_session is not None and updated_video is not None:
                registry.video_parameter_sets.pop(call_id, None)
                commit_video_session_update(
                    video_session,
                    updated,
                    local_direction=video_direction,
                )
            elif video_session is not None:
                # RFC 3264 section 8.2: a port-zero re-offer removes the
                # stream.  Wake the media owner so RTP/RTCP and the video
                # WebSocket are closed without ending the audio dialog.
                video_session.removed = True
                video_session.media_generation += 1
                video_session.update_event.set()
            if updated_video is None:
                for key in ("video_rtp_socket", "video_rtcp_socket"):
                    sock = media.pop(key, None)
                    if sock is not None and video_session is None:
                        sock.close()
                reservation = media.pop("video_rtp_reservation", None)
                if reservation is not None:
                    reservation.release()
                media["local_video_rtp_port"] = 0
            media["video_failure_reason"] = ""
            store = _ha_softphone_store(hass, media_endpoint_id)
            if str(store.get("call_id") or "") == call_id:
                store.update(
                    {
                        "audio_direction": updated.local_audio_direction,
                        "audio_connection_held": updated.remote_audio_connection_held,
                        "video_active": bool(
                            updated_video is not None
                            and video_direction != "inactive"
                        ),
                        "video_requested": bool(updated_video is not None),
                        "video_negotiated": bool(updated_video is not None),
                        "video_status": (
                            "active"
                            if updated_video is not None
                            and video_direction != "inactive"
                            else "inactive"
                        ),
                        "video_failure_reason": "",
                        "video_format": (
                            updated_video.wire_token()
                            if updated_video is not None
                            else ""
                        ),
                        "video_send_format": (
                            updated.send_video_format.wire_token()
                            if updated.send_video_format is not None
                            else ""
                        ),
                        "video_receive_format": (
                            updated.recv_video_format.wire_token()
                            if updated.recv_video_format is not None
                            else ""
                        ),
                        "video_direction": video_direction,
                        "video_connection_held": updated.remote_video_connection_held,
                        "last_sip_event": method,
                        "media_renegotiations": int(
                            store.get("media_renegotiations") or 0
                        )
                        + 1,
                    }
                )
                _fire_call_event(
                    hass,
                    dict(
                        store,
                        endpoint_id=media_endpoint_id,
                        device_id=media_device_id,
                    ),
                    "session",
                )

        async def _rollback_softphone_update() -> None:
            _release_staged_video()

        return SipInviteResult(
            200,
            "OK",
            answer_sdp=answer,
            commit=_commit_softphone_update,
            rollback=_rollback_softphone_update,
        )

    relay = registry.relays.get(call_id)
    if relay is None:
        _LOGGER.warning(
            "SIP media update rejected without media owner call_id=%s "
            "softphone=%s relay=%s",
            call_id,
            call_id in registry.softphone_media,
            call_id in registry.relays,
        )
        return SipInviteResult(488, "Not Acceptable Here")
    session = registry.sessions.get(registry.resolve_session_id(call_id))
    if session is None:
        return SipInviteResult(481, "Call/Transaction Does Not Exist")
    call_generation = session.generation
    if isinstance(relay, AssistMediaSession):
        # Assist terminates the media locally; it is deliberately stored under
        # the registry's generic media-owner slot for common teardown, but it
        # is not a two-leg RTP relay.  Answer every offered media section while
        # rejecting video with port zero, and atomically update its audio leg.
        try:
            commit_assist = relay.prepare_media_update(updated)
            answer = build_answer_directional(
                local_ip,
                local_ip,
                int(relay.local_rtp_port),
                updated.send_format,
                updated.recv_format,
                dtmf=first_offered_dtmf_format(updated.remote_sdp),
                remote_sdp=updated.remote_sdp,
                audio_direction=updated.local_audio_direction,
                video_port=0,
                video_format=updated.answer_video_format,
                video_direction="inactive",
            )
        except (RuntimeError, TypeError, ValueError) as err:
            _LOGGER.warning(
                "SIP Assist media update rejected call_id=%s reason=%s",
                call_id,
                err,
            )
            return SipInviteResult(488, "Not Acceptable Here")

        async def _commit_assist_update() -> None:
            if not registry.is_generation_current(call_id, call_generation):
                raise RuntimeError(
                    "SIP Assist media update belongs to a terminated call"
                )
            if registry.relays.get(call_id) is not relay:
                raise RuntimeError("SIP Assist media owner changed before commit")
            commit_assist()
            _LOGGER.info(
                "SIP Assist %s committed call_id=%s remote_rtp=%s:%s "
                "audio_direction=%s video=declined",
                method,
                call_id,
                updated.remote_rtp_host,
                updated.remote_rtp_port,
                updated.remote_audio_direction,
            )

        return SipInviteResult(
            200,
            "OK",
            answer_sdp=answer,
            commit=_commit_assist_update,
        )

    right_peer = relay.right
    audio_direction = constrained_media_direction(
        updated.remote_audio_direction,
        allow_send=(
            bool(right_peer.can_send) and not updated.remote_audio_connection_held
        ),
        allow_receive=bool(right_peer.can_receive),
    )
    video_relay = getattr(relay, "video_relay", None)
    video_direction = "inactive"
    local_video_port = 0
    if updated.video_format is not None:
        if video_relay is None:
            _LOGGER.warning(
                "SIP video media update rejected without video relay call_id=%s "
                "old_direction=%s new_direction=%s",
                call_id,
                previous.video_format.direction
                if previous.video_format is not None
                else "none",
                updated.video_format.direction,
            )
            return SipInviteResult(488, "Not Acceptable Here")
        video_offer = validate_bridged_video_reoffer(
            previous.video_format,
            updated.video_format,
            updated.recv_video_format,
            peer_send=video_relay.right.send_format,
            peer_recv=video_relay.right.recv_format,
            peer_direction=video_relay.right.video_format,
            peer_held=video_relay.right.connection_held,
            updated_held=updated.remote_video_connection_held,
        )
        if not video_offer.accepted:
            _LOGGER.warning(
                "SIP video relay update rejected call_id=%s old_direction=%s "
                "new_direction=%s reason=%s",
                call_id,
                previous.video_format.direction
                if previous.video_format is not None
                else "none",
                updated.video_format.direction,
                video_offer.reason,
            )
            return SipInviteResult(488, "Not Acceptable Here")
        video_direction = constrained_video_direction(
            updated.video_format.direction,
            allow_send=(
                remote_can_send(video_relay.right.video_format)
                and not updated.remote_video_connection_held
            ),
            allow_receive=remote_can_receive(
                video_relay.right.video_format,
                connection_held=video_relay.right.connection_held,
            ),
        )
        local_video_port = int(video_relay.left_port)
    answer = build_answer_directional(
        local_ip,
        local_ip,
        int(relay.left_port),
        updated.send_format,
        updated.recv_format,
        dtmf=first_offered_dtmf_format(updated.remote_sdp),
        remote_sdp=updated.remote_sdp,
        audio_direction=audio_direction,
        video_port=local_video_port,
        video_format=updated.answer_video_format,
        video_direction=video_direction,
    )
    try:
        previous_audio_peer = relay.left
        commit_audio = relay.prepare_peer_reconfiguration(
            "left", invite_rtp_peer(updated)
        )
        previous_video_peer = video_relay.left if video_relay is not None else None
        commit_video = (
            video_relay.prepare_peer_reconfiguration(
                "left", invite_video_rtp_peer(updated)
            )
            if video_relay is not None and updated.video_format is not None
            else None
        )
    except (TypeError, ValueError):
        return SipInviteResult(488, "Not Acceptable Here")

    async def _commit_relay_update() -> None:
        if not registry.is_generation_current(call_id, call_generation):
            raise RuntimeError(
                "SIP relay media update belongs to a terminated call"
            )
        if relay.left is not previous_audio_peer or (
            video_relay is not None
            and previous_video_peer is not None
            and video_relay.left is not previous_video_peer
        ):
            raise RuntimeError("SIP relay media owner changed before commit")
        commit_audio()
        if commit_video is not None:
            commit_video()

    return SipInviteResult(
        200, "OK", answer_sdp=answer, commit=_commit_relay_update
    )
