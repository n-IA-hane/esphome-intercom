"""Native group editors share the existing persistent phone membership."""

import voluptuous as vol
from homeassistant.config_entries import ConfigSubentryFlow
from homeassistant.helpers.selector import BooleanSelector, NumberSelector, SelectSelector, TextSelector

from .group_config import group_members, normalize_group_data, validate_group_namespace


class GroupSubentryFlowHandler(ConfigSubentryFlow):
    def __init__(self):
        self._current = None
        self._kind = "ring"

    async def async_step_reconfigure(self, user_input=None):
        self._current = self._get_reconfigure_subentry()
        self._kind = self._current.data["kind"]
        return await self.async_step_group(user_input)

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            self._kind = user_input["kind"]
            return await self.async_step_group()
        return self.async_show_form(step_id="user", data_schema=vol.Schema({
            vol.Required("kind", default="ring"): SelectSelector({"options": ["ring", "queue"], "translation_key": "group_kind"})}))

    async def async_step_group(self, user_input=None):
        entry = self._get_entry()
        previous = dict(self._current.data) if self._current is not None else {}
        runtime = getattr(entry, "runtime_data", None)
        choices = [{"value": endpoint.endpoint_id, "label": endpoint.name}
                   for endpoint in getattr(getattr(runtime, "endpoints", None), "endpoints", ())]
        members = list(group_members(self.hass, entry, previous)) if previous else []
        values = {**previous, **previous.get("policy", {})}
        errors = {}
        if user_input is not None:
            values.update(user_input)
            try:
                kind = self._kind
                policy = {key: user_input[key] for key in (
                    "max_waiting", "max_wait", "ring_timeout", "no_agent_wait", "retry_delay", "wrap_up", "fallback", "hold_media")
                    if key in user_input} if kind == "queue" else {}
                if "max_waiting" in policy:
                    if float(policy["max_waiting"]) != int(policy["max_waiting"]):
                        raise ValueError("Queue capacity must be an integer")
                    policy["max_waiting"] = int(policy["max_waiting"])
                data = normalize_group_data({**previous, **user_input, "kind": kind, "origin":
                    "manual" if user_input.get("keep_empty", previous.get("origin") != "auto") else "auto",
                    "policy": {**previous.get("policy", {}), **policy}}, identity=previous.get("group_id", ""))
                validate_group_namespace(self.hass, entry, data, exclude=previous.get("group_id", ""))
                selected = list(dict.fromkeys(user_input.get("members", members)))
                if any(identity not in {choice["value"] for choice in choices} for identity in selected):
                    raise ValueError("Select configured phones")
                data["member_update"] = selected
                if previous and previous["name"] != data["name"]:
                    data["previous_name"] = previous["name"]
            except (ValueError, TypeError, KeyError):
                errors["base"] = "invalid_group"
            else:
                if self._current is not None:
                    return self.async_update_and_abort(entry, self._current, data=data, title=data["name"])
                return self.async_create_entry(title=data["name"], data=data, unique_id=f"group:{data['group_id']}")
        kind = self._kind
        fields = {
            vol.Required("name", default=values.get("name", "")): TextSelector(),
            vol.Optional("extension", default=values.get("extension", "")): TextSelector(),
            vol.Required("members", default=values.get("members", members)): SelectSelector({"options": choices, "multiple": True}),
            vol.Required("enabled", default=values.get("enabled", True)): BooleanSelector(),
            vol.Required("keep_empty", default=values.get("origin") != "auto"): BooleanSelector(),
        }
        for key, default, minimum, maximum in (
            ("max_waiting", 10, 1, 64), ("max_wait", 60, 1, 3600), ("ring_timeout", 20, 1, 120),
            ("no_agent_wait", 1, 0, 120), ("retry_delay", 1, 0.1, 120), ("wrap_up", 0, 0, 3600)):
            if kind == "queue":
                fields[vol.Optional(key, default=values.get(key, default))] = NumberSelector({"min": minimum, "max": maximum, "mode": "box"})
        for key in ("fallback", "hold_media"):
            if kind == "queue":
                fields[vol.Optional(key, default=values.get(key, ""))] = TextSelector()
        return self.async_show_form(step_id="group", errors=errors, data_schema=vol.Schema(fields))
