"""Authorized RFC 3891 replacement of a session-owned inbound bridge leg."""

import asyncio
from dataclasses import dataclass, replace

from .core.sip_transfer import SipReplaces
from .endpoint_session import EndpointCallSession, SipTerminationDisposition, TerminationIntent, SessionHandoff
from .sip_listener import SipInviteResult
from .session_cleanup import async_wait_for_cleanup


class ReplacementRejected(Exception):
    def __init__(self, status: int):
        self.status = status


@dataclass(slots=True)
class ReplacementGrant:
    """One REFER authorizes its opposite phone for the matching consultation."""

    original: EndpointCallSession
    consultation: EndpointCallSession
    endpoint_id: str
    replaces: SipReplaces
    pending_call_id: str = ""

    @property
    def resource_name(self):
        return f"replacement_grant:{self.consultation.call_id}"

    def current(self):
        return any(item.name == self.resource_name and item.value is self
                   for item in self.consultation.resources)

    def release(self):
        if self.consultation.live:
            self.consultation.release_resource(self.resource_name, value=self)

    def attach(self):
        async def discard(_reason):
            pass
        self.consultation.add_resource(self.resource_name, self, discard)


def replacement_invite(runtime, replaces):
    from .call_transfer import _inbound_server

    server, _uri = _inbound_server(runtime, replaces.call_id)
    lookup = getattr(server, "invite_for_replaces", None)
    return lookup(replaces) if callable(lookup) else None


def prepare_replacement_grant(runtime, call_id, target, source_client):
    if target.replaces is None or replacement_invite(runtime, target.replaces) is None:
        return None
    calls = runtime.sip
    original = calls.get_session(calls.resolve_session_id(call_id))
    consultation = calls.get_session(target.replaces.call_id)
    if original is None or consultation is None or not original.live or not consultation.live:
        raise ReplacementRejected(481)
    if target.replaces.early_only:
        raise ReplacementRejected(486)
    from_callee = (source_client is not None and source_client.dialog_ids.call_id
                   == original.metadata.get("bridge_dest_call_id"))
    sender_key, receiver_key = (
        ("dest_endpoint_id", "source_endpoint_id") if from_callee
        else ("source_endpoint_id", "dest_endpoint_id")
    )
    sender = original.metadata.get(sender_key)
    receiver = original.metadata.get(receiver_key)
    if not sender or sender != consultation.metadata.get("source_endpoint_id"):
        raise ReplacementRejected(403)
    if calls.resource_for(consultation.call_id, "replacement_grant") is not None:
        raise ReplacementRejected(491)
    grant = ReplacementGrant(original, consultation, str(receiver or ""), target.replaces)
    grant.attach()
    return grant


async def prepare_replacement(hass, invite, local_ip):
    from .runtime_data import require_runtime_data
    from .call_transfer import _inbound_server

    runtime = require_runtime_data(hass)
    client = runtime.sip.sip_client_for(invite.replaces.call_id)
    # RFC 3891 permits replacing an early dialog without the optional flag.
    if invite.replaces.early_only or (client is not None and client.dialog is None):
        return await _prepare_early_pickup(hass, runtime, invite, local_ip)
    server, _uri = _inbound_server(runtime, invite.replaces.call_id)
    if server is None:
        return SipInviteResult(481, "Call/Transaction Does Not Exist")
    try:
        acquired = await server.acquire_replacement_dialog(invite.replaces)
    except TimeoutError:
        return SipInviteResult(491, "Request Pending")
    if acquired is None:
        return SipInviteResult(481, "Call/Transaction Does Not Exist")
    previous, release = acquired
    try:
        result = await _prepare_replacement_locked(hass, invite, local_ip, previous)
    except BaseException:
        release()
        raise
    if not 200 <= result.status < 300:
        release()
        return result

    async def commit():
        try:
            if result.commit is not None:
                await result.commit()
        finally:
            release()

    async def rollback():
        try:
            if result.rollback is not None:
                await result.rollback()
        finally:
            release()

    return replace(result, commit=commit, rollback=rollback)


