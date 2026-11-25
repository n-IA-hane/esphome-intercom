"""RFC 3891 successor admission for an already answered browser destination."""

import asyncio

from .call_projection import publish_phone_projection
from .core.sdp import build_answer_directional, browser_video_send_supported, constrained_video_direction
from .core.video_rtp import RtpSenderState
from .const import CONF_VIDEO_CAMERA_SEND
from .endpoint_session import LegKind, SessionHandoff, SipTerminationDisposition, TerminationIntent
from .media_ports import allocate_sip_rtp_port, reserve_sip_video_media
from .media_reservation import release_media_reservation
from .sip_listener import SipInviteResult
from .session_cleanup import async_wait_for_cleanup


async def prepare_browser_replacement(hass, runtime, invite, local_ip, old, grant):
    calls = runtime.sip
    endpoint_id = str(old.metadata.get("dest_endpoint_id") or old.metadata.get("endpoint_id") or "")
    endpoint = runtime.endpoints.get(endpoint_id)
    media = calls.resource_for(old.call_id, "softphone_media")
    if (endpoint is None or not endpoint.supports_ha_client or media is None
            or media.get("endpoint_id") != endpoint_id):
        return None
    if grant.pending_call_id:
        return SipInviteResult(491, "Request Pending")
    if old.state not in {"in_call", "held"} or endpoint_id not in old.endpoint_claims:
        grant.release()
        return SipInviteResult(488, "Not Acceptable Here")
    new = None
    committed = False
    descriptor = None

    async def rollback():
        if committed:
            return
        release_media_reservation(descriptor)
        grant.pending_call_id = ""
        grant.release()
        if new is not None:
            barrier = calls.request_termination(new.call_id, TerminationIntent("replacement_failed",
                sip_disposition=SipTerminationDisposition.NONE), generation=new.generation)
            if barrier is not None:
                await async_wait_for_cleanup(barrier)

    try:
        video_port, video_direction = 0, "inactive"
        descriptor = {"invite": invite, "endpoint_id": endpoint_id,
            "media_client_id": str(media.get("media_client_id") or ""),
            "camera_send_authorized": bool(media.get("camera_send_authorized"))
                and bool(runtime.transport_config.get(CONF_VIDEO_CAMERA_SEND, False)),
            "media_handoff": {"previous_call_id": old.call_id, "endpoint_id": endpoint_id}}
        if invite.video_format is not None and endpoint.supports("video"):
            reservation, rtp_socket, rtcp_socket = reserve_sip_video_media(hass)
            local_port, video_port = reservation.ports
            descriptor.update(rtp_reservation=reservation, video_rtp_socket=rtp_socket,
                              video_rtcp_socket=rtcp_socket)
            video_direction = constrained_video_direction(invite.video_format.direction,
                allow_send=descriptor["camera_send_authorized"]
                    and browser_video_send_supported(invite.send_video_format or invite.video_format)
                    and not invite.remote_video_connection_held)
            descriptor["video_rtp_source"] = RtpSenderState.create(
                clock_rate=int(invite.video_format.clock_rate),
                now=asyncio.get_running_loop().time())
        else:
            local_port = allocate_sip_rtp_port(hass)
        descriptor.update(local_rtp_port=local_port, local_video_rtp_port=video_port,
                          video_direction=video_direction)
        answer = build_answer_directional(local_ip, local_ip, local_port, invite.send_format,
            invite.recv_format, recv_formats=invite.recv_formats, send_formats=invite.send_formats, remote_sdp=invite.remote_sdp, dtmf_direction=getattr(invite, "dtmf_direction", None),
            video_port=video_port,
            video_format=invite.answer_video_format if video_port else None,
            video_direction=video_direction)
        policies = tuple(sorted(set((*old.endpoint_claims, *grant.original.endpoint_claims,
            *(old.metadata.get("policy_endpoint_ids") or ()),
            *(grant.original.metadata.get("policy_endpoint_ids") or ())))))
        # Capture the exact successor before batched observers can fail.
        with calls.state_operation():
            new = calls.upsert(invite.call_id, state="connecting", owner="ha_softphone", caller=invite.caller,
                callee=old.callee, route_kind=old.route_kind, source_endpoint_id=grant.endpoint_id,
                dest_endpoint_id=endpoint_id, target_endpoint_id=endpoint_id, endpoint_id=endpoint_id,
                inbound_trunk_id=invite.trunk_id, policy_endpoint_ids=policies)
        grant.pending_call_id = new.call_id
    except BaseException:
        await rollback()
        raise

    def validate():
        if (not new.live or not old.live or not grant.original.live or not grant.current()
                or calls.get_session(new.call_id, generation=new.generation) is not new
                or calls.get_session(old.call_id, generation=old.generation) is not old
                or calls.resource_for(old.call_id, "softphone_media") is not media
                or runtime.endpoints.require(endpoint_id).active_call_id != old.call_id
                or endpoint_id not in old.endpoint_claims or new.answer_committed):
            raise RuntimeError("browser replacement owner changed")

    async def commit():
        nonlocal committed
        validate()
        with SessionHandoff(calls, old, new):
            calls.claim_endpoint(new.call_id, grant.endpoint_id, role="source")
            calls.transfer_endpoint_claim(old.call_id, new.call_id, endpoint_id)
            # The predecessor's terminal projection must not end the browser
            # leg now owned by this successor or erase its media descriptor.
            for leg in tuple(old.legs.values()):
                if leg.endpoint_id == endpoint_id and leg.kind is LegKind.BROWSER:
                    calls.remove_leg(old.call_id, leg.leg_id)
            if old.metadata.get("endpoint_id") == endpoint_id:
                old.metadata.pop("endpoint_id")
            calls.attach_media(new.call_id, descriptor)
            controller = str((old.metadata.get("controller_user_ids") or {}).get(endpoint_id)
                             or old.metadata.get("controller_user_id") or "")
            if controller:
                calls.bind_controller(new.call_id, user_id=controller, endpoint_id=endpoint_id)
            calls.observe_leg(new.call_id, f"browser:{endpoint_id}", role="ha_softphone",
                endpoint_id=endpoint_id, state="in_call", generation=new.generation)
            calls.remember_invite_identity(new.call_id, invite)
            if not new.claim_answer(new.token, lambda: True):
                raise RuntimeError("browser replacement answer changed")
            calls.transition(new.call_id, state="in_call", owner="ha_softphone",
                media_client_id=descriptor["media_client_id"], session_device_id=endpoint.device_id)
        committed = True
        grant.release()
        from .core.media.bridge import plan_browser_audio
        browser_audio = plan_browser_audio(invite.send_format, invite.recv_format)
        publish_phone_projection(hass, new, endpoint_id, peer_name=invite.caller, direction="incoming",
            event_type="transferred", last_sip_event="REPLACES",
            audio_direction=invite.local_audio_direction,
            audio_connection_held=invite.remote_audio_connection_held,
            selected_tx_format=browser_audio.microphone.wire_token(),
            selected_rx_format=browser_audio.speaker.wire_token(),
            selected_tx_rtp_format=invite.send_format.wire_token(),
            selected_rx_rtp_format=invite.recv_format.wire_token(),
            video_requested=invite.video_format is not None,
            video_negotiated=bool(video_port),
            video_active=bool(video_port and video_direction != "inactive"),
            video_format=invite.video_format.wire_token() if video_port else "",
            video_send_format=invite.send_video_format.wire_token() if video_port and invite.send_video_format else "",
            video_receive_format=invite.recv_video_format.wire_token() if video_port and invite.recv_video_format else "",
            video_direction=video_direction)
        def retire(_transaction=None):
            calls.request_termination(old.call_id, TerminationIntent("forwarded"), generation=old.generation)
        transactions = [getattr(leg.dialog, "_incoming_refer_task", None)
                        for leg in grant.original.legs.values() if leg.dialog is not None]
        transaction = next((task for task in transactions if task is not None and not task.done()), None)
        if transaction is not None:
            transaction.add_done_callback(retire)
        else:
            retire()

    return SipInviteResult(200, "OK", answer_sdp=answer, commit=commit, rollback=rollback,
                           media_contract=invite)
