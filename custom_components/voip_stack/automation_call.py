"""An automation controls one call while the PBX retains signaling and media."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import ClassVar

from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .authorization import async_require_service_admin
from .automation_context import current_execution
from .call_projection import publish_bridge_projection
from .call_deadlines import CallDeadline
from .duration_policy import resolve_service_timeout, validate_duration
from .endpoint_lifecycle import call_registry
from .endpoint_session import TerminationIntent
from .fsm import CallState
from .runtime_data import call_runtime_artifacts

from .local_application import LocalCallApplication, attach_local_application


def application_idle_duration(contact) -> float:
    """Validate the configured duration for the actual application owner."""
    policy = "announcement_timeout" if contact.metadata.get("virtual_endpoint") == "paging" else "automation_timeout"
    return validate_duration(contact.metadata.get("automation_timeout"), policy)


@dataclass(slots=True)
class AutomationCall(LocalCallApplication):
    """Call-scoped automation control over the common local media owner."""

    owner: ClassVar[str] = "automation"
    controller: str = ""
    deadline: CallDeadline | None = None
    operation: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

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
        delay = application_idle_duration(self.contact)
        self.deadline = CallDeadline(
            call_registry(self.hass), self.session, f"deadline:automation:{self.session.call_id}",
            lambda: self.session.create_task(self.expire(), name="automation-timeout"),
        )
        self.deadline.arm(self.hass.loop, delay)

    async def expire(self) -> None:
        self.deadline = None
        self.require_current()
        fallback = str(self.contact.metadata.get("fallback_destination") or "")
        if fallback and not self.outgoing:
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
            except BaseException:
                if self.outgoing:
                    await call_registry(self.hass).terminate_call_wait(
                        self.session.call_id, reason="automation_action_failed"
                    )
                raise
            finally:
                self.operation = None
                if self.media is not None:
                    self.media.discard_output()
                self.arm_deadline()

    async def _play_announcement(self, call: ServiceCall, stream_factory) -> None:
        """Share ownership, readiness and final RTP drain for speech and files."""
        from contextlib import aclosing

        async def play() -> None:
            await self.answer()
            self.require_current()
            if self.playback is not None:
                await self.playback.wait_ready()
                self.require_current()
            assert self.media is not None
            if not self.media.can_send:
                raise ServiceValidationError("This caller cannot receive announcement audio")
            errors = self.media.counters["tx_error"]
            suppressed = self.media.counters["tx_suppressed"]
            async with aclosing(stream_factory()) as chunks:
                await self.media.play_pcm_stream(chunks)
            if self.outgoing:
                await self.media.drain_output()
                if self.media.counters["tx_error"] != errors or self.media.counters["tx_suppressed"] != suppressed:
                    raise ServiceValidationError("Announcement audio could not be transmitted")
            else:
                await self.media.tx_queue.join()
            self.require_current()

        await self.run_operation(call, play, timeout=resolve_service_timeout(self.hass, "media_timeout", call.data.get("timeout")))

    async def say(self, call: ServiceCall) -> None:
        await self._play_announcement(call, lambda: self.speech_stream(
            call.data["message"], call.data["tts_entity_id"], call.data.get("language"), call.data.get("options")))

    async def play_media(self, call: ServiceCall) -> None:
        from .announcement_media import async_audio_stream

        await self._play_announcement(
            call, lambda: async_audio_stream(self.hass, call.data["media"]["media_content_id"])
        )

    async def wait_unanswered(self, call: ServiceCall) -> None:
        """Keep the existing ringing application unaccepted without media."""
        if self.answered or self.session.state != CallState.RINGING.value:
            raise ServiceValidationError("Wait without answering requires an unanswered automation call")

        async def wait() -> None:
            self.require_current()
            await asyncio.sleep(resolve_service_timeout(self.hass, "unanswered_wait", call.data.get("duration")))
            self.require_current()

        await self.run_operation(call, wait, timeout=None)

    async def wait_for_dtmf(self, call: ServiceCall) -> dict:
        from .const import SIP_DTMF_EVENT
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
            inter_digit_timeout = call.data.get("inter_digit_timeout")
            digit_timer = None

            @callback
            def finish_gap():
                if not completed.done():
                    completed.set_result({"status": "timeout", "digits": collected})

            @callback
            def receive(event):
                nonlocal collected, digit_timer
                data = event.data
                if (
                    data.get("call_id") != self.session.call_id
                    or data.get("generation") != self.session.generation
                    or data.get("source_leg") != ("callee" if self.outgoing else "caller")
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
                elif inter_digit_timeout is not None:
                    if digit_timer is not None:
                        digit_timer.cancel()
                    digit_timer = self.hass.loop.call_later(float(inter_digit_timeout), finish_gap)

            remove = self.hass.bus.async_listen(SIP_DTMF_EVENT, receive)
            try:
                async with asyncio.timeout(resolve_service_timeout(self.hass, "dtmf_timeout", call.data.get("timeout"))):
                    return await completed
            except TimeoutError:
                return {"status": "timeout", "digits": collected}
            finally:
                if digit_timer is not None:
                    digit_timer.cancel()
                remove()

        result = await self.run_operation(call, collect, timeout=None)
        if execution is not None:
            execution.dtmf_result = result
        return result

    async def send_dtmf(self, call: ServiceCall) -> dict:
        """Serialize keypad input with speech, waits and call cleanup."""
        from .dtmf import send_dtmf_sequence

        async def send():
            await self.answer()
            self.require_current()
            media = self.media
            assert media is not None
            digits = str(call.data["digits"]).upper()
            duration_ms = int(call.data.get("duration_ms", 160))
            gap_ms = int(call.data.get("gap_ms", 100))

            def require_media():
                self.require_current()
                if (self.media is not media or self.session.state != CallState.IN_CALL.value
                        or call_registry(self.hass).resource_for(self.session.call_id, "relay") is not media):
                    raise ServiceValidationError("The call's application media owner changed")

            if not await send_dtmf_sequence(digits, duration_ms=duration_ms, gap_ms=gap_ms,
                    schedule=media.schedule_dtmf, require_current=require_media):
                raise ServiceValidationError("The negotiated media owner could not send keypad input")
            return {"call_id": self.session.call_id, "generation": self.session.generation,
                    "digits": digits, "sent": len(digits)}

        return await self.run_operation(call, send, timeout=None)

    async def forward(self, destination: str, *, on_failure: str = "resume", trunk_id: str = "", ringing_timeout: float | None = None) -> None:
        self.require_current()
        if self.outgoing:
            raise ServiceValidationError("Forward is supported for incoming automation calls")
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
                **({"trunk_id": trunk_id} if trunk_id else {}),
                **({"ringing_timeout": ringing_timeout} if ringing_timeout is not None else {}),
            )
        except BaseException:
            self.arm_deadline()
            raise

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


def attach_automation_application(hass, session, invite, contact, local_ip, *, controller="", local_source=None, previous=None) -> AutomationCall:
    """Attach Automation control to the shared local application media owner."""
    try:
        application_idle_duration(contact)
    except ValueError as error:
        raise ServiceValidationError(str(error)) from error
    return attach_local_application(AutomationCall, hass, session, invite, contact, local_ip,
                                    controller=controller, local_source=local_source, previous=previous)


def queue_automation_request(application: AutomationCall) -> None:
    """Publish the accepted application target, suppressing superseded routes."""
    session = application.session
    registry = call_registry(application.hass)
    revision, contact = session.revision, application.contact

    def publish() -> None:
        if (not session.live or application.contact is not contact
                or not registry.is_current(session.call_id, revision=revision, owner="automation")):
            return
        application.arm_deadline()
        publish_bridge_projection(
            application.hass, session, direction="incoming", event_type="automation_requested",
            route_kind="automation", generation=session.generation,
            called_extension=contact.extension, destination=contact.display_name,
        )

    application.hass.loop.call_soon(publish)


async def take_over_ringing_phone(call: ServiceCall, call_id: str) -> AutomationCall:
    """Let a native call execution explicitly claim its unanswered SIP caller.

    An arbitrary call_id is insufficient. The execution must still refer to
    this generation and the phone must not have answered in the meantime.
    """
    from .peer_snapshot import async_advertise_host
    from .roster import RosterEntry
    from .browser_phone_runtime import _release_ha_softphone_claim

    execution = current_execution.get()
    registry = call_registry(call.hass)
    if (execution is None or not execution.active
            or execution.token.call_id != registry.resolve_session_id(call_id)
            or execution.snapshot.get("automation_control") == "observed"):
        raise ServiceValidationError("The selected phone has no call handled by an automation")
    local_ip = await async_advertise_host(call.hass)
    session = registry.get_session(call_id, generation=execution.token.generation)
    invite = registry.artifact_for(call_id, "pending_invite")
    from .pending_sip_destination import PendingSipDestination
    native_destination = PendingSipDestination.capture(call.hass, registry, session)
    if native_destination is not None:
        invite = native_destination.invite
    if (not local_ip or session is None or not session.live
            or session.owner not in {"ha_softphone", "local_bridge", "bridge"}
            or session.state not in {CallState.RINGING.value, CallState.REMOTE_RINGING.value}):
        raise ServiceValidationError("The triggering call is no longer an unanswered Home Assistant phone call")
    local_source = None
    if session.owner == "local_bridge":
        from .local_softphone_runtime import PendingLocalSource, local_softphone_bridge
        from .runtime_data import require_runtime_data

        bridge = local_softphone_bridge(call.hass)
        snapshot = bridge.get_call(call_id) if bridge is not None else None
        if snapshot is not None:
            local_source = PendingLocalSource(call.hass, snapshot, local_ip, int(
                require_runtime_data(call.hass).transport_config.get("sip_port", 5060)
            ))
            invite = local_source.invite
    elif invite is None and session.owner == "ha_softphone" and session.state == CallState.REMOTE_RINGING.value:
        from .local_softphone_runtime import PendingLocalSource
        from .runtime_data import require_runtime_data

        local_source = PendingLocalSource(call.hass, None, local_ip, int(
            require_runtime_data(call.hass).transport_config.get("sip_port", 5060)
        ), call_id=call_id)
        invite = local_source.invite
    if invite is None:
        raise ServiceValidationError("The triggering caller has no media source available")
    revision = session.revision
    endpoint_id = str(session.metadata.get("endpoint_id") or "")
    contact = RosterEntry(session.callee or invite.target, metadata={"virtual_endpoint": "automation",
        "automation_timeout": resolve_service_timeout(call.hass, "automation_timeout")})
    claimed = registry.transition(
        call_id, owner="automation", state=CallState.RINGING.value, route_kind="automation",
        expected_owner=session.owner, expected_revision=revision,
        expected_generation=execution.token.generation,
    )
    if claimed is None:
        raise ServiceValidationError("The call changed while automation control was being acquired")
    registry.set_pending_invite(call_id, invite)
    try:
        if native_destination is not None:
            await native_destination.retire()
        if local_source is not None:
            await local_source.retire_destination()
        application = attach_automation_application(
            call.hass, session, invite, contact, local_ip, controller=execution.controller,
            local_source=local_source,
        )
    except BaseException:
        if registry.get_session(call_id, generation=execution.token.generation) is session:
            await registry.terminate_call_wait(call_id, intent=TerminationIntent("application_takeover_failed"))
        raise
    if endpoint_id and local_source is None and native_destination is None:
        if not _release_ha_softphone_claim(call.hass, call_id, endpoint_id=endpoint_id):
            registry.release_endpoint_claim(call_id, endpoint_id)
    # Application control remains valid after the call leaves ringing.
    execution.ringing_guard = None
    publish_bridge_projection(
        call.hass, session, direction="incoming", event_type="forwarding", route_kind="automation",
    )
    return application


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
        application = await take_over_ringing_phone(call, call_id)
    return application, call


async def async_tts_say(call: ServiceCall) -> None:
    application, call = await resolve_application_action(call)
    try:
        await application.say(call)
    except Exception as err:
        raise ServiceValidationError(f"Announcement failed: {err}") from err


async def async_play_media(call: ServiceCall) -> None:
    application, call = await resolve_application_action(call)
    try:
        await application.play_media(call)
    except Exception as err:
        raise ServiceValidationError("Audio file playback failed; check the media source and call state") from err


async def async_wait_unanswered(call: ServiceCall) -> None:
    application, call = await resolve_application_action(call)
    await application.wait_unanswered(call)


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
        call.data["destination"], on_failure=call.data.get("on_failure", "resume"),
        trunk_id=str(call.data.get("trunk_id") or ""),
        ringing_timeout=call.data.get("ringing_timeout"),
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
    application = attach_automation_application(runtime.hass, session, invite, decision.entry, runtime.local_ip)
    queue_automation_request(application)
    return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)
