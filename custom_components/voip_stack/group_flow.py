"""Native group editors use one ordered list of canonical destinations."""

import voluptuous as vol
from homeassistant.config_entries import ConfigSubentryFlow
from homeassistant.data_entry_flow import Required as visible_required
from homeassistant.helpers.selector import (
    BooleanSelector, MediaSelector, NumberSelector, ObjectSelector, SelectSelector, TextSelector,
)

from .endpoint_device import selection_device_picker, selection_device_refs
from .group_config import (
    INITIAL_RING_STEP_TIMEOUT, group_members, normalize_group_data, validate_group_namespace,
)
from .phone_queue_membership import iter_phone_memberships


def destination_options(hass, entry):
    """Take one public directory snapshot for readable destination selectors."""
    runtime = getattr(entry, "runtime_data", None)
    directory = getattr(runtime, "directory", None)
    options = {}
    for endpoint in getattr(getattr(runtime, "endpoints", None), "endpoints", ()):
        if endpoint.username:
            options.setdefault(endpoint.username, endpoint.name)
    for row in directory.snapshot() if directory is not None else ():
        if not row.enabled:
            continue
        endpoint = runtime.endpoints.get(row.metadata.get("endpoint_id", ""))
        if endpoint is not None:
            value = endpoint.username
        else:
            value = row.extension or row.number or row.sip_uri or row.name
        if value:
            options.setdefault(value, row.display_name)
    return [{"value": value, "label": label} for value, label in options.items()]


def member_rows(members, device_refs):
    devices = {identity: device for device, identity in device_refs.items()}
    return [{"device": devices[identity]} if identity in devices else {"destination": identity}
            for identity in members]


def ordered_members_selector(hass, device_refs, options, *, allow_destinations, previous=()):
    """Keep removed members editable while offering native account devices."""
    fields = {}
    if device_refs:
        fields["device"] = {"selector": selection_device_picker(hass, device_refs)}
    missing = [value for value in previous if value not in set(device_refs.values())]
    choices = list(options) if allow_destinations else []
    present = {row["value"] for row in choices}
    from homeassistant.helpers import device_registry as dr
    devices = dr.async_get(hass)
    for value in missing:
        if value in present:
            continue
        device = devices.async_get(value.removeprefix('esphome:')) if value.startswith('esphome:') else None
        choices.append({'value': value, 'label': (device.name_by_user or device.name or value) if device is not None else value})
    if allow_destinations or choices:
        fields["destination"] = {"selector": SelectSelector({"options": choices, "custom_value": allow_destinations,
                                                              "mode": "dropdown"})}
    return ObjectSelector({"multiple": True, "read_only": not fields,
                           "translation_key": "group_members", "fields": fields})


