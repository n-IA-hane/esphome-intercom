"""Config flow for VoIP Stack."""

from collections.abc import Mapping
import re
import time
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntry,
    ConfigFlow,
    ConfigSubentryFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    MediaSelector,
    AssistPipelineSelector,
    NumberSelector,
    NumberSelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .config_validation import entry_route_mappings as _entry_route_mappings
from .codec_policy_form import codec_form_fields, normalize_codec_form
from homeassistant.data_entry_flow import Optional as visible_optional, Required as visible_required
from .trunk_policy import EXTERNAL_CALL_BLOCKED_ENDPOINTS, trusted_networks
from .config_validation import extension_conflicts, route_namespace_conflicts, mailbox_route_aliases, mailbox_namespace_conflicts
from .phone_config import (
    CONF_PHONE_AUTO_ANSWER,
    CONF_PHONE_CONFERENCE_GROUP,
    CONF_PHONE_CONFERENCE_RING,
    CONF_PHONE_DND,
    CONF_PHONE_ENABLED,
    CONF_PHONE_ENDPOINT_ID,
    CONF_PHONE_EXTENSION,
    CONF_PHONE_KIND,
    CONF_PHONE_NAME,
    CONF_PHONE_OFFLINE_FORWARD_TARGET,
    CONF_PHONE_OFFLINE_POLICY,
    CONF_PHONE_PASSWORD,
    CONF_PHONE_RING_GROUP,
    CONF_PHONE_QUEUES,
    CONF_PHONE_SEND_VIDEO,
    CONF_PHONE_USERNAME,
    CONF_PHONE_VIDEO_ENABLED,
    PHONE_SUBENTRY_TYPE,
    CONF_PHONE_POLICY,
    persisted_phone_policy,
    draft_phone_subentry,
    phone_endpoint_id,
    phone_subentry_data,
    phone_subentry_storage_data,
    validate_phone_subentry_data,
)
from .phone_endpoint import EndpointKind, OfflinePolicy
from .sip_registrar import generate_password, normalize_username
from .const import (
    CONF_ASSIST_ADVANCED_CALL_CONTEXT,
    CONF_ASSIST_ENDPOINT_ENABLED,
    CONF_ASSIST_EXTENSION,
    CONF_ASSIST_PIPELINE,
    CONF_ASSIST_INTENTS,
    CONF_AUTOMATION_ROUTING_ENABLED,
    CONF_DEBUG_MODE,
    CONF_MEDIA_CAPTURE,
    CONF_SIP_VIDEO,
    CONF_VIDEO_CAMERA_SEND,
    CONF_VIDEO_TRANSCODING,
    CONF_PHONEBOOK_CONTACTS,
    CONF_REGISTRAR_ENABLED,
    CONF_TRUNK_REGISTER,
    CONF_TRUNK_ALLOWED_IPS,
    CONF_TRUNK_AUTH_USERNAME,
    CONF_TRUNK_DOMAIN,
    CONF_TRUNK_DTMF_ENABLED,
    CONF_TRUNK_DTMF_TERMINATOR,
    CONF_TRUNK_DTMF_TIMEOUT_MS,
    CONF_TRUNK_ENABLED,
    CONF_TRUNK_EXPIRES,
    CONF_TRUNK_INBOUND_DEFAULT_TARGET,
    CONF_TRUNK_INBOUND_MODE,
    CONF_TRUNK_OUTBOUND_PROXY,
    CONF_TRUNK_PASSWORD,
    CONF_TRUNK_PORT,
    CONF_TRUNK_SERVER,
    CONF_TRUNK_TRANSPORT,
    CONF_TRUNK_USERNAME,
    DOMAIN,
    TRUNK_INBOUND_MODE_DIRECT,
    TRUNK_INBOUND_MODE_DTMF,
    VOIP_STACK_RTP_PORT,
    VOIP_STACK_SIP_PORT,
)


def _group_route_values(*values: object) -> list[str]:
    return [
        part.strip()
        for value in values
        for part in str(value or "").split(",")
        if part.strip()
    ]




def _port_selector():
    return NumberSelector(NumberSelectorConfig(min=1, max=65535, step=1, mode="box"))


def _trunk_target_choices(hass, entry, existing):
    """Return available trunk destinations and the configured default."""
    from .runtime_data import runtime_data, preferred_browser_phone

    runtime = runtime_data(hass)
    names = set()
    if runtime is not None:
        names.update(endpoint.name for endpoint in runtime.endpoints.endpoints)
    for item in _entry_route_mappings(entry, entry.data if entry is not None else existing):
        name = str(item.get("name") or item.get("id") or "").strip()
        if name:
            names.add(name)
        names.update(_group_route_values(item.get("ring_group"), item.get("conference_group")))
    selected = str(existing.get(CONF_TRUNK_INBOUND_DEFAULT_TARGET) or "").strip()
    if not selected or (selected.casefold() == "ha" and selected not in names):
        preferred = preferred_browser_phone(hass) if runtime is not None else None
        selected = preferred.name if preferred is not None else ""
    return sorted(names, key=str.casefold), selected


def _dtmf_timeout_seconds(value: object) -> int:
    raw = 3000 if value in (None, "") else int(value)
    if 0 <= raw <= 10:
        return raw
    return max(0, min(10, round(raw / 1000)))


def _disabled_trunk_data(data: dict, existing: Mapping[str, Any]) -> dict:
    """Return entry data with trunk explicitly disabled."""

    def legacy_value(key: str, default: Any) -> Any:
        """Replace empty values written by older flows with a safe default."""
        value = existing.get(key)
        return default if value in (None, "") else value

    data.update(
        {
            "trunk_outbound_media_address": existing.get("trunk_outbound_media_address", ""),
            "trunk_media_nat": existing.get("trunk_media_nat") or (
                "manual" if existing.get("trunk_outbound_media_address") else "auto"
            ),
            CONF_TRUNK_ENABLED: False,
            CONF_TRUNK_REGISTER: legacy_value(CONF_TRUNK_REGISTER, True),
            CONF_TRUNK_ALLOWED_IPS: legacy_value(CONF_TRUNK_ALLOWED_IPS, []),
            CONF_TRUNK_TRANSPORT: legacy_value(CONF_TRUNK_TRANSPORT, "udp"),
            CONF_TRUNK_SERVER: legacy_value(CONF_TRUNK_SERVER, ""),
            CONF_TRUNK_PORT: int(legacy_value(CONF_TRUNK_PORT, VOIP_STACK_SIP_PORT)),
            CONF_TRUNK_DOMAIN: legacy_value(CONF_TRUNK_DOMAIN, ""),
            CONF_TRUNK_USERNAME: legacy_value(CONF_TRUNK_USERNAME, ""),
            CONF_TRUNK_AUTH_USERNAME: legacy_value(CONF_TRUNK_AUTH_USERNAME, ""),
            CONF_TRUNK_PASSWORD: legacy_value(CONF_TRUNK_PASSWORD, ""),
            CONF_TRUNK_EXPIRES: int(legacy_value(CONF_TRUNK_EXPIRES, 300)),
            CONF_TRUNK_OUTBOUND_PROXY: legacy_value(CONF_TRUNK_OUTBOUND_PROXY, ""),
            CONF_TRUNK_INBOUND_DEFAULT_TARGET: legacy_value(
                CONF_TRUNK_INBOUND_DEFAULT_TARGET, "HA"
            ),
            CONF_TRUNK_INBOUND_MODE: legacy_value(
                CONF_TRUNK_INBOUND_MODE, TRUNK_INBOUND_MODE_DIRECT
            ),
            CONF_AUTOMATION_ROUTING_ENABLED: legacy_value(
                CONF_AUTOMATION_ROUTING_ENABLED, False
            ),
            CONF_TRUNK_DTMF_ENABLED: legacy_value(CONF_TRUNK_DTMF_ENABLED, False),
            CONF_TRUNK_DTMF_TIMEOUT_MS: int(
                legacy_value(CONF_TRUNK_DTMF_TIMEOUT_MS, 3000)
            ),
            CONF_TRUNK_DTMF_TERMINATOR: legacy_value(CONF_TRUNK_DTMF_TERMINATOR, ""),
            "sip_accounts": legacy_value("sip_accounts", []),
            CONF_PHONEBOOK_CONTACTS: legacy_value(CONF_PHONEBOOK_CONTACTS, []),
        }
    )
    codec_errors, _inherited = normalize_codec_form(data, {}, existing, prefix="trunk_", inherit=True)
    if codec_errors:
        raise ValueError("trunk_codecs_invalid")
    return data


