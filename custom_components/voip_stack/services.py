"""Home Assistant service schemas and registration for VoIP Stack."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv

from .authorization import (
    async_require_service_admin,
    async_require_service_control,
)
from .const import DOMAIN


SIP_FAILURE_STATUS = vol.All(vol.Coerce(int), vol.Range(min=300, max=699))
SIP_FAILURE_STATUS_OR_DEFAULT = vol.All(
    vol.Coerce(int), vol.Any(0, vol.Range(min=300, max=699))
)
SHORT_TEXT = vol.All(cv.string, vol.Length(max=256))
IDENTIFIER_TEXT = vol.All(cv.string, vol.Length(max=128))
URI_TEXT = vol.All(cv.string, vol.Length(max=2048))
REASON_TEXT = vol.All(cv.string, vol.Length(max=512))
PASSWORD_TEXT = vol.All(cv.string, vol.Length(max=256))
ROSTER_JSON_TEXT = vol.All(cv.string, vol.Length(max=256 * 1024))
FORMAT_TEXT = vol.All(cv.string, vol.Length(max=128))
FORMAT_LIST = vol.All([FORMAT_TEXT], vol.Length(max=32))
PORT = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))
SAMPLE_RATE = vol.All(vol.Coerce(int), vol.Range(min=8000, max=192000))
PAYLOAD_BYTES = vol.All(vol.Coerce(int), vol.Range(min=64, max=65507))
SEQUENCE = vol.All(vol.Coerce(int), vol.Range(min=0, max=2**31 - 1))


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    """Registration and authorization contract for one HA service."""

    schema: Any | None = None
    supports_response: SupportsResponse | None = None
    admin_only: bool = False


async def async_register_services(hass: HomeAssistant, handlers: dict[str, object]) -> None:
    phone_selector_fields = {vol.Optional("device_id"): IDENTIFIER_TEXT}
    purge_schema = vol.Schema(
        {
            **phone_selector_fields,
            vol.Optional("min_unavailable_hours", default=0): vol.All(
                vol.Coerce(float), vol.Range(min=0, max=87600)
            ),
        },
        extra=vol.PREVENT_EXTRA,
    )
    sip_answer_schema = vol.Schema(
        {
            **phone_selector_fields,
            vol.Optional("call_id", default=""): SHORT_TEXT,
            vol.Optional("send_video", default=False): cv.boolean,
            vol.Optional("media_client_id", default=""): IDENTIFIER_TEXT,
        },
        extra=vol.PREVENT_EXTRA,
    )
    sip_decline_schema = vol.Schema(
        {
            **phone_selector_fields,
            vol.Optional("call_id", default=""): SHORT_TEXT,
            vol.Optional("status", default=603): SIP_FAILURE_STATUS,
            vol.Optional("reason", default="Decline"): REASON_TEXT,
            vol.Optional("decline_reason", default=""): REASON_TEXT,
        },
        extra=vol.PREVENT_EXTRA,
    )
    sip_hangup_schema = vol.Schema(
        {
            **phone_selector_fields,
            vol.Optional("call_id", default=""): SHORT_TEXT,
            vol.Optional("reason", default="local_hangup"): REASON_TEXT,
        },
        extra=vol.PREVENT_EXTRA,
    )
    send_dtmf_schema = vol.Schema(
        {
            **phone_selector_fields,
            vol.Optional("call_id", default=""): SHORT_TEXT,
            vol.Required("digits"): vol.All(
                cv.string,
                vol.Match(r"^[0-9*#A-Da-d]+$"),
                vol.Length(min=1, max=64),
            ),
            vol.Optional("duration_ms", default=160): vol.All(
                vol.Coerce(int), vol.Range(min=40, max=5000)
            ),
            vol.Optional("gap_ms", default=80): vol.All(
                vol.Coerce(int), vol.Range(min=40, max=5000)
            ),
        },
        extra=vol.PREVENT_EXTRA,
    )
    sip_call_schema = vol.Schema(
        {
            **phone_selector_fields,
            vol.Optional("call_id", default=""): SHORT_TEXT,
            vol.Required("destination"): URI_TEXT,
            vol.Optional("ha_bridge", default=False): cv.boolean,
            vol.Optional("send_video", default=False): cv.boolean,
            vol.Optional("media_client_id", default=""): IDENTIFIER_TEXT,
        },
        extra=vol.PREVENT_EXTRA,
    )
    sip_forward_schema = vol.Schema(
        {
            **phone_selector_fields,
            vol.Optional("call_id", default=""): SHORT_TEXT,
            vol.Required("destination"): URI_TEXT,
            vol.Optional("ha_bridge", default=False): cv.boolean,
            vol.Optional("on_failure", default="resume"): vol.In(
                ["resume", "terminate", "busy"]
            ),
            vol.Optional("expected_state", default=""): IDENTIFIER_TEXT,
            vol.Optional("expected_sequence", default=0): SEQUENCE,
        },
        extra=vol.PREVENT_EXTRA,
    )
    sip_transfer_schema = vol.Schema(
        {
            **phone_selector_fields,
            vol.Required("call_id"): SHORT_TEXT,
            vol.Optional("destination", default=""): URI_TEXT,
            vol.Optional("replaces_call_id", default=""): SHORT_TEXT,
        },
        extra=vol.PREVENT_EXTRA,
    )
    sip_deadline_schema = vol.Schema(
        {
            vol.Required("call_id"): SHORT_TEXT,
            vol.Required("phase"): vol.In(["calling", "ringing"]),
            vol.Required("timeout"): vol.All(
                vol.Coerce(float), vol.Range(min=0.1, max=3600)
            ),
            vol.Optional("expected_state", default=""): IDENTIFIER_TEXT,
            vol.Optional("expected_sequence", default=0): SEQUENCE,
        },
        extra=vol.PREVENT_EXTRA,
    )
    sip_cancel_deadline_schema = vol.Schema(
        {vol.Required("call_id"): SHORT_TEXT},
        extra=vol.PREVENT_EXTRA,
    )
    sip_route_schema = vol.Schema(
        {
            vol.Required("call_id"): SHORT_TEXT,
            vol.Optional("action", default="default"): vol.In(
                ["answer_ha", "decline", "busy", "forward", "bridge", "default", "cancel"]
            ),
            vol.Optional("destination"): URI_TEXT,
            vol.Optional("status", default=0): SIP_FAILURE_STATUS_OR_DEFAULT,
            vol.Optional("reason", default=""): REASON_TEXT,
            vol.Optional("decline_reason", default=""): REASON_TEXT,
            vol.Optional("expected_state", default=""): IDENTIFIER_TEXT,
            vol.Optional("expected_sequence", default=0): SEQUENCE,
        },
        extra=vol.PREVENT_EXTRA,
    )
    select_inbound_destination_schema = vol.Schema(
        {
            vol.Optional("call_id", default=""): SHORT_TEXT,
            vol.Required("destination"): URI_TEXT,
            vol.Optional("expected_state", default=""): IDENTIFIER_TEXT,
            vol.Optional("expected_sequence", default=0): SEQUENCE,
        },
        extra=vol.PREVENT_EXTRA,
    )
    phonebook_add_schema = vol.Schema(
        {
            vol.Required("name"): SHORT_TEXT,
            vol.Optional("id", default=""): SHORT_TEXT,
            vol.Optional("address", default=""): URI_TEXT,
            vol.Optional("sip_uri", default=""): URI_TEXT,
            vol.Optional("extension", default=""): IDENTIFIER_TEXT,
            vol.Optional("number", default=""): IDENTIFIER_TEXT,
            vol.Optional("ha_bridge", default=False): cv.boolean,
            vol.Optional("transport", default=""): vol.Any(
                "", vol.In(["tcp", "tls", "udp"])
            ),
            vol.Optional("port"): PORT,
            vol.Optional("rtp_port"): PORT,
            vol.Optional("tx_rate"): vol.Any("auto", SAMPLE_RATE),
            vol.Optional("rx_rate"): vol.Any("auto", SAMPLE_RATE),
            vol.Optional("tx_formats"): vol.Any(FORMAT_TEXT, FORMAT_LIST),
            vol.Optional("rx_formats"): vol.Any(FORMAT_TEXT, FORMAT_LIST),
            vol.Optional("max_payload_bytes"): PAYLOAD_BYTES,
            vol.Optional("conference_group", default=""): SHORT_TEXT,
            vol.Optional("conference_ring", default=False): cv.boolean,
            vol.Optional("ring_group", default=""): SHORT_TEXT,
        },
        extra=vol.PREVENT_EXTRA,
    )
    phonebook_remove_schema = vol.Schema(
        {vol.Required("name"): SHORT_TEXT}, extra=vol.PREVENT_EXTRA
    )
    phonebook_set_schema = vol.Schema(
        {vol.Required("roster_json"): ROSTER_JSON_TEXT}, extra=vol.PREVENT_EXTRA
    )
    set_dnd_schema = vol.Schema(
        {
            **phone_selector_fields,
            vol.Required("dnd"): cv.boolean,
        },
        extra=vol.PREVENT_EXTRA,
    )
    set_auto_answer_schema = vol.Schema(
        {
            **phone_selector_fields,
            vol.Required("auto_answer"): cv.boolean,
        },
        extra=vol.PREVENT_EXTRA,
    )
    set_send_video_schema = vol.Schema(
        {
            **phone_selector_fields,
            vol.Required("send_video"): cv.boolean,
        },
        extra=vol.PREVENT_EXTRA,
    )
    set_ha_softphone_settings_schema = vol.Schema(
        {
            **phone_selector_fields,
            vol.Optional("extension"): IDENTIFIER_TEXT,
            vol.Optional("ring_group"): SHORT_TEXT,
            vol.Optional("conference_group"): SHORT_TEXT,
            vol.Optional("conference_ring"): cv.boolean,
            vol.Optional("auto_answer"): cv.boolean,
            vol.Optional("send_video"): cv.boolean,
        },
        extra=vol.PREVENT_EXTRA,
    )
    sip_account_create_schema = vol.Schema(
        {
            vol.Required("username"): vol.All(cv.string, vol.Length(max=64)),
            vol.Optional("display_name", default=""): SHORT_TEXT,
            vol.Optional("password", default=""): PASSWORD_TEXT,
            vol.Optional("enabled", default=True): cv.boolean,
            vol.Optional("replace", default=False): cv.boolean,
            vol.Optional("extension", default=""): IDENTIFIER_TEXT,
            vol.Optional("conference_group", default=""): SHORT_TEXT,
            vol.Optional("conference_ring", default=False): cv.boolean,
            vol.Optional("ring_group", default=""): SHORT_TEXT,
        },
        extra=vol.PREVENT_EXTRA,
    )
    sip_account_name_schema = vol.Schema(
        {vol.Required("username"): vol.All(cv.string, vol.Length(max=64))},
        extra=vol.PREVENT_EXTRA,
    )

    def handler_for(name: str, spec: ServiceSpec):
        async def _handle(call: ServiceCall) -> object:
            if spec.admin_only:
                await async_require_service_admin(hass, call)
            else:
                await async_require_service_control(hass, call)
            return await handlers[name](call)

        return _handle

    service_specs = {
        "purge_devices": ServiceSpec(purge_schema, admin_only=True),
        "answer": ServiceSpec(sip_answer_schema),
        "decline": ServiceSpec(sip_decline_schema),
        "hangup": ServiceSpec(sip_hangup_schema),
        "send_dtmf": ServiceSpec(send_dtmf_schema),
        "call": ServiceSpec(sip_call_schema, SupportsResponse.OPTIONAL),
        "forward": ServiceSpec(sip_forward_schema),
        "transfer": ServiceSpec(sip_transfer_schema, SupportsResponse.OPTIONAL),
        "route": ServiceSpec(sip_route_schema, admin_only=True),
        "select_inbound_destination": ServiceSpec(
            select_inbound_destination_schema,
            admin_only=True,
        ),
        "set_deadline": ServiceSpec(sip_deadline_schema, admin_only=True),
        "cancel_deadline": ServiceSpec(sip_cancel_deadline_schema, admin_only=True),
        "add_contact": ServiceSpec(phonebook_add_schema, admin_only=True),
        "remove_contact": ServiceSpec(phonebook_remove_schema, admin_only=True),
        "set_contacts": ServiceSpec(phonebook_set_schema, admin_only=True),
        "clear_contacts": ServiceSpec(admin_only=True),
        "export_phonebook": ServiceSpec(
            supports_response=SupportsResponse.ONLY,
            admin_only=True,
        ),
        "push_phonebook": ServiceSpec(admin_only=True),
        "set_dnd": ServiceSpec(set_dnd_schema),
        "set_auto_answer": ServiceSpec(set_auto_answer_schema),
        "set_send_video": ServiceSpec(set_send_video_schema),
        "set_ha_softphone_settings": ServiceSpec(
            set_ha_softphone_settings_schema,
            admin_only=True,
        ),
        "create_account": ServiceSpec(
            sip_account_create_schema,
            SupportsResponse.ONLY,
            admin_only=True,
        ),
        "remove_account": ServiceSpec(sip_account_name_schema, admin_only=True),
        "rotate_account_password": ServiceSpec(
            sip_account_name_schema,
            SupportsResponse.ONLY,
            admin_only=True,
        ),
        "enable_account": ServiceSpec(sip_account_name_schema, admin_only=True),
        "disable_account": ServiceSpec(sip_account_name_schema, admin_only=True),
        "list_accounts": ServiceSpec(
            supports_response=SupportsResponse.ONLY,
            admin_only=True,
        ),
    }
    missing_handlers = service_specs.keys() - handlers.keys()
    unexpected_handlers = handlers.keys() - service_specs.keys()
    if missing_handlers or unexpected_handlers:
        raise ValueError(
            "VoIP service handler mismatch "
            f"missing={sorted(missing_handlers)} unexpected={sorted(unexpected_handlers)}"
        )
    for name, spec in service_specs.items():
        options = {}
        if spec.schema is not None:
            options["schema"] = spec.schema
        if spec.supports_response is not None:
            options["supports_response"] = spec.supports_response
        hass.services.async_register(DOMAIN, name, handler_for(name, spec), **options)
