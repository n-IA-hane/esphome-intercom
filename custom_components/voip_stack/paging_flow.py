"""Configure one native paging destination and ordered account receivers."""

import voluptuous as vol

from homeassistant.config_entries import ConfigSubentryFlow
from homeassistant.data_entry_flow import Required as visible_required
from homeassistant.helpers.selector import BooleanSelector, NumberSelector, TextSelector

from .endpoint_device import selection_device_refs
from .group_flow import member_rows, ordered_members_selector, selected_members
from .paging_config import normalize_paging_group


class PagingSubentryFlowHandler(ConfigSubentryFlow):
    async def async_step_user(self, user_input=None):
        return await self._form(user_input)

    async def async_step_reconfigure(self, user_input=None):
        self._current = self._get_reconfigure_subentry()
        return await self._form(user_input)

    async def _form(self, user_input):
        from .phone_config import phone_subentries, phone_endpoint_id
        from .config_validation import entry_route_mappings, route_namespace_conflicts

        entry = self._get_entry()
        current = getattr(self, "_current", None)
        previous = dict(current.data) if current is not None else {}
        members = list(previous.get("members", ()))
        identities = dict.fromkeys(phone_endpoint_id(row) for row in phone_subentries(entry))
        runtime = getattr(entry, "runtime_data", None)
        if runtime is not None:
            identities.update(dict.fromkeys(endpoint.endpoint_id for endpoint in runtime.endpoints.endpoints))
        device_refs = selection_device_refs(self.hass, entry, identities)
        errors = {}
        values = {**previous, **(user_input or {})}
        if user_input is not None:
            try:
                selected = selected_members(self.hass, entry, user_input.get("members", member_rows(members, device_refs)),
                    device_refs, allow_destinations=False, previous=members)
                if user_input.get("inherit_ringing_timeout") is True:
                    values["answer_timeout"] = None
                data = normalize_paging_group({**values, "members": selected})
                existing = entry_route_mappings(entry, entry.data,
                    exclude_subentry_id=current.subentry_id if current is not None else "")
                if route_namespace_conflicts(candidate_routes=(
                        f"paging:{data['paging_group_id']}", data["name"], data["extension"]), existing=existing):
                    raise ValueError("The paging name or extension belongs to another destination")
            except (ValueError, TypeError, KeyError):
                errors["base"] = "invalid_paging_group"
            else:
                if current is not None:
                    return self.async_update_and_abort(entry, current, data=data, title=data["name"])
                return self.async_create_entry(title=data["name"], data=data,
                                               unique_id=f"paging:{data['paging_group_id']}")
        return self.async_show_form(step_id="user", errors=errors, data_schema=vol.Schema({
            vol.Required("name", default=values.get("name", "")): TextSelector(),
            vol.Optional("extension", default=values.get("extension", "")): TextSelector(),
            vol.Required("members", default=values.get("members") if user_input is not None and "members" in user_input
                         else member_rows(members, device_refs)): ordered_members_selector(self.hass, device_refs, (),
                             allow_destinations=False, previous=members),
            vol.Required("enabled", default=values.get("enabled", True)): BooleanSelector(),
            vol.Required("inherit_ringing_timeout", default=values.get("inherit_ringing_timeout", values.get("answer_timeout") is None)): BooleanSelector(),
            visible_required("answer_timeout", default=values.get("answer_timeout") if values.get("answer_timeout") is not None
                             else entry.data.get("default_ringing_timeout", 0),
                             visible={"field": "inherit_ringing_timeout", "value": False}): NumberSelector({
                "min": 0, "max": 3600, "step": 0.1, "unit_of_measurement": "s", "mode": "box"}),
            vol.Required("timeout", default=values.get("timeout", 120)): NumberSelector({
                "min": 1, "max": 600, "step": 0.1, "unit_of_measurement": "s", "mode": "box"}),
        }))
