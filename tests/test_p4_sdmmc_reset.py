"""Use official fixed-source fixtures as specifications, not SDK selection pins."""

import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/espressif_p4_official"
SOURCE = FIXTURES / "esp-idf/components/esp_system/port/soc/esp32p4/system_internal.c"


def test_exact_official_fixtures_keep_provenance_and_licenses():
    manifest = json.loads((FIXTURES / "PROVENANCE.json").read_text())
    commits = {
        "espressif/esp-idf": "dd47ea3696b419fdb19cf747a6b96e2c4f6abcc5",
        "espressif/esp-video-components": "79ae6117e58e0cf831c8e96820af72e877b5fb82",
    }
    assert len(manifest["files"]) == 5
    for item in manifest["files"]:
        data = (FIXTURES / item["path"]).read_bytes()
        assert item["commit"] == commits[item["repository"]]
        assert not item["modified"]
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
        blob = b"blob " + str(len(data)).encode() + b"\0" + data
        assert hashlib.sha1(blob, usedforsecurity=False).hexdigest() == item["git_blob"]
        assert item["url"] == (
            f"https://github.com/{item['repository']}/blob/"
            f"{item['commit']}/{item['source_path']}"
        )
        if item["source_path"].endswith(".c"):
            assert f"SPDX-License-Identifier: {item['license']}" in data.decode()
    assert "Apache License" in (FIXTURES / "esp-idf/LICENSE").read_text()
    assert "ESPRESSIF MIT License" in (
        FIXTURES / "esp-video-components/esp_video/LICENSE"
    ).read_text()


def test_sdmmc_reset_precedes_memory_bus_and_other_peripheral_resets():
    source = SOURCE.read_text()
    function = source.split("void esp_system_reset_modules_on_exit(void)", 1)[1]
    function = function.split("static void IRAM_ATTR", 1)[0]
    operations = re.findall(
        r"(SET|CLEAR)_PERI_REG_MASK\(\s*([^,]+),\s*([^\)]+)\);", function
    )
    assert operations[:2] == [
        ("SET", "LP_CLKRST_HP_SDMMC_EMAC_RST_CTRL_REG", "LP_CLKRST_RST_EN_SDMMC"),
        ("CLEAR", "LP_CLKRST_HP_SDMMC_EMAC_RST_CTRL_REG", "LP_CLKRST_RST_EN_SDMMC"),
    ]
    assert [row for row in operations if "SDMMC" in row[2]] == operations[:2]
    assert operations[2] == (
        "SET", "HP_SYS_CLKRST_HP_RST_EN0_REG", "HP_SYS_CLKRST_REG_RST_EN_MSPI_AXI"
    )
    assert operations[3] == (
        "SET", "HP_SYS_CLKRST_HP_RST_EN0_REG", "HP_SYS_CLKRST_REG_RST_EN_DUAL_MSPI_AXI"
    )
    assert function.index("dw_gdma_ll_channel_abort") < function.index(
        "LP_CLKRST_RST_EN_SDMMC"
    )
    restart = source.split("static void IRAM_ATTR", 1)[1]
    assert restart.index("esp_system_reset_modules_on_exit();") < restart.index(
        "esp_cpu_reset("
    )


def test_retired_replacement_components_are_absent_but_build_local_fix_is_available():
    for path in (
        "idf_components/esp_driver_ppa",
        "idf_components/esp_video_csi_compat",
    ):
        assert not (ROOT / path).exists()
    for package in (ROOT / "packages").rglob("*.yaml"):
        text = package.read_text()
        for name in ("esp_driver_ppa", "esp_video_csi_compat"):
            assert name not in text, str(package.relative_to(ROOT))
    assert (ROOT / "esphome/components/p4_sdmmc_reset/p4_sdmmc_reset.cmake").is_file()
