"""User-facing documentation contracts."""

from __future__ import annotations

import ast
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

import voluptuous as vol
import yaml

from tests.support.service_schemas import load_service_schemas, schema_fields


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
AUTOMATION_COOKBOOK = DOCS / "AUTOMATION_DIALPLAN.md"
AUTOMATION_ROUTING = ROOT / "custom_components" / "voip_stack" / "automation_routing.py"
DTMF_EVENTS = ROOT / "custom_components" / "voip_stack" / "dtmf_events.py"
WEBSOCKET_API = ROOT / "custom_components" / "voip_stack" / "websocket_api.py"
MARKDOWN_FILES = (
    ROOT / "README.md",
    *sorted(path for path in DOCS.rglob("*.md") if "private" not in path.parts),
)
CURRENT_SERVICE_DOCS = (
    ROOT / "README.md",
    DOCS / "AUTOMATION_DIALPLAN.md",
    DOCS / "GROUPS.md",
    DOCS / "SERVICES.md",
    DOCS / "SIP_TRUNK.md",
    DOCS / "reference.md",
    DOCS / "troubleshooting.md",
)
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
HTML_IMAGE = re.compile(r"<img\b[^>]*\bsrc=[\"']([^\"']+)", re.IGNORECASE)
YAML_FENCE = re.compile(r"```ya?ml\s*\n(.*?)```", re.DOTALL)
MARKDOWN_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*$", re.MULTILINE)
YAML_EXAMPLES = tuple(sorted((ROOT / "examples").glob("*.yaml")))


def _service_fields() -> dict[str, set[str]]:
    services = yaml.safe_load(
        (ROOT / "custom_components/voip_stack/services.yaml").read_text()
    )
    result: dict[str, set[str]] = {}
    for name, service in services.items():
        fields: set[str] = set()
        for key, value in (service.get("fields") or {}).items():
            if key == "advanced":
                fields.update((value.get("fields") or {}).keys())
            else:
                fields.add(key)
        result[name] = fields
    return result


def _walk_service_calls(value):
    if isinstance(value, dict):
        action = value.get("action") or value.get("service")
        if isinstance(action, str) and action.startswith("voip_stack."):
            yield action.removeprefix("voip_stack."), set(
                (value.get("data") or {}).keys()
            )
        for child in value.values():
            yield from _walk_service_calls(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_service_calls(child)


def _walk_service_payloads(value):
    if isinstance(value, dict):
        action = value.get("action") or value.get("service")
        if isinstance(action, str) and action.startswith("voip_stack."):
            yield action.removeprefix("voip_stack."), dict(value.get("data") or {})
        for child in value.values():
            yield from _walk_service_payloads(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_service_payloads(child)


def _walk_event_received_triggers(value):
    if isinstance(value, dict):
        if value.get("trigger") == "event.received":
            yield value
        for child in value.values():
            yield from _walk_event_received_triggers(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_event_received_triggers(child)


def _literal_assignment(path: Path, name: str):
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path.relative_to(ROOT)}")


def _mapping_keys_in_function(path: Path, function_name: str, mapping_name: str) -> set[str]:
    tree = ast.parse(path.read_text())
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    )
    keys: set[str] = set()
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == mapping_name
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            keys.add(node.slice.value)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == mapping_name
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.add(node.args[0].value)
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == mapping_name for target in node.targets)
            and isinstance(node.value, ast.Dict)
        ):
            keys.update(
                key.value
                for key in node.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            )
    return keys


def _markdown_anchors(document: Path) -> set[str]:
    """Return the GitHub-style anchors used by project documentation links."""

    anchors: set[str] = set()
    for heading in MARKDOWN_HEADING.findall(document.read_text()):
        value = re.sub(r"<[^>]+>", "", heading).lower().strip()
        value = re.sub(r"[^\w\- ]", "", value)
        anchors.add(re.sub(r"\s+", "-", value))
    return anchors


def test_local_markdown_links_resolve() -> None:
    broken: list[str] = []
    anchors = {document.resolve(): _markdown_anchors(document) for document in MARKDOWN_FILES}
    for document in MARKDOWN_FILES:
        for raw_target in MARKDOWN_LINK.findall(document.read_text()):
            target = raw_target.strip().split()[0].strip("<>")
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            relative, _, fragment = target.partition("#")
            resolved = (document.parent / relative).resolve()
            if relative and not resolved.exists():
                broken.append(f"{document.relative_to(ROOT)} -> {target}")
            elif fragment and resolved in anchors and unquote(fragment) not in anchors[resolved]:
                broken.append(
                    f"{document.relative_to(ROOT)} -> {target} (unknown anchor)"
                )
    assert not broken, "Broken local documentation links:\n" + "\n".join(broken)


def test_every_documentation_image_is_embedded() -> None:
    embedded: set[str] = set()
    for document in MARKDOWN_FILES:
        if document.name == "MEDIA_SHOT_LIST.md":
            continue
        text = document.read_text()
        for target in (*MARKDOWN_IMAGE.findall(text), *HTML_IMAGE.findall(text)):
            embedded.add(Path(urlsplit(target).path).name)
    orphaned = sorted(
        image.name for image in (DOCS / "images").iterdir() if image.name not in embedded
    )
    assert not orphaned, "Unembedded docs/images assets: " + ", ".join(orphaned)


