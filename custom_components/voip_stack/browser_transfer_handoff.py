"""Adopt an answered consultation into the retained browser's existing session."""

import asyncio

from .bridge_manager import async_watch_sip_bridge_destination
from .bridge_media_updates import BridgeMediaUpdateBinder
from .call_projection import publish_bridge_projection, publish_esp_media_route
from .core.sip_transfer import SipTransferResult
from .endpoint_session import SessionHandoff, TerminationIntent
from .dtmf_events import attach_dtmf_event_bridge
from .sip_bridge import local_rtp_peer
from .sip_rtp_bridge import SipRtpRelay
from .softphone_termination import async_terminate_sip_bridge_session


async def adopt_consultation(hass, runtime, source, grant):
    calls, consultation = runtime.sip, grant.consultation
    original = source.validate()
    target = str(consultation.metadata.get("dest_endpoint_id") or consultation.metadata.get("endpoint_id") or "")
    endpoint = runtime.endpoints.get(target)
    if (not grant.current() or not consultation.live or consultation.state not in {"in_call", "held"}
            or original is consultation or endpoint is None or target not in consultation.endpoint_claims
            or grant.endpoint_id != source.call.caller_endpoint_id):
        return SipTransferResult(False, 481, "replacement_not_found")
    descriptor = calls.resource_for(consultation.call_id, "softphone_media")
    if endpoint.supports_ha_client and descriptor is not None and descriptor.get("endpoint_id") == target:
        with SessionHandoff(calls, original, consultation):
            calls.transfer_endpoint_claim(consultation.call_id, original.call_id, target)
            source.connect_answered_browser(target, str(descriptor.get("media_client_id") or ""))
        owner = runtime.media.owners_for("audio").get(f"{target}|{consultation.call_id}")
        if owner is not None:
            owner.revoke()
    else:
        destination_id = calls.bridge_link_for(consultation.call_id)
        client = calls.sip_client_for(destination_id)
        relay = calls.resource_for(consultation.call_id, "relay")
        if (client is None or client.dialog is None or not isinstance(relay, SipRtpRelay)
                or relay.video_relay is not None):
            return SipTransferResult(False, 488, "local_transfer_unavailable")
        ready = asyncio.Event()
        async def watch():
            await ready.wait()
            await async_watch_sip_bridge_destination(hass, client=client, source_call_id=original.call_id,
                terminate_sip_bridge=async_terminate_sip_bridge_session)
        watcher = asyncio.create_task(watch(), name=f"voip-bridge-destination-{destination_id}")
        try:
            async with relay.media_reconfiguration_guard():
                source.validate()
                if not grant.current() or not consultation.live:
                    raise RuntimeError("consultation changed before transfer")
                peer = local_rtp_peer(source.invite.source_host, source.invite.recv_format, source.invite.send_format)
                reconfigure = relay.prepare_peer_reconfiguration("left", peer)
                with SessionHandoff(calls, original, consultation) as handoff:
                    handoff.preserve(source, "retired", "committed", "retired_leg")
                    handoff.preserve(client, "on_refer", "on_media_update", "on_info_dtmf")
                    handoff.preserve(relay, "on_dtmf", "on_dtmf_fallback", "on_media_route_changed")
                    source.commit()
                    calls.remove_leg(consultation.call_id, destination_id)
                    calls.forget_bridge_link(consultation.call_id)
                    calls.release_resource(consultation.call_id, f"relay:{consultation.call_id}", value=relay)
                    client.on_refer = None
                    calls.register_bridge(source_call_id=original.call_id, dest_call_id=destination_id,
                        client=client, lifecycle_task=watcher, state="in_call", caller=original.caller,
                        callee=consultation.callee, route_kind=consultation.route_kind,
                        expected_generation=original.generation)
                    calls.attach_relay(original.call_id, relay)
                    calls.transfer_endpoint_claim(consultation.call_id, original.call_id, target)
                    original.update_metadata(dest_endpoint_id=target, target_endpoint_id=target,
                        connected_party=consultation.callee,
                        outbound_trunk_id=consultation.metadata.get("outbound_trunk_id", ""),
                        policy_endpoint_ids=tuple(sorted(set((*original.endpoint_claims,
                            *consultation.endpoint_claims, *(original.metadata.get("policy_endpoint_ids") or ()),
                            *(consultation.metadata.get("policy_endpoint_ids") or ()))))))
                    BridgeMediaUpdateBinder(hass).attach(client, relay, source_call_id=original.call_id)
                    attach_dtmf_event_bridge(hass, relay, call_id=original.call_id, dest_call_id=destination_id,
                        caller=original.caller, callee=consultation.callee, client=client)
                    calls.attach_media(original.call_id, {"rtp_loopback": True,
                        "remote_rtp_host": source.invite.source_host, "remote_rtp_port": relay.left_port,
                        "send_format": source.invite.recv_format, "recv_format": source.invite.send_format,
                        "endpoint_id": source.call.caller_endpoint_id,
                        "media_client_id": source.call.caller_media_owner_id})
                    reconfigure()
                    publish_esp_media_route(hass, original.call_id, relay.media_route)
        except BaseException:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            raise
        calls.cancel_task(consultation.call_id, f"client_watcher:{destination_id}")
        ready.set()
        source.connected(consultation.callee, consultation.route_kind)
        publish_bridge_projection(hass, original, event_type="transferred", last_sip_event="REFER")
    async def retire():
        await source.close_referrer()
        barrier = calls.request_termination(consultation.call_id, TerminationIntent("forwarded"),
                                            generation=consultation.generation)
        if barrier is not None:
            await barrier
    source.after_refer(retire)
    return SipTransferResult(True, 200, "completed", retained_call=True)
