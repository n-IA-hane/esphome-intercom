"""Native conditions for the triggering call and configured phone availability."""

import voluptuous as vol

from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.condition import Condition

from .automation_context import current_execution
from .endpoint_lifecycle import call_registry
from .phone_endpoint import EndpointAvailability
from .runtime_data import endpoint_directory


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
        phone = endpoint_directory(self._hass).resolve(self._phone)
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
