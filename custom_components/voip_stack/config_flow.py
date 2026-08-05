"""Config flow for VoIP Stack."""

from collections.abc import Mapping
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigSubentryFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    AssistPipelineSelector,
    NumberSelector,
    NumberSelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .config_validation import extension_conflicts, route_namespace_conflicts
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
    CONF_PHONE_SEND_VIDEO,
    CONF_PHONE_USERNAME,
    CONF_PHONE_VIDEO_ENABLED,
    PHONE_SUBENTRY_TYPE,
    new_browser_endpoint_id,
    new_sip_account_endpoint_id,
    phone_subentries,
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


def _entry_route_mappings(
    entry: ConfigEntry | None,
    data: Mapping[str, Any],
    *,
    exclude_subentry_id: str = "",
    include_assist: bool = True,
) -> list[Mapping[str, Any]]:
    """Build the complete persisted routing namespace for flow validation."""
    mappings: list[Mapping[str, Any]] = [
        item
        for item in data.get(CONF_PHONEBOOK_CONTACTS, []) or []
        if isinstance(item, Mapping)
    ]
    mappings.extend(
        item
        for item in data.get("sip_accounts", []) or []
        if isinstance(item, Mapping)
    )
    if entry is not None:
        mappings.extend(
            subentry.data
            for subentry in phone_subentries(entry)
            if subentry.subentry_id != exclude_subentry_id
        )
    assist_extension = str(data.get(CONF_ASSIST_EXTENSION) or "").strip()
    if include_assist and data.get(CONF_ASSIST_ENDPOINT_ENABLED) and assist_extension:
        mappings.append(
            {
                "id": "assist",
                "name": "Assist",
                "extension": assist_extension,
            }
        )
    return mappings


def _port_selector():
    return NumberSelector(NumberSelectorConfig(min=1, max=65535, step=1, mode="box"))


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
            CONF_TRUNK_ENABLED: False,
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
    return data


def _base_entry_data(existing: Mapping[str, Any]) -> dict[str, Any]:
    """Return the non-trunk settings preserved by direct trunk edits."""

    return {
        "sip_port": int(existing.get("sip_port", VOIP_STACK_SIP_PORT)),
        "rtp_port": int(existing.get("rtp_port", VOIP_STACK_RTP_PORT)),
        "advertise_host": str(existing.get("advertise_host", "") or "").strip(),
        CONF_ASSIST_INTENTS: bool(existing.get(CONF_ASSIST_INTENTS, False)),
        CONF_ASSIST_ENDPOINT_ENABLED: bool(
            existing.get(CONF_ASSIST_ENDPOINT_ENABLED, False)
        ),
        CONF_ASSIST_EXTENSION: str(
            existing.get(CONF_ASSIST_EXTENSION, "") or ""
        ).strip(),
        CONF_ASSIST_PIPELINE: str(
            existing.get(CONF_ASSIST_PIPELINE, "") or ""
        ).strip(),
        CONF_ASSIST_ADVANCED_CALL_CONTEXT: bool(
            existing.get(CONF_ASSIST_ADVANCED_CALL_CONTEXT, False)
        ),
        CONF_DEBUG_MODE: bool(existing.get(CONF_DEBUG_MODE, False)),
        CONF_MEDIA_CAPTURE: bool(existing.get(CONF_MEDIA_CAPTURE, False)),
        CONF_SIP_VIDEO: bool(existing.get(CONF_SIP_VIDEO, False)),
        CONF_VIDEO_TRANSCODING: bool(
            existing.get(CONF_VIDEO_TRANSCODING, False)
        ),
        CONF_VIDEO_CAMERA_SEND: bool(existing.get(CONF_VIDEO_CAMERA_SEND, False)),
        CONF_REGISTRAR_ENABLED: bool(existing.get(CONF_REGISTRAR_ENABLED, False)),
    }


class VoipStackConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for VoIP Stack."""

    VERSION = 5
    _base_input: dict | None = None

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Expose the native Add phone flow on the integration entry."""
        return {PHONE_SUBENTRY_TYPE: PhoneSubentryFlowHandler}

    def _current_entry(self) -> ConfigEntry | None:
        if self.source == SOURCE_RECONFIGURE:
            return self._get_reconfigure_entry()
        return next(iter(self._async_current_entries()), None)

    def _current_entry_data(self) -> tuple[ConfigEntry | None, Mapping[str, Any]]:
        current_entry = self._current_entry()
        return current_entry, (current_entry.data if current_entry else {})

    def _store_entry(self, data: dict):
        current_entry, _existing = self._current_entry_data()
        if current_entry is not None:
            return self.async_update_and_abort(
                current_entry,
                data=data,
                reason="reconfigure_successful",
            )
        return self.async_create_entry(title="VoIP Stack", data=data)

    async def async_step_reconfigure(self, user_input=None):
        """Reconfigure the existing VoIP Stack entry from HA's native menu."""
        return await self.async_step_user(user_input)

    async def async_step_user(self, user_input=None):
        """Handle install/reconfigure of the single VoIP Stack entry.

        SIP is the only HA call-control protocol. HA is always both a
        softphone and SIP router/B2BUA, and it listens on both SIP/UDP and
        SIP/TCP like a normal SIP endpoint.
        """
        _current_entry, existing = self._current_entry_data()
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
            CONF_TRUNK_ENABLED: existing.get(CONF_TRUNK_ENABLED, False),
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
                vol.Required(
                    CONF_TRUNK_ENABLED, default=defaults[CONF_TRUNK_ENABLED]
                ): BooleanSelector(),
            }
        )
        errors: dict[str, str] = {}

        if user_input is not None:
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
                if user_input[CONF_TRUNK_ENABLED]:
                    return await self.async_step_trunk()
                data = dict(self._base_input)
                _disabled_trunk_data(data, existing)
                current_entry, _existing = self._current_entry_data()
                if current_entry is None:
                    await self.async_set_unique_id(DOMAIN)
                    self._abort_if_unique_id_configured()
                return self._store_entry(data)

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
            if self._base_input[CONF_TRUNK_ENABLED]:
                return await self.async_step_trunk()
            data = dict(self._base_input)
            _disabled_trunk_data(data, existing)
            current_entry, _existing = self._current_entry_data()
            if current_entry is None:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
            return self._store_entry(data)
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
            if not extension.isdigit() or not 1 <= len(extension) <= 8:
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
                if self._base_input[CONF_TRUNK_ENABLED]:
                    return await self.async_step_trunk()
                data = dict(self._base_input)
                _disabled_trunk_data(data, existing)
                current_entry, _existing = self._current_entry_data()
                if current_entry is None:
                    await self.async_set_unique_id(DOMAIN)
                    self._abort_if_unique_id_configured()
                return self._store_entry(data)
        return self.async_show_form(step_id="assist", data_schema=schema, errors=errors)

    async def async_step_trunk(self, user_input=None):
        _current_entry, existing = self._current_entry_data()
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
            CONF_TRUNK_INBOUND_DEFAULT_TARGET: existing.get(
                CONF_TRUNK_INBOUND_DEFAULT_TARGET, "HA"
            ),
            CONF_TRUNK_INBOUND_MODE: existing.get(CONF_TRUNK_INBOUND_MODE, legacy_mode),
            CONF_AUTOMATION_ROUTING_ENABLED: existing.get(
                CONF_AUTOMATION_ROUTING_ENABLED, False
            ),
            CONF_TRUNK_DTMF_TIMEOUT_MS: legacy_timeout,
            CONF_TRUNK_DTMF_TERMINATOR: existing.get(CONF_TRUNK_DTMF_TERMINATOR, ""),
        }
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_TRUNK_TRANSPORT, default=defaults[CONF_TRUNK_TRANSPORT]
                ): SelectSelector(SelectSelectorConfig(options=["udp", "tcp"])),
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
                vol.Optional(
                    CONF_TRUNK_INBOUND_DEFAULT_TARGET,
                    default=defaults[CONF_TRUNK_INBOUND_DEFAULT_TARGET],
                ): TextSelector(),
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
        errors: dict[str, str] = {}
        if user_input is not None:
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
            trunk_fields_empty = not any(
                user_input.get(k)
                for k in (
                    CONF_TRUNK_SERVER,
                    CONF_TRUNK_USERNAME,
                    CONF_TRUNK_AUTH_USERNAME,
                    CONF_TRUNK_PASSWORD,
                    CONF_TRUNK_DOMAIN,
                    CONF_TRUNK_OUTBOUND_PROXY,
                )
            )
            if trunk_fields_empty:
                data = dict(self._base_input or _base_entry_data(existing))
                return self._store_entry(_disabled_trunk_data(data, existing))
            if not user_input[CONF_TRUNK_SERVER]:
                errors["base"] = "trunk_server_required"
            elif not user_input[CONF_TRUNK_USERNAME]:
                errors["base"] = "trunk_username_required"
            elif not user_input[CONF_TRUNK_PASSWORD]:
                errors["base"] = "trunk_password_required"
            if not errors:
                data = dict(self._base_input or _base_entry_data(existing))
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
                return self._store_entry(data)
        return self.async_show_form(step_id="trunk", data_schema=schema, errors=errors)


