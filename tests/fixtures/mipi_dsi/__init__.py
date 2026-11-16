"""Build the complete owned MIPI driver with the canonical host runner."""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import os
from collections.abc import Iterator

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.core import CORE
from esphome.loader import FileResource
from esphome.types import ConfigType
from tests.testing_helpers import ComponentManifestOverride

ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class OwnerResource(FileResource):
    """Keep the canonical destination while reading the actual local owner."""

    @contextmanager
    def path(self) -> Iterator[Path]:
        yield ROOT / "esphome/components/mipi_dsi" / self.resource


def override_manifest(manifest: ComponentManifestOverride) -> None:
    manifest.dependencies = ["display"]
    manifest.auto_load = []
    manifest.config_schema = cv.Schema({})
    manifest.resources = [
        OwnerResource("esphome.components.mipi_dsi", name)
        for name in ("mipi_dsi.cpp", "mipi_dsi.h")
    ]

    async def to_code_testing(config: ConfigType) -> None:
        cg.add_define("USE_ESP32_VARIANT_ESP32P4")
        cg.add_define("USE_MIPI_DSI_RGB565_OWNED_TRANSFORM")
        cg.add_build_flag("-DCONFIG_CACHE_L2_CACHE_LINE_SIZE=64")
        cg.add_build_flag(
            f"-DMIPI_TEST_IDF_MAJOR={os.environ.get('MIPI_TEST_IDF_MAJOR', '5')}"
        )
        cg.add_build_flag(f"-I{CORE.relative_src_path('mipi_dsi', '__sdk')}")

    manifest.to_code = to_code_testing