class TrunkFormMixin:
    """Registered/static forms for native trunk entries."""

    async def async_step_trunk_mode(self, user_input=None):
        _entry, existing = self._current_entry_data()
        if user_input is not None:
            self._trunk_register = bool(user_input[CONF_TRUNK_REGISTER])
            return await self.async_step_trunk()
        return self.async_show_form(
            step_id="trunk_mode",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TRUNK_REGISTER,
                        default=existing.get(CONF_TRUNK_REGISTER, True),
                    ): BooleanSelector(),
                }
            ),
        )

    async def async_step_trunk(self, user_input=None):
        from .inbound_identity_form import did_form_fields, validate_did_form

        _current_entry, existing = self._current_entry_data()
        target_choices, target_default = _trunk_target_choices(self.hass, _current_entry, existing)
        register = getattr(
            self, "_trunk_register", existing.get(CONF_TRUNK_REGISTER, True)
        )
        legacy_timeout = _dtmf_timeout_seconds(
            existing.get(CONF_TRUNK_DTMF_TIMEOUT_MS, 3000)
        )
        legacy_mode = (
            TRUNK_INBOUND_MODE_DTMF
            if existing.get(CONF_TRUNK_DTMF_ENABLED, True) and legacy_timeout > 0
            else TRUNK_INBOUND_MODE_DIRECT
        )
        defaults = {
            CONF_TRUNK_TRANSPORT: existing.get(CONF_TRUNK_TRANSPORT, "udp"),
            CONF_TRUNK_SERVER: existing.get(CONF_TRUNK_SERVER, ""),
            CONF_TRUNK_PORT: existing.get(CONF_TRUNK_PORT, VOIP_STACK_SIP_PORT),
            CONF_TRUNK_DOMAIN: existing.get(CONF_TRUNK_DOMAIN, ""),
            CONF_TRUNK_USERNAME: existing.get(CONF_TRUNK_USERNAME, ""),
            CONF_TRUNK_AUTH_USERNAME: existing.get(CONF_TRUNK_AUTH_USERNAME, ""),
            CONF_TRUNK_PASSWORD: existing.get(CONF_TRUNK_PASSWORD, ""),
            CONF_TRUNK_EXPIRES: existing.get(CONF_TRUNK_EXPIRES, 300),
            CONF_TRUNK_OUTBOUND_PROXY: existing.get(CONF_TRUNK_OUTBOUND_PROXY, ""),
            CONF_TRUNK_INBOUND_DEFAULT_TARGET: target_default,
            CONF_TRUNK_INBOUND_MODE: existing.get(CONF_TRUNK_INBOUND_MODE, legacy_mode),
            CONF_AUTOMATION_ROUTING_ENABLED: existing.get(
                CONF_AUTOMATION_ROUTING_ENABLED, False
            ),
            CONF_TRUNK_DTMF_TIMEOUT_MS: legacy_timeout,
            CONF_TRUNK_DTMF_TERMINATOR: existing.get(CONF_TRUNK_DTMF_TERMINATOR, ""),
        }
        schema = vol.Schema(
            {
                **did_form_fields(existing),
                vol.Optional("trunk_media_nat", default=existing.get("trunk_media_nat") or (
                    "manual" if existing.get("trunk_outbound_media_address") else "auto"
                )): SelectSelector(SelectSelectorConfig(
                    options=["auto", "direct", "manual"], translation_key="trunk_media_nat",
                )),
                vol.Optional(
                    "trunk_outbound_media_address",
                    default=existing.get("trunk_outbound_media_address", ""),
                ): TextSelector(),
                **codec_form_fields(existing, prefix="trunk_", inherit=True,
                                    parent=_current_entry.data if _current_entry is not None else {}),
                vol.Required(
                    CONF_TRUNK_TRANSPORT, default=defaults[CONF_TRUNK_TRANSPORT]
                ): SelectSelector(
                    SelectSelectorConfig(options=["udp", "tcp", "tls"] if register else ["udp", "tcp"])
                ),
                vol.Required(
                    CONF_TRUNK_SERVER, default=defaults[CONF_TRUNK_SERVER]
                ): TextSelector(),
                vol.Required(
                    CONF_TRUNK_PORT, default=defaults[CONF_TRUNK_PORT]
                ): _port_selector(),
                vol.Optional(
                    CONF_TRUNK_DOMAIN, default=defaults[CONF_TRUNK_DOMAIN]
                ): TextSelector(),
                vol.Required(
                    CONF_TRUNK_USERNAME, default=defaults[CONF_TRUNK_USERNAME]
                ): TextSelector(),
                vol.Optional(
                    CONF_TRUNK_AUTH_USERNAME, default=defaults[CONF_TRUNK_AUTH_USERNAME]
                ): TextSelector(),
                vol.Required(
                    CONF_TRUNK_PASSWORD, default=defaults[CONF_TRUNK_PASSWORD]
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Required(
                    CONF_TRUNK_EXPIRES, default=defaults[CONF_TRUNK_EXPIRES]
                ): NumberSelector(
                    NumberSelectorConfig(min=60, max=3600, step=30, mode="box")
                ),
                vol.Optional(
                    CONF_TRUNK_OUTBOUND_PROXY,
                    default=defaults[CONF_TRUNK_OUTBOUND_PROXY],
                ): TextSelector(),
                vol.Required(
                    CONF_TRUNK_INBOUND_DEFAULT_TARGET,
                    **({"default": target_default} if target_default else {}),
                ): SelectSelector(SelectSelectorConfig(
                    options=target_choices, custom_value=True, mode="dropdown",
                )),
                vol.Required(
                    CONF_TRUNK_INBOUND_MODE, default=defaults[CONF_TRUNK_INBOUND_MODE]
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[TRUNK_INBOUND_MODE_DIRECT, TRUNK_INBOUND_MODE_DTMF],
                        translation_key="trunk_inbound_mode",
                    )
                ),
                vol.Required(
                    CONF_AUTOMATION_ROUTING_ENABLED,
                    default=defaults[CONF_AUTOMATION_ROUTING_ENABLED],
                ): BooleanSelector(),
                vol.Required(
                    CONF_TRUNK_DTMF_TIMEOUT_MS,
                    default=defaults[CONF_TRUNK_DTMF_TIMEOUT_MS],
                ): NumberSelector(
                    NumberSelectorConfig(min=0, max=10, step=1, mode="box")
                ),
                vol.Optional(
                    CONF_TRUNK_DTMF_TERMINATOR,
                    default=defaults[CONF_TRUNK_DTMF_TERMINATOR],
                ): TextSelector(),
            }
        )
        if not register:
            fields = {
                key: value
                for key, value in schema.schema.items()
                if key.schema not in {
                    CONF_TRUNK_USERNAME, CONF_TRUNK_PASSWORD, CONF_TRUNK_EXPIRES
                }
            }
            fields[vol.Optional(CONF_TRUNK_USERNAME, default=defaults[CONF_TRUNK_USERNAME])] = TextSelector()
            fields[vol.Optional(CONF_TRUNK_PASSWORD, default=defaults[CONF_TRUNK_PASSWORD])] = TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            )
            fields[vol.Required(CONF_TRUNK_ALLOWED_IPS, default=existing.get(CONF_TRUNK_ALLOWED_IPS, []))] = TextSelector(
                TextSelectorConfig(multiple=True)
            )
            schema = vol.Schema(fields)
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = dict(user_input)
            errors.update(validate_did_form(user_input, existing))
            user_input[CONF_TRUNK_REGISTER] = register
            user_input.setdefault(CONF_TRUNK_EXPIRES, defaults[CONF_TRUNK_EXPIRES])
            if not register:
                try:
                    user_input[CONF_TRUNK_ALLOWED_IPS] = [
                        str(network)
                        for network in trusted_networks(user_input.get(CONF_TRUNK_ALLOWED_IPS))
                    ]
                except ValueError:
                    errors[CONF_TRUNK_ALLOWED_IPS] = "trunk_allowed_ips_invalid"
            else:
                user_input[CONF_TRUNK_ALLOWED_IPS] = []
            for k in (CONF_TRUNK_PORT, CONF_TRUNK_EXPIRES, CONF_TRUNK_DTMF_TIMEOUT_MS):
                user_input[k] = int(user_input[k])
            user_input[CONF_TRUNK_DTMF_TIMEOUT_MS] = (
                max(0, min(10, user_input[CONF_TRUNK_DTMF_TIMEOUT_MS])) * 1000
            )
            inbound_mode = str(
                user_input.get(CONF_TRUNK_INBOUND_MODE) or TRUNK_INBOUND_MODE_DIRECT
            )
            user_input[CONF_TRUNK_DTMF_ENABLED] = (
                inbound_mode == TRUNK_INBOUND_MODE_DTMF
                and user_input[CONF_TRUNK_DTMF_TIMEOUT_MS] > 0
            )
            for k in (
                CONF_TRUNK_SERVER,
                CONF_TRUNK_DOMAIN,
                CONF_TRUNK_USERNAME,
                CONF_TRUNK_AUTH_USERNAME,
                CONF_TRUNK_OUTBOUND_PROXY,
                CONF_TRUNK_INBOUND_DEFAULT_TARGET,
                CONF_TRUNK_DTMF_TERMINATOR,
            ):
                user_input[k] = (user_input.get(k) or "").strip()
            # SIP digest credentials are opaque. Leading/trailing whitespace is
            # uncommon but valid and must survive a reconfigure unchanged.
            user_input[CONF_TRUNK_PASSWORD] = str(
                user_input.get(CONF_TRUNK_PASSWORD) or ""
            )
            if not user_input[CONF_TRUNK_SERVER]:
                errors["base"] = "trunk_server_required"
            elif register and not user_input[CONF_TRUNK_USERNAME]:
                errors["base"] = "trunk_username_required"
            elif register and not user_input[CONF_TRUNK_PASSWORD]:
                errors["base"] = "trunk_password_required"
            if not user_input[CONF_TRUNK_INBOUND_DEFAULT_TARGET]:
                errors[CONF_TRUNK_INBOUND_DEFAULT_TARGET] = "trunk_target_required"
            from ipaddress import IPv4Address
            media_address = str(user_input.get(
                "trunk_outbound_media_address",
                existing.get("trunk_outbound_media_address", ""),
            ) or "").strip()
            if media_address:
                try:
                    address = IPv4Address(media_address)
                    if address.is_unspecified or address.is_multicast or address.is_loopback:
                        raise ValueError("not a unicast media address")
                    media_address = str(address)
                except ValueError:
                    errors["trunk_outbound_media_address"] = "trunk_media_address_invalid"
            user_input["trunk_outbound_media_address"] = media_address
            media_mode = str(user_input.get("trunk_media_nat") or existing.get("trunk_media_nat") or (
                "manual" if media_address else "auto"
            ))
            if media_mode not in {"auto", "direct", "manual"}:
                errors["trunk_media_nat"] = "trunk_media_nat_invalid"
            elif media_mode == "manual" and not media_address:
                errors["trunk_outbound_media_address"] = "trunk_media_address_invalid"
            user_input["trunk_media_nat"] = media_mode
            codec_errors, inherited = normalize_codec_form(user_input, user_input, existing,
                prefix="trunk_", inherit=True, error_key="trunk_codecs_invalid")
            errors.update(codec_errors)
            self._codec_inherited_keys = inherited
            if not errors:
                data = dict(self._base_input or {})
                data[CONF_TRUNK_ENABLED] = True
                data.setdefault("sip_accounts", existing.get("sip_accounts", []))
                data.setdefault(
                    CONF_PHONEBOOK_CONTACTS, existing.get(CONF_PHONEBOOK_CONTACTS, [])
                )
                data.update(user_input)
                current_entry, _existing = self._current_entry_data()
                if current_entry is None:
                    await self.async_set_unique_id(DOMAIN)
                    self._abort_if_unique_id_configured()
                return await self._async_store_trunk(data)
        return self.async_show_form(step_id="trunk", data_schema=schema, errors=errors)


    async def _async_store_trunk(self, data):
        return self._store_entry(data)


class VoipStackConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for VoIP Stack."""

    VERSION = 7
    MINOR_VERSION = 4
    _base_input: dict | None = None

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Expose the native Add phone flow on the integration entry."""
        from .contact_flow import ContactSubentryFlowHandler
        from .trunk_flow import TrunkSubentryFlowHandler
        from .paging_flow import PagingSubentryFlowHandler
        from .group_flow import GroupSubentryFlowHandler

        result = {PHONE_SUBENTRY_TYPE: PhoneSubentryFlowHandler, "contact": ContactSubentryFlowHandler,
                "trunk": TrunkSubentryFlowHandler, "paging": PagingSubentryFlowHandler,
                "group": GroupSubentryFlowHandler}
        from .physical_phone import PHYSICAL_PHONE_SUBENTRY_TYPE, provisioning_enabled
        if provisioning_enabled(config_entry):
            from .physical_phone_flow import PhysicalPhoneSubentryFlowHandler
            result[PHYSICAL_PHONE_SUBENTRY_TYPE] = PhysicalPhoneSubentryFlowHandler
        return result

    def _current_entry(self) -> ConfigEntry | None:
        if self.source == SOURCE_RECONFIGURE:
            return self._get_reconfigure_entry()
        return next((entry for entry in self._async_current_entries()
                     if entry.source != "ignore"), None)

    def _current_entry_data(self) -> tuple[ConfigEntry | None, Mapping[str, Any]]:
        current_entry = self._current_entry()
        return current_entry, current_entry.data if current_entry is not None else {}

    def _store_entry(self, data: dict):
        registrar_choice = data.get(CONF_REGISTRAR_ENABLED)
        current_entry, _existing = self._current_entry_data()
        if current_entry is not None:
            data = {**current_entry.data, **data}
        for key in getattr(self, "_codec_inherited_keys", ()):
            data.pop(key, None)
        for feature in ("parking", "voicemail"):
            if feature in data:
                settings = dict(data[feature])
                settings.pop("enabled", None)
                data[feature] = settings
        if current_entry is None or current_entry.version >= 6:
            data.pop(CONF_PHONEBOOK_CONTACTS, None)
        if current_entry is not None:
            # Phone permissions are managed by their action, not this wizard.
            # Read the current entry so an action run while the form was open
            # is preserved rather than replaced by an older form snapshot.
            if EXTERNAL_CALL_BLOCKED_ENDPOINTS in current_entry.data:
                data[EXTERNAL_CALL_BLOCKED_ENDPOINTS] = list(
                    current_entry.data[EXTERNAL_CALL_BLOCKED_ENDPOINTS]
                )
            else:
                data.pop(EXTERNAL_CALL_BLOCKED_ENDPOINTS, None)
            return self.async_update_and_abort(
                current_entry,
                data=data,
                options={**current_entry.options, **({CONF_REGISTRAR_ENABLED: bool(registrar_choice)}
                    if registrar_choice is not None and CONF_REGISTRAR_ENABLED in current_entry.options else {})},
                reason="reconfigure_successful",
            )
        return self.async_create_entry(title="VoIP Stack", data=data)

    async def async_step_reconfigure(self, user_input=None):
        """Reconfigure the existing VoIP Stack entry from HA's native menu."""
        return self.async_show_menu(step_id="reconfigure", menu_options=["user", "parking", "voicemail", "phone_services", "phone_provisioning", "esphome_enrollment"])

    async def async_step_esphome_enrollment(self, user_input=None):
        """Explicitly re-enroll an ESP whose managed account was removed."""
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers.selector import DeviceSelector, DeviceSelectorConfig
        from .managed_phones import CONF_ESPHOME_ENROLLMENTS

        entry = self._current_entry()
        errors = {}
        if user_input is not None and entry is not None:
            device_id = user_input["device_id"]
            receipts = dict(entry.data.get(CONF_ESPHOME_ENROLLMENTS, {}))
            receipt = receipts.get(device_id)
            device = dr.async_get(self.hass).async_get(device_id)
            if (device is None or not isinstance(receipt, dict) or "endpoint_id" in receipt
                    or device.config_entry_id != receipt.get("origin_entry_id")):
                errors["base"] = "managed_esphome_missing"
            else:
                receipts.pop(device_id)
                result = self.async_update_and_abort(entry,
                    data_updates={CONF_ESPHOME_ENROLLMENTS: receipts}, reason="reconfigure_successful")
                runtime = getattr(entry, "runtime_data", None)
                if runtime is not None and runtime.phone_provisioning is not None:
                    runtime.phone_provisioning.schedule()
                return result
        return self.async_show_form(step_id="esphome_enrollment", errors=errors, data_schema=vol.Schema({
            vol.Required("device_id"): DeviceSelector(DeviceSelectorConfig(integration="esphome")),
        }))

    async def async_step_phone_provisioning(self, user_input=None):
        """Download an optional index only after explicit opt-in."""
        from .physical_phone import CONF_PHONE_PROVISIONING, discovery_enabled
        entry = self._current_entry()
        if entry is None:
            return self.async_abort(reason="configure_pbx_first")
        values = dict(entry.data.get(CONF_PHONE_PROVISIONING, {}))
        values.setdefault("discovery_enabled", discovery_enabled(entry))
        errors = {}
        if user_input is not None:
            previous = dict(values)
            values.update(user_input)
            provisioning_changed = not previous.get("enabled", False) or any(
                values.get(key, default) != previous.get(key, default)
                for key, default in (("base_url", ""), ("allow_insecure_http", False)))
            if values.get("enabled") and provisioning_changed:
                from .physical_phone_provisioning import provisioning_base_url
                from .phone_profile_catalog import PhoneProfileCatalog
                try:
                    provisioning_base_url(self.hass, entry, config=values)
                except ValueError as error:
                    errors["base"] = str(error)
                if not errors:
                    try:
                        await PhoneProfileCatalog(self.hass).async_manifest()
                    except Exception:
                        errors["base"] = "phone_catalog_unavailable"
            if not errors:
                return self.async_update_and_abort(entry,
                    data_updates={CONF_PHONE_PROVISIONING: values}, reason="reconfigure_successful")
        return self.async_show_form(step_id="phone_provisioning", errors=errors, data_schema=vol.Schema({
            vol.Required("discovery_enabled", default=values["discovery_enabled"]): BooleanSelector(),
            vol.Required("enabled", default=bool(values.get("enabled", False))): BooleanSelector(),
            vol.Optional("base_url", default=values.get("base_url", "")): TextSelector(),
            vol.Required("allow_insecure_http", default=bool(values.get("allow_insecure_http", False))): BooleanSelector(),
        }))

    async def async_step_dhcp(self, discovery_info):
        """Admit a native DHCP hint into the same physical-phone proposal."""
        from .physical_phone import discovery_enabled, physical_phone_records
        from .physical_phone_discovery import dhcp_identity
        from .runtime_data import runtime_data

        try:
            mac, _host, _vendor, _model = dhcp_identity(discovery_info)
        except ValueError:
            return self.async_abort(reason="invalid_phone_discovery")
        entry = self._current_entry()
        if entry is None:
            return self.async_abort(reason="configure_pbx_first")
        if any(phone["mac"] == mac for phone in physical_phone_records(entry).values()):
            return self.async_abort(reason="already_configured")
        runtime = runtime_data(self.hass)
        discovery = getattr(runtime, "phone_discovery", None)
        if not discovery_enabled(entry) or discovery is None or not discovery.owned():
            return self.async_abort(reason="phone_discovery_expired")
        candidate = discovery.observe_dhcp(discovery_info, start_flow=False)
        if candidate is None:
            return self.async_abort(reason="invalid_phone_discovery")
        result = await self.async_step_integration_discovery({"discovery_candidate": candidate.public()})
        if result["type"] == "form":
            candidate.flow_id, candidate._flow_context = self.flow_id, self.context
        return result

    async def async_step_integration_discovery(self, discovery_info):
        """Native discovery card chains into the existing PBX's child editor."""
        candidate = discovery_info.get("discovery_candidate", {}) if isinstance(discovery_info, Mapping) else {}
        if not isinstance(candidate, Mapping):
            return self.async_abort(reason="invalid_phone_discovery")
        mac = str(candidate.get("mac") or "")
        if not re.fullmatch(r"[0-9a-f]{12}", mac):
            return self.async_abort(reason="invalid_phone_discovery")
        await self.async_set_unique_id("physical_phone:" + mac)
        self._abort_if_unique_id_configured()
        self._physical_candidate_mac = mac
        entry = self._current_entry()
        if entry is None:
            return self.async_abort(reason="configure_pbx_first")
        self._physical_entry_id = entry.entry_id
        return await self.async_step_phone_discovered()

    def _observed_phone_candidate(self):
        """Read the current owner and announcement without allocating a draft."""
        from .physical_phone import discovery_enabled, physical_phone_records
        from .runtime_data import runtime_data
        entry = self._current_entry()
        if entry is None:
            return None, None, "configure_pbx_first"
        if entry.entry_id != getattr(self, "_physical_entry_id", None) or not discovery_enabled(entry):
            return entry, None, "phone_discovery_expired"
        mac = getattr(self, "_physical_candidate_mac", "")
        if any(phone["mac"] == mac for phone in physical_phone_records(entry).values()):
            return entry, None, "already_configured"
        runtime = runtime_data(self.hass)
        discovery = getattr(runtime, "phone_discovery", None)
        observed = discovery.candidates.get(mac) if discovery and not getattr(discovery, "_closed", False) else None
        if observed is None or time.monotonic() - observed.seen >= 86400:
            return entry, None, "phone_discovery_expired"
        return entry, observed, None

    async def async_step_phone_discovered(self, user_input=None):
        from .physical_phone import phone_candidate_name, provisioning_enabled
        entry, observed, error = self._observed_phone_candidate()
        if error:
            return self.async_abort(reason=error)
        name = phone_candidate_name(observed.vendor, observed.model) or observed.mac
        self.context["title_placeholders"] = {"name": name}
        if user_input is None or not provisioning_enabled(entry):
            return self.async_show_form(step_id="phone_discovered", data_schema=vol.Schema({}),
                errors={"base": "phone_provisioning_disabled"} if user_input is not None else {},
                description_placeholders={"name": name, "mac": ":".join(observed.mac[i:i + 2] for i in range(0, 12, 2)),
                    "host": observed.host, "vendor": observed.vendor or "-", "model": observed.model or "-"})
        if not hasattr(self, "_phone_editor"):
            from .physical_phone_flow import DiscoveredPhoneEditor
            self._phone_editor = DiscoveredPhoneEditor(self, entry, observed)
            return await self._phone_editor.async_step_user({"discovery_candidate": observed.setup_data()})
        return await self._phone_editor.async_step_user(user_input)

    async def async_step_phone_user(self, user_input=None):
        return await self._phone_editor.async_step_user(user_input)

    async def async_step_phone_language(self, user_input=None):
        return await self._phone_editor.async_step_language(user_input)

    async def async_step_phone_bindings(self, user_input=None):
        return await self._phone_editor.async_step_bindings(user_input)

    async def async_step_phone_account(self, user_input=None):
        return await self._phone_editor.async_step_account(user_input)

    async def async_step_user(self, user_input=None):
        """Handle install/reconfigure of the single VoIP Stack entry.

        SIP is the only HA call-control protocol. HA is always both a
        softphone and SIP router/B2BUA, and it listens on both SIP/UDP and
        SIP/TCP like a normal SIP endpoint.
        """
        _current_entry, existing = self._current_entry_data()
        if _current_entry is not None and self.source == SOURCE_USER:
            return self.async_abort(reason="single_instance_allowed")
        defaults = {
            "sip_port": existing.get("sip_port", VOIP_STACK_SIP_PORT),
            "rtp_port": existing.get("rtp_port", VOIP_STACK_RTP_PORT),
            "advertise_host": existing.get("advertise_host", ""),
            CONF_ASSIST_INTENTS: existing.get(CONF_ASSIST_INTENTS, False),
            CONF_ASSIST_ENDPOINT_ENABLED: existing.get(
                CONF_ASSIST_ENDPOINT_ENABLED, False
            ),
            CONF_DEBUG_MODE: existing.get(CONF_DEBUG_MODE, False),
            CONF_MEDIA_CAPTURE: existing.get(CONF_MEDIA_CAPTURE, False),
            CONF_SIP_VIDEO: existing.get(CONF_SIP_VIDEO, False),
            CONF_REGISTRAR_ENABLED: existing.get(CONF_REGISTRAR_ENABLED, False),
        }
        schema = vol.Schema(
            {
                vol.Required(
                    "sip_port", default=defaults["sip_port"]
                ): _port_selector(),
                vol.Required(
                    "rtp_port", default=defaults["rtp_port"]
                ): _port_selector(),
                vol.Optional("advertise_host", default=defaults["advertise_host"]): str,
                vol.Required(
                    CONF_ASSIST_INTENTS,
                    default=defaults[CONF_ASSIST_INTENTS],
                ): BooleanSelector(),
                vol.Required(
                    CONF_ASSIST_ENDPOINT_ENABLED,
                    default=defaults[CONF_ASSIST_ENDPOINT_ENABLED],
                ): BooleanSelector(),
                vol.Required(
                    CONF_DEBUG_MODE, default=defaults[CONF_DEBUG_MODE]
                ): BooleanSelector(),
                vol.Required(
                    CONF_MEDIA_CAPTURE, default=defaults[CONF_MEDIA_CAPTURE]
                ): BooleanSelector(),
                vol.Required(
                    CONF_SIP_VIDEO,
                    default=defaults[CONF_SIP_VIDEO],
                ): BooleanSelector(),
                vol.Required(
                    CONF_REGISTRAR_ENABLED, default=defaults[CONF_REGISTRAR_ENABLED]
                ): BooleanSelector(),
                **codec_form_fields(existing),
            }
        )
        errors: dict[str, str] = {}

        if user_input is not None:
            codec_errors, _inherited = normalize_codec_form(user_input, user_input, existing)
            errors.update(codec_errors)
            # Number selectors hand floats; coerce to int once on the boundary.
            for k in ("sip_port", "rtp_port"):
                user_input[k] = int(user_input[k])
            for k in ("advertise_host",):
                user_input[k] = (user_input.get(k) or "").strip()
            if user_input["sip_port"] == user_input["rtp_port"]:
                errors["base"] = "sip_rtp_port_conflict"

            if not errors:
                self._base_input = dict(user_input)
                if user_input[CONF_SIP_VIDEO]:
                    return await self.async_step_video()
                self._base_input[CONF_VIDEO_TRANSCODING] = False
                self._base_input[CONF_VIDEO_CAMERA_SEND] = False
                if user_input[CONF_ASSIST_ENDPOINT_ENABLED]:
                    return await self.async_step_assist()
                self._base_input[CONF_ASSIST_EXTENSION] = str(
                    existing.get(CONF_ASSIST_EXTENSION) or ""
                ).strip()
                self._base_input[CONF_ASSIST_PIPELINE] = str(
                    existing.get(CONF_ASSIST_PIPELINE) or ""
                ).strip()
                self._base_input[CONF_ASSIST_ADVANCED_CALL_CONTEXT] = bool(
                    existing.get(CONF_ASSIST_ADVANCED_CALL_CONTEXT, False)
                )
                data = dict(self._base_input)
                current_entry, _existing = self._current_entry_data()
                if current_entry is None:
                    await self.async_set_unique_id(DOMAIN)
                    self._abort_if_unique_id_configured()
                return await self._finish_optional_features(data)

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_video(self, user_input=None):
        """Configure optional browser video features without affecting audio."""

        _current_entry, existing = self._current_entry_data()
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_VIDEO_TRANSCODING,
                    default=bool(existing.get(CONF_VIDEO_TRANSCODING, False)),
                ): BooleanSelector(),
                vol.Required(
                    CONF_VIDEO_CAMERA_SEND,
                    default=bool(existing.get(CONF_VIDEO_CAMERA_SEND, False)),
                ): BooleanSelector(),
            }
        )
        if user_input is not None:
            assert self._base_input is not None
            self._base_input[CONF_VIDEO_TRANSCODING] = bool(
                user_input[CONF_VIDEO_TRANSCODING]
            )
            self._base_input[CONF_VIDEO_CAMERA_SEND] = bool(
                user_input[CONF_VIDEO_CAMERA_SEND]
            )
            if self._base_input[CONF_ASSIST_ENDPOINT_ENABLED]:
                return await self.async_step_assist()
            self._base_input[CONF_ASSIST_EXTENSION] = str(
                existing.get(CONF_ASSIST_EXTENSION) or ""
            ).strip()
            self._base_input[CONF_ASSIST_PIPELINE] = str(
                existing.get(CONF_ASSIST_PIPELINE) or ""
            ).strip()
            self._base_input[CONF_ASSIST_ADVANCED_CALL_CONTEXT] = bool(
                existing.get(CONF_ASSIST_ADVANCED_CALL_CONTEXT, False)
            )
            data = dict(self._base_input)
            current_entry, _existing = self._current_entry_data()
            if current_entry is None:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
            return await self._finish_optional_features(data)
        return self.async_show_form(step_id="video", data_schema=schema)

    async def async_step_assist(self, user_input=None):
        """Configure the optional local Assist pipeline extension."""
        current_entry, existing = self._current_entry_data()
        suggested = str(existing.get(CONF_ASSIST_EXTENSION) or "").strip()
        pipeline = str(existing.get(CONF_ASSIST_PIPELINE) or "").strip()
        advanced_call_context = bool(
            existing.get(CONF_ASSIST_ADVANCED_CALL_CONTEXT, False)
        )
        extension_key = (
            vol.Required(CONF_ASSIST_EXTENSION, default=suggested)
            if suggested
            else vol.Required(CONF_ASSIST_EXTENSION)
        )
        # HA's native selector represents "Preferred assistant" as an empty
        # selection, while named pipelines carry their concrete pipeline ID.
        pipeline_key = (
            vol.Optional(
                CONF_ASSIST_PIPELINE, description={"suggested_value": pipeline}
            )
            if pipeline and pipeline != "preferred"
            else vol.Optional(CONF_ASSIST_PIPELINE)
        )
        schema = vol.Schema(
            {
                extension_key: TextSelector(),
                pipeline_key: AssistPipelineSelector(),
                vol.Required(
                    CONF_ASSIST_ADVANCED_CALL_CONTEXT,
                    default=advanced_call_context,
                ): BooleanSelector(),
            }
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            extension = str(user_input.get(CONF_ASSIST_EXTENSION) or "").strip()
            if not extension.isdigit() or not 1 <= len(extension) <= 255:
                errors["base"] = "assist_extension_invalid"
            elif (
                extension_conflicts(extension, existing)
                or route_namespace_conflicts(
                    candidate_routes=(extension,),
                    existing=_entry_route_mappings(
                        current_entry,
                        existing,
                        include_assist=False,
                    ),
                )
            ):
                errors["base"] = "assist_extension_conflict"
            if not errors:
                assert self._base_input is not None
                self._base_input[CONF_ASSIST_EXTENSION] = extension
                self._base_input[CONF_ASSIST_PIPELINE] = str(
                    user_input.get(CONF_ASSIST_PIPELINE) or "preferred"
                ).strip()
                self._base_input[CONF_ASSIST_ADVANCED_CALL_CONTEXT] = bool(
                    user_input.get(CONF_ASSIST_ADVANCED_CALL_CONTEXT, False)
                )
                data = dict(self._base_input)
                current_entry, _existing = self._current_entry_data()
                if current_entry is None:
                    await self.async_set_unique_id(DOMAIN)
                    self._abort_if_unique_id_configured()
                return await self._finish_optional_features(data)
        return self.async_show_form(step_id="assist", data_schema=schema, errors=errors)


    async def _finish_optional_features(self, data):
        return self._store_entry(data)

    async def async_step_voicemail(self, user_input=None):
        """Configure the shared IVR voice independently of basic phone setup."""
        from .voicemail_config import voice_engine, prompt_languages

        _entry, existing = self._current_entry_data()
        self._base_input = self._base_input or {}
        current = existing.get("voicemail", {})
        errors = {}
        if user_input is not None:
            identity = str(user_input.get("tts_entity_id") or "")
            entity = voice_engine(self.hass, identity)
            if entity is None or not entity.available:
                errors["tts_entity_id"] = "voicemail_tts_unavailable"
            elif not prompt_languages(entity):
                errors["tts_entity_id"] = "voicemail_language_unsupported"
            else:
                self._voicemail_tts = identity
                return await self.async_step_voicemail_language()
        return self.async_show_form(step_id="voicemail", errors=errors, data_schema=vol.Schema({
            vol.Required("tts_entity_id", default=current.get("tts_entity_id", "")):
                EntitySelector(EntitySelectorConfig(domain="tts")),
        }))

    async def async_step_voicemail_language(self, user_input=None):
        from .voicemail_config import voice_engine, prompt_languages

        entity = voice_engine(self.hass, self._voicemail_tts)
        if entity is None or not entity.available:
            return await self.async_step_voicemail({"tts_entity_id": self._voicemail_tts})
        languages = prompt_languages(entity)
        _entry, existing = self._current_entry_data()
        previous = existing.get("voicemail", {}).get("language")
        default = previous if previous in languages else entity.default_language
        if default not in languages:
            default = next(iter(languages), "")
        errors = {}
        if user_input is not None:
            language = str(user_input.get("language") or "")
            if language not in languages:
                errors["language"] = "voicemail_language_unsupported"
            elif mailbox_namespace_conflicts(_entry_route_mappings(_entry, existing)):
                errors["base"] = "voicemail_route_conflict"
            else:
                self._base_input["voicemail"] = {"tts_entity_id": self._voicemail_tts,
                                                  "language": language}
                voices = getattr(entity, "async_get_supported_voices", lambda _language: None)(language)
                if voices:
                    return await self.async_step_voicemail_voice()
                return self._store_entry(dict(self._base_input))
        return self.async_show_form(step_id="voicemail_language", errors=errors, data_schema=vol.Schema({
            vol.Required("language", default=default or ""):
                SelectSelector(SelectSelectorConfig(options=languages, mode="dropdown")),
        }))



    async def async_step_voicemail_voice(self, user_input=None):
        """Select a voice from the existing HA TTS provider's catalog."""
        from .voicemail_config import voice_engine

        settings = self._base_input["voicemail"]
        entity = voice_engine(self.hass, settings["tts_entity_id"])
        if entity is None or not entity.available:
            return await self.async_step_voicemail()
        voices = getattr(entity, "async_get_supported_voices", lambda _language: None)(settings["language"]) or []
        choices = [{"value": "", "label": "TTS default voice"},
                   *({"value": voice.voice_id, "label": voice.name} for voice in voices)]
        valid = {choice["value"] for choice in choices}
        _entry, existing = self._current_entry_data()
        current = str(existing.get("voicemail", {}).get("voice") or "")
        errors = {}
        if user_input is not None:
            voice = str(user_input.get("voice") or "")
            if voice not in valid:
                errors["voice"] = "voicemail_voice_unavailable"
            elif mailbox_namespace_conflicts(_entry_route_mappings(_entry, existing)):
                errors["base"] = "voicemail_route_conflict"
            else:
                if voice:
                    settings["voice"] = voice
                else:
                    settings.pop("voice", None)
                return self._store_entry(dict(self._base_input))
        return self.async_show_form(step_id="voicemail_voice", errors=errors, data_schema=vol.Schema({
            vol.Required("voice", default=current if current in valid else ""):
                SelectSelector(SelectSelectorConfig(options=choices, mode="dropdown")),
        }))


    async def async_step_phone_services(self, user_input=None):
        """Configure the integration-owned standard LDAP directory."""
        from .phone_services import CONF_PHONE_SERVICES, settings, validate_settings

        entry, _existing = self._current_entry_data()
        current = settings(entry)
        errors = {}
        if user_input is not None:
            current = {**current, **user_input, "ldap_port": int(user_input["ldap_port"])}
            for name in ("listen_host", "base_dn", "certificate", "private_key"):
                current[name] = current[name].strip()
            try:
                validate_settings(current)
            except ValueError as error:
                errors["base"] = str(error)
            else:
                return self._store_entry({CONF_PHONE_SERVICES: current})
        return self.async_show_form(step_id="phone_services", errors=errors, data_schema=vol.Schema({
            vol.Required("enabled", default=current["enabled"]): BooleanSelector(),
            vol.Required("listen_host", default=current["listen_host"]): str,
            vol.Required("ldap_port", default=current["ldap_port"]): _port_selector(),
            vol.Required("base_dn", default=current["base_dn"]): str,
            vol.Required("allow_anonymous", default=current["allow_anonymous"]): BooleanSelector(),
            vol.Required("tls", default=current["tls"]): BooleanSelector(),
            visible_optional("certificate", default=current["certificate"], visible={"field": "tls", "value": True}): str,
            visible_optional("private_key", default=current["private_key"], visible={"field": "tls", "value": True}): str,
        }))

    async def async_step_parking(self, user_input=None):
        from .parking_config import DEFAULT_SLOTS, normalize_parking_config, parking_namespace_conflicts
        from .runtime_data import runtime_data

        entry, existing = self._current_entry_data()
        self._base_input = self._base_input or {}
        current = existing.get("parking", {})
        slots = current.get("slots") or DEFAULT_SLOTS
        options = [{"value": "", "label": "Phone that parked the call"}]
        runtime = runtime_data(self.hass)
        if runtime is not None:
            options.extend({"value": phone.endpoint_id, "label": phone.name} for phone in runtime.endpoints.endpoints)
        errors = {}
        if user_input is not None:
            try:
                candidate = normalize_parking_config({
                    "access": user_input["access"],
                    "slots": str(user_input["slots"]).replace("\n", ",").split(","),
                    "timeout": user_input["timeout"], "return_timeout": user_input["return_timeout"],
                    "return_destination": user_input.get("return_destination", ""),
                    "hold_media": "" if user_input.get("clear_hold_media") else
                        user_input.get("hold_media", {}).get("media_content_id", current.get("hold_media", "")),
                })
                mappings = _entry_route_mappings(entry, existing)
                owner = runtime.sip if runtime is not None else None
                lot = owner.component("parking") if owner is not None else None
                if lot is not None and any(slot not in candidate["slots"] for slot in lot.calls):
                    errors["base"] = "parking_slots_in_use"
                elif parking_namespace_conflicts(candidate, mappings):
                    errors["base"] = "parking_route_conflict"
                else:
                    self._base_input["parking"] = candidate
                    return self._store_entry(dict(self._base_input))
            except (TypeError, ValueError):
                errors["base"] = "parking_configuration_invalid"
        return self.async_show_form(step_id="parking", errors=errors, data_schema=vol.Schema({
            vol.Required("access", default=current.get("access", "700")): str,
            vol.Required("slots", default=", ".join(slots)): TextSelector(TextSelectorConfig(multiline=True)),
            vol.Required("timeout", default=current.get("timeout", 180)): NumberSelector(NumberSelectorConfig(min=1, max=3600, mode="box", unit_of_measurement="s")),
            vol.Required("return_timeout", default=current.get("return_timeout", 30)): NumberSelector(NumberSelectorConfig(min=1, max=3600, mode="box", unit_of_measurement="s")),
            vol.Optional("return_destination", default=current.get("return_destination", "")): SelectSelector(SelectSelectorConfig(options=options, custom_value=True, mode="dropdown")),
            vol.Optional("hold_media"): MediaSelector({"accept": ["audio/*"]}),
            vol.Optional("clear_hold_media", default=False): BooleanSelector(),
        }))


class PhoneSubentryFlowHandler(ConfigSubentryFlow):
    """Add or reconfigure one logical SIP account."""

    _kind: EndpointKind | None = None
    _reconfigure = False
    _suggested_password = ""
    _account_draft = None
    _physical_binding_options = None

    @property
    def account_draft(self):
        """Expose the same preallocated record to a containing physical flow."""
        return self._account_draft

    def _current_data(self) -> Mapping[str, Any]:
        if not self._reconfigure:
            return {}
        from .phone_queue_membership import project_phone_memberships
        row = self._get_reconfigure_subentry()
        return project_phone_memberships(self._get_entry(), phone_endpoint_id(row), phone_subentry_data(row))

    def _common_schema(self, *, sip_account: bool) -> vol.Schema:
        from .group_flow import destination_options
        from .group_config import group_records
        from .phone_queue_membership import membership_names, normalize_phone_queues
        from .phone_policy import DestinationKind

        current = self._current_data()
        entry = self._get_entry()
        destinations = destination_options(self.hass, entry)
        group_choices = {kind: [{'value': row['name'], 'label': row['name']}
                               for row in group_records(entry, kind).values()] for kind in ('ring', 'queue')}
        runtime = getattr(entry, 'runtime_data', None)
        directory = getattr(runtime, 'directory', None)
        conferences = {row.name for row in directory.snapshot() if row.metadata.get('group_type') == 'conference'} if directory is not None else set()
        conference_choices = [{'value': name, 'label': name} for name in sorted(conferences)]
        policy = persisted_phone_policy(current)

        def action(destination):
            return 'none' if destination is None else 'forward' if destination.kind is DestinationKind.CONTACT else destination.kind.value

        def target(destination):
            return destination.target if destination is not None else ''

        def destination_selector():
            return SelectSelector({'options': destinations, 'custom_value': True, 'mode': 'dropdown'})

        fields: dict[Any, Any] = {
            vol.Required(
                CONF_PHONE_NAME,
                default=str(current.get(CONF_PHONE_NAME) or ""),
            ): TextSelector(),
            vol.Optional(
                CONF_PHONE_EXTENSION,
                default=str(current.get(CONF_PHONE_EXTENSION) or ""),
            ): TextSelector(),
            vol.Required(
                CONF_PHONE_ENABLED,
                default=bool(current.get(CONF_PHONE_ENABLED, True)),
            ): BooleanSelector(),
            vol.Required(
                CONF_PHONE_DND,
                default=bool(current.get(CONF_PHONE_DND, False)),
            ): BooleanSelector(),
            vol.Optional(
                CONF_PHONE_RING_GROUP,
                default=list(membership_names('ring_group', current.get(CONF_PHONE_RING_GROUP))),
            ): SelectSelector({'options': group_choices['ring'], 'multiple': True, 'custom_value': True, 'mode': 'dropdown'}),
            vol.Optional(CONF_PHONE_QUEUES, default=list(normalize_phone_queues(current.get(CONF_PHONE_QUEUES)))):
                SelectSelector({'options': group_choices['queue'], 'multiple': True, 'custom_value': True, 'mode': 'dropdown'}),
            vol.Optional(
                CONF_PHONE_CONFERENCE_GROUP,
                default=list(membership_names('ring_group', current.get(CONF_PHONE_CONFERENCE_GROUP))),
            ): SelectSelector({'options': conference_choices, 'multiple': True, 'custom_value': True, 'mode': 'dropdown'}),
            vol.Required(
                CONF_PHONE_CONFERENCE_RING,
                default=bool(current.get(CONF_PHONE_CONFERENCE_RING, False)),
            ): BooleanSelector(),
            vol.Required(
                CONF_PHONE_VIDEO_ENABLED,
                default=bool(current.get(CONF_PHONE_VIDEO_ENABLED, True)),
            ): BooleanSelector(),
        }
        if sip_account:
            for field, slot in (('forward', 'always'), ('busy', 'busy'), ('unavailable', 'unavailable')):
                destination = getattr(policy, slot)
                fields[vol.Optional(field + '_action', default=action(destination))] = SelectSelector({
                    'options': ['none', 'forward', 'voicemail', 'hangup'], 'translation_key': 'phone_forward_action'})
                fields[vol.Optional(field + '_to', default=target(destination))] = destination_selector()
            fields[vol.Optional('ringing_timeout', default=policy.ringing_timeout or 0)] = NumberSelector({
                'min': 0, 'max': 3600, 'step': 0.1, 'unit_of_measurement': 's', 'mode': 'box'})
            fields[vol.Optional('no_answer_action', default=action(policy.no_answer) if policy.no_answer else 'forward')] = SelectSelector({
                'options': ['forward', 'voicemail', 'hangup'], 'translation_key': 'phone_forward_action'})
            fields[vol.Optional('no_answer_to', default=target(policy.no_answer))] = destination_selector()
            if not self._suggested_password:
                self._suggested_password = str(
                    current.get(CONF_PHONE_PASSWORD) or generate_password()
                )
            fields.update(
                {
                    vol.Optional(
                        CONF_PHONE_USERNAME,
                        default=str(current.get(CONF_PHONE_USERNAME) or ""),
                    ): TextSelector(),
                    vol.Required(
                        CONF_PHONE_PASSWORD,
                        default=self._suggested_password,
                    ): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            )
        fields.update({
            vol.Required(CONF_PHONE_AUTO_ANSWER, default=bool(current.get(CONF_PHONE_AUTO_ANSWER, False))): BooleanSelector(),
            visible_required(CONF_PHONE_SEND_VIDEO, default=bool(current.get(CONF_PHONE_SEND_VIDEO, False)),
                visible={"field": CONF_PHONE_VIDEO_ENABLED, "value": True}): BooleanSelector(),
        })
        fields.update(codec_form_fields(current, inherit=True, parent=self._get_entry().data))
        if sip_account:
            from .physical_phone import (
                account_physical_bindings, account_physical_bindings_form_rows,
                account_physical_bindings_selector,
            )
            current_bindings = account_physical_bindings(
                self._get_entry(), str(current.get(CONF_PHONE_ENDPOINT_ID) or ""))
            fields[vol.Optional("physical_bindings", default=account_physical_bindings_form_rows(
                current_bindings))] = account_physical_bindings_selector(
                    self._get_entry(), options=self._physical_binding_options)
        fields[vol.Optional("voicemail_enabled", default=bool(current.get("voicemail", {}).get("enabled")))] = BooleanSelector()
        return vol.Schema(fields)

    def _validate_aliases(
        self,
        *,
        name: str,
        extension: str,
        username: str,
        ring_group: str,
        conference_group: str,
        voicemail: Mapping[str, Any],
    ) -> bool:
        current_id = (
            self._get_reconfigure_subentry().subentry_id
            if self._reconfigure
            else ""
        )
        entry = self._get_entry()
        return not route_namespace_conflicts(
            candidate_routes=(extension,) if username else (name, extension),
            candidate_username=username,
            candidate_mailbox_routes=mailbox_route_aliases({
                **self._current_data(), "name": name, "extension": extension, "username": username,
                "voicemail": voicemail,
            }),
            candidate_groups=_group_route_values(
                ring_group,
                conference_group,
            ),
            existing=_entry_route_mappings(
                entry,
                entry.data,
                exclude_subentry_id=current_id,
            ),
        )

    def _normalized_common(
        self,
        user_input: Mapping[str, Any],
        *,
        kind: EndpointKind,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        errors: dict[str, str] = {}
        from .phone_queue_membership import membership_names, normalize_phone_queues
        from .phone_policy import PhonePolicy
        from .sip_account_identity import canonical_ha_destination
        from .service_endpoints import resolve_public_phone
        current = self._current_data()
        values = {**current, **user_input}
        membership_values = {}
        for field in (CONF_PHONE_RING_GROUP, CONF_PHONE_QUEUES, CONF_PHONE_CONFERENCE_GROUP):
            try:
                names = normalize_phone_queues(values.get(field)) if field == CONF_PHONE_QUEUES else membership_names('ring_group', values.get(field))
                membership_values[field] = list(names) if field == CONF_PHONE_QUEUES else ', '.join(names)
            except ValueError:
                membership_values[field] = [] if field == CONF_PHONE_QUEUES else ''
                errors[field] = 'phone_groups_invalid'
        unavailable = persisted_phone_policy(current).unavailable
        previous_offline_target = unavailable.target if unavailable is not None else ""
        offline_target = str(user_input.get(CONF_PHONE_OFFLINE_FORWARD_TARGET, previous_offline_target) or "").strip()
        voicemail = dict(current.get("voicemail", {}))
        if "voicemail_enabled" in user_input:
            voicemail["enabled"] = bool(user_input["voicemail_enabled"])
        name = str(values.get(CONF_PHONE_NAME) or "").strip()
        extension = str(values.get(CONF_PHONE_EXTENSION) or "").strip()
        username = ""
        password = ""
        if kind is EndpointKind.SIP_ACCOUNT:
            username = str(values.get(CONF_PHONE_USERNAME) or "").strip()
            if not username:
                username = name
            try:
                username = normalize_username(username)
            except ValueError:
                errors[CONF_PHONE_USERNAME] = "phone_username_invalid"
            password = str(values.get(CONF_PHONE_PASSWORD) or "")
            if not password:
                errors[CONF_PHONE_PASSWORD] = "phone_password_required"
        if not name:
            errors[CONF_PHONE_NAME] = "phone_name_required"
        if (
            kind is EndpointKind.SIP_ACCOUNT
            and str(user_input.get(CONF_PHONE_OFFLINE_POLICY))
            == OfflinePolicy.FORWARD.value
            and not offline_target
        ):
            errors[CONF_PHONE_OFFLINE_FORWARD_TARGET] = "phone_forward_target_required"
        if not self._validate_aliases(
            name=name,
            extension=extension,
            username=username,
            ring_group=membership_values[CONF_PHONE_RING_GROUP],
            conference_group=membership_values[CONF_PHONE_CONFERENCE_GROUP],
            voicemail=voicemail,
        ):
            errors["base"] = "phone_alias_conflict"
        endpoint_id = str(current.get(CONF_PHONE_ENDPOINT_ID) or "")
        if not self._reconfigure and not errors:
            self._account_draft = draft_phone_subentry(
                {CONF_PHONE_KIND: kind.value, CONF_PHONE_NAME: name, CONF_PHONE_USERNAME: username}, previous=self._account_draft, title=name)
            endpoint_id = phone_endpoint_id(self._account_draft)
        data = {
            **current,
            **({"voicemail": voicemail} if voicemail else {}),
            CONF_PHONE_ENDPOINT_ID: endpoint_id,
            CONF_PHONE_KIND: kind.value,
            CONF_PHONE_NAME: name,
            CONF_PHONE_EXTENSION: extension,
            CONF_PHONE_USERNAME: username,
            'username_source': 'user',
            CONF_PHONE_PASSWORD: password,
            CONF_PHONE_ENABLED: bool(values.get(CONF_PHONE_ENABLED, True)),
            CONF_PHONE_DND: bool(values.get(CONF_PHONE_DND, False)),
            **membership_values,
            CONF_PHONE_CONFERENCE_RING: bool(
                membership_values[CONF_PHONE_CONFERENCE_GROUP] and values.get(CONF_PHONE_CONFERENCE_RING, False)
            ),
            CONF_PHONE_VIDEO_ENABLED: bool(
                values.get(CONF_PHONE_VIDEO_ENABLED, True)
            ),
        }
        # Reconfiguring a name/account must preserve separately edited policies.
        policy_data = persisted_phone_policy(current).as_data()
        if kind is EndpointKind.SIP_ACCOUNT:
            if CONF_PHONE_OFFLINE_POLICY in user_input:
                if user_input[CONF_PHONE_OFFLINE_POLICY] == OfflinePolicy.FORWARD.value:
                    policy_data["unavailable"] = {
                        "kind": "contact",
                        "target": offline_target,
                    }
                else:
                    policy_data.pop("unavailable", None)
            runtime = getattr(self._get_entry(), 'runtime_data', None)

            def selected_target(value, field):
                selected = canonical_ha_destination(self.hass, str(value or '').strip())
                from homeassistant.exceptions import ServiceValidationError
                try:
                    endpoint = resolve_public_phone(runtime.endpoints, selected, directory=runtime.directory) if runtime is not None and selected else None
                except ServiceValidationError:
                    errors[field] = 'phone_alias_conflict'
                    return ''
                return endpoint.endpoint_id if endpoint is not None else selected

            def destination(field, selected_action):
                if selected_action == 'none':
                    return None
                if selected_action == 'hangup':
                    return {'kind': 'hangup'}
                selected = selected_target(user_input.get(field + '_to', ''), field + '_to')
                if not selected:
                    errors.setdefault(field + '_to', 'phone_forward_target_required')
                    return None
                if selected_action == 'forward' and selected.casefold() in {username.casefold(), endpoint_id.casefold()}:
                    errors[field + '_to'] = 'phone_forward_loop'
                if selected_action == 'voicemail':
                    from .mailbox import mailbox_settings
                    enabled = bool(voicemail.get('enabled')) if selected.casefold() in {username.casefold(), endpoint_id.casefold()} else (
                        bool(mailbox_settings(self.hass, selected).get('enabled')) if runtime is not None else False)
                    if not enabled:
                        errors[field + '_to'] = 'phone_mailbox_required'
                return {'kind': 'voicemail' if selected_action == 'voicemail' else 'contact', 'target': selected}

            for field, slot in (('forward', 'always'), ('busy', 'busy'), ('unavailable', 'unavailable')):
                if field + '_action' in user_input or field + '_to' in user_input:
                    selected = destination(field, user_input.get(field + '_action', 'forward'))
                    if selected is None:
                        policy_data.pop(slot, None)
                    else:
                        policy_data[slot] = selected
            if 'ringing_timeout' in user_input or 'no_answer_action' in user_input or 'no_answer_to' in user_input:
                try:
                    timeout = float(user_input.get('ringing_timeout', policy_data.get('ringing_timeout') or 0))
                except (TypeError, ValueError):
                    errors['ringing_timeout'] = 'phone_policy_invalid'
                    timeout = 0
                if timeout == 0:
                    policy_data.pop('ringing_timeout', None)
                    policy_data.pop('no_answer', None)
                else:
                    policy_data['ringing_timeout'] = timeout
                    policy_data['no_answer'] = destination('no_answer', user_input.get('no_answer_action', 'forward'))
            try:
                PhonePolicy.from_data(policy_data)
            except (ValueError, TypeError):
                errors['base'] = 'phone_policy_invalid'
            data.pop(CONF_PHONE_OFFLINE_POLICY, None)
            data.pop(CONF_PHONE_OFFLINE_FORWARD_TARGET, None)
        data.update({CONF_PHONE_AUTO_ANSWER: bool(values.get(CONF_PHONE_AUTO_ANSWER, False)),
                     CONF_PHONE_SEND_VIDEO: bool(values.get(CONF_PHONE_SEND_VIDEO, False))})
        data[CONF_PHONE_POLICY] = policy_data
        codec_errors, _inherited = normalize_codec_form(data, user_input, current, inherit=True)
        errors.update(codec_errors)
        if not self._reconfigure and not errors:
            self._account_draft = draft_phone_subentry(data, previous=self._account_draft, title=name)
        return data, errors

    async def _finish(self, data: dict[str, Any], *, remove_mailbox: bool = False, physical_bindings=None):
        from .phone_queue_membership import GROUP_MEMBERSHIP_VERSION, _apply_membership_updates

        def apply_memberships(identity):
            if self._get_entry().data.get(GROUP_MEMBERSHIP_VERSION) == 1:
                fields = {field: data[field] for field in ("ring_group", "queues") if field in data}
                if fields:
                    _apply_membership_updates(self.hass, self._get_entry(), {identity: fields})
                data.pop("ring_group", None)
                data.pop("queues", None)

        from .physical_phone import (
            account_physical_bindings_from_form, async_prepare_account_bindings, apply_account_bindings,
        )
        from homeassistant.exceptions import ServiceValidationError
        prepared = ()
        if physical_bindings is not None:
            try:
                prepared = await async_prepare_account_bindings(self.hass, self._get_entry(),
                    data[CONF_PHONE_ENDPOINT_ID], account_physical_bindings_from_form(physical_bindings), account_data=data)
            except (ValueError, ServiceValidationError) as error:
                return self.async_show_form(step_id="sip_account", data_schema=self._common_schema(sip_account=True),
                    errors={"base": str(error)})
        try:
            validate_phone_subentry_data(self._get_entry(), data)
        except ValueError as error:
            return self.async_show_form(step_id="sip_account", data_schema=self._common_schema(sip_account=True),
                                        errors={"base": "phone_forward_loop" if str(error) == 'phone_forward_loop' else "phone_alias_conflict"})
        if self._reconfigure:
            if remove_mailbox and self._current_data().get("voicemail", {}).get("enabled"):
                from .mailbox import async_remove_mailbox
                from homeassistant.exceptions import ServiceValidationError

                try:
                    await async_remove_mailbox(self.hass, data[CONF_PHONE_ENDPOINT_ID])
                except ServiceValidationError:
                    is_sip = self._kind is EndpointKind.SIP_ACCOUNT
                    return self.async_show_form(
                        step_id="sip_account" if is_sip else "browser",
                        data_schema=self._common_schema(sip_account=is_sip),
                        errors={"base": "mailbox_removal_failed"},
                    )
                data["voicemail"] = {}
            # Mailbox cleanup may yield. Recheck the current namespace at the
            # final synchronous boundary rather than committing an old draft.
            try:
                validate_phone_subentry_data(self._get_entry(), data)
            except ValueError as error:
                return self.async_show_form(step_id="sip_account", data_schema=self._common_schema(sip_account=True),
                                            errors={"base": "phone_forward_loop" if str(error) == 'phone_forward_loop' else "phone_alias_conflict"})
            current = self._get_reconfigure_subentry()
            if data.get(CONF_PHONE_USERNAME) and data[CONF_PHONE_USERNAME] != phone_endpoint_id(current):
                from .sip_account_identity import async_migrate_account_identities, _typed_data

                try:
                    mapping = await async_migrate_account_identities(self.hass, self._get_entry(),
                        account_updates={current.subentry_id: data})
                except ValueError:
                    return self.async_show_form(step_id="sip_account", data_schema=self._common_schema(sip_account=True),
                                                errors={"base": "phone_alias_conflict"})
                prepared = tuple((row, _typed_data(values, 'physical_phone', mapping)) for row, values in prepared)
                apply_memberships(data[CONF_PHONE_USERNAME])
                apply_account_bindings(self.hass, self._get_entry(), prepared)
                self.hass.config_entries.async_schedule_reload(self._get_entry().entry_id)
                return self.async_abort(reason="reconfigure_successful")
            apply_memberships(data[CONF_PHONE_ENDPOINT_ID])
            result = self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                data=phone_subentry_storage_data(self._get_reconfigure_subentry(), data),
                title=str(data[CONF_PHONE_NAME]),
            )
            apply_account_bindings(self.hass, self._get_entry(), prepared)
            return result
        apply_memberships(data[CONF_PHONE_ENDPOINT_ID])
        apply_account_bindings(self.hass, self._get_entry(), prepared)
        # The native manager creates the subentry and owns flow completion.
        # Physical bindings refer to the canonical username, never a draft ID.
        draft = draft_phone_subentry(data, previous=self._account_draft, title=str(data[CONF_PHONE_NAME]))
        if data.get(CONF_PHONE_ENABLED, True):
            from .phone_config import async_enable_registrar_for_new_account
            async_enable_registrar_for_new_account(self.hass, self._get_entry())
        return self.async_create_entry(title=draft.title, data=draft.data, unique_id=draft.unique_id)

    async def async_step_user(self, user_input=None):
        """Every managed phone is one ordinary SIP account."""
        self._kind = EndpointKind.SIP_ACCOUNT
        return await self.async_step_sip_account(user_input)

    async def async_step_sip_account(self, user_input=None):
        """Configure one standard SIP registrar account."""
        from .physical_phone import async_account_physical_binding_options
        self._physical_binding_options = await async_account_physical_binding_options(
            self.hass, self._get_entry(), str(self._current_data().get(CONF_PHONE_ENDPOINT_ID) or ""))
        if user_input is not None:
            data, errors = self._normalized_common(
                user_input, kind=EndpointKind.SIP_ACCOUNT
            )
            if not errors:
                return await self._finish(data, remove_mailbox=user_input.get("voicemail_enabled") is False,
                    physical_bindings=user_input.get("physical_bindings"))
            return self.async_show_form(
                step_id="sip_account",
                data_schema=self._common_schema(sip_account=True),
                errors=errors,
            )
        return self.async_show_form(
            step_id="sip_account",
            data_schema=self._common_schema(sip_account=True),
        )

    async def async_step_reconfigure(self, user_input=None):
        """Keep endpoint identity and kind stable while editing settings."""
        self._reconfigure = True
        self._kind = EndpointKind.SIP_ACCOUNT
        return await self.async_step_sip_account(user_input)