async def _prepare_early_pickup(hass, runtime, invite, local_ip):
    """Match RFC 3891 tags in HA's dialog orientation before directed pickup."""
    from .call_pickup import prepare_sip_pickup

    if not invite.identified_source_endpoint_id:
        return SipInviteResult(403, "Forbidden")
    replaces = invite.replaces
    calls = runtime.sip
    session = calls.get_session(calls.resolve_session_id(replaces.call_id))
    client = calls.sip_client_for(replaces.call_id)
    if session is None:
        return SipInviteResult(481, "Call/Transaction Does Not Exist")
    if client is None:
        if replacement_invite(runtime, replaces) is not None:
            return SipInviteResult(486, "Busy Here")
        return SipInviteResult(481, "Call/Transaction Does Not Exist")
    if client.dialog is not None:
        matched = (replaces.to_tag == client.dialog_ids.local_tag and replaces.from_tag == client.dialog_ids.remote_tag)
        return SipInviteResult(486, "Busy Here") if matched else SipInviteResult(481, "Call/Transaction Does Not Exist")
    if (replaces.to_tag != client.dialog_ids.local_tag or replaces.from_tag not in client.early_dialogs):
        return SipInviteResult(481, "Call/Transaction Does Not Exist")
    leg = session.legs.get(replaces.call_id)
    if leg is None:
        return SipInviteResult(481, "Call/Transaction Does Not Exist")
    target, leg_id = leg.endpoint_id, leg.leg_id
    if (session.owner == "ha_softphone" and session.route_kind == "direct"
            and not session.metadata.get("bridge_dest_call_id")
            and replaces.call_id == session.call_id):
        # A direct browser call owns one SIP leg and a separate callee claim.
        target = str(session.metadata.get("dest_endpoint_id") or session.metadata.get("target_endpoint_id") or "")
        if (not target or target == session.metadata.get("source_endpoint_id")
                or target not in session.endpoint_claims):
            return SipInviteResult(481, "Call/Transaction Does Not Exist")
        leg_id = leg.leg_id if leg.endpoint_id == target else ""
    if not target:
        return SipInviteResult(481, "Call/Transaction Does Not Exist")
    return await prepare_sip_pickup(hass, invite, target, local_ip,
                                  call_id=session.call_id, generation=session.generation, leg_id=leg_id)