class PhoneSubentryFlowHandler(ConfigSubentryFlow):
    """Add or reconfigure one logical browser/SIP phone."""

    _kind: EndpointKind | None = None
    _reconfigure = False
    _suggested_password = ""

    def _current_data(self) -> Mapping[str, Any]:
        if not self._reconfigure:
            return {}
        return self._get_reconfigure_subentry().data

    def _common_schema(self, *, sip_account: bool) -> vol.Schema:
        current = self._current_data()
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
                default=str(current.get(CONF_PHONE_RING_GROUP) or ""),
            ): TextSelector(),
            vol.Optional(
                CONF_PHONE_CONFERENCE_GROUP,
                default=str(current.get(CONF_PHONE_CONFERENCE_GROUP) or ""),
            ): TextSelector(),
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
            offline_policy = str(
                current.get(CONF_PHONE_OFFLINE_POLICY)
                or OfflinePolicy.UNAVAILABLE.value
            )
            fields.update(
                {
                    vol.Required(
                        CONF_PHONE_OFFLINE_POLICY,
                        default=offline_policy,
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                OfflinePolicy.UNAVAILABLE.value,
                                OfflinePolicy.FORWARD.value,
                            ],
                            mode="dropdown",
                        )
                    ),
                    vol.Optional(
                        CONF_PHONE_OFFLINE_FORWARD_TARGET,
                        default=str(
                            current.get(CONF_PHONE_OFFLINE_FORWARD_TARGET) or ""
                        ),
                    ): TextSelector(),
                }
            )
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
        else:
            fields.update(
                {
                    vol.Required(
                        CONF_PHONE_AUTO_ANSWER,
                        default=bool(
                            current.get(CONF_PHONE_AUTO_ANSWER, False)
                        ),
                    ): BooleanSelector(),
                    vol.Required(
                        CONF_PHONE_SEND_VIDEO,
                        default=bool(current.get(CONF_PHONE_SEND_VIDEO, False)),
                    ): BooleanSelector(),
                }
            )
        return vol.Schema(fields)

    def _validate_aliases(
        self,
        *,
        name: str,
        extension: str,
        username: str,
        ring_group: str,
        conference_group: str,
    ) -> bool:
        current_id = (
            self._get_reconfigure_subentry().subentry_id
            if self._reconfigure
            else ""
        )
        entry = self._get_entry()
        return not route_namespace_conflicts(
            candidate_routes=(name, extension, username),
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
        current = self._current_data()
        name = str(user_input.get(CONF_PHONE_NAME) or "").strip()
        extension = str(user_input.get(CONF_PHONE_EXTENSION) or "").strip()
        username = ""
        password = ""
        if kind is EndpointKind.SIP_ACCOUNT:
            username = str(user_input.get(CONF_PHONE_USERNAME) or "").strip()
            if not username:
                username = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-.")
            try:
                username = normalize_username(username)
            except ValueError:
                errors[CONF_PHONE_USERNAME] = "phone_username_invalid"
            password = str(user_input.get(CONF_PHONE_PASSWORD) or "")
            if not password:
                errors[CONF_PHONE_PASSWORD] = "phone_password_required"
        if not name:
            errors[CONF_PHONE_NAME] = "phone_name_required"
        if (
            kind is EndpointKind.SIP_ACCOUNT
            and str(user_input.get(CONF_PHONE_OFFLINE_POLICY))
            == OfflinePolicy.FORWARD.value
            and not str(
                user_input.get(CONF_PHONE_OFFLINE_FORWARD_TARGET) or ""
            ).strip()
        ):
            errors[CONF_PHONE_OFFLINE_FORWARD_TARGET] = "phone_forward_target_required"
        if not self._validate_aliases(
            name=name,
            extension=extension,
            username=username,
            ring_group=str(user_input.get(CONF_PHONE_RING_GROUP) or "").strip(),
            conference_group=str(
                user_input.get(CONF_PHONE_CONFERENCE_GROUP) or ""
            ).strip(),
        ):
            errors["base"] = "phone_alias_conflict"
        endpoint_id = str(current.get(CONF_PHONE_ENDPOINT_ID) or "")
        if not endpoint_id:
            if kind is EndpointKind.BROWSER:
                endpoint_id = new_browser_endpoint_id()
            elif CONF_PHONE_USERNAME not in errors:
                endpoint_id = new_sip_account_endpoint_id()
        data = {
            CONF_PHONE_ENDPOINT_ID: endpoint_id,
            CONF_PHONE_KIND: kind.value,
            CONF_PHONE_NAME: name,
            CONF_PHONE_EXTENSION: extension,
            CONF_PHONE_USERNAME: username,
            CONF_PHONE_PASSWORD: password,
            CONF_PHONE_ENABLED: bool(user_input.get(CONF_PHONE_ENABLED, True)),
            CONF_PHONE_DND: bool(user_input.get(CONF_PHONE_DND, False)),
            CONF_PHONE_RING_GROUP: str(
                user_input.get(CONF_PHONE_RING_GROUP) or ""
            ).strip(),
            CONF_PHONE_CONFERENCE_GROUP: str(
                user_input.get(CONF_PHONE_CONFERENCE_GROUP) or ""
            ).strip(),
            CONF_PHONE_CONFERENCE_RING: bool(
                user_input.get(CONF_PHONE_CONFERENCE_RING, False)
            ),
            CONF_PHONE_VIDEO_ENABLED: bool(
                user_input.get(CONF_PHONE_VIDEO_ENABLED, True)
            ),
        }
        if kind is EndpointKind.SIP_ACCOUNT:
            data.update(
                {
                    CONF_PHONE_OFFLINE_POLICY: str(
                        user_input.get(CONF_PHONE_OFFLINE_POLICY)
                        or OfflinePolicy.UNAVAILABLE.value
                    ),
                    CONF_PHONE_OFFLINE_FORWARD_TARGET: str(
                        user_input.get(CONF_PHONE_OFFLINE_FORWARD_TARGET) or ""
                    ).strip(),
                }
            )
        else:
            data.update(
                {
                    CONF_PHONE_AUTO_ANSWER: bool(
                        user_input.get(CONF_PHONE_AUTO_ANSWER, False)
                    ),
                    CONF_PHONE_SEND_VIDEO: bool(
                        user_input.get(CONF_PHONE_SEND_VIDEO, False)
                    ),
                }
            )
        return data, errors

    def _finish(self, data: dict[str, Any]):
        if self._reconfigure:
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                data=data,
                title=str(data[CONF_PHONE_NAME]),
            )
        return self.async_create_entry(
            title=str(data[CONF_PHONE_NAME]),
            data=data,
            unique_id=f"phone:{data[CONF_PHONE_ENDPOINT_ID]}",
        )

    async def async_step_user(self, user_input=None):
        """Choose which standard endpoint implementation to add."""
        if user_input is not None:
            self._kind = EndpointKind(str(user_input[CONF_PHONE_KIND]))
            if self._kind is EndpointKind.BROWSER:
                return await self.async_step_browser()
            return await self.async_step_sip_account()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PHONE_KIND, default=EndpointKind.BROWSER.value
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                EndpointKind.BROWSER.value,
                                EndpointKind.SIP_ACCOUNT.value,
                            ],
                            mode="dropdown",
                        )
                    )
                }
            ),
        )

    async def async_step_browser(self, user_input=None):
        """Configure one browser/card-backed HA softphone."""
        if user_input is not None:
            data, errors = self._normalized_common(
                user_input, kind=EndpointKind.BROWSER
            )
            if not errors:
                return self._finish(data)
            return self.async_show_form(
                step_id="browser",
                data_schema=self._common_schema(sip_account=False),
                errors=errors,
            )
        return self.async_show_form(
            step_id="browser", data_schema=self._common_schema(sip_account=False)
        )

    async def async_step_sip_account(self, user_input=None):
        """Configure one standard SIP registrar account."""
        if user_input is not None:
            data, errors = self._normalized_common(
                user_input, kind=EndpointKind.SIP_ACCOUNT
            )
            if not errors:
                return self._finish(data)
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
        current = self._get_reconfigure_subentry().data
        self._kind = EndpointKind(
            str(current.get(CONF_PHONE_KIND) or EndpointKind.BROWSER.value)
        )
        if self._kind is EndpointKind.BROWSER:
            return await self.async_step_browser(user_input)
        return await self.async_step_sip_account(user_input)
