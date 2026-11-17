"""Configure a saved paging destination without creating another phone."""

import voluptuous as vol

from homeassistant.config_entries import ConfigSubentryFlow
from homeassistant.helpers.selector import NumberSelector, SelectSelector, TextSelector

from .paging_config import normalize_paging_group
from .runtime_data import runtime_data


class PagingSubentryFlowHandler(ConfigSubentryFlow):
    async def async_step_user(self, user_input=None):
        return await self._form(user_input)

    async def async_step_reconfigure(self, user_input=None):
        self._current = self._get_reconfigure_subentry()
        return await self._form(user_input)

    async def _form(self, user_input):
        current = getattr(self, "_current", None)
        previous = dict(current.data) if current is not None else {}
        runtime = runtime_data(self.hass)
        choices = [{"value": endpoint.endpoint_id, "label": endpoint.name}
                   for endpoint in runtime.endpoints.endpoints] if runtime is not None else []
        # Keep removed phones visible until the user deliberately changes membership.
        present = {item["value"] for item in choices}
        choices.extend({"value": member, "label": member} for member in previous.get("members", ()) if member not in present)
        errors = {}
        values = {**previous, **(user_input or {})}
        if user_input is not None:
            try:
                data = normalize_paging_group(values)
                from .phonebook_services import _validate_contact_namespace
                from .paging_config import paging_group_entries
                from .roster import RosterEntry
                proposed = RosterEntry(id=f"paging:{data['paging_group_id']}", name=data["name"], extension=data["extension"])
                existing = [row for row in paging_group_entries(self._get_entry())
                            if row.metadata["paging_group_id"] != data["paging_group_id"]]
                _validate_contact_namespace(self.hass, [proposed], existing_manual=existing)
            except ValueError:
                errors["base"] = "invalid_paging_group"
            else:
                if current is not None:
                    return self.async_update_and_abort(self._get_entry(), current, data=data, title=data["name"])
                return self.async_create_entry(title=data["name"], data=data,
                                               unique_id=f"paging:{data['paging_group_id']}")
        return self.async_show_form(step_id="user", errors=errors, data_schema=vol.Schema({
            vol.Required("name", default=values.get("name", "")): TextSelector(),
            vol.Optional("extension", default=values.get("extension", "")): TextSelector(),
            vol.Required("members", default=values.get("members", [])): SelectSelector({"options": choices, "multiple": True}),
            vol.Required("answer_timeout", default=values.get("answer_timeout", 5)): NumberSelector({"min": 1, "max": 120, "unit_of_measurement": "s", "mode": "box"}),
            vol.Required("timeout", default=values.get("timeout", 120)): NumberSelector({"min": 1, "max": 600, "unit_of_measurement": "s", "mode": "box"}),
        }))
