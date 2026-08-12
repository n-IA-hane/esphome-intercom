#!/usr/bin/env python3
"""Print a quiescent, call-scoped Home Assistant runtime snapshot."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from run_answered_sipp_lab import runtime_quiescence
from tools.ha_playwright_auth import ha_token


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ha-url", required=True)
    parser.add_argument("--auth-file", type=Path, required=True)
    args = parser.parse_args()
    os.environ["HA_URL"] = args.ha_url
    os.environ["HA_PLAYWRIGHT_REFRESH_CREDENTIALS"] = str(args.auth_file)
    token = ha_token()
    print(json.dumps(asyncio.run(runtime_quiescence(args.ha_url, token))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
