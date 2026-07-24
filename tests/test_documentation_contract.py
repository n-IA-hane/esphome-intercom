"""User-facing documentation contracts."""

from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

import yaml


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
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
