"""Phonebook contacts configured without creating phone devices or entities."""

import voluptuous as vol

from homeassistant.config_entries import ConfigSubentryFlow
from homeassistant.helpers.selector import SelectSelector, TextSelector, NumberSelector

from .contact_config import contact_dicts
from .phonebook_services import contact_from_data, _validate_contact_namespace
from .roster import parse_roster_json


class ContactSubentryFlowHandler(ConfigSubentryFlow):
    def __init__(self):
        self._kind = "contact"
        self._current = None

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            self._kind = user_input["type"]
            return await self.async_step_contact()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("type", default="contact"): SelectSelector(
                        {
                            "options": ["contact", "automation"],
                            "mode": "dropdown",
                            "translation_key": "contact_type",
                        }
                    ),
                }
            ),
        )

    async def async_step_reconfigure(self, user_input=None):
        self._current = self._get_reconfigure_subentry()
        self._kind = (
            "automation"
            if self._current.data.get("metadata", {}).get("virtual_endpoint")
            == "automation"
            else "contact"
        )
        return await self.async_step_contact(user_input)

    async def async_step_contact(self, user_input=None):
        previous = dict(self._current.data) if self._current is not None else {}
        values = {**previous, **previous.get("metadata", {})}
        values["timeout"] = values.get("automation_timeout", 30)
        errors = {}
        if user_input is not None:
            values.update(user_input)
            values["type"] = self._kind
            try:
                contact = contact_from_data(values)
                others = [
                    raw
                    for raw in contact_dicts(self._get_entry())
                    if raw.get("id") != previous.get("id")
                ]
                _validate_contact_namespace(
                    self.hass, [contact], existing_manual=parse_roster_json(others)
                )
                if self._kind == "contact" and not (
                    contact.number or contact.sip_uri or contact.address
                ):
                    raise ValueError("A contact requires a number or SIP address")
            except ValueError:
                errors["base"] = "invalid_contact"
            else:
                data = {**previous, **vars_from_contact(contact)}
                if self._current is not None:
                    return self.async_update_and_abort(
                        self._get_entry(),
                        self._current,
                        data=data,
                        title=contact.display_name,
                    )
                return self.async_create_entry(
                    title=contact.display_name,
                    data=data,
                    unique_id=f"contact:{contact.id}",
                )
        schema = {
            vol.Required("name", default=values.get("name", "")): TextSelector(),
            vol.Optional(
                "extension", default=values.get("extension", "")
            ): TextSelector(),
        }
        if self._kind == "automation":
            schema.update(
                {
                    vol.Optional(
                        "fallback_destination",
                        default=values.get("fallback_destination", ""),
                    ): TextSelector(),
                    vol.Required(
                        "timeout", default=values.get("timeout", 30)
                    ): NumberSelector(
                        {
                            "min": 1,
                            "max": 300,
                            "unit_of_measurement": "s",
                            "mode": "box",
                        }
                    ),
                }
            )
        else:
            schema.update(
                {
                    vol.Optional(
                        "number", default=values.get("number", "")
                    ): TextSelector(),
                    vol.Optional(
                        "sip_uri", default=values.get("sip_uri", "")
                    ): TextSelector(),
                }
            )
        return self.async_show_form(
            step_id="contact", data_schema=vol.Schema(schema), errors=errors
        )


def vars_from_contact(contact):
    return {
        key: getattr(contact, key)
        for key in (
            "id",
            "name",
            "extension",
            "address",
            "sip_uri",
            "number",
            "port",
            "enabled",
            "ha_bridge",
            "metadata",
        )
    }
