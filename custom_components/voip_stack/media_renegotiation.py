"""SIP in-dialog media renegotiation for active PBX calls.

Every accepted offer follows the same two-phase rule: validate and reserve
without touching live media, send the SIP answer, then commit only while the
original call generation and media owner are still current.  Rollback owns
every staged resource until commit.  This keeps a concurrent BYE or hangup
authoritative and preserves the previously negotiated media contract when a
re-INVITE fails.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.core import HomeAssistant

from .assist_runtime import AssistMediaSession
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
from .runtime_data import endpoint_directory, require_runtime_data
from .core.sdp import (
    build_answer_directional,
    constrained_media_direction,
    constrained_video_direction,
    first_offered_dtmf_format,
    remote_can_receive,
    remote_can_send,
)
from .sip_bridge import (
    invite_rtp_peer,
    invite_video_rtp_peer,
)
from .sip_listener import SipInvite, SipInviteResult
from .session_cleanup import async_wait_for_cleanup
from .websocket_api import _fire_call_event, _ha_softphone_store


_LOGGER = logging.getLogger(__name__)


async def _prepare_bridge_video_contract_change(
    hass: HomeAssistant,
    local_ip: str,
    previous: SipInvite,
    updated: SipInvite,
    relay,
) -> SipInviteResult:
    """Stage one video add, remove or direction change on both dialogs."""

    from .config import transport_config
    from .const import CONF_SIP_VIDEO, CONF_VIDEO_TRANSCODING
    from .media_ports import (
        release_sip_rtp_port_pair,
        reserve_sip_video_relay_media,
    )
    from .sip_bridge import (
        build_pending_invite_video_relay,
        configure_answered_invite_video_relay,
        dialog_rtp_peer,
        dialog_video_rtp_peer,
        invite_rtp_peer,
        video_bridge_offer_formats,
    )

    registry = _call_registry(hass)
    source_call_id, dest_call_id = registry.bridge_for(updated.call_id)
    session = registry.sessions.get(registry.resolve_session_id(updated.call_id))
    client = registry.sip_client_for(dest_call_id)
    cfg = transport_config(hass)
    current_video_relay = getattr(relay, "video_relay", None)
    adding_video = current_video_relay is None and updated.video_format is not None
    removing_video = current_video_relay is not None and updated.video_format is None
    retaining_video = current_video_relay is not None and updated.video_format is not None
    if (
        source_call_id != updated.call_id
        or session is None
        or client is None
        or client.dialog is None
        or (
            adding_video
            and not bool(cfg.get(CONF_SIP_VIDEO, False))
        )
    ):
        return SipInviteResult(488, "Not Acceptable Here")
    call_generation = session.generation
    destination_dialog = client.dialog
    enable_transcoding = bool(cfg.get(CONF_VIDEO_TRANSCODING, False))
    staged_video_relay = None
    if adding_video:
        if current_video_relay is not None:
            return SipInviteResult(488, "Not Acceptable Here")
        try:
            reservation, sockets = reserve_sip_video_relay_media(hass)
            staged_video_relay = build_pending_invite_video_relay(
                updated,
                remote_host=destination_dialog.remote_host,
                left_port=reservation.ports[0],
                right_port=reservation.ports[1],
                sockets=sockets,
                on_release=lambda ports: release_sip_rtp_port_pair(hass, ports),
            )
            reservation.detach()
        except (OSError, RuntimeError, ValueError) as err:
            _LOGGER.warning(
                "SIP bridge video re-INVITE could not reserve media call_id=%s: %s",
                updated.call_id,
                err,
            )
            return SipInviteResult(488, "Not Acceptable Here")
        offered_video_formats = video_bridge_offer_formats(
            updated.video_format,
            enable_transcoding=enable_transcoding,
        )
        destination_video_port = int(staged_video_relay.right_port)
        destination_video_direction = updated.video_format.direction
    elif removing_video or retaining_video:
        if current_video_relay is None:
            return SipInviteResult(488, "Not Acceptable Here")
        offered_video_formats = tuple(getattr(client, "video_formats", ()))
        if not offered_video_formats:
            offered_video_formats = tuple(
                item
                for item in (
                    destination_dialog.recv_video_format,
                    destination_dialog.video_format,
                )
                if item is not None
            )
        if not offered_video_formats:
            return SipInviteResult(488, "Not Acceptable Here")
        destination_video_port = (
            int(current_video_relay.right_port) if retaining_video else 0
        )
        destination_video_direction = (
            (
                "inactive"
                if updated.remote_video_connection_held
                else updated.video_format.direction
            )
            if retaining_video
            else "inactive"
        )
    else:
        return SipInviteResult(488, "Not Acceptable Here")

    candidate = None
    result_transferred = False
    try:
        candidate = await client.async_prepare_video_reinvite(
            local_video_rtp_port=destination_video_port,
            video_formats=offered_video_formats,
            video_direction=destination_video_direction,
        )
        if candidate is None:
            return SipInviteResult(488, "Not Acceptable Here")
        answer_video_format = None
        answer_video_direction = "inactive"
        if adding_video:
            video_answer = configure_answered_invite_video_relay(
                updated,
                candidate,
                staged_video_relay,
                hass=hass,
                enable_transcoding=enable_transcoding,
            )
            if video_answer is None:
                return SipInviteResult(488, "Not Acceptable Here")
            answer_video_format = video_answer.video_format
            answer_video_direction = video_answer.direction
            await staged_video_relay.start()
        elif retaining_video:
            if candidate.video_format is None:
                return SipInviteResult(488, "Not Acceptable Here")
            candidate_video_peer = dialog_video_rtp_peer(candidate)
            video_offer = validate_bridged_video_reoffer(
                previous.video_format,
                updated.video_format,
                updated.recv_video_format,
                peer_send=candidate_video_peer.send_format,
                peer_recv=candidate_video_peer.recv_format,
                peer_direction=candidate_video_peer.video_format,
                peer_held=candidate_video_peer.connection_held,
                updated_held=updated.remote_video_connection_held,
                caller_to_peer_transcoding=current_video_relay.transcodes_from(
                    "left"
                ),
                peer_to_caller_transcoding=current_video_relay.transcodes_from(
                    "right"
                ),
            )
            if not video_offer.accepted:
                return SipInviteResult(488, "Not Acceptable Here")
            answer_video_format = updated.answer_video_format
            answer_video_direction = constrained_video_direction(
                updated.video_format.direction,
                allow_send=(
                    remote_can_send(candidate.video_format)
                    and not updated.remote_video_connection_held
                ),
                allow_receive=remote_can_receive(
                    candidate.video_format,
                    connection_held=candidate.remote_video_connection_held,
                ),
            )
        next_left = invite_rtp_peer(updated, established=relay.left)
        next_right = dialog_rtp_peer(candidate)
        previous_left = relay.left
        previous_right = relay.right
        commit_left = relay.prepare_peer_reconfiguration("left", next_left)
        commit_right = relay.prepare_peer_reconfiguration("right", next_right)
        answer = build_answer_directional(
            local_ip,
            local_ip,
            int(relay.left_port),
            next_left.outbound_rtp_format,
            next_left.inbound_rtp_format,
            dtmf=first_offered_dtmf_format(updated.remote_sdp),
            remote_sdp=updated.remote_sdp,
            audio_direction=constrained_media_direction(
                updated.remote_audio_direction,
                allow_send=(
                    next_right.can_send
                    and not updated.remote_audio_connection_held
                ),
                allow_receive=next_right.can_receive,
            ),
            video_port=(
                int(staged_video_relay.left_port)
                if adding_video
                else int(current_video_relay.left_port)
                if retaining_video
                else 0
            ),
            video_format=answer_video_format,
            video_direction=answer_video_direction,
        )
        committed = False
        previous_video_left = (
            current_video_relay.left if retaining_video else None
        )
        previous_video_right = (
            current_video_relay.right if retaining_video else None
        )
        commit_video_left = (
            current_video_relay.prepare_peer_reconfiguration(
                "left", invite_video_rtp_peer(updated)
            )
            if retaining_video
            else None
        )
        commit_video_right = (
            current_video_relay.prepare_peer_reconfiguration(
                "right", dialog_video_rtp_peer(candidate)
            )
            if retaining_video
            else None
        )

        async def commit() -> None:
            nonlocal committed
            if (
                not registry.is_generation_current(
                    updated.call_id, call_generation
                )
                or registry.resource_for(updated.call_id, "relay") is not relay
                or registry.sip_client_for(dest_call_id) is not client
                or relay.left is not previous_left
                or relay.right is not previous_right
                or relay.video_relay is not current_video_relay
                or (
                    retaining_video
                    and (
                        current_video_relay.left is not previous_video_left
                        or current_video_relay.right is not previous_video_right
                    )
                )
            ):
                raise RuntimeError("SIP bridge media owner changed before commit")
            if not client.commit_prepared_reinvite(
                destination_dialog, candidate
            ):
                raise RuntimeError("SIP destination re-INVITE owner changed")
            if adding_video:
                relay.attach_video_relay(staged_video_relay)
            elif removing_video:
                relay.video_relay = None
            commit_left()
            commit_right()
            if commit_video_left is not None:
                commit_video_left()
            if commit_video_right is not None:
                commit_video_right()
            committed = True
            if removing_video and current_video_relay is not None:
                await current_video_relay.stop()

        async def rollback() -> None:
            if committed:
                return
            client.abort_prepared_reinvite(destination_dialog, candidate)
            if staged_video_relay is not None:
                await staged_video_relay.stop()

        result = SipInviteResult(
            200,
            "OK",
            answer_sdp=answer,
            commit=commit,
            rollback=rollback,
        )
        result_transferred = True
        return result
    except (OSError, RuntimeError, TypeError, ValueError):
        return SipInviteResult(488, "Not Acceptable Here")
    finally:
        if not result_transferred:
            if candidate is not None:
                client.abort_prepared_reinvite(destination_dialog, candidate)
            if staged_video_relay is not None:
                cleanup = asyncio.create_task(
                    staged_video_relay.stop(),
                    name=f"voip-video-reinvite-rollback-{updated.call_id}",
                )
                await async_wait_for_cleanup(cleanup)


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

    preanswered = registry.resource_for(call_id, "preanswered")
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
            current = registry.resource_for(call_id, "preanswered")
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
            registry.set_pending_invite(call_id, updated)

        async def _rollback_preanswered_update() -> None:
            _release_staged_preanswer_video()

        return SipInviteResult(
            200,
            "OK",
            answer_sdp=answer,
            commit=_commit_preanswered_update,
            rollback=_rollback_preanswered_update,
        )

    media = registry.resource_for(call_id, "softphone_media")
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
        ).strip()
        if not media_endpoint_id:
            return SipInviteResult(481, "Call/Transaction Does Not Exist")
        media_endpoint = endpoint_directory(hass).get(media_endpoint_id)
        media_device_id = str(getattr(media_endpoint, "device_id", ""))
        local_rtp_port = int(media.get("local_rtp_port") or 0)
        if not local_rtp_port:
            return SipInviteResult(488, "Not Acceptable Here")
        browser_media = require_runtime_data(hass).media
        audio_session = browser_media.sessions_for("audio").get(call_id)
        previous_video = previous.video_format
        updated_video = updated.video_format
        video_session = browser_media.sessions_for("video").get(call_id)
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
                registry.clear_video_parameter_sets(call_id)
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

    relay = registry.resource_for(call_id, "relay")
    if relay is None:
        _LOGGER.warning(
            "SIP media update rejected without media owner call_id=%s "
            "softphone=%s relay=%s",
            call_id,
            registry.resource_for(call_id, "softphone_media") is not None,
            relay is not None,
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
            if registry.resource_for(call_id, "relay") is not relay:
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
    video_presence_changed = (previous.video_format is None) != (
        updated.video_format is None
    )
    video_direction_changed = bool(
        previous.video_format is not None
        and updated.video_format is not None
        and (
            previous.video_format.direction != updated.video_format.direction
            or previous.remote_video_connection_held
            != updated.remote_video_connection_held
        )
    )
    video_relay_missing = updated.video_format is not None and video_relay is None
    if video_presence_changed or video_direction_changed or video_relay_missing:
        return await _prepare_bridge_video_contract_change(
            hass,
            local_ip,
            previous,
            updated,
            relay,
        )
    if updated.video_format is not None:
        if video_relay is None:
            _LOGGER.warning(
                "SIP video update has no active relay call_id=%s",
                call_id,
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
            caller_to_peer_transcoding=video_relay.transcodes_from("left"),
            peer_to_caller_transcoding=video_relay.transcodes_from("right"),
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