def selected_members(hass, entry, rows, device_refs, *, allow_destinations, previous=()):
    """Resolve selected HA devices once and retain literal public dial strings."""
    from .service_endpoints import resolve_public_phone
    from homeassistant.exceptions import ServiceValidationError
    runtime = getattr(entry, "runtime_data", None)
    selected = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Select a device or destination in each member row")
        values = [key for key in ("device", "destination") if row.get(key)]
        if len(values) != 1:
            raise ValueError("Select exactly one device or destination per member")
        if values[0] == "device":
            identity = device_refs[row["device"]]
        else:
            identity = str(row["destination"]).strip()
            if not allow_destinations and identity not in previous:
                raise ValueError("Select an existing account device")
            if allow_destinations and runtime is not None:
                try:
                    endpoint = resolve_public_phone(runtime.endpoints, identity, directory=runtime.directory)
                except ServiceValidationError as error:
                    raise ValueError("Select an unambiguous public destination") from error
                if endpoint is not None:
                    identity = endpoint.endpoint_id
        if not identity or identity in selected:
            raise ValueError("Each member must appear exactly once")
        selected.append(identity)
    return selected


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
        members = list(group_members(self.hass, entry, previous)) if previous else []
        eligible = dict(iter_phone_memberships(self.hass, entry, self._kind))
        device_refs = selection_device_refs(self.hass, entry, eligible)
        options = destination_options(self.hass, entry)
        values = {**previous.get("policy", {}), **previous}
        errors = {}
        if user_input is not None:
            values.update(user_input)
            try:
                selected = selected_members(self.hass, entry, user_input.get("members", member_rows(members, device_refs)),
                    device_refs, allow_destinations=self._kind == "ring", previous=members)
                policy_fields = (("max_waiting", "max_wait", "ring_timeout", "no_agent_wait", "retry_delay", "wrap_up",
                    "fallback", "strategy", "auto_pause_after", "fallback_full", "fallback_timeout", "fallback_no_agents",
                    "fallback_disabled", "fallback_removed") if self._kind == "queue" else
                    ("strategy", "overall_timeout", "step_timeout"))
                policy = {**previous.get("policy", {}), **{key: values[key] for key in policy_fields if key in values}}
                timeout_key = "ring_timeout" if self._kind == "queue" else "overall_timeout"
                if user_input.get("inherit_ringing_timeout") is True:
                    policy[timeout_key] = None
                if self._kind == "queue":
                    for integer in ("max_waiting", "auto_pause_after"):
                        if integer in policy:
                            if float(policy[integer]) != int(policy[integer]):
                                raise ValueError("Queue capacity and pause threshold must be integers")
                            policy[integer] = int(policy[integer])
                    media = user_input.get("hold_media")
                    if media is not None:
                        if not isinstance(media, dict):
                            raise ValueError("Select hold audio from Media")
                        policy["hold_media"] = str(media.get("media_content_id") or "")
                    if user_input.get("clear_hold_media"):
                        policy["hold_media"] = ""
                data = normalize_group_data({**previous, **user_input, "kind": self._kind, "members": selected,
                    "origin": "manual" if user_input.get("keep_empty", previous.get("origin") != "auto") else "auto",
                    "policy": policy}, identity=previous.get("group_id", ""))
                validate_group_namespace(self.hass, entry, data, exclude=previous.get("group_id", ""))
            except (ValueError, TypeError, KeyError):
                errors["base"] = "invalid_group"
            else:
                if self._current is not None:
                    return self.async_update_and_abort(entry, self._current, data=data, title=data["name"])
                return self.async_create_entry(title=data["name"], data=data, unique_id=f"group:{data['group_id']}")
        fields = {
            vol.Required("name", default=values.get("name", "")): TextSelector(),
            vol.Optional("extension", default=values.get("extension", "")): TextSelector(),
            vol.Required("members", default=values.get("members") if user_input is not None and "members" in user_input
                         else member_rows(members, device_refs)): ordered_members_selector(self.hass, device_refs, options,
                             allow_destinations=self._kind == "ring", previous=members),
            vol.Required("enabled", default=values.get("enabled", True)): BooleanSelector(),
            vol.Required("keep_empty", default=values.get("origin") != "auto"): BooleanSelector(),
        }
        timeout_key = "ring_timeout" if self._kind == "queue" else "overall_timeout"
        fields[vol.Required("inherit_ringing_timeout", default=values.get("inherit_ringing_timeout", values.get(timeout_key) is None))] = BooleanSelector()
        fields[visible_required(timeout_key,
                default=values.get(timeout_key) if values.get(timeout_key) is not None else entry.data.get("default_ringing_timeout", 0),
                visible={"field": "inherit_ringing_timeout", "value": False})] = NumberSelector({
                    "min": 0, "max": 3600, "step": 0.1, "unit_of_measurement": "s", "mode": "box"})
        if self._kind == "ring":
            fields[vol.Required("strategy", default=values.get("strategy", "parallel"))] = SelectSelector({
                "options": ["parallel", "sequential"], "translation_key": "ring_strategy"})
            for key, default, maximum in (("step_timeout", INITIAL_RING_STEP_TIMEOUT, 3600),):
                fields[vol.Required(key, default=values.get(key, default))] = NumberSelector({
                    "min": 0.1, "max": maximum, "step": 0.1, "unit_of_measurement": "s", "mode": "box"})
            fields[vol.Optional("fallback", default=values.get("fallback", ""))] = SelectSelector({
                "options": options, "custom_value": True, "mode": "dropdown"})
        else:
            for key, default, minimum, maximum in (
                ("max_waiting", 10, 1, 64), ("max_wait", 60, 1, 3600),
                ("no_agent_wait", 1, 0, 120), ("retry_delay", 1, 0.1, 120), ("wrap_up", 0, 0, 3600)):
                config = {"min": minimum, "max": maximum, "mode": "box", "step": 1 if key == "max_waiting" else 0.1}
                if key != "max_waiting":
                    config["unit_of_measurement"] = "s"
                fields[vol.Optional(key, default=values.get(key, default))] = NumberSelector(config)
            fields[vol.Optional("strategy", default=values.get("strategy", "ring_all"))] = SelectSelector({
                "options": ["ring_all", "round_robin", "longest_idle", "ordered"], "translation_key": "queue_strategy"})
            fields[vol.Optional("auto_pause_after", default=values.get("auto_pause_after", 0))] = NumberSelector({
                "min": 0, "max": 100, "step": 1, "mode": "box"})
            for key in ("fallback", "fallback_full", "fallback_timeout", "fallback_no_agents", "fallback_disabled", "fallback_removed"):
                fields[vol.Optional(key, default=values.get(key, ""))] = SelectSelector({
                    "options": options, "custom_value": True, "mode": "dropdown"})
            media_default = values["hold_media"] if isinstance(values.get("hold_media"), dict) else (
                {"media_content_id": values["hold_media"], "media_content_type": "music"} if values.get("hold_media") else vol.UNDEFINED)
            fields[vol.Optional("hold_media", default=media_default)] = MediaSelector({"accept": ["audio/*"]})
            fields[vol.Optional("clear_hold_media", default=False)] = BooleanSelector()
        return self.async_show_form(step_id="group", errors=errors, data_schema=vol.Schema(fields))
