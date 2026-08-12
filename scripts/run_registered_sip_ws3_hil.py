#!/usr/bin/env python3
"""Qualify a registered SIP client against the real WS3 in both BYE directions."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from ha_softphone_matrix import BareSip  # noqa: E402
from live_voip_qualification import DEFAULT_ESPS, EspApi, norm  # noqa: E402


async def run(args: argparse.Namespace) -> dict[str, object]:
    results: list[dict[str, object]] = []
    async with EspApi(DEFAULT_ESPS["ws3"]) as esp:
        original_auto_answer = norm(esp.values.get("auto_answer")) == "on"
        await esp.switch("auto_answer", True)
        try:
            for terminal in ("registered", "ws3"):
                peer = BareSip(args.baresip_config, headless_audio=True)
                try:
                    peer.dial(args.target)
                    await esp.wait("voip_state", {"in_call"}, timeout=10)
                    await asyncio.sleep(args.media_seconds)
                    if norm(esp.values.get("voip_state")) != "in_call":
                        raise AssertionError("WS3 did not remain established beyond media watchdog")
                    if terminal == "registered":
                        peer.hangup()
                    else:
                        await esp.button("call")
                        peer.wait_for_any(("call closed", "session closed"), 10)
                    await esp.wait("voip_state", {"idle"}, timeout=10)
                    results.append({"terminal": terminal, "status": "passed"})
                finally:
                    peer.close()
        finally:
            with suppress(Exception):
                await esp.button("call")
            await esp.switch("auto_answer", original_auto_answer)
    return {"schema_version": 1, "status": "passed", "cycles": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baresip-config", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--media-seconds", type=float, default=8)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = asyncio.run(run(args))
    except BaseException as error:
        result = {"schema_version": 1, "status": "failed", "error": str(error)}
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return 2
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
