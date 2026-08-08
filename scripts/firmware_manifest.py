#!/usr/bin/env python3
"""Record immutable metadata for every compiled ESPHome firmware artifact."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_size(value: str) -> int:
    text = str(value or "").strip().lower()
    units = {"k": 1024, "m": 1024 * 1024}
    return int(text[:-1], 0) * units[text[-1]] if text[-1:] in units else int(text, 0)


def _app_partition_bytes(build_root: Path) -> int:
    path = build_root / "partitions.csv"
    if not path.is_file():
        return 0
    with path.open(encoding="utf-8") as stream:
        rows = csv.reader(line for line in stream if not line.lstrip().startswith("#"))
        for row in rows:
            if len(row) >= 5 and row[1].strip() == "app":
                return _parse_size(row[4])
    return 0


def _section_sizes(description: dict[str, object], build_dir: Path) -> dict[str, int]:
    compiler_value = str(description.get("c_compiler") or "").strip()
    if not compiler_value:
        return {}
    compiler = Path(compiler_value)
    elf = build_dir / str(description.get("app_elf") or "")
    size_tool = compiler.with_name(compiler.name.removesuffix("gcc") + "size")
    if not size_tool.is_file() or not elf.is_file():
        return {}
    output = subprocess.run(
        [str(size_tool), "-A", str(elf)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout
    sections: dict[str, int] = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[0].startswith("."):
            try:
                sections[fields[0]] = int(fields[1])
            except ValueError:
                continue
    return sections


def build_records(root: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    # ``**`` may discover the same nested ESPHome build through more than one
    # expansion.  A build artifact is evidence once, irrespective of how the
    # glob reached it.
    descriptions = sorted(
        set(root.glob("yamls/**/.esphome/**/build/*/build/project_description.json"))
    )
    for description_path in descriptions:
        build_dir = description_path.parent
        binaries = sorted(build_dir.glob("firmware*.bin"))
        if not binaries:
            continue
        description = json.loads(description_path.read_text(encoding="utf-8"))
        build_info_path = build_dir.parent / "build_info.json"
        build_info = (
            json.loads(build_info_path.read_text(encoding="utf-8"))
            if build_info_path.is_file()
            else {}
        )
        sections = _section_sizes(description, build_dir)
        application = build_dir / str(description.get("app_bin") or "")
        partition_bytes = _app_partition_bytes(build_dir.parent)
        records.append(
            {
                "node": str(description.get("project_name") or build_dir.parent.name),
                "target": str(description.get("target") or ""),
                "esphome_version": str(
                    build_info.get("esphome_version")
                    or description.get("project_version")
                    or ""
                ),
                "esp_idf_version": str(description.get("git_revision") or ""),
                "config_hash": build_info.get("config_hash"),
                "memory": {
                    "dram_data_bytes": sections.get(".dram0.data", 0),
                    "dram_bss_bytes": sections.get(".dram0.bss", 0),
                    "iram_text_bytes": sections.get(".iram0.text", 0),
                    "flash_text_bytes": sections.get(".flash.text", 0),
                    "flash_rodata_bytes": sections.get(".flash.rodata", 0),
                    "application_bytes": application.stat().st_size
                    if application.is_file()
                    else 0,
                    "application_partition_bytes": partition_bytes,
                    "application_partition_headroom_bytes": max(
                        0,
                        partition_bytes - application.stat().st_size,
                    )
                    if partition_bytes and application.is_file()
                    else 0,
                },
                "artifacts": [
                    {
                        "path": str(binary.relative_to(root)),
                        "bytes": binary.stat().st_size,
                        "sha256": sha256(binary),
                    }
                    for binary in binaries
                ],
            }
        )
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=Path("firmware-manifest.json"))
    parser.add_argument("--expected", type=int, default=6)
    args = parser.parse_args()

    root = args.root.resolve()
    records = build_records(root)
    if len(records) != args.expected:
        raise RuntimeError(
            f"expected {args.expected} compiled firmware profiles, found {len(records)}"
        )
    payload = {"schema_version": 1, "firmware": records}
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