async def _prepare_replacement_locked(hass, invite, local_ip, previous):
    from .runtime_data import require_runtime_data
    from .media_renegotiation import async_prepare_media_update
    from .bridge_manager import async_watch_sip_bridge_destination
    from .bridge_media_updates import BridgeMediaUpdateBinder
    from .dtmf_events import attach_dtmf_event_bridge
    from .softphone_termination import async_terminate_sip_bridge_session
    from .call_projection import publish_bridge_projection
    from .endpoint_registry import EndpointBusyError

    runtime = require_runtime_data(hass)
    calls = runtime.sip
    if invite.replaces.early_only:
        return SipInviteResult(486, "Busy Here")
    if calls.resolve_session_id(invite.call_id) != invite.call_id or calls.get_session(invite.call_id) is not None:
        return SipInviteResult(482, "Loop Detected")
    old = calls.get_session(previous.call_id)
    grant = calls.resource_for(old.call_id, "replacement_grant") if old is not None else None
    if (grant is None and old is not None and old.live and invite.identified_source_endpoint_id
            and invite.identified_source_endpoint_id == old.metadata.get("source_endpoint_id")):
        # RFC 3891 also authorizes a new dialog from the same identified user.
        grant = ReplacementGrant(old, old, invite.identified_source_endpoint_id, invite.replaces)
        grant.attach()
    if (not isinstance(grant, ReplacementGrant) or not grant.original.live
            or not old.live or grant.replaces != invite.replaces
            or not grant.endpoint_id
            or grant.endpoint_id != invite.identified_source_endpoint_id):
        return SipInviteResult(403, "Forbidden")
    _source, destination_id = calls.bridge_for(old.call_id)
    client = calls.sip_client_for(destination_id)
    relay = calls.resource_for(old.call_id, "relay")
    if client is None or client.dialog is None or relay is None:
        grant.release()
        return SipInviteResult(488, "Not Acceptable Here")
    if grant.pending_call_id:
        return SipInviteResult(491, "Request Pending")
    try:
        ready = await client.async_wait_for_media_idle()
    except BaseException:
        grant.release()
        raise
    if (not old.live or not grant.original.live or not grant.current()
            or calls.sip_client_for(destination_id) is not client
            or calls.resource_for(old.call_id, "relay") is not relay):
        grant.release()
        return SipInviteResult(481, "Call/Transaction Does Not Exist")
    if not ready:
        grant.release()
        return SipInviteResult(491, "Request Pending")
    grant.pending_call_id = invite.call_id
    destination_dialog = client.dialog
    destination_key = (client.dialog_ids.call_id, client.dialog_ids.local_tag, client.dialog_ids.remote_tag)
    target_endpoint = str(old.metadata.get("dest_endpoint_id") or "")
    try:
        new = calls.upsert(
            invite.call_id, state="connecting", owner="bridge", caller=invite.caller,
            callee=old.callee, route_kind=old.route_kind,
            source_endpoint_id=grant.endpoint_id, dest_endpoint_id=target_endpoint,
            inbound_trunk_id=invite.trunk_id,
            outbound_trunk_id=old.metadata.get("outbound_trunk_id", ""),
            policy_endpoint_ids=tuple(sorted(set(
                (*old.endpoint_claims, *grant.original.endpoint_claims,
                 *(old.metadata.get("policy_endpoint_ids") or ()),
                 *(grant.original.metadata.get("policy_endpoint_ids") or ()))
            ))),
        )
    except BaseException:
        grant.release()
        raise
    prepared = None
    committed = False
    rollback_task = None

    async def cleanup():
        grant.release()
        try:
            if prepared is not None and prepared.rollback is not None:
                await prepared.rollback()
        finally:
            barrier = calls.request_termination(
                new.call_id,
                TerminationIntent("replacement_failed", sip_disposition=SipTerminationDisposition.NONE),
                generation=new.generation,
            )
            if barrier is not None:
                await async_wait_for_cleanup(barrier)

    async def rollback():
        nonlocal rollback_task
        if committed:
            return
        if rollback_task is None:
            rollback_task = asyncio.create_task(cleanup(), name=f"voip-replacement-rollback-{new.call_id}")
        await async_wait_for_cleanup(rollback_task)

    try:
        calls.claim_endpoint(new.call_id, grant.endpoint_id, role="source")
        prepared = await async_prepare_media_update(
            hass, local_ip, previous, replace(invite, call_id=old.call_id), "INVITE",
        )
        if not 200 <= prepared.status < 300:
            await rollback()
            return replace(prepared, commit=None, rollback=None)
    except EndpointBusyError:
        await rollback()
        return SipInviteResult(486, "Busy Here")
    except BaseException:
        await rollback()
        raise

    def validate(*, media_committed=False):
        if (rollback_task is not None or not new.live or not old.live or not grant.original.live
                or calls.get_session(old.call_id) is not old or calls.get_session(new.call_id) is not new
                or calls.sip_client_for(destination_id) is not client
                or (client.dialog_ids.call_id, client.dialog_ids.local_tag, client.dialog_ids.remote_tag) != destination_key
                or new.owner != "bridge" or new.answer_committed
                or not grant.current()
                or (prepared.dialog_identity is not None and (
                    prepared.dialog_identity.owner is not client
                    or not prepared.dialog_identity.current(committed=media_committed)))
                or (prepared.dialog_identity is None and client.dialog is not destination_dialog)
                or calls.resource_for(old.call_id, "relay") is not relay):
            raise RuntimeError("replacement dialog ownership changed")
        if target_endpoint and target_endpoint not in old.endpoint_claims:
            raise RuntimeError("replacement destination claim changed")

    async def commit():
        nonlocal committed
        validate()
        if prepared.commit is not None:
            await prepared.commit()
        validate(media_committed=True)
        ready = asyncio.Event()

        async def watch():
            await ready.wait()
            await async_watch_sip_bridge_destination(
                hass, client=client, source_call_id=new.call_id,
                terminate_sip_bridge=async_terminate_sip_bridge_session,
            )

        watcher = asyncio.create_task(watch(), name=f"voip-bridge-destination-{destination_id}")
        try:
            with SessionHandoff(calls, old, new) as handoff:
                handoff.preserve(client, "on_refer", "on_media_update", "on_info_dtmf")
                handoff.preserve(relay, "on_dtmf", "on_dtmf_fallback")
                calls.remove_leg(old.call_id, destination_id)
                old.release_resource(f"relay:{old.call_id}", value=relay)
                calls.forget_bridge_link(old.call_id)
                client.on_refer = None
                calls.register_bridge(
                    source_call_id=new.call_id, dest_call_id=destination_id,
                    client=client, lifecycle_task=watcher, state="in_call",
                    caller=invite.caller, callee=old.callee, route_kind=old.route_kind,
                    expected_generation=new.generation,
                )
                calls.attach_relay(new.call_id, relay)
                if target_endpoint:
                    calls.transfer_endpoint_claim(old.call_id, new.call_id, target_endpoint)
                BridgeMediaUpdateBinder(hass).attach(
                    client, relay, source_call_id=new.call_id,
                    source_video_send_format=invite.send_video_format,
                    source_video_receive_format=invite.recv_video_format,
                )
                attach_dtmf_event_bridge(hass, relay, call_id=new.call_id,
                                         dest_call_id=destination_id, caller=invite.caller,
                                         callee=old.callee, client=client)
                if not new.claim_answer(new.token, lambda: True):
                    raise RuntimeError("replacement answer ownership changed")
        except BaseException:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            raise
        retired = calls.cancel_task(old.call_id, f"client_watcher:{destination_id}")
        committed = True
        grant.release()
        ready.set()
        calls.request_termination(old.call_id, TerminationIntent("forwarded"))
        if prepared.finalize is not None:
            await async_wait_for_cleanup(asyncio.create_task(prepared.finalize(), name="voip-replacement-finalize"))
        publish_bridge_projection(hass, new, direction="incoming", last_sip_event="REPLACES")
        if retired is not None:
            await asyncio.gather(retired, return_exceptions=True)

    return SipInviteResult(200, "OK", answer_sdp=prepared.answer_sdp, commit=commit, rollback=rollback)
