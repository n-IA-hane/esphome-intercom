"""Native Home Assistant call triggers backed by canonical PBX events."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import logging

import voluptuous as vol

from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.trigger import Trigger, TriggerConfig

from .automation_context import CallExecution, current_execution
from .automation_routing import automation_event_type, matches_call
from .call_deadlines import CallDeadline
from .endpoint_lifecycle import call_registry
from .endpoint_session import CallToken, TerminationIntent
from .websocket_api import CALL_EVENT, SIP_DTMF_EVENT
from .runtime_data import registration_data


TRIGGER_EVENTS = {
    "call_started": frozenset({"outgoing_call"}),
    "call_received": frozenset({"ringing", "automation_requested"}),
    "call_unanswered": frozenset({"ringing", "remote_ringing"}),
    "route_requested": frozenset({"route_requested"}),
    "call_connected": frozenset({"answered", "connected"}),
    "call_ended": frozenset({"ended", "missed", "failed"}),
    "call_picked_up": frozenset({"picked_up"}),
    "dtmf_received": frozenset({"dtmf"}),
}
_LOGGER = logging.getLogger(__name__)


def _trunk_selector_id(hass, value):
    """Convert the native line device selector to its stable trunk identity."""
    from homeassistant.helpers import device_registry as dr
    device = dr.async_get(hass).async_get(str(value))
    if device is None:
        return value
    identities = [identity.removeprefix("trunk:") for domain, identity in device.identifiers
                  if domain == "voip_stack" and identity.startswith("trunk:")]
    if len(identities) != 1:
        raise ValueError("Select a VoIP Stack SIP trunk")
    return identities[0]

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional("caller"): cv.string,
        vol.Optional("destination"): cv.string,
        vol.Optional("ingress"): vol.In(["trunk", "extension"]),
        vol.Optional("trunk"): cv.string,
        vol.Optional("inbound_trunk_id"): cv.string,
        vol.Optional("outbound_trunk_id"): cv.string,
        vol.Optional("called_number"): cv.string,
        vol.Optional("reason"): cv.string,
        vol.Optional("outcome"): vol.In(["ended", "missed", "failed"]),
        vol.Optional("digit"): vol.In(list("0123456789*#ABCD")),
        vol.Optional("source_leg"): vol.In(["caller", "callee"]),
        vol.Optional("for"): cv.positive_time_period,
    }
)


class VoipCallTrigger(Trigger):
    @classmethod
    async def async_validate_config(cls, hass, config):
        result = vol.Schema({vol.Optional("options", default=dict): OPTIONS_SCHEMA})(
            config
        )
        if result["options"].get("trunk"):
            result["options"]["trunk"] = _trunk_selector_id(hass, result["options"]["trunk"])
        return result

    def __init__(self, hass, config: TriggerConfig):
        super().__init__(hass, config)
        self._kind = config.key.rsplit(".", 1)[-1]
        self._options = dict(config.options or {})
        if self._options.get("trunk"):
            self._options["trunk"] = _trunk_selector_id(hass, self._options["trunk"])

    async def async_attach_runner(self, run_action, did_not_trigger=None):
        runs: set[asyncio.Task] = set()
        deadlines: dict[CallToken, CallDeadline] = {}
        registration = registration_data(self._hass)
        if self._kind == "route_requested":
            registration.route_trigger_filters[self] = dict(self._options)
        if self._kind == "call_unanswered":
            registration.unanswered_trigger_filters[self] = dict(self._options)

        async def finish(task, execution):
            try:
                result = await task
            except asyncio.CancelledError:
                result = None
            except Exception:
                _LOGGER.exception("Call automation execution failed")
                result = None
            finally:
                execution.active = False
            registry = call_registry(self._hass)
            if not registry.is_generation_current(
                execution.token.call_id, execution.token.generation
            ):
                return
            application = registry.resource_for(execution.token.call_id, "automation")
            if application is None or application.controller != execution.controller:
                return
            if application.session.owner != "automation":
                return
            if result is None:
                await application.expire()
            else:
                registry.request_termination(
                    execution.token.call_id,
                    (
                        TerminationIntent.bye("local_hangup")
                        if application.answered
                        else TerminationIntent.final_response("timeout", 480)
                    ),
                    generation=execution.token.generation,
                )

        def dispatch(event, payload):
            token = CallToken(
                str(payload.get("call_id") or ""), int(payload.get("generation") or 0)
            )
            registry = call_registry(self._hass)
            session = registry.get_session(token.call_id)
            if session is not None and session.generation != token.generation:
                return
            context = registry.event_context(token.call_id)
            if (
                session is None
                and context is not None
                and not registry.is_terminated(
                    token.call_id, generation=token.generation
                )
            ):
                return
            if (
                self._kind not in {"dtmf_received", "call_unanswered"}
                and context is not None
            ):
                if not context.accept_trigger(
                    self,
                    int(payload.get("sequence") or 0),
                    str(payload.get("callee") or ""),
                ):
                    return
            execution = CallExecution(token, payload)
            if (
                self._kind == "call_unanswered"
                and session is not None
                and context is not None
            ):
                execution.ringing_guard = (context.sequence, session.callee)
            binding = current_execution.set(execution)
            try:
                task = run_action(
                    {"call": payload},
                    f"Call from {payload.get('caller') or 'unknown'} to {payload.get('callee') or 'unknown'}",
                    event.context,
                )
            finally:
                current_execution.reset(binding)
            observer = self._hass.async_create_task(finish(task, execution))
            runs.add(observer)
            observer.add_done_callback(runs.discard)

        @callback
        def receive(event):
            payload = deepcopy(dict(event.data))
            kind = (
                "dtmf"
                if event.event_type == SIP_DTMF_EVENT
                else automation_event_type(payload)
            )
            if payload.get("scope") == "phone_policy":
                if (self._kind == "call_unanswered" and self._options.get("for") is None
                        and matches_call(payload, self._options)):
                    dispatch(event, payload)
                return
            token = CallToken(
                str(payload.get("call_id") or ""), int(payload.get("generation") or 0)
            )
            matched = kind in TRIGGER_EVENTS[self._kind] and matches_call(
                payload, self._options
            )
            if self._kind != "call_unanswered":
                if matched:
                    dispatch(event, payload)
                return
            previous = deadlines.get(token)
            if previous is not None:
                if kind in {
                    "answered",
                    "connected",
                    "ended",
                    "missed",
                    "failed",
                    "forwarding",
                }:
                    previous.cancel()
                return
            if not matched:
                return
            if self._options.get("for") is None:
                # A phone-configured deadline emits the native no-answer event.
                # Explicit durations retain their independent automation timer.
                return
            registry = call_registry(self._hass)
            session = registry.get_session(token.call_id)
            if session is None or not session.owns(token):
                return
            destination = session.callee

            def expired():
                current = registry.get_session(token.call_id)
                if (
                    current is not None
                    and current.owns(token)
                    and current.state in {"ringing", "remote_ringing"}
                    and current.callee == destination
                ):
                    dispatch(event, payload)

            deadline = CallDeadline(
                registry, session, f"deadline:unanswered:{id(self)}", expired
            )
            deadline.on_close = lambda: deadlines.pop(token, None)
            deadlines[token] = deadline
            duration = self._options.get("for")
            deadline.arm(
                self._hass.loop,
                duration.total_seconds(),
            )

        remove = self._hass.bus.async_listen(
            SIP_DTMF_EVENT if self._kind == "dtmf_received" else CALL_EVENT, receive
        )

        @callback
        def detach():
            remove()
            registration.route_trigger_filters.pop(self, None)
            registration.unanswered_trigger_filters.pop(self, None)
            for deadline in tuple(deadlines.values()):
                deadline.cancel()
            # HA owns running automations; their completion releases call control.

        return detach


class VoicemailReceivedTrigger(Trigger):
    """Run ordinary HA actions after the complete message has been saved."""

    def __init__(self, hass, config: TriggerConfig):
        super().__init__(hass, config)
        self._options = config.options or {}

    @classmethod
    async def async_validate_config(cls, hass, config):
        return vol.Schema({vol.Optional("options", default=dict): vol.Schema({
            vol.Optional("mailbox"): cv.string,
        })})(config)

    async def async_attach_runner(self, run_action, did_not_trigger=None):
        from .mailbox import EVENT_VOICEMAIL_RECEIVED
        from .mailbox_actions import current_message
        from .runtime_data import endpoint_directory

        selector = self._options.get("mailbox")

        @callback
        def receive(event):
            data = dict(event.data)
            if selector:
                endpoints = endpoint_directory(self._hass)
                endpoint = endpoints.by_device_id(selector) or endpoints.resolve(selector)
                if endpoint is None or endpoint.endpoint_id != data.get("mailbox_id"):
                    return
            binding = current_message.set((data["mailbox_id"], data["message_id"]))
            try:
                run_action({"voicemail": data}, "Voicemail received", event.context)
            finally:
                current_message.reset(binding)

        return self._hass.bus.async_listen(EVENT_VOICEMAIL_RECEIVED, receive)


async def async_get_triggers(hass):
    return {**dict.fromkeys(TRIGGER_EVENTS, VoipCallTrigger), "voicemail_received": VoicemailReceivedTrigger,
            "phone_state_changed": PhoneStateTrigger, "mailbox_changed": PhoneStateTrigger}


class PhoneStateTrigger(Trigger):
    """Expose committed phone and mailbox transitions through native HA triggers."""

    def __init__(self, hass, config):
        super().__init__(hass, config)
        self._options = config.options or {}
        self._kind = config.key.rsplit(".", 1)[-1]

    @classmethod
    async def async_validate_config(cls, hass, config):
        return vol.Schema(
            {
                vol.Required("options"): {
                    vol.Required("phone"): cv.string,
                    vol.Optional("from"): vol.In(
                        ["idle", "unavailable", "calling", "ringing", "in_call", "held"]
                    ),
                    vol.Optional("to"): vol.In(
                        ["idle", "unavailable", "calling", "ringing", "in_call", "held"]
                    ),
                }
            }
        )(config)

    async def async_attach_runner(self, run_action, did_not_trigger=None):
        from .phone_state import phone_state_observer, resolve_phone
        from homeassistant.exceptions import ServiceValidationError

        observer = phone_state_observer(self._hass)

        @callback
        def changed(payload, context=None):
            try:
                endpoint = resolve_phone(self._hass, self._options["phone"])
            except ServiceValidationError:
                return
            if endpoint.endpoint_id != payload["endpoint_id"]:
                return
            before, after = payload["previous"] or {}, payload["phone"] or {}
            field = "mailbox" if self._kind == "mailbox_changed" else "state"
            if before.get(field) == after.get(field):
                return
            if self._kind == "phone_state_changed" and any(
                self._options.get(key) is not None and self._options[key] != value
                for key, value in (
                    ("from", before.get("state")),
                    ("to", after.get("state")),
                )
            ):
                return
            call = payload.get("call") if self._kind == "phone_state_changed" else None
            if (
                self._kind == "phone_state_changed"
                and call is None
                and len(after.get("calls", ())) == 1
            ):
                call = after["calls"][0]
            execution = None
            if call is not None and call.get("call_id") and call.get("generation"):
                snapshot = {
                    **call,
                    "endpoint_id": endpoint.endpoint_id,
                    "device_id": endpoint.device_id,
                }
                execution = CallExecution(
                    CallToken(call["call_id"], call["generation"]), snapshot
                )
            binding = current_execution.set(execution)
            try:
                task = run_action(
                    deepcopy({"phone": after, "previous": before, "call": call}),
                    self._kind,
                    context,
                )
                if execution is not None:

                    def finished(_task):
                        execution.active = False

                    task.add_done_callback(finished)
            finally:
                current_execution.reset(binding)

        return observer.subscribe(changed)
