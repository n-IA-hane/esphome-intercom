"""Behavioral contracts for configuration and service input hardening."""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import voluptuous as vol
import yaml

from tests.support.service_schemas import load_service_schemas


ROOT = Path(__file__).resolve().parents[1]
CONFIG_FLOW = ROOT / "custom_components" / "voip_stack" / "config_flow.py"
CONST = ROOT / "custom_components" / "voip_stack" / "const.py"
SERVICES = ROOT / "custom_components" / "voip_stack" / "services.py"
SERVICES_YAML = ROOT / "custom_components" / "voip_stack" / "services.yaml"
STRINGS = ROOT / "custom_components" / "voip_stack" / "strings.json"
TRANSLATIONS = ROOT / "custom_components" / "voip_stack" / "translations"
CONFIG = ROOT / "custom_components" / "voip_stack" / "config.py"
INIT = ROOT / "custom_components" / "voip_stack" / "__init__.py"


def _load_disabled_trunk_data():
    """Load the pure config helper without importing Home Assistant."""

    config_tree = ast.parse(CONFIG_FLOW.read_text())
    helper = next(
        node
        for node in config_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_disabled_trunk_data"
    )
    const_namespace: dict[str, object] = {"__file__": str(CONST)}
    exec(compile(CONST.read_text(), str(CONST), "exec"), const_namespace)
    namespace = {
        "Mapping": dict,
        "Any": object,
        **const_namespace,
    }
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[])),
            str(CONFIG_FLOW),
            "exec",
        ),
        namespace,
    )
    return namespace["_disabled_trunk_data"], namespace


def _load_remove_entry_hook():
    """Load the pure final-removal hook without importing Home Assistant."""

    tree = ast.parse(INIT.read_text())
    nodes = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "_REMOVED_ENTRY_RUNTIME_KEYS"
                for target in node.targets
            )
        )
        or (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "async_remove_entry"
        )
    ]
    namespace = {
        "HomeAssistant": object,
        "ConfigEntry": object,
        "DOMAIN": "voip_stack",
        "CONF_DEBUG_MODE": "debug_mode",
    }
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
            str(INIT),
            "exec",
        ),
        namespace,
    )
    return namespace["async_remove_entry"]


@pytest.mark.parametrize("legacy_value", [None, ""])
def test_disabled_trunk_uses_safe_defaults_for_empty_legacy_values(
    legacy_value: object,
) -> None:
    helper, constants = _load_disabled_trunk_data()
    existing = {
        constants["CONF_TRUNK_TRANSPORT"]: legacy_value,
        constants["CONF_TRUNK_PORT"]: legacy_value,
        constants["CONF_TRUNK_EXPIRES"]: legacy_value,
        constants["CONF_TRUNK_DTMF_TIMEOUT_MS"]: legacy_value,
        "sip_accounts": legacy_value,
        constants["CONF_PHONEBOOK_CONTACTS"]: legacy_value,
    }

    result = helper({}, existing)

    assert result[constants["CONF_TRUNK_TRANSPORT"]] == "udp"
    assert result[constants["CONF_TRUNK_PORT"]] == constants["VOIP_STACK_SIP_PORT"]
    assert result[constants["CONF_TRUNK_EXPIRES"]] == 300
    assert result[constants["CONF_TRUNK_DTMF_TIMEOUT_MS"]] == 3000
    assert result["sip_accounts"] == []
    assert result[constants["CONF_PHONEBOOK_CONTACTS"]] == []


def test_trunk_password_config_field_is_masked() -> None:
    source = CONFIG_FLOW.read_text()
    trunk_step = source[source.index("async def async_step_trunk") :]

    assert "TextSelectorConfig" in source
    assert "CONF_TRUNK_PASSWORD, default=defaults[CONF_TRUNK_PASSWORD]" in trunk_step
    assert (
        "TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))" in trunk_step
    )
    assert (
        source.count("TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))")
        == 1
    )


def test_create_account_password_service_field_is_masked() -> None:
    document = yaml.safe_load(SERVICES_YAML.read_text())

    assert document["create_account"]["fields"]["password"]["selector"] == {
        "text": {"type": "password"}
    }


def _service_description_field(document: dict, service: str, field: str) -> dict:
    fields = document[service]["fields"]
    if field in fields:
        return fields[field]
    for value in fields.values():
        if isinstance(value, dict) and field in (value.get("fields") or {}):
            return value["fields"][field]
    raise KeyError(f"{service}.{field}")


@pytest.mark.parametrize(
    ("service", "expected_integrations"),
    [
        ("answer", {"voip_stack", "esphome"}),
        ("decline", {"voip_stack", "esphome"}),
        ("call", {"voip_stack", "esphome"}),
        ("hangup", {"voip_stack", "esphome"}),
        ("forward", {"voip_stack"}),
        ("set_dnd", {"voip_stack"}),
        ("set_ha_softphone_settings", {"voip_stack"}),
    ],
)
def test_phone_service_pickers_expose_only_compatible_integrations(
    service: str,
    expected_integrations: set[str],
) -> None:
    document = yaml.safe_load(SERVICES_YAML.read_text())

    selector = _service_description_field(document, service, "device_id")["selector"]
    assert "device" in selector
    filters = (selector.get("device") or {}).get("filter") or []
    assert {item["integration"] for item in filters} == expected_integrations
    fields = document[service]["fields"]
    assert "endpoint_id" not in fields
    assert "entity_id" not in fields
    advanced = fields.get("advanced", {}).get("fields", {})
    assert "endpoint_id" not in advanced
    assert "entity_id" not in advanced


