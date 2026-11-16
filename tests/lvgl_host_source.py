"""Reviewed managed LVGL identities for real host ownership contracts."""

import hashlib
from pathlib import Path

import yaml


_REFERENCES = {
    "9.5.0": {
        "component_hash": "184e532558c1c45fefed631f3e235423d22582aafb4630f3e8885c35281a49ae",
    },
    "9.6.0": {
        "component_hash": "d5e723db3a3feca71e34e333a6598b7624e3822577d374e1d44f0e18374952e1",
        "repository_commit": "80ca777e37a2b176770726a02e07a6fb79ef0b39",
        "owner_sources": {
            "src/widgets/label/lv_label.c": "7b0eb4b14e3aa2beb99c1a51bac5adc55998233d7ddd4b196184338426153525",
            "src/misc/lv_text.c": "a7cb697f8378233bef1691787e5051cc55ba97712dd61cb08fbbad88e8ab56ee",
        },
    },
}


def lvgl_source_tag(source: Path) -> str:
    """Validate the actual selected package before compiling any oracle."""
    metadata = yaml.safe_load((source / "idf_component.yml").read_text())
    version = metadata["version"]
    assert version in _REFERENCES, f"Unreviewed managed LVGL version: {version}"
    reference = _REFERENCES[version]
    assert (source / ".component_hash").read_text().strip() == reference["component_hash"]
    if "repository_commit" in reference:
        assert metadata["repository_info"]["commit_sha"] == reference["repository_commit"]
    for filename, expected in reference.get("owner_sources", {}).items():
        assert hashlib.sha256((source / filename).read_bytes()).hexdigest() == expected, filename
    major, minor, _patch = version.split(".")
    return f"real_lvgl_{major}_{minor}"