def test_ha_services_are_documented_and_examples_use_real_fields() -> None:
    service_fields = _service_fields()
    services_guide = (DOCS / "SERVICES.md").read_text()
    reference = (DOCS / "reference.md").read_text()
    for service in service_fields:
        token = f"voip_stack.{service}"
        assert token in services_guide, f"{token} missing from SERVICES.md"
        assert token in reference, f"{token} missing from reference.md"

    errors: list[str] = []
    for document in CURRENT_SERVICE_DOCS:
        for index, block in enumerate(YAML_FENCE.findall(document.read_text()), 1):
            try:
                parsed = yaml.safe_load(block)
            except yaml.YAMLError:
                # ESPHome examples may contain custom !include/!lambda tags.
                continue
            for service, fields in _walk_service_calls(parsed):
                if service not in service_fields:
                    errors.append(
                        f"{document.relative_to(ROOT)} block {index}: unknown voip_stack.{service}"
                    )
                    continue
                unknown = fields - service_fields[service]
                if unknown:
                    errors.append(
                        f"{document.relative_to(ROOT)} block {index}: "
                        f"voip_stack.{service} fields {sorted(unknown)}"
                    )
    for document in YAML_EXAMPLES:
        parsed = yaml.safe_load(document.read_text())
        for service, fields in _walk_service_calls(parsed):
            if service not in service_fields:
                errors.append(
                    f"{document.relative_to(ROOT)}: unknown voip_stack.{service}"
                )
                continue
            unknown = fields - service_fields[service]
            if unknown:
                errors.append(
                    f"{document.relative_to(ROOT)}: "
                    f"voip_stack.{service} fields {sorted(unknown)}"
                )
    assert not errors, "Invalid documented HA service examples:\n" + "\n".join(errors)


def test_service_descriptions_are_accepted_by_the_runtime_schemas() -> None:
    """Prevent UI documentation from exposing fields rejected by the backend."""

    described = _service_fields()
    runtime = load_service_schemas()
    errors: list[str] = []
    for service, schema in runtime.items():
        if service not in described:
            errors.append(f"voip_stack.{service} has a runtime schema but no services.yaml entry")
            continue
        runtime_fields = schema_fields(schema)
        unsupported = described[service] - runtime_fields
        if unsupported:
            errors.append(
                f"voip_stack.{service}: services.yaml exposes unsupported fields "
                f"{sorted(unsupported)}"
            )
    assert not errors, "Service description/runtime schema mismatch:\n" + "\n".join(errors)


def test_automation_cookbook_actions_pass_the_runtime_schemas() -> None:
    """Validate every cookbook VoIP action with the schema HA actually registers."""

    runtime = load_service_schemas()
    errors: list[str] = []
    for index, block in enumerate(YAML_FENCE.findall(AUTOMATION_COOKBOOK.read_text()), 1):
        parsed = yaml.safe_load(block)
        for service, payload in _walk_service_payloads(parsed):
            schema = runtime.get(service)
            if schema is None:
                errors.append(f"block {index}: voip_stack.{service} has no runtime schema")
                continue
            try:
                schema(payload)
            except vol.Invalid as err:
                errors.append(f"block {index}: voip_stack.{service}: {err}")
    assert not errors, "Cookbook actions rejected by runtime schemas:\n" + "\n".join(errors)


def test_automation_cookbook_uses_real_event_types_and_payload_fields() -> None:
    """Pin cookbook event recipes to the event types and fields emitted by code."""

    event_types = set(_literal_assignment(AUTOMATION_ROUTING, "AUTOMATION_EVENT_TYPES"))
    call_fields = _mapping_keys_in_function(WEBSOCKET_API, "_fire_call_event", "event")
    dtmf_fields = _mapping_keys_in_function(DTMF_EVENTS, "publish_dtmf_event", "payload")
    errors: list[str] = []
    for index, block in enumerate(YAML_FENCE.findall(AUTOMATION_COOKBOOK.read_text()), 1):
        parsed = yaml.safe_load(block)
        received_types: set[str] = set()
        for trigger in _walk_event_received_triggers(parsed):
            configured = (trigger.get("options") or {}).get("event_type") or []
            if isinstance(configured, str):
                configured = [configured]
            received_types.update(str(item) for item in configured)
        unknown_types = received_types - event_types
        if unknown_types:
            errors.append(f"block {index}: unknown event types {sorted(unknown_types)}")

        allowed_fields = dtmf_fields if received_types == {"dtmf"} else call_fields
        for condition in (parsed or {}).get("conditions", []):
            if not isinstance(condition, dict):
                continue
            entity_id = str(condition.get("entity_id") or "")
            attribute = str(condition.get("attribute") or "")
            if entity_id.startswith("event.") and attribute and attribute not in allowed_fields:
                errors.append(
                    f"block {index}: event attribute {attribute!r} is not emitted "
                    f"for {sorted(received_types)}"
                )
    assert not errors, "Cookbook event contract errors:\n" + "\n".join(errors)
