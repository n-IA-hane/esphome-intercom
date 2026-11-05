"""Tests for immutable firmware build evidence."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "firmware_manifest.py"


def load_script():
    spec = importlib.util.spec_from_file_location("firmware_manifest", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load firmware manifest script")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repository_notices(tmp_path: Path, monkeypatch):
    module = load_script()
    repository = tmp_path / "repository"
    paths = (
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "licenses/ESPHOME-LICENSE.txt",
        "licenses/MICRO-OPUS-LICENSE.txt",
        "licenses/MICRO-OPUS-NOTICE.txt",
        "licenses/OPUS-COPYING.txt",
        "licenses/MICRO-OGG-DEMUXER-LICENSE.txt",
        "licenses/OTHER-LICENSE.txt",
    )
    notices = {path: f"Original notice for {path}\r\n".encode() for path in paths}
    for name, content in notices.items():
        source = repository / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
    monkeypatch.setattr(module, "ROOT", repository)
    return module, notices


def test_manifest_records_toolchain_config_and_binary_hash(tmp_path: Path) -> None:
    build_root = tmp_path / "yamls/voip-only/.esphome/build/test-phone"
    output = build_root / "build"
    output.mkdir(parents=True)
    (output / "project_description.json").write_text(
        json.dumps(
            {
                "project_name": "test-phone",
                "project_version": "2026.8.0",
                "git_revision": "v5.5.5",
                "target": "esp32s3",
            }
        ),
        encoding="utf-8",
    )
    (build_root / "build_info.json").write_text(
        json.dumps({"config_hash": 123, "esphome_version": "2026.8.0"}),
        encoding="utf-8",
    )
    firmware = output / "firmware.factory.bin"
    firmware.write_bytes(b"qualified firmware")

    records = load_script().build_records(tmp_path)

    assert records == [
        {
            "node": "test-phone",
            "target": "esp32s3",
            "esphome_version": "2026.8.0",
            "esp_idf_version": "v5.5.5",
            "config_hash": 123,
            "memory": {
                "dram_data_bytes": 0,
                "dram_bss_bytes": 0,
                "iram_text_bytes": 0,
                "flash_text_bytes": 0,
                "flash_rodata_bytes": 0,
                "application_bytes": 0,
                "application_partition_bytes": 0,
                "application_partition_headroom_bytes": 0,
            },
            "artifacts": [
                {
                    "path": str(firmware.relative_to(tmp_path)),
                    "bytes": 18,
                    "sha256": "a73f7eb84c526b35d9de799110f78869cff494b3d24eefa6910bb528b49165f4",
                }
            ],
        }
    ]


def test_manifest_does_not_duplicate_nested_esphome_builds(tmp_path: Path) -> None:
    build_root = tmp_path / "yamls/profile/.esphome/.esphome/build/phone"
    output = build_root / "build"
    output.mkdir(parents=True)
    (output / "project_description.json").write_text(
        json.dumps({"project_name": "phone"}),
        encoding="utf-8",
    )
    (output / "firmware.bin").write_bytes(b"firmware")

    records = load_script().build_records(tmp_path)

    assert [record["node"] for record in records] == ["phone"]


def test_manifest_binds_profile_candidate_and_factory_binary(
    tmp_path: Path, repository_notices
) -> None:
    module, notices = repository_notices
    firmware = tmp_path / "build/firmware.factory.bin"
    firmware.parent.mkdir()
    firmware.write_bytes(b"candidate")
    records = [
        {
            "node": "waveshare-s3",
            "profile": "waveshare-s3-full",
            "artifact": {
                "path": str(firmware.relative_to(tmp_path)),
                "sha256": module.sha256(firmware),
            },
        }
    ]
    bundle = tmp_path / "evidence/firmware"

    payload = module.bind_candidate(
        records,
        {"firmware_profiles": [{"id": "waveshare-s3-full"}]},
        {"candidate_id": "candidate"},
        source_lock_sha256="lock",
        root=tmp_path,
        bundle=bundle,
    )

    artifact = payload["firmware"][0]["artifact"]
    assert payload["candidate_id"] == "candidate"
    assert payload["source_lock_sha256"] == "lock"
    assert payload["firmware"][0]["profile"] == "waveshare-s3-full"
    assert (bundle.parent / artifact["path"]).read_bytes() == b"candidate"
    assert {name: (bundle / name).read_bytes() for name in notices} == notices


@pytest.mark.parametrize(
    "missing",
    (
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "licenses/ESPHOME-LICENSE.txt",
        "licenses/MICRO-OPUS-LICENSE.txt",
        "licenses/MICRO-OPUS-NOTICE.txt",
        "licenses/OPUS-COPYING.txt",
        "licenses/MICRO-OGG-DEMUXER-LICENSE.txt",
    ),
)
def test_manifest_refuses_firmware_without_required_notice(
    tmp_path: Path, repository_notices, missing: str
) -> None:
    module, _ = repository_notices
    (module.ROOT / missing).unlink()
    firmware = tmp_path / "firmware.factory.bin"
    firmware.write_bytes(b"candidate")
    bundle = tmp_path / "evidence/firmware"

    with pytest.raises(RuntimeError, match="redistribution notice is missing") as error:
        module.bind_candidate(
            [
                {
                    "profile": "waveshare-s3-full",
                    "artifact": {"path": firmware.name},
                }
            ],
            {"firmware_profiles": [{"id": "waveshare-s3-full"}]},
            {"candidate_id": "candidate"},
            source_lock_sha256="lock",
            root=tmp_path,
            bundle=bundle,
        )

    assert missing in str(error.value)
    assert not bundle.exists()


def test_manifest_index_uses_repository_notices_and_external_firmware(
    tmp_path: Path, repository_notices, monkeypatch
) -> None:
    module, notices = repository_notices
    evidence = tmp_path / "external-evidence"
    evidence.mkdir()
    (evidence / "firmware.factory.bin").write_bytes(b"external candidate")
    index = evidence / "build-index.json"
    index.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "firmware": [
                    {
                        "profile": "waveshare-s3-full",
                        "artifact": {"path": "firmware.factory.bin"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    plan = evidence / "plan.json"
    plan.write_text(
        json.dumps({"firmware_profiles": [{"id": "waveshare-s3-full"}]}),
        encoding="utf-8",
    )
    candidate = evidence / "candidate.json"
    candidate.write_text(json.dumps({"candidate_id": "external"}), encoding="utf-8")
    bundle = evidence / "firmware"
    output = evidence / "firmware-manifest.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--index", str(index),
            "--expected", "1",
            "--plan", str(plan),
            "--candidate", str(candidate),
            "--bundle", str(bundle),
            "--output", str(output),
        ],
    )

    assert module.main() == 0

    assert (bundle / "waveshare-s3-full/firmware.factory.bin").read_bytes() == (
        b"external candidate"
    )
    assert {name: (bundle / name).read_bytes() for name in notices} == notices
    assert json.loads(output.read_text(encoding="utf-8"))["candidate_id"] == "external"