def test_purge_picker_is_limited_to_esphome_devices() -> None:
    document = yaml.safe_load(SERVICES_YAML.read_text())
    selector = document["purge_devices"]["fields"]["device_id"]["selector"]

    assert selector["device"]["filter"] == [{"integration": "esphome"}]


def test_everyday_call_actions_hide_technical_fields_in_collapsed_sections() -> None:
    document = yaml.safe_load(SERVICES_YAML.read_text())

    expected_primary = {
        "call": {"device_id", "destination", "send_video"},
        "answer": {"device_id", "send_video"},
        "decline": {"device_id"},
        "hangup": {"device_id"},
        "forward": {"device_id", "destination", "on_failure"},
        "select_inbound_destination": {"destination"},
    }
    for service, primary in expected_primary.items():
        fields = document[service]["fields"]
        assert set(fields) == {*primary, "advanced"}
        assert fields["advanced"]["collapsed"] is True


def test_home_assistant_call_actions_have_one_destination_vocabulary() -> None:
    schemas = load_service_schemas()

    for service in ("call", "forward"):
        assert schemas[service]({"destination": "Kitchen"})["destination"] == "Kitchen"
        for removed_alias in ("target", "call"):
            with pytest.raises(vol.Invalid):
                schemas[service]({"destination": "Kitchen", removed_alias: "Desk"})

    route = schemas["route"]
    for removed_alias in ("target", "call"):
        with pytest.raises(vol.Invalid):
            route({"call_id": "call-1", removed_alias: "Desk"})

    document = yaml.safe_load(SERVICES_YAML.read_text())
    assert "export_accounts" not in document


def test_phone_actions_have_one_device_selector() -> None:
    schemas = load_service_schemas()

    for service in (
        "call",
        "answer",
        "decline",
        "hangup",
        "forward",
        "set_dnd",
        "set_ha_softphone_settings",
    ):
        required = (
            {"destination": "Kitchen"}
            if service in {"call", "forward"}
            else {"dnd": True}
            if service == "set_dnd"
            else {}
        )
        assert schemas[service]({**required, "device_id": "device-kitchen"})[
            "device_id"
        ] == "device-kitchen"
        for removed_selector in ("endpoint_id", "entity_id"):
            with pytest.raises(vol.Invalid):
                schemas[service]({**required, removed_selector: "kitchen"})


def test_final_entry_removal_forgets_runtime_but_preserves_global_views() -> None:
    hook = _load_remove_entry_hook()
    unsubscribed: list[str] = []

    class Registry:
        cleared = False

        def clear_runtime(self) -> None:
            self.cleared = True

    registry = Registry()
    bucket = {
        "initialized": True,
        "audio_ws_view_registered": True,
        "video_ws_view_registered": True,
        "media_shutdown": object(),
        "debug_capture_tasks": {"finishing-write"},
        "endpoint_registry": object(),
        "call_registry": registry,
        "ha_softphones": {"kitchen": {"state": "in_call"}},
        "ha_softphone_presence": {"kitchen": 1},
        "local_softphone_bridge_unsub": lambda: unsubscribed.append("local"),
        "pending_endpoint_removal_unsub": lambda: unsubscribed.append("pending"),
        "entry-id": {"legacy": True},
    }
    hass = SimpleNamespace(data={"voip_stack": bucket})
    entry = SimpleNamespace(entry_id="entry-id")

    asyncio.run(hook(hass, entry))

    assert registry.cleared
    assert unsubscribed == ["local", "pending"]
    assert "endpoint_registry" not in bucket
    assert "call_registry" not in bucket
    assert "ha_softphones" not in bucket
    assert "ha_softphone_presence" not in bucket
    assert "entry-id" not in bucket
    assert bucket["initialized"] is True
    assert bucket["audio_ws_view_registered"] is True
    assert bucket["video_ws_view_registered"] is True
    assert bucket["debug_capture_tasks"] == {"finishing-write"}


@pytest.mark.parametrize(
    "path",
    [STRINGS, TRANSLATIONS / "en.json", TRANSLATIONS / "it.json"],
)
def test_debug_option_discloses_private_audio_capture_and_retention(path: Path) -> None:
    document = json.loads(path.read_text())
    user_step = document["config"]["step"]["user"]
    title = user_step["data"]["debug_mode"].lower()
    description = user_step["data_description"]["debug_mode"].lower()

    assert "audio" in title
    assert "privat" in title
    assert "15" in description
    assert "8" in description
    assert "~/.cache/voip_stack_debug" in description
    assert "0700" in description
    assert "24" in description
    assert "64 mib" in description


