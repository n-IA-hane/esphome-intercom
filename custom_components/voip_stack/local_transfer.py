"""Local audio transfer for a remaining peer which cannot execute REFER."""

import asyncio
import logging
import secrets

from .bridge_manager import async_watch_sip_bridge_destination
from .bridge_media_updates import BridgeMediaUpdateBinder
from .call_projection import publish_bridge_projection, publish_media_route
from .core.sip_transfer import SipTransferResult
from .dtmf_events import attach_dtmf_event_bridge
from .endpoint_session import CleanupStage, LegPhase, SessionHandoff, TerminationIntent
from .media_ports import RtpPortReservation
from .session_cleanup import async_wait_for_cleanup
from .sip_rtp_bridge import SipRtpRelay, audio_reconfiguration_guard
from .softphone_termination import async_terminate_sip_bridge_session

_LOGGER = logging.getLogger(__name__)


async def try_local_audio_transfer(hass, runtime, grant, referrer):
    calls = runtime.sip
    original, consultation = grant.original, grant.consultation
    if (original is consultation or not original.live or not consultation.live
            or not grant.current() or grant.pending_call_id):
        return SipTransferResult(False, 481, "call_not_found")
    _root, old_destination = calls.bridge_for(original.call_id)
    _root, new_destination = calls.bridge_for(consultation.call_id)
    old_leg = original.legs.get(old_destination)
    client = calls.sip_client_for(new_destination)
    source_relay = calls.resource_for(original.call_id, "relay")
    destination_relay = calls.resource_for(consultation.call_id, "relay")
    if (old_leg is None or old_leg.dialog is None or old_leg.dialog.dialog is None
            or client is None or client.dialog is None
            or not isinstance(source_relay, SipRtpRelay) or not isinstance(destination_relay, SipRtpRelay)
            or source_relay.video_relay is not None or destination_relay.video_relay is not None):
        return None
    owned_ports = {source_relay.left_port, source_relay.right_port,
                   destination_relay.left_port, destination_relay.right_port}
    if not owned_ports <= runtime.rtp_port_pool.get("used", set()):
        return SipTransferResult(False, 500, "media_ownership_changed")
    destination_leg = consultation.legs.get(new_destination)
    destination_trunk_id = destination_leg.trunk_id if destination_leg is not None else ""
    from_callee = old_leg.dialog is referrer
    source_client = (next((leg.dialog for leg in original.legs.values()
                           if leg.role == "caller" and leg.dialog is not None
                           and leg.phase not in {LegPhase.CLOSING, LegPhase.CLOSED}), None)
                     if from_callee else old_leg.dialog)
    transferrer = str(original.metadata.get("dest_endpoint_id" if from_callee else "source_endpoint_id") or "")
    target = str(consultation.metadata.get("dest_endpoint_id") or "")
    if target and (target not in consultation.endpoint_claims
                   or runtime.endpoints.require(target).active_call_id != consultation.call_id):
        return SipTransferResult(False, 486, "busy")
    if not from_callee and grant.endpoint_id and (
        grant.endpoint_id not in original.endpoint_claims
        or runtime.endpoints.require(grant.endpoint_id).active_call_id != original.call_id
    ):
        return SipTransferResult(False, 486, "busy")
    def merge_reservations(previous, retained):
        reservation = RtpPortReservation.merge(hass, previous, retained)
        return lambda _ports: reservation.release()

    goal = original if from_callee else calls.upsert(
        f"ha-transfer-{secrets.token_hex(12)}", state="connecting", owner="bridge",
        caller=str(original.metadata.get("connected_party") or original.callee),
        callee=consultation.callee, route_kind=consultation.route_kind,
        source_endpoint_id=grant.endpoint_id,
        ingress="trunk" if old_leg.trunk_id or original.route_kind == "trunk" else "extension",
        inbound_trunk_id=old_leg.trunk_id,
    )
    ready = asyncio.Event()
    watchers = []
    publish_connected_identity = None
    def commit_owner(relay):
        nonlocal publish_connected_identity
        calls.remove_leg(consultation.call_id, new_destination)
        calls.forget_bridge_link(consultation.call_id)
        for session, retired in ((original, source_relay), (consultation, destination_relay)):
            session.release_resource(f"relay:{session.call_id}", value=retired)
            goal.add_resource(f"retired_relay:{session.call_id}", retired,
                              lambda _reason, value=retired: value.stop(), stage=CleanupStage.MEDIA)
        calls.attach_relay(goal.call_id, relay)

        def watch_client(value):
            async def watch():
                await ready.wait()
                await async_watch_sip_bridge_destination(
                    hass, client=value, source_call_id=goal.call_id,
                    terminate_sip_bridge=async_terminate_sip_bridge_session,
                )
            task = asyncio.create_task(watch(), name=f"voip-bridge-destination-{value.dialog_ids.call_id}")
            watchers.append(task)
            return task

        if from_callee:
            if not calls.retire_leg(original.call_id, old_leg, endpoint_id=transferrer,
                                    generation=original.generation):
                raise RuntimeError("transfer participant changed before retirement")
            referrer.on_media_update = None
            referrer.on_info_dtmf = None
            referrer.on_info_video = None
        else:
            calls.remove_leg(original.call_id, old_destination)
            calls.forget_bridge_link(original.call_id)
            source_client.on_refer = None
            calls.add_leg(goal.call_id, old_destination, role="caller", state="in_call", endpoint_id=grant.endpoint_id)
            calls.attach_sip_client(goal.call_id, old_destination, source_client, role="caller", state="in_call", trunk_id=old_leg.trunk_id)
            calls.attach_client_watcher(old_destination, watch_client(source_client))
            if grant.endpoint_id:
                calls.transfer_endpoint_claim(original.call_id, goal.call_id, grant.endpoint_id)
        client.on_refer = None
        calls.register_bridge(source_call_id=goal.call_id, dest_call_id=new_destination,
                              client=client, lifecycle_task=watch_client(client), state="in_call",
                              caller=goal.caller, callee=consultation.callee, route_kind=consultation.route_kind,
                              expected_generation=goal.generation)
        goal.legs[new_destination].trunk_id = destination_trunk_id
        if target:
            calls.transfer_endpoint_claim(consultation.call_id, goal.call_id, target)
        goal.update_metadata(dest_endpoint_id=target, target_endpoint_id=target,
                                 connected_party=consultation.callee,
                                 outbound_trunk_id=consultation.metadata.get("outbound_trunk_id", ""),
                                 policy_endpoint_ids=tuple(sorted(set(
                                     (*goal.endpoint_claims, *original.endpoint_claims, *consultation.endpoint_claims,
                                      *(original.metadata.get("policy_endpoint_ids") or ()),
                                      *(consultation.metadata.get("policy_endpoint_ids") or ()))
                                 ))))
        binder = BridgeMediaUpdateBinder(hass)
        publish_connected_identity = binder.attach(
            client, relay, source_call_id=goal.call_id, source_client=source_client)
        if source_client is not None:
            binder.attach_audio_side(source_client, relay, source_call_id=goal.call_id, side="left")
            goal.claim_answer(goal.token, lambda: True)
        attach_dtmf_event_bridge(hass, relay, call_id=goal.call_id, dest_call_id=new_destination,
                                 caller=goal.caller, callee=consultation.callee, client=client, source_client=source_client)

    try:
        async with audio_reconfiguration_guard(source_relay, destination_relay):
            with SessionHandoff(calls, original, consultation, goal) as handoff:
                handoff.preserve(old_leg, "phase", "endpoint_id")
                for value in (client, source_client, referrer):
                    if value is not None:
                        handoff.preserve(value, "on_refer", "on_media_update", "on_connected_identity", "on_info_dtmf", "on_info_video")
                relay = SipRtpRelay.splice_audio(
                    source_relay, "left" if from_callee else "right", destination_relay, "right",
                    merge_reservations=merge_reservations, commit_owner=commit_owner,
                )
    except BaseException:
        for task in watchers:
            task.cancel()
        await async_wait_for_cleanup(asyncio.gather(*watchers, return_exceptions=True))
        if goal is not original:
            await async_wait_for_cleanup(calls.request_termination(
                goal.call_id, TerminationIntent("transfer_failed"), generation=goal.generation))
        raise
    if publish_connected_identity is not None:
        publish_connected_identity()
    old_watcher = calls.cancel_task(original.call_id, f"client_watcher:{old_destination}")
    new_watcher = calls.cancel_task(consultation.call_id, f"client_watcher:{new_destination}")
    ready.set()
    grant.release()
    calls.request_termination(consultation.call_id, TerminationIntent("forwarded"))

    async def retire_referrer():
        await old_leg.close("forwarded")
        if goal.live:
            calls.remove_leg(goal.call_id, old_destination)
            if transferrer:
                calls.release_endpoint_claim(goal.call_id, transferrer)

    def after_refer(_task=None):
        if from_callee and goal.live:
            goal.create_task(retire_referrer(), name=f"voip-retired-transfer-{old_destination}")
        elif not from_callee and original.live:
            calls.request_termination(original.call_id, TerminationIntent("forwarded"))
    transaction = getattr(referrer, "_incoming_refer_task", None) if referrer is not None else asyncio.current_task()
    if transaction is not None and not transaction.done():
        transaction.add_done_callback(after_refer)
    else:
        after_refer()
    publish_bridge_projection(hass, goal, event_type="transferred", last_sip_event="REFER")
    publish_media_route(hass, goal.call_id, relay.media_route)
    results = await async_wait_for_cleanup(asyncio.gather(
        source_relay.stop(), destination_relay.stop(),
        *(task for task in (old_watcher, new_watcher) if task is not None),
        return_exceptions=True,
    ))
    if goal.live:
        for session, retired, result in ((original, source_relay, results[0]), (consultation, destination_relay, results[1])):
            if isinstance(result, BaseException):
                _LOGGER.error("Retired transfer media cleanup failed call_id=%s", session.call_id)
            else:
                goal.release_resource(f"retired_relay:{session.call_id}", value=retired)
    return SipTransferResult(True, 200, "completed")
