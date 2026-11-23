"""The session-owned forward attempt and its staged destination and media resources."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

from .call_projection import publish_bridge_projection
from .config import (
    trunk_config as _get_trunk_config,
)
from .config import (
    trunk_identity_uri as _trunk_identity_uri,
)
from .const import (
    CONF_SIP_VIDEO,
    CONF_TRUNK_AUTH_USERNAME,
    CONF_TRUNK_OUTBOUND_PROXY,
    CONF_TRUNK_PASSWORD,
    CONF_TRUNK_PORT,
    CONF_TRUNK_SERVER,
    CONF_TRUNK_TRANSPORT,
    CONF_TRUNK_USERNAME,
    CONF_VIDEO_TRANSCODING,
)
from .core.sdp import build_answer_directional
from .core.sip import sip_default_port
from .endpoint_dialing import OutboundLegPolicy
from .endpoint_session import TerminationIntent
from .forward_group import ForwardGroup
from .forward_plan import (
    ForwardPlan,
    ForwardRuntime,
    ForwardSource,
    TransferResult,
    source_dialog_is_answered,
)
from .fsm import (
    CallState,
    TerminalReason,
)
from .inbound_answer import (
    AnswerCommitResult,
    async_commit_runtime_answer,
)
from .media_ports import RtpPortReservation
from .media_reservation import release_video_media_reservation
from .outbound_bridge_commit import (
    BridgeCommitData,
    BridgeCommitPolicy,
    async_commit_outbound_bridge,
)
from .pbx_trace import trace_call
from .phone_endpoint import EndpointAvailability
from .route_abort import (
    RouteAbortContext,
    RouteAbortIntent,
    async_abort_route,
)
from .router import RouteAction
from .runtime_data import (
    endpoint_directory,
)
from .session_cleanup import async_cleanup_sip_runtime
from .sip_client import SIP_TIMER_B
from .sip_runtime import (
    send_final_response as _sip_send_final_response,
)
from .call_events import home_phone_name as _ha_peer_name

_LOGGER = logging.getLogger(f"{__package__}.call_forwarder")


def validate_resolved_pickup(registry, call_id, endpoint_id, expected=None):
    """Resolve the same live directed or parked pickup claim at admission and handoff."""
    from .call_pickup import PickupClaim
    from .parking import ParkingCall

    parked = registry.resource_for(call_id, "parking")
    directed = registry.resource_for(call_id, "pickup")
    claim = (
        directed
        if isinstance(directed, PickupClaim)
        else parked.parked.claim
        if isinstance(parked, ParkingCall)
        else None
    )
    if (
        claim is None
        or (expected is not None and claim is not expected)
        or not claim.authorizes_browser_answer(
            registry.get_session(call_id), endpoint_id
        )
    ):
        raise ValueError(
            "A resolved HA pickup requires its current pickup claim and exact endpoint"
        )
    return claim


class ForwardOperation:
    """Perform the admitted handoff in the existing session's single forward task."""

    def __init__(
        self,
        runtime: ForwardRuntime,
        registry,
        source: ForwardSource,
        plan: ForwardPlan,
        artifacts,
        abort: RouteAbortContext,
        *,
        queue_offer=None,
        pickup_claim=None,
    ):
        self.runtime = runtime
        self.registry = registry
        self.source = source
        self.plan = plan
        self.artifacts = artifacts
        self.abort = abort
        self.queue_offer = queue_offer
        self.pickup_claim = pickup_claim
        self.invite = source.invite
        self.client = None
        self.reservation = None
        self.borrowed_reservation = False
        self.video_relay = None
        self.destination_call_id = ""
        self.claimed_endpoint_id = ""
        self.prepared_media = None
        self.parked_audio_socket = None
        self.parked_media = None
        self.retained_media = None
        self.retained_video_update = None
        self.retained_video_answer = None
        self.preanswered = None
        self.failure_sip_status = 0
        self.owned_sip_source = source.owned_sip_source

    async def commit_source_answer(
        self,
        answer_sdp: str,
        *,
        owner: str,
        callee: str,
        route_kind: str,
        response_already_sent: bool,
    ) -> AnswerCommitResult:
        local_source = self.source.local
        return await async_commit_runtime_answer(
            self.registry,
            self.source.call_id,
            answer_sdp,
            send_final_response=_sip_send_final_response,
            response_context=self.runtime.hass,
            owner=owner,
            caller=self.invite.caller,
            callee=callee,
            route_kind=route_kind,
            response_already_sent=response_already_sent or local_source is not None,
            **(
                {
                    "endpoint_id": local_source.call.caller_endpoint_id,
                    "source_endpoint_id": local_source.call.caller_endpoint_id,
                    "media_client_id": local_source.call.caller_media_owner_id,
                }
                if local_source is not None
                else {}
            ),
        )

    def local_answer_authorized(self, endpoint_id):
        from .call_pickup import PickupClaim
        from .parking import ParkingCall

        directed = self.registry.resource_for(self.source.call_id, "pickup")
        if isinstance(directed, PickupClaim) and directed.authorizes_browser_answer(
            self.source.session, endpoint_id
        ):
            return True
        parked = (
            self.source.application.parked.claim
            if isinstance(self.source.application, ParkingCall)
            else None
        )
        return parked is not None and parked.authorizes_browser_answer(
            self.source.session, endpoint_id
        )

    async def retire_application(self, *, pending_browser=False):
        application = self.source.application
        if application is None:
            return
        if application.owner == "parking":
            claim = application.parked.claim
            if pending_browser and claim is not None and claim.current:
                return
        await application.close("forwarded")
        self.registry.release_resource(
            self.source.call_id, application.resource_name, value=application
        )

    async def cleanup_failed_route(self, reason: str) -> None:
        registry = self.registry
        session = self.source.session
        call_id = self.source.call_id
        if self.owned_sip_source is not None and not self.owned_sip_source.adopted:
            await self.owned_sip_source.rollback_video()
        if self.retained_video_update is not None:
            if self.retained_video_update.rollback is not None:
                await self.retained_video_update.rollback()
            self.retained_video_update = None
        if self.parked_audio_socket is not None:
            self.parked_audio_socket.close()
            self.parked_audio_socket = None
        if self.prepared_media is not None:
            await self.prepared_media.rollback()
            if self.prepared_media.committed and self.parked_media is not None:
                await self.parked_media.stop()
        attached_client = False
        if self.destination_call_id:
            leg = session.legs.get(self.destination_call_id) if session is not None else None
            owns_client = bool(leg is not None and leg.dialog is self.client and leg.closer is not None)
            if registry.bridge_link_for(call_id) == self.destination_call_id:
                registry.forget_bridge_link(call_id)
            closed_leg = await registry.close_leg(
                call_id,
                self.destination_call_id,
                reason=reason,
            )
            # A projected fork leg may observe the client without owning it.
            attached_client = owns_client and closed_leg
        if self.claimed_endpoint_id and session is not None and session.live:
            registry.release_endpoint_claim(call_id, self.claimed_endpoint_id)
            if session.metadata.get("dest_endpoint_id") == self.claimed_endpoint_id:
                session.update_metadata(dest_endpoint_id="", target_endpoint_id="")
        if self.reservation is not None and not self.borrowed_reservation:
            self.reservation.release()
        if self.video_relay is not None:
            await self.video_relay.stop()
            self.video_relay = None
        if self.client is not None and not attached_client:
            await async_cleanup_sip_runtime(
                client=self.client,
                terminate_client=True,
            )

    async def _answer_browser(self):
        registry = self.registry
        session = self.source.session
        call_id = self.source.call_id
        destination = self.plan.destination
        application = self.source.application
        local_source = self.source.local
        if self.pickup_claim is not None:
            validate_resolved_pickup(
                self.registry,
                call_id,
                self.plan.browser_endpoint.endpoint_id,
                self.pickup_claim,
            )
        endpoint = self.plan.browser_endpoint
        if endpoint is None or not endpoint.supports_ha_client:
            raise RuntimeError("target Home Assistant phone disappeared")
        if endpoint.dnd:
            raise RuntimeError("target Home Assistant phone is in DND")
        if endpoint.active_call_id and endpoint.active_call_id != call_id:
            raise RuntimeError("target Home Assistant phone is busy")
        if endpoint.availability is EndpointAvailability.UNAVAILABLE:
            raise RuntimeError("target Home Assistant phone is disabled")
        if self.owned_sip_source is not None:
            from .browser_application_handoff import forward_application_to_browser

            await forward_application_to_browser(self.runtime, registry, session, self.invite,
                endpoint, None, timeout=self.plan.ring_timeout, owned_source=self.owned_sip_source)
            return TransferResult("connected", destination)
        if local_source is not None and local_source.outgoing and local_source.established:
            await local_source.forward_to_browser(self.runtime, endpoint, timeout=self.plan.ring_timeout)
            return TransferResult("connected", destination)
        if (
            application is not None
            and application.media is not None
            and not self.local_answer_authorized(endpoint.endpoint_id)
        ):
            from .browser_application_handoff import forward_application_to_browser

            await forward_application_to_browser(
                self.runtime,
                registry,
                session,
                self.invite,
                endpoint,
                application,
                timeout=self.plan.ring_timeout,
            )
            return TransferResult("connected", destination)
        if local_source is not None and (
            local_source.outgoing or local_source.committed
        ):
            if application is not None:
                await application.release_media_for_forward()
            local_source.redirect_browser(endpoint.endpoint_id)
            await self.retire_application(pending_browser=True)
            return TransferResult("ringing", destination)
        session_id = registry.resolve_session_id(call_id)
        claims = registry.endpoint_claims_for(session_id)
        target_was_claimed = endpoint.endpoint_id in claims
        old_was_claimed = self.source.receiver_endpoint_id in claims
        target_claimed = False
        old_released = False
        try:
            registry.claim_endpoint(
                call_id,
                endpoint.endpoint_id,
                role="destination",
            )
            target_claimed = not target_was_claimed
            source_id = (
                str(session.metadata.get("source_endpoint_id") or "")
                if session is not None
                else ""
            )
            if self.source.receiver_endpoint_id != endpoint.endpoint_id and not (
                application is not None
                and self.source.receiver_endpoint_id == source_id
            ):
                old_released = (
                    registry.release_endpoint_claim(
                        call_id,
                        self.source.receiver_endpoint_id,
                    )
                    or old_was_claimed
                )
            if application is not None and self.parked_media is None:
                await application.release_media_for_forward()
            self.runtime.defer_invite_to_softphone(
                self.invite,
                route_kind=self.plan.decision.action.value,
                endpoint_id=endpoint.endpoint_id,
                endpoint_device_id=endpoint.device_id,
                callee=endpoint.name,
                sip_uri=self.plan.decision.sip_uri,
                last_sip_event="ROUTE_FORWARD",
            )
            await self.retire_application(pending_browser=True)
        except Exception:
            if target_claimed:
                registry.release_endpoint_claim(
                    call_id,
                    endpoint.endpoint_id,
                )
            if old_released and old_was_claimed:
                registry.claim_endpoint(
                    call_id,
                    self.source.receiver_endpoint_id,
                    role="destination",
                )
            raise
        return TransferResult("ringing", destination)

    async def _route_application(self):
        registry = self.registry
        hass = self.runtime.hass
        local_ip = self.runtime.local_ip
        session = self.source.session
        call_id = self.source.call_id
        decision = self.plan.decision
        application = self.source.application
        local_source = self.source.local
        invite = self.invite
        if decision.action is RouteAction.PARKING:
            from .parking import start_parking_application

            if decision.entry.metadata["parking_mode"] != "deposit":
                raise RuntimeError(
                    "Forward calls to a parking destination, not a pickup slot"
                )
            next_application = await start_parking_application(
                hass,
                session,
                invite,
                local_ip,
                slot=decision.entry.metadata["parking_slot"],
                parker=self.source.receiver_endpoint_id,
                previous=application,
            )
            if self.owned_sip_source is not None:
                await next_application.answer()
            return

        if decision.action is RouteAction.VOICEMAIL:
            from .voicemail import start_voicemail_application

            mailbox = endpoint_directory(hass).get(
                decision.entry.metadata["mailbox_endpoint_id"]
            )
            if mailbox is None:
                raise RuntimeError("The selected mailbox is no longer available")
            voicemail = await start_voicemail_application(
                hass,
                session,
                invite,
                mailbox,
                local_ip,
                previous=application,
                local_source=local_source,
            )
            await voicemail.answer()
            return TransferResult("connected", self.plan.destination)

        if decision.action is RouteAction.AUTOMATION:
            from .automation_call import (
                attach_automation_application,
                queue_automation_request,
            )

            current = registry.get_session(call_id)
            if current is None or decision.entry is None:
                raise RuntimeError("Automation route is no longer available")
            claimed = registry.transition(
                call_id,
                owner="automation",
                state=CallState.IN_CALL.value
                if source_dialog_is_answered(self.preanswered)
                else CallState.RINGING.value,
                callee=decision.entry.display_name,
                route_kind="automation",
                expected_owner=current.owner,
                expected_revision=current.revision,
            )
            if claimed is None:
                raise RuntimeError("Automation route ownership changed")
            if application is None or application.owner != "automation":
                next_application = attach_automation_application(
                    hass,
                    current,
                    invite,
                    decision.entry,
                    local_ip,
                    local_source=local_source,
                    previous=application,
                )
                if application is not None:
                    await application.close("forwarded")
                    name = (
                        "assist_application"
                        if application.owner == "assist"
                        else application.owner
                    )
                    registry.release_resource(
                        call_id, f"{name}:{call_id}", value=application
                    )
            else:
                # Transfer the existing media/application resource rather
                # than binding a second receiver to its RTP port.
                application.cancel_deadline()
                application.contact = decision.entry
                application.controller = ""
                next_application = application
            if self.owned_sip_source is not None:
                await next_application.answer()
            queue_automation_request(next_application)
            return

        if decision.action in {RouteAction.QUEUE, RouteAction.PAGING}:
            from .paging import start_paging_application
            from .queue_call import start_queue_application, transfer_queue_application

            if decision.entry is None:
                raise RuntimeError("The application destination disappeared")
            if (
                application is not None
                and application.owner == "queue"
                and decision.action is RouteAction.QUEUE
            ):
                await transfer_queue_application(
                    hass, session, invite, decision.entry, local_ip, application
                )
                await self.retire_application()
                return TransferResult("connected", self.plan.destination)
            if application is not None and application.owner == decision.action.value:
                raise RuntimeError("A call cannot re-enter the same application")
            factory = (
                start_queue_application
                if decision.action is RouteAction.QUEUE
                else start_paging_application
            )
            next_application = factory(
                hass,
                session,
                invite,
                decision.entry,
                local_ip,
                previous=application,
                local_source=local_source,
            )
            if self.owned_sip_source is not None:
                await next_application.answer()
            if application is not None:
                await application.close("forwarded")
                name = (
                    "assist_application"
                    if application.owner == "assist"
                    else application.owner
                )
                registry.release_resource(
                    call_id, f"{name}:{call_id}", value=application
                )
            return

    async def _route_assist(self):
        registry = self.registry
        call_id = self.source.call_id
        decision = self.plan.decision
        destination = self.plan.destination
        application = self.source.application
        local_source = self.source.local
        invite = self.invite
        if application is not None and application.owner == "parking":
            await application.stop_music()
        current = registry.sessions.get(registry.resolve_session_id(call_id))
        if current is not None:
            claimed_assist = registry.transition(
                call_id,
                state=CallState.IN_CALL.value
                if application and application.answered
                else CallState.CONNECTING.value,
                owner="assist",
                callee=destination,
                expected_revision=current.revision,
                expected_owner=current.owner,
            )
            if claimed_assist is None:
                raise RuntimeError("Assist route ownership changed")
        assist_invite = invite
        existing_media = application.media if application and application.media else None
        if self.owned_sip_source is not None:
            async def complete(reason):
                registry.request_termination(call_id, TerminationIntent(reason), generation=current.generation)
            existing_media = await self.owned_sip_source.adopt_application(complete, staged=self.reservation)
            self.reservation = existing_media.reservation
            assist_invite = existing_media.invite
        if local_source is not None:
            assist_invite = local_source.stage_application_media(
                invite, self.reservation.ports[0], self.reservation.ports[1]
            )
        await self.runtime.start_local_assist_bridge(
            assist_invite,
            reservation=self.reservation,
            local_rtp_port=self.reservation.ports[0],
            roster_entries=self.plan.roster_entries,
            source="browser"
            if local_source is not None
            else "trunk"
            if self.preanswered is not None
            else "sip",
            called_extension=str(
                (decision.entry.extension if decision.entry is not None else "")
                or destination
            ),
            release_reservation_on_failure=self.preanswered is None,
            **(
                {"existing_media": existing_media}
                if existing_media is not None
                else {}
            ),
        )
        if local_source is not None:
            local_source.commit(keep_media=True)
        response_already_sent = source_dialog_is_answered(self.preanswered)
        answer = ""
        if not response_already_sent:
            answer = build_answer_directional(
                self.runtime.local_ip,
                self.runtime.local_ip,
                self.reservation.ports[0],
                invite.send_format,
                invite.recv_format,
                recv_formats=invite.recv_formats,
                remote_sdp=invite.remote_sdp, dtmf_direction=getattr(invite, "dtmf_direction", None),
            )
        registry.take_pending_invite(call_id)
        transferred_media = registry.take_media(call_id, provisional=True)
        if application is not None and application.media is not None:
            application.media.release_reservation_on_stop = True
        release_video_media_reservation(transferred_media)
        answer_result = await self.commit_source_answer(
            answer,
            owner="assist",
            callee=destination,
            route_kind=decision.action.value,
            response_already_sent=response_already_sent,
        )
        if not answer_result.committed:
            raise RuntimeError(answer_result.reason)
        await self.retire_application()
        if local_source is not None:
            local_source.connected(destination, decision.action.value)
        return

    def prepare_sip_target(self):
        registry = self.registry
        hass = self.runtime.hass
        session = self.source.session
        call_id = self.source.call_id
        decision = self.plan.decision
        invite = self.invite
        bridge_to_trunk = decision.action is RouteAction.TRUNK
        target = decision.target or self.plan.destination
        if bridge_to_trunk:
            selected_trunk_id = getattr(decision, "trunk_id", "")
            trunk_cfg = (
                _get_trunk_config(hass, trunk_id=selected_trunk_id)
                if selected_trunk_id
                else _get_trunk_config(hass)
            )
            uri_override = (
                f"sip:{target}@{trunk_cfg[CONF_TRUNK_SERVER]}:"
                f"{int(trunk_cfg[CONF_TRUNK_PORT])};"
                f"transport={str(trunk_cfg[CONF_TRUNK_TRANSPORT]).lower()}"
            )
        else:
            uri_override = str(decision.sip_uri or "")

        source_route_endpoint_id = str(
            ((session.metadata if session is not None else {}) or {}).get(
                "source_endpoint_id"
            )
            or ""
        ).strip()
        target_route_endpoint_id = str(
            ((decision.entry.metadata if decision.entry is not None else {}) or {}).get(
                "endpoint_id"
            )
            or ""
        ).strip()
        # Directory contacts/services have stable IDs too, but only a
        # real logical phone can own an EndpointRegistry claim.
        target_route_endpoint = endpoint_directory(hass).get(target_route_endpoint_id)
        target_route_endpoint_id = (
            target_route_endpoint.endpoint_id
            if target_route_endpoint is not None
            else ""
        )
        forward_video_enabled = bool(
            (self.preanswered is None or self.parked_media is not None or self.retained_media is not None
             or self.owned_sip_source is not None)
            and self.runtime.config.get(CONF_SIP_VIDEO, False)
            and invite.video_format is not None
        )
        video_transcoding_enabled = bool(
            forward_video_enabled
            and self.runtime.config.get(CONF_VIDEO_TRANSCODING, False)
        )
        selected_trunk_id = getattr(decision, "trunk_id", "")
        trunk_cfg = (
            (
                _get_trunk_config(hass, trunk_id=selected_trunk_id)
                if selected_trunk_id
                else _get_trunk_config(hass)
            )
            if bridge_to_trunk
            else {}
        )
        direct_winner = self.runtime.prepare_outbound_leg(
            member=target,
            peers=self.plan.peers,
            roster_entries=self.plan.roster_entries,
            local_name=invite.caller or _ha_peer_name(hass),
            local_rtp_port_index=1,
            uri_override=uri_override,
            peer_user_agent_override=str(
                (
                    ((decision.entry.metadata or {}).get("user_agent"))
                    if decision.entry is not None
                    else ""
                )
                or ""
            ),
            invite=invite,
            port_reservation=self.reservation,
            roster_entry_override=decision.entry,
            policy=OutboundLegPolicy(
                local_uri_user=(
                    str(trunk_cfg.get(CONF_TRUNK_USERNAME) or _ha_peer_name(hass))
                    if bridge_to_trunk
                    else invite.routing_caller or _ha_peer_name(hass)
                ),
                local_identity_uri=(
                    _trunk_identity_uri(trunk_cfg) if bridge_to_trunk else ""
                ),
                auth_username=str(trunk_cfg.get(CONF_TRUNK_AUTH_USERNAME) or ""),
                username=str(trunk_cfg.get(CONF_TRUNK_USERNAME) or ""),
                password=str(trunk_cfg.get(CONF_TRUNK_PASSWORD) or ""),
                outbound_proxy=str(trunk_cfg.get(CONF_TRUNK_OUTBOUND_PROXY) or ""),
                reuse_registered_flow=not bridge_to_trunk,
                use_trunk_flow=bridge_to_trunk,
                trunk_id=selected_trunk_id if bridge_to_trunk else "",
                allow_video=forward_video_enabled,
                retained_source_media=self.retained_media,
            ),
        )
        if direct_winner is None:
            raise RuntimeError(
                f"destination {self.plan.destination} has no reachable SIP leg"
            )
        self.client = direct_winner.client
        self.destination_call_id = self.client.dialog_ids.call_id
        candidate_endpoint_id = direct_winner.endpoint_id or target_route_endpoint_id
        if candidate_endpoint_id and session is not None:
            if candidate_endpoint_id == source_route_endpoint_id:
                raise RuntimeError("A caller cannot be forwarded to its own phone")
            if any(
                resource.name.startswith("retired_destination:")
                and resource.value.endpoint_id == candidate_endpoint_id
                for resource in session.resources
            ):
                raise RuntimeError(
                    "The selected phone is still ending its previous call"
                )
            registry.claim_endpoint(call_id, candidate_endpoint_id, role="destination")
            self.claimed_endpoint_id = candidate_endpoint_id
            session.update_metadata(
                dest_endpoint_id=self.claimed_endpoint_id,
                target_endpoint_id=self.claimed_endpoint_id,
            )
        registry.add_leg(
            call_id,
            self.destination_call_id,
            role="callee",
            state=CallState.CALLING.value,
        )
        registry.attach_sip_client(
            call_id,
            self.destination_call_id,
            self.client,
            role="callee",
            state=CallState.CALLING.value,
            trunk_id=selected_trunk_id if bridge_to_trunk else "",
        )
        if self.claimed_endpoint_id:
            registry.observe_leg(
                call_id,
                self.destination_call_id,
                role="callee",
                endpoint_id=self.claimed_endpoint_id,
                generation=session.generation,
            )
        if session is not None and bridge_to_trunk:
            session.update_metadata(outbound_trunk_id=selected_trunk_id)
        self.video_relay = direct_winner.video_relay
        return (
            direct_winner,
            source_route_endpoint_id,
            forward_video_enabled,
            video_transcoding_enabled,
        )

    def _prepare_parked_audio_socket(self):
        """Keep the one spare receive fd under this forward operation."""
        if self.parked_audio_socket is None:
            self.parked_audio_socket = self.reservation.take_audio_socket(self.reservation.ports[1])
            if self.parked_audio_socket is None:
                from .media_ports import bind_sip_audio_socket

                self.parked_audio_socket = bind_sip_audio_socket(self.reservation.ports[1])

    def prepared_outbound_audio_socket(self):
        """Read the still-owned parked receive socket without transferring it."""
        if (self.parked_media is None or self.reservation is None or self.reservation.released
                or self.registry.resource_for(self.source.call_id, "relay") is not self.parked_media
                or self.parked_audio_socket is None or self.parked_audio_socket.fileno() < 0
                or self.parked_audio_socket.getsockname()[1] != self.reservation.ports[1]):
            raise RuntimeError("The prepared parked audio owner changed before dialing")
        return self.parked_audio_socket

    async def _prepare_parked_socket(self, candidate):
        """Prepare a candidate against the single retained media reservation."""
        client, video = candidate.client, candidate.video_relay
        if self.retained_media is not None and self.retained_media.video_relay is not None:
            self.retained_media.validate(self.registry)
            if video is None:
                return False
            # The new SIP leg uses the existing destination sockets. Its
            # unused reservation remains local until the answer is planned.
            client.local_video_rtp_port = self.retained_media.video_relay.right_port
        if self.parked_media is not None:
            if video is None:
                return False
            # The held relay already owns its four video sockets. Retire
            # only this unused dial candidate, then advertise the held
            # destination port. Bind audio before the first INVITE too.
            await video.stop()
            candidate.video_relay = None
            if self.video_relay is video:
                self.video_relay = None
            client.local_video_rtp_port = self.parked_media.video_relay.right_port
            client.local_rtp_port = self.reservation.ports[1]
            self._prepare_parked_audio_socket()
        return True

    async def _invite_sip_target(self, direct_winner):
        destination = self.plan.destination
        bridge_uri = direct_winner.uri
        bridge_to_trunk = self.plan.decision.action is RouteAction.TRUNK
        result = await self.client.invite(
            target=bridge_uri.user,
            target_display_name=(
                self.plan.decision.entry.display_name
                if self.plan.decision.entry is not None
                else destination
            ),
            remote_host=bridge_uri.host,
            remote_sip_port=sip_default_port(bridge_uri),
            request_uri=str(bridge_uri),
            timeout=SIP_TIMER_B if bridge_to_trunk else 8.0,
        )
        if self.artifacts.trunk_closed:
            self.artifacts.trunk_closed = False
            raise RuntimeError(TerminalReason.CANCELLED.value)
        if result not in {"ringing", "in_call"}:
            raise RuntimeError(result)
        if result == "ringing":
            self.registry.observe_leg(
                self.source.call_id,
                self.destination_call_id,
                role="callee",
                state=CallState.REMOTE_RINGING.value,
                generation=self.source.session.generation,
            )
            ringing_session = self.registry.transition(
                self.source.call_id,
                state=CallState.REMOTE_RINGING.value,
                caller=self.invite.caller,
                callee=destination,
            )
            if ringing_session is not None:
                ringing_session.metadata["bridge_dest_call_id"] = (
                    self.client.dialog_ids.call_id
                )
                publish_bridge_projection(
                    self.runtime.hass,
                    ringing_session,
                    peer_name=destination,
                    direction="outgoing"
                    if self.source.local is not None
                    else "incoming",
                    route_source="automation",
                    last_sip_event="SIP_RESPONSE",
                )
            # Keep the source and reservation with the route until the
            # destination answers. A rejected ringing attempt must still
            # be eligible for on_failure: resume.
            result = await self.client.wait_for_final()
            if result != "in_call" or self.client.dialog is None:
                raise RuntimeError(result)
        return result

    async def _prepare_destination_media(self, direct_winner):
        application = self.source.application
        if self.retained_media is not None and self.retained_media.video_relay is not None:
            from .bridge_media_updates import prepare_retained_video_handoff
            from .sip_bridge import configure_answered_invite_video_relay

            self.retained_media.validate(self.registry)
            planned_video = direct_winner.video_relay
            if planned_video is None or self.client.dialog is None:
                raise RuntimeError("The transfer destination did not negotiate video")
            self.retained_video_answer = configure_answered_invite_video_relay(
                self.invite, self.client.dialog, planned_video, hass=self.runtime.hass,
                enable_transcoding=bool(self.runtime.config.get(CONF_VIDEO_TRANSCODING, False)))
            if self.retained_video_answer is None:
                raise RuntimeError("The transfer destination has no usable video contract")
            source_peer, destination_peer = planned_video.left, planned_video.right
            await planned_video.stop()
            self.video_relay = direct_winner.video_relay = None
            self.retained_video_update = await prepare_retained_video_handoff(
                self.runtime.hass, self.source.session, self.retained_media.video_relay,
                source_peer, destination_peer, source_client=self.retained_media.source_client)
        elif self.owned_sip_source is not None:
            from .local_call_media import ApplicationCallSource
            from .local_media_bridge import LocalMediaBridge

            destination = ApplicationCallSource.from_client(self.client,
                call_id=self.destination_call_id, caller=self.plan.destination, allow_video=True)
            self.prepared_media = await LocalMediaBridge.prepare_owned_source(
                self.owned_sip_source, destination, direct_winner.ports)
        elif self.parked_media is not None:
            from .local_call_media import ApplicationCallSource
            from .local_media_bridge import LocalMediaBridge

            destination_source = ApplicationCallSource.from_client(
                self.client,
                call_id=self.destination_call_id,
                caller=self.plan.destination,
                allow_video=True,
            )
            update = await application.prepare_video_pickup(destination_source)
            self.prepared_media = await LocalMediaBridge.prepare(
                self.parked_media,
                destination_source,
                video_update=update,
                right_socket=self.parked_audio_socket,
            )
            self.parked_audio_socket = None
            self.prepared_media.validate()
        elif application is not None:
            await application.release_media_for_forward()
        if self.source.local is not None:
            self.source.local.commit()

    async def commit_sip(
        self,
        direct_winner,
        source_route_endpoint_id,
        forward_video_enabled,
        video_transcoding_enabled,
        result,
    ):
        session = self.source.session
        decision = self.plan.decision
        destination = self.plan.destination
        application = self.source.application
        local_source = self.source.local
        invite = self.invite
        bridge_uri = direct_winner.uri
        video_failure_reason = direct_winner.video_failure_reason
        response_already_sent = source_dialog_is_answered(self.preanswered)
        committed = await async_commit_outbound_bridge(
            self.runtime.hass,
            self.registry,
            BridgeCommitData(
                invite=invite,
                winner=direct_winner,
                source_relay_port=self.owned_sip_source.local_port if self.owned_sip_source is not None
                else self.reservation.ports[0],
                dest_relay_port=self.reservation.ports[1],
                local_ip=self.runtime.local_ip,
                release_port_pairs=(self.reservation.ports,),
                detach_reservations=(self.reservation,),
                enable_video_transcoding=video_transcoding_enabled,
                local_endpoint_id=local_source.call.caller_endpoint_id
                if local_source is not None
                else "",
                source_client=application.source_client if application is not None
                else self.owned_sip_source.client if self.owned_sip_source is not None
                else self.retained_media.source_client if self.retained_media is not None else None,
                prepared_media=self.prepared_media,
                retained_media=self.retained_media,
                retained_video_update=self.retained_video_update,
                retained_video_answer=self.retained_video_answer,
            ),
            BridgeCommitPolicy(
                route_kind=decision.action.value,
                caller=invite.caller,
                callee=destination,
                connected_party=destination,
                source_role="trunk" if invite.received_via_trunk else "caller",
                source_state=(
                    CallState.IN_CALL.value
                    if response_already_sent
                    else CallState.CONNECTING.value
                ),
                bridge_state=(
                    CallState.REMOTE_RINGING.value
                    if result == "ringing"
                    else CallState.CONNECTING.value
                ),
                dest_state=result,
                response_already_sent=response_already_sent,
                local_source=local_source is not None,
                endpoint_id=local_source.call.caller_endpoint_id
                if local_source is not None
                else "",
                source_endpoint_id=local_source.call.caller_endpoint_id
                if local_source is not None
                else self.owned_sip_source.peer_endpoint_id if self.owned_sip_source is not None
                else source_route_endpoint_id,
                dest_endpoint_id=self.claimed_endpoint_id,
                media_client_id=local_source.call.caller_media_owner_id
                if local_source is not None
                else "",
                expected_generation=session.generation if session is not None else None,
            ),
        )
        if committed is None:
            raise RuntimeError(TerminalReason.CANCELLED.value)
        self.retained_video_update = None
        if self.parked_media is not None:
            await application.release_media_for_forward()
        await self.retire_application()
        if local_source is not None:
            local_source.connected(destination, decision.action.value)
        failure = committed.video_failure_reason or video_failure_reason
        if session is not None:
            publish_bridge_projection(
                self.runtime.hass,
                session,
                peer_name=destination,
                direction="outgoing" if local_source is not None else "incoming",
                route_source="automation",
                answered_by=destination,
                selected_tx_format=invite.send_format.audio_format.wire_token(),
                selected_rx_format=invite.recv_format.audio_format.wire_token(),
                selected_tx_rtp_format=invite.send_format.wire_token(),
                selected_rx_rtp_format=invite.recv_format.wire_token(),
                sip_status_code=200,
                last_sip_event="SIP_RESPONSE",
                route_kind=decision.action.value,
                sip_uri=str(bridge_uri),
                video_active=bool(committed.relay.video_relay),
                video_requested=forward_video_enabled,
                video_negotiated=bool(committed.relay.video_relay),
                video_failure_reason=failure,
            )

    async def forward_sip(self):
        winner, source_id, wants_video, transcode = self.prepare_sip_target()
        if await self._prepare_parked_socket(winner) is False:
            raise RuntimeError("The destination cannot retain the existing video")
        result = await self._invite_sip_target(winner)
        await self._prepare_destination_media(winner)
        await self.commit_sip(winner, source_id, wants_video, transcode, result)
        return TransferResult(
            "connected"
            if self.source.session is not None and self.source.session.live
            else "ended",
            self.plan.destination,
        )

    async def _dispatch(self):
        registry = self.registry
        decision = self.plan.decision
        application = self.source.application
        from .local_call_media import LocalCallMedia
        from .parking import ParkingCall

        self.parked_media = (
            application.media
            if isinstance(application, ParkingCall)
            and isinstance(application.media, LocalCallMedia)
            and application.media.video_relay is not None
            and registry.resource_for(self.source.call_id, "relay") is application.media
            else None
        )
        self.preanswered = registry.resource_for(self.source.call_id, "preanswered")
        if self.owned_sip_source is not None:
            self.owned_sip_source.validate()
            self.preanswered = {"final_response_sent": True,
                "rtp_reservation": self.owned_sip_source.reservation}
        self.retained_media = (self.preanswered or {}).get("retained_source_media")
        if self.retained_media is not None:
            self.retained_media.validate(registry)
            self.invite = self.retained_media.caller_invite
        if decision.action is RouteAction.ANSWER_HA:
            return await self._answer_browser()
        if decision.action in {
            RouteAction.PARKING,
            RouteAction.VOICEMAIL,
            RouteAction.AUTOMATION,
            RouteAction.QUEUE,
            RouteAction.PAGING,
        }:
            return await self._route_application()
        self.reservation = (None if self.owned_sip_source is not None
                            else (self.preanswered or {}).get("rtp_reservation"))
        self.borrowed_reservation = self.reservation is not None
        if self.reservation is None:
            self.reservation = RtpPortReservation.allocate(self.runtime.hass)
        if decision.action is RouteAction.GROUP:
            return await ForwardGroup(self).run()
        if decision.action is RouteAction.ASSIST:
            return await self._route_assist()
        return await self.forward_sip()

    async def run(self) -> TransferResult:
        session = self.source.session
        destination = self.plan.destination
        application = self.source.application
        try:
            result = await self._dispatch()
            if result is not None:
                if result.status == "connected" and session is not None and session.live:
                    return replace(result, committed=True, owner=session.owner,
                                   generation=session.generation, call_id=session.call_id)
                return result
            if session is None or not session.live:
                return TransferResult("ended", destination)
            if session.state in {"ringing", "remote_ringing", "connecting", "calling"}:
                return TransferResult("ringing", destination)
            return TransferResult("connected", destination, committed=True,
                                  owner=session.owner, generation=session.generation, call_id=session.call_id)
        except asyncio.CancelledError:
            await self.cleanup_failed_route(TerminalReason.CANCELLED.value)
            if self.owned_sip_source is not None and not self.owned_sip_source.adopted and session.live:
                if await async_abort_route(self.abort, RouteAbortIntent(TerminalReason.CANCELLED.value, "resume")):
                    return TransferResult("failed_resumed", destination, "cancelled")
            resumable_media = self.parked_media or (
                application.media if application is not None else None
            )
            if (
                resumable_media is not None
                and session.live
                and self.registry.resource_for(self.source.call_id, "relay")
                is resumable_media
                and not resumable_media.closed.is_set()
            ):
                if await async_abort_route(
                    self.abort,
                    RouteAbortIntent(TerminalReason.CANCELLED.value, "resume"),
                ):
                    return TransferResult("failed_resumed", destination, "cancelled")
            raise
        except Exception as err:  # noqa: BLE001 - convert route failures to policy.
            _LOGGER.debug("Forward attempt failed call_id=%s destination=%s",
                          self.source.call_id, destination, exc_info=True)
            reason = (TerminalReason.TIMEOUT.value if isinstance(err, TimeoutError) else
                      str(err).strip() or TerminalReason.TRANSPORT_UNREACHABLE.value)
            failure_sip_status = self.failure_sip_status or (
                self.client.last_sip_status_code
                if self.client is not None and 300 <= self.client.last_sip_status_code <= 699 else 0)
            sip_status = (
                failure_sip_status
                if self.plan.initial_selection
                and self.source.native_caller
                and not source_dialog_is_answered(self.preanswered)
                and failure_sip_status
                else 0
            )
            failure_endpoint = failure_destination = None
            if self.plan.account is not None:
                from .phone_policy_runtime import phone_failure_destination
                from .fsm import sip_failure_response

                failure_endpoint = endpoint_directory(self.runtime.hass).get(self.plan.account.endpoint_id)
                if failure_endpoint is not None:
                    failure_destination = (failure_endpoint.policy.no_answer if reason == TerminalReason.TIMEOUT.value
                        else phone_failure_destination(failure_endpoint,
                            failure_sip_status or sip_failure_response(reason)[0]))
            if (
                self.plan.decision.action is RouteAction.FORWARD
                and self.client is not None
            ):
                from .phone_policy_runtime import phone_failure_destination

                metadata = self.plan.decision.entry.metadata if self.plan.decision.entry else {}
                failure_endpoint = endpoint_directory(self.runtime.hass).get(
                    str(metadata.get("endpoint_id") or "")
                )
                failure_destination = phone_failure_destination(
                    failure_endpoint, self.client.last_sip_status_code
                )
            await self.cleanup_failed_route(reason)
            if self.plan.decision.action is RouteAction.GROUP and self.plan.decision.entry is not None:
                fallback = str(self.plan.decision.entry.metadata.get("fallback_destination") or "")
                if fallback:
                    from .route_decisions import dispatch_route_fallback
                    continuation = dispatch_route_fallback(self.runtime.hass, session, fallback,
                        on_failure=self.plan.on_failure)
                    if continuation:
                        return TransferResult("rerouted", fallback, reason, continuation=continuation)
            if failure_destination is not None:
                from .phone_policy_runtime import dispatch_phone_failure

                continuation = dispatch_phone_failure(
                    self.runtime.hass, session, failure_endpoint, failure_destination,
                    on_failure=self.plan.on_failure, failed_forward=asyncio.current_task())
                if continuation:
                    # The same session owns the next route. Return without an
                    # await so finally releases this attempt's forward claim
                    # before the new route task starts.
                    return TransferResult("rerouted", destination, reason, continuation=continuation)
            resumed = await async_abort_route(
                self.abort,
                RouteAbortIntent(
                    reason,
                    "terminate"
                    if (self.owned_sip_source is not None and self.owned_sip_source.adopted)
                    or (self.source.local is not None
                    and self.source.local.committed
                    and application is None)
                    else self.plan.on_failure,
                    sip_status=sip_status,
                ),
            )
            trace_call(
                _LOGGER,
                self.source.call_id,
                "Forward to %s failed (%s); failure policy=%s, original call resumed=%s",
                destination,
                reason
                if reason in {value.value for value in TerminalReason}
                else type(err).__name__,
                self.plan.on_failure,
                resumed,
            )
            return TransferResult(
                "failed_resumed" if resumed else "ended", destination, reason,
                sip_status=failure_sip_status,
            )
        finally:
            self.artifacts.forward_claim = False
