"""Native conditions for the triggering call and configured phone availability."""

import voluptuous as vol

from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.condition import Condition

from .automation_context import current_execution
from .endpoint_lifecycle import call_registry
from .phone_endpoint import EndpointAvailability


class CallStateCondition(Condition):
    @classmethod
    async def async_validate_config(cls, hass, config):
        return vol.Schema(
            {
                vol.Required("options"): {
                    vol.Required("state"): vol.In(
                        [
                            "calling",
                            "ringing",
                            "remote_ringing",
                            "in_call",
                            "idle",
                        ]
                    )
                }
            }
        )(config)

    def __init__(self, hass, config):
        super().__init__(hass, config)
        self._state = config.options["state"]

    def _async_check(self, **kwargs):
        execution = current_execution.get()
        if execution is None or not execution.active:
            return False
        session = call_registry(self._hass).get_session(execution.token.call_id)
        return (
            session is not None
            and session.generation == execution.token.generation
            and session.state == self._state
        )


class CallOriginCondition(Condition):
    @classmethod
    async def async_validate_config(cls, hass, config):
        return vol.Schema(
            {
                vol.Required("options"): {
                    vol.Required("ingress"): vol.In(["extension", "trunk"])
                }
            }
        )(config)

    def __init__(self, hass, config):
        super().__init__(hass, config)
        self._origin = config.options["ingress"]

    def _async_check(self, **kwargs):
        execution = current_execution.get()
        return (
            execution is not None and execution.snapshot.get("ingress") == self._origin
        )


class PhoneAvailableCondition(Condition):
    @classmethod
    async def async_validate_config(cls, hass, config):
        return vol.Schema(
            {vol.Required("options"): {vol.Required("phone"): cv.string}}
        )(config)

    def __init__(self, hass, config):
        super().__init__(hass, config)
        self._phone = config.options["phone"]

    def _async_check(self, **kwargs):
        from .phone_state import resolve_phone
        from homeassistant.exceptions import ServiceValidationError
        try:
            phone = resolve_phone(self._hass, self._phone)
        except ServiceValidationError:
            return False
        return (
            phone is not None
            and phone.availability is EndpointAvailability.AVAILABLE
            and not phone.dnd
            and not phone.active_call_id
        )


async def async_get_conditions(hass):
    return {
        "is_call_state": CallStateCondition,
        "is_call_origin": CallOriginCondition,
        "is_phone_available": PhoneAvailableCondition,
        "is_dtmf_result": DtmfResultCondition,
        "is_phone_state": PhoneStateCondition,
        "has_ringing_call": HasRingingCallCondition,
        "has_voicemail": HasVoicemailCondition,
    }


class DtmfResultCondition(Condition):
    @classmethod
    async def async_validate_config(cls, hass, config):
        return vol.Schema(
            {
                vol.Required("options"): {
                    vol.Required("status", default="received"): vol.In(
                        ["received", "timeout"]
                    ),
                    vol.Optional("digits"): cv.string,
                }
            }
        )(config)

    def __init__(self, hass, config):
        super().__init__(hass, config)
        self._options = config.options

    def _async_check(self, **kwargs):
        execution = current_execution.get()
        result = execution.dtmf_result if execution is not None else None
        return (
            result is not None
            and result["status"] == self._options["status"]
            and (
                "digits" not in self._options
                or result["digits"] == self._options["digits"]
            )
        )


class PhoneStateCondition(Condition):
    """Read a selected phone independently of the automation's call context."""

    @classmethod
    async def async_validate_config(cls, hass, config):
        return vol.Schema({vol.Required("options"): {
            vol.Required("phone"): cv.string,
            vol.Required("state"): vol.In(["idle", "unavailable", "calling", "ringing", "in_call", "held"]),
        }})(config)

    def __init__(self, hass, config):
        super().__init__(hass, config)
        self._options = config.options

    def _async_check(self, **kwargs):
        from .phone_state import phone_states, resolve_phone
        from homeassistant.exceptions import ServiceValidationError
        try:
            phone = resolve_phone(self._hass, self._options["phone"])
        except ServiceValidationError:
            return False
        return phone_states(self._hass)[phone.endpoint_id]["state"] == self._options["state"]


class HasRingingCallCondition(PhoneStateCondition):
    @classmethod
    async def async_validate_config(cls, hass, config):
        return vol.Schema({vol.Required("options"): {vol.Required("phone"): cv.string}})(config)

    def _async_check(self, **kwargs):
        from .phone_state import phone_states, resolve_phone
        from homeassistant.exceptions import ServiceValidationError
        try:
            phone = resolve_phone(self._hass, self._options["phone"])
        except ServiceValidationError:
            return False
        return any(item["pickup_available"] for item in phone_states(self._hass)[phone.endpoint_id]["calls"])


class HasVoicemailCondition(PhoneStateCondition):
    @classmethod
    async def async_validate_config(cls, hass, config):
        return vol.Schema({vol.Required("options"): {
            vol.Required("phone"): cv.string,
            vol.Optional("unread_only", default=True): cv.boolean,
        }})(config)

    def _async_check(self, **kwargs):
        from .phone_state import mailbox_state, resolve_phone
        from homeassistant.exceptions import ServiceValidationError
        try:
            phone = resolve_phone(self._hass, self._options["phone"])
        except ServiceValidationError:
            return False
        mailbox = mailbox_state(self._hass, phone.endpoint_id)
        return bool(mailbox["new"] or (not self._options.get("unread_only", True) and mailbox["old"]))