@pytest.mark.parametrize("status", [300, 486, 603, 699])
def test_decline_schema_accepts_only_negative_final_sip_statuses(status: int) -> None:
    schema = load_service_schemas()["decline"]

    assert schema({"status": status})["status"] == status


@pytest.mark.parametrize("status", [-1, 0, 99, 100, 200, 299, 700, 999])
def test_decline_schema_rejects_non_failure_sip_statuses(status: int) -> None:
    schema = load_service_schemas()["decline"]

    with pytest.raises(vol.Invalid):
        schema({"status": status})


@pytest.mark.parametrize("status", [0, 300, 486, 603, 699])
def test_route_schema_accepts_default_or_negative_final_sip_status(status: int) -> None:
    schema = load_service_schemas()["route"]

    assert schema({"call_id": "call-1", "status": status})["status"] == status


@pytest.mark.parametrize("status", [-1, 1, 100, 200, 299, 700, 999])
def test_route_schema_rejects_invalid_override_status(status: int) -> None:
    schema = load_service_schemas()["route"]

    with pytest.raises(vol.Invalid):
        schema({"call_id": "call-1", "status": status})


@pytest.mark.parametrize(
    ("service", "payload"),
    [
        ("call", {"destination": "x" * 2049}),
        ("set_contacts", {"roster_json": "[" + " " * (256 * 1024) + "]"}),
        ("add_contact", {"name": "desk", "port": 0}),
        ("add_contact", {"name": "desk", "rtp_port": 70000}),
        ("add_contact", {"name": "desk", "tx_formats": ["L16"] * 33}),
        ("create_account", {"username": "u" * 65}),
        ("create_account", {"username": "desk", "password": "p" * 257}),
    ],
)
def test_service_schemas_reject_oversized_or_invalid_resource_inputs(
    service: str,
    payload: dict[str, object],
) -> None:
    schema = load_service_schemas()[service]

    with pytest.raises(vol.Invalid):
        schema(payload)


def test_phonebook_schema_accepts_bounded_standard_media_fields() -> None:
    schema = load_service_schemas()["add_contact"]

    validated = schema(
        {
            "name": "Desk phone",
            "sip_uri": "sip:desk@192.0.2.10:5060;transport=tcp",
            "port": 5060,
            "rtp_port": 40000,
            "tx_rate": 48000,
            "rx_rate": "auto",
            "tx_formats": ["OPUS/48000/2/20", "PCMA/8000/1/20"],
            "max_payload_bytes": 1400,
        }
    )

    assert validated["port"] == 5060
    assert validated["tx_formats"] == ["OPUS/48000/2/20", "PCMA/8000/1/20"]


def test_advanced_assist_context_is_opt_in_and_persisted_by_config_flow() -> None:
    config_source = CONFIG.read_text()
    flow_source = CONFIG_FLOW.read_text()

    assert (
        'CONF_ASSIST_ADVANCED_CALL_CONTEXT = "assist_advanced_call_context"'
        in CONST.read_text()
    )
    assert "data.get(CONF_ASSIST_ADVANCED_CALL_CONTEXT, False)" in config_source
    assert "CONF_ASSIST_ADVANCED_CALL_CONTEXT" in flow_source
    assert "BooleanSelector()" in flow_source
    assert "user_input.get(CONF_ASSIST_ADVANCED_CALL_CONTEXT, False)" in flow_source
    assert "data = dict(self._base_input)" in flow_source


def test_phone_and_assist_flows_share_one_complete_route_namespace() -> None:
    flow_source = CONFIG_FLOW.read_text()
    mappings = flow_source[
        flow_source.index("def _entry_route_mappings(") :
        flow_source.index("def _port_selector(")
    ]
    assist = flow_source[
        flow_source.index("async def async_step_assist(") :
        flow_source.index("async def async_step_trunk(")
    ]
    phone_aliases = flow_source[
        flow_source.index("    def _validate_aliases(") :
        flow_source.index("    def _normalized_common(")
    ]

    assert "include_assist: bool = True" in mappings
    assert "phone_subentries(entry)" in mappings
    assert "CONF_PHONEBOOK_CONTACTS" in mappings
    assert 'data.get("sip_accounts"' in mappings
    assert "include_assist=False" in assist
    assert "_entry_route_mappings(" in phone_aliases
    assert "include_assist=False" not in phone_aliases


@pytest.mark.parametrize(
    "path",
    [STRINGS, TRANSLATIONS / "en.json", TRANSLATIONS / "it.json"],
)
def test_assist_config_explains_agent_instructions_and_untrusted_context(
    path: Path,
) -> None:
    assist = json.loads(path.read_text())["config"]["step"]["assist"]
    description = assist["description"]
    advanced = assist["data_description"]["assist_advanced_call_context"].lower()

    assert 'Incoming SIP call from "Daniele".' in description
    assert "Instructions" in description
    assert "do not repeat their name" in description
    assert "s**t" in description
    assert "f**k" in description
    assert "untrusted" in advanced or "non attendibili" in advanced
    assert (
        "not authentication" in advanced or "non costituisce autenticazione" in advanced
    )
