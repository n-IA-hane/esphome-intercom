"""Keep the qualified FULL S3 heap policy scoped to its PSRAM hardware owner."""
from pathlib import Path

import pytest
from esphome import yaml_util

from tests.native_profile import profile

ROOT = Path(__file__).resolve().parents[1]
KEY = "CONFIG_SPIRAM_MALLOC_ALWAYSINTERNAL"


@pytest.mark.parametrize("entrypoint", [
    "full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full.yaml",
    "full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full-afe.yaml",
])
def test_full_s3_psram_board_selects_qualified_heap_threshold(entrypoint):
    document = profile(entrypoint)
    assert document["psram"]["mode"] == "octal"
    assert int(document["esp32"]["framework"]["sdkconfig_options"][KEY]) == 512


def test_lite_and_shared_s3_default_keep_existing_threshold():
    lite = profile("full-experience/single-bus/generic-s3-full-lite-aec.yaml")
    assert lite["esp32"]["flash_size"] == "4MB"
    assert int(lite["esp32"]["framework"]["sdkconfig_options"][KEY]) == 4096
    base = yaml_util.load_yaml(ROOT / "packages/platform/esp32s3_base.yaml")
    assert int(base["esp32"]["framework"]["sdkconfig_options"][KEY]) == 4096


def test_p4_keeps_its_original_sdk_heap_selection():
    p4 = profile("full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml")
    assert KEY not in p4["esp32"]["framework"]["sdkconfig_options"]
