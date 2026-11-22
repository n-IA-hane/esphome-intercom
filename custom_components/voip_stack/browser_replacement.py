"""RFC 3891 successor admission for an already answered browser destination."""


from .call_projection import publish_phone_projection
from .core.sdp import build_answer_directional
from .endpoint_session import SessionHandoff, SipTerminationDisposition, TerminationIntent
from .media_ports import allocate_sip_rtp_port
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
    if (old.state not in {"in_call", "held"} or endpoint_id not in old.endpoint_claims
            or invite.video_format is not None or media.get("local_video_rtp_port")):
        grant.release()
        return SipInviteResult(488, "Not Acceptable Here")
    new = None
    committed = False

    async def rollback():
        if committed:
            return
        grant.pending_call_id = ""
        grant.release()
        if new is not None:
            barrier = calls.request_termination(new.call_id, TerminationIntent("replacement_failed",
                sip_disposition=SipTerminationDisposition.NONE), generation=new.generation)
            if barrier is not None:
                await async_wait_for_cleanup(barrier)

    try:
        local_port = allocate_sip_rtp_port(hass)
        answer = build_answer_directional(local_ip, local_ip, local_port, invite.send_format,
            invite.recv_format, recv_formats=invite.recv_formats, remote_sdp=invite.remote_sdp,
            dtmf=invite.recv_dtmf_format)
        policies = tuple(sorted(set((*old.endpoint_claims, *grant.original.endpoint_claims,
            *(old.metadata.get("policy_endpoint_ids") or ()),
            *(grant.original.metadata.get("policy_endpoint_ids") or ())))))
        # Capture the exact successor before batched observers can fail. The
        # audio-only allocator above chooses a port, not a pool reservation.
        with calls.state_operation():
            new = calls.upsert(invite.call_id, state="connecting", owner="ha_softphone", caller=invite.caller,
                callee=old.callee, route_kind=old.route_kind, source_endpoint_id=grant.endpoint_id,
                dest_endpoint_id=endpoint_id, target_endpoint_id=endpoint_id, endpoint_id=endpoint_id,
                inbound_trunk_id=invite.trunk_id, policy_endpoint_ids=policies)
        grant.pending_call_id = new.call_id
        descriptor = {"invite": invite, "local_rtp_port": local_port, "local_video_rtp_port": 0,
            "video_direction": "inactive", "endpoint_id": endpoint_id,
            "media_client_id": str(media.get("media_client_id") or "")}
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
            calls.attach_media(new.call_id, descriptor)
            calls.observe_leg(new.call_id, f"browser:{endpoint_id}", role="ha_softphone",
                endpoint_id=endpoint_id, state="in_call", generation=new.generation)
            calls.remember_invite_identity(new.call_id, invite)
            if not new.claim_answer(new.token, lambda: True):
                raise RuntimeError("browser replacement answer changed")
            calls.transition(new.call_id, state="in_call", owner="ha_softphone",
                media_client_id=descriptor["media_client_id"], session_device_id=endpoint.device_id)
        committed = True
        grant.release()
        calls.request_termination(old.call_id, TerminationIntent("forwarded"), generation=old.generation)
        publish_phone_projection(hass, new, endpoint_id, peer_name=invite.caller, direction="incoming",
                                 event_type="transferred", last_sip_event="REPLACES")

    return SipInviteResult(200, "OK", answer_sdp=answer, commit=commit, rollback=rollback)
