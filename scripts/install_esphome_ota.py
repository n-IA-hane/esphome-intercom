#!/usr/bin/env python3
"""Install the attested HIL OTA image on one ESPHome device."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from esphome.espota2 import run_ota


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=3232)
    args = parser.parse_args()
    if os.environ.get("HIL_FIRMWARE_INSTALLABLE") != "true":
        raise RuntimeError(
            "refusing OTA image without an installable firmware attestation"
        )
    firmware = Path(os.environ["HIL_FIRMWARE_PATH"])
    status, _address = run_ota(
        args.host,
        args.port,
        os.environ.get("ESPHOME_OTA_PASSWORD"),
        firmware,
    )
    return status


if __name__ == "__main__":
    raise SystemExit(main())
