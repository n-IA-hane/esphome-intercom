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
    "dtmf_received": frozenset({"dtmf"}),
}
_LOGGER = logging.getLogger(__name__)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional("caller"): cv.string,
        vol.Optional("destination"): cv.string,
        vol.Optional("ingress"): vol.In(["trunk", "extension"]),
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
        return vol.Schema({vol.Optional("options", default=dict): OPTIONS_SCHEMA})(
            config
        )

    def __init__(self, hass, config: TriggerConfig):
        super().__init__(hass, config)
        self._kind = config.key.rsplit(".", 1)[-1]
        self._options = config.options or {}

    async def async_attach_runner(self, run_action, did_not_trigger=None):
        runs: set[asyncio.Task] = set()
        deadlines: dict[CallToken, CallDeadline] = {}
        registration = registration_data(self._hass)
        if self._kind == "route_requested":
            registration.route_trigger_filters[self] = dict(self._options)

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
                    TerminationIntent.bye("local_hangup"),
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
                duration.total_seconds() if duration is not None else 30,
            )

        remove = self._hass.bus.async_listen(
            SIP_DTMF_EVENT if self._kind == "dtmf_received" else CALL_EVENT, receive
        )

        @callback
        def detach():
            remove()
            registration.route_trigger_filters.pop(self, None)
            for deadline in tuple(deadlines.values()):
                deadline.cancel()
            # HA owns running automations; their completion releases call control.

        return detach


async def async_get_triggers(hass):
    return dict.fromkeys(TRIGGER_EVENTS, VoipCallTrigger)
