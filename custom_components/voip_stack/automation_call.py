"""An automation controls one call while the PBX retains signaling and media."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .authorization import async_require_service_admin
from .automation_context import current_execution
from .browser_playback import BrowserPlayback
from .call_projection import publish_bridge_projection
from .endpoint_lifecycle import call_registry
from .endpoint_session import CleanupStage, EndpointCallSession, TerminationIntent
from .fsm import CallState
from .inbound_answer import async_commit_runtime_answer
from .local_call_media import LocalCallMedia
from .media_ports import RtpPortReservation, take_delayed_offer_ports
from .runtime_data import call_runtime_artifacts
from .core.sdp import build_answer_directional, first_offered_dtmf_format
from .sip_runtime import send_final_response

if TYPE_CHECKING:
    from .roster import RosterEntry
    from .sip_listener import SipInvite


@dataclass(slots=True)
class AutomationCall:
    """Call-scoped application control, never an independent SIP dialog."""

    hass: HomeAssistant
    session: EndpointCallSession
    invite: SipInvite
    contact: RosterEntry
    local_ip: str
    media: LocalCallMedia | None = None
    playback: BrowserPlayback | None = None
    controller: str = ""
    answered: bool = False
    deadline: asyncio.TimerHandle | None = None
    operation: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def require_current(self, generation: int | None = None) -> None:
        registry = call_registry(self.hass)
        if (
            not registry.is_generation_current(
                self.session.call_id, self.session.generation
            )
            or self.session.owner != "automation"
            or (generation is not None and generation != self.session.generation)
        ):
            raise ServiceValidationError(
                "This automation call has ended or changed owner"
            )

    def claim(self, call: ServiceCall) -> None:
        self.require_current(int(call.data.get("expected_generation", -1)))
        execution = current_execution.get()
        controller = execution.controller if execution is not None else call.context.id
        if self.controller and self.controller != controller:
            raise ServiceValidationError(
                "Another automation already controls this call"
            )
        self.controller = controller

    def cancel_deadline(self) -> None:
        if self.deadline is not None:
            self.deadline.cancel()
            self.deadline = None

    def arm_deadline(self) -> None:
        self.cancel_deadline()
        if not self.session.live or self.session.owner != "automation":
            return
        execution = current_execution.get()
        if execution is not None and execution.active and self.controller == execution.controller:
            # The native HA execution owns its waits until completion or cancellation.
            return
        delay = float(self.contact.metadata.get("automation_timeout", 30))
        self.deadline = self.hass.loop.call_later(
            delay,
            lambda: self.session.create_task(self.expire(), name="automation-timeout"),
        )

    async def expire(self) -> None:
        self.deadline = None
        self.require_current()
        fallback = str(self.contact.metadata.get("fallback_destination") or "")
        if fallback:
            try:
                await self.forward(fallback, on_failure="terminate")
            except Exception:
                self.cancel_deadline()
                call_registry(self.hass).request_termination(
                    self.session.call_id,
                    TerminationIntent("fallback_failed"),
                    generation=self.session.generation,
                )
        else:
            intent = (
                TerminationIntent.bye("timeout")
                if self.answered
                else TerminationIntent.final_response("timeout", 480)
            )
            call_registry(self.hass).request_termination(
                self.session.call_id, intent, generation=self.session.generation
            )

    async def close(self, _reason: str) -> None:
        self.cancel_deadline()
        if self.operation is not None and self.operation is not asyncio.current_task():
            self.operation.cancel()
            await asyncio.gather(self.operation, return_exceptions=True)

    async def answer(self) -> None:
        self.require_current()
        if self.answered and self.media is not None:
            return
        registry = call_registry(self.hass)
        preanswered = registry.resource_for(self.session.call_id, "preanswered")
        reservation = (
            (preanswered or {}).get("rtp_reservation")
            or take_delayed_offer_ports(registry, self.session.call_id)
            or RtpPortReservation.allocate(self.hass)
        )

        async def complete(reason: str) -> None:
            registry.request_termination(
                self.session.call_id,
                TerminationIntent(reason),
                generation=self.session.generation,
            )

        media = LocalCallMedia(
            self.hass,
            invite=self.invite,
            local_rtp_port=reservation.ports[0],
            reservation=reservation,
            on_complete=complete,
            rtp_source=(preanswered or {}).get("audio_rtp_source"),
        )
        from .dtmf_events import publish_dtmf_event

        media.on_dtmf = lambda side, digit, transport: publish_dtmf_event(
            self.hass,
            call_id=self.session.call_id,
            dest_call_id="",
            caller=self.session.caller,
            callee=self.session.callee,
            side=side,
            digit=digit,
            transport=transport,
        )
        try:
            await media.start()
            self.require_current()
            registry.attach_relay(self.session.call_id, media)
            self.media = media
            dtmf = first_offered_dtmf_format(self.invite.remote_sdp)
            if dtmf is not None:
                dtmf = replace(dtmf, events=dtmf.events & frozenset(range(16)))
            answer = build_answer_directional(
                self.local_ip,
                self.local_ip,
                reservation.ports[0],
                self.invite.send_format,
                self.invite.recv_format,
                dtmf=dtmf,
                remote_sdp=self.invite.remote_sdp,
            )
            result = await async_commit_runtime_answer(
                registry,
                self.session.call_id,
                answer,
                send_final_response=send_final_response,
                response_context=self.hass,
                owner="automation",
                caller=self.invite.caller,
                callee=self.contact.display_name,
                route_kind="automation",
                response_already_sent=self.answered,
            )
            if not result.committed:
                raise ServiceValidationError(
                    "The caller ended before the announcement could start"
                )
            self.answered = True
            if preanswered is None:
                registry.attach_media(
                    self.session.call_id,
                    {
                        "rtp_reservation": reservation,
                        "local_rtp_port": reservation.ports[0],
                        "final_response_sent": True,
                        "early_answer_sdp": answer,
                        "audio_rtp_source": media.rtp_source,
                    },
                    provisional=True,
                )
            else:
                registry.update_media(
                    self.session.call_id,
                    provisional=True,
                    final_response_sent=True,
                    early_answer_sdp=answer,
                    audio_rtp_source=media.rtp_source,
                )
            # The shared preanswered resource now owns the reservation through handoff.
            media.release_reservation_on_stop = False
            publish_bridge_projection(
                self.hass,
                self.session,
                event_type="answered",
                direction="incoming",
                route_kind="automation",
            )
        except BaseException:
            await media.stop()
            raise

    async def run_operation(
        self, call: ServiceCall, operation, *, timeout: float | None
    ):
        """Serialize application work under the existing call cleanup owner."""
        self.claim(call)
        if self.lock.locked():
            raise ServiceValidationError("An action is already running on this call")
        async with self.lock:
            self.cancel_deadline()
            self.operation = self.session.create_task(
                operation(), name=f"automation-{call.service}"
            )
            try:
                async with asyncio.timeout(timeout):
                    return await self.operation
            finally:
                self.operation = None
                if self.media is not None:
                    self.media.discard_output()
                self.arm_deadline()

    async def say(self, call: ServiceCall) -> None:
        async def play() -> None:
            from homeassistant.components import tts

            await self.answer()
            self.require_current()
            if self.playback is not None:
                await self.playback.wait_ready()
                self.require_current()
            assert self.media is not None
            if not self.media.can_send:
                raise ServiceValidationError(
                    "This caller cannot receive announcement audio"
                )
            options = {**call.data.get("options", {}), **self.media._tts_audio_output()}
            stream = tts.async_create_stream(
                self.hass,
                call.data["tts_entity_id"],
                call.data.get("language"),
                options,
            )
            stream.async_set_message(call.data["message"])
            await self.media.play_pcm_stream(stream.async_stream_result())
            await self.media.tx_queue.join()
            self.require_current()

        await self.run_operation(
            call, play, timeout=float(call.data.get("timeout", 120))
        )

    async def wait_for_dtmf(self, call: ServiceCall) -> dict:
        from .websocket_api import SIP_DTMF_EVENT
        from homeassistant.core import callback

        execution = current_execution.get()
        if execution is not None:
            execution.dtmf_result = None

        async def collect():
            await self.answer()
            collected = ""
            completed = self.hass.loop.create_future()
            maximum = int(call.data.get("max_digits", 1))
            terminator = str(call.data.get("terminator", "#"))

            @callback
            def receive(event):
                nonlocal collected
                data = event.data
                if (
                    data.get("call_id") != self.session.call_id
                    or data.get("generation") != self.session.generation
                    or data.get("source_leg") != "caller"
                    or completed.done()
                ):
                    return
                digit = str(data.get("digit") or "")
                if len(digit) != 1 or digit not in "0123456789*#ABCD":
                    return
                if digit != terminator:
                    collected += digit
                if digit == terminator or len(collected) >= maximum:
                    completed.set_result({"status": "received", "digits": collected})

            remove = self.hass.bus.async_listen(SIP_DTMF_EVENT, receive)
            try:
                async with asyncio.timeout(float(call.data.get("timeout", 10))):
                    return await completed
            except TimeoutError:
                return {"status": "timeout", "digits": collected}
            finally:
                remove()

        result = await self.run_operation(call, collect, timeout=None)
        if execution is not None:
            execution.dtmf_result = result
        return result

    async def forward(self, destination: str, *, on_failure: str = "resume") -> None:
        self.require_current()
        if self.lock.locked():
            raise ServiceValidationError(
                "Wait for the current announcement before forwarding"
            )
        self.cancel_deadline()
        callback = call_runtime_artifacts(self.hass).forward_call
        if callback is None:
            raise ServiceValidationError("SIP endpoint is not running")
        try:
            await callback(
                call_id=self.session.call_id,
                destination=destination,
                on_failure=on_failure,
            )
        except BaseException:
            self.arm_deadline()
            raise

    async def release_media_for_forward(self) -> None:
        """Release a local transport only when another media owner will bind it."""
        if self.media is None:
            return
        media = self.media
        await media.stop()
        call_registry(self.hass).release_resource(
            self.session.call_id,
            f"relay:{self.session.call_id}",
            value=media,
            generation=self.session.generation,
        )
        self.media = None

    async def resume(self) -> None:
        if self.answered and self.media is None:
            await self.answer()
        self.arm_deadline()
        publish_bridge_projection(
            self.hass,
            self.session,
            direction="incoming",
            event_type="state_changed",
            route_kind="automation",
        )


def automation_call(hass, call_id: str) -> AutomationCall | None:
    return call_registry(hass).resource_for(call_id, "automation")


async def resolve_application_action(call: ServiceCall):
    """Use the trigger's call or an explicitly selected phone's active call."""
    call_id = str(call.data.get("call_id") or "")
    if not call_id:
        if not call.data.get("device_id"):
            raise ServiceValidationError("Select a phone when running this action without a VoIP call trigger")
        from .runtime_data import require_runtime_data

        runtime = require_runtime_data(call.hass)
        phone = await runtime.phones.resolve_source(call)
        endpoint = runtime.endpoints.get(phone.endpoint_id)
        call_id = endpoint.active_call_id if endpoint is not None else ""
        application = automation_call(call.hass, call_id)
        if application is not None:
            call = ServiceCall(call.hass, call.domain, call.service, {
                **call.data, "call_id": call_id,
                "expected_generation": application.session.generation,
            }, context=call.context, return_response=call.return_response)
    else:
        application = automation_call(call.hass, call_id)
    if application is None:
        raise ServiceValidationError("The selected phone has no call handled by an automation")
    return application, call


async def async_tts_say(call: ServiceCall) -> None:
    application, call = await resolve_application_action(call)
    try:
        await application.say(call)
    except Exception as err:
        raise ServiceValidationError(f"Announcement failed: {err}") from err


async def async_wait_for_dtmf(call: ServiceCall) -> dict:
    application, call = await resolve_application_action(call)
    return await application.wait_for_dtmf(call)


async def async_forward_automation_call(call: ServiceCall) -> bool:
    application = automation_call(call.hass, str(call.data.get("call_id") or ""))
    if application is None:
        return False
    await async_require_service_admin(call.hass, call)
    application.claim(call)
    await application.forward(
        call.data["destination"], on_failure=call.data.get("on_failure", "resume")
    )
    return True


async def route_automation_call(runtime, invite, decision, registry, source_endpoint):
    """Reserve a call and announce its immutable automation event before answer."""
    from .inbound_routing.local import _claim_source
    from .sip_listener import SipInviteResult

    if busy := await _claim_source(
        hass=runtime.hass,
        registry=registry,
        invite=invite,
        source_endpoint=source_endpoint,
        state=CallState.RINGING.value,
        route_kind="automation",
    ):
        return busy
    preanswered = registry.resource_for(invite.call_id, "preanswered")
    answered = bool(preanswered and preanswered.get("final_response_sent"))
    session = registry.upsert(
        invite.call_id,
        state=CallState.IN_CALL.value if answered else CallState.RINGING.value,
        owner="automation",
        caller=invite.caller,
        callee=decision.entry.display_name,
        route_kind="automation",
    )
    registry.set_pending_invite(invite.call_id, invite)
    application = AutomationCall(
        runtime.hass,
        session,
        invite,
        decision.entry,
        runtime.local_ip,
        answered=answered,
        playback=registry.resource_for(invite.call_id, "browser_playback"),
    )
    registry.own_resource(
        invite.call_id,
        f"automation:{invite.call_id}",
        application,
        application.close,
        stage=CleanupStage.MEDIA,
        generation=session.generation,
    )

    def publish() -> None:
        if not registry.is_generation_current(session.call_id, session.generation):
            return
        application.arm_deadline()
        publish_bridge_projection(
            runtime.hass,
            session,
            direction="incoming",
            event_type="automation_requested",
            route_kind="automation",
            generation=session.generation,
            called_extension=decision.entry.extension,
            destination=decision.entry.display_name,
        )

    runtime.hass.loop.call_soon(publish)
    return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)
