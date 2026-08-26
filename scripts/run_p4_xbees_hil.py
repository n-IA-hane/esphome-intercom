#!/usr/bin/env python3
"""Qualify X-Bees to P4 audio, delayed video and teardown on real hardware."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, "/home/codex/android-voip-lab")

from live_voip_qualification import (  # noqa: E402
    DEFAULT_AUTH_FILE,
    DEFAULT_ESPS,
    DEFAULT_TOKEN_FILE,
    EspApi,
    HaWs,
    candidate_revision,
    norm,
    qualification_token,
)
from run_p4_wildix_hil import wait_media, wait_quiescent  # noqa: E402
import xbees_driver as xb  # noqa: E402
from xbees_stress import xbees_hangup  # noqa: E402


async def wait_esp(esp: EspApi, wanted: set[str], timeout: float = 18) -> None:
    await esp.wait("voip_state", wanted, timeout=timeout)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    token = qualification_token(args)
    spec = replace(DEFAULT_ESPS["p4"], host=args.p4_host)
    cycles: list[dict[str, Any]] = []
    async with HaWs(args.ha_url, token, insecure=args.insecure) as ws:
        async with EspApi(spec, capture_info_logs=True) as esp:
            video_switch = next(
                (name for name in esp.entities if name in {"send_video", "video_send"}),
                "",
            )
            if not video_switch:
                raise AssertionError("P4 firmware does not expose Send Video")
            original_volume = float(esp.values.get("master_volume") or 1)
            original_auto = norm(esp.values.get("auto_answer")) == "on"
            original_extension = str(esp.values.get("voip_extension") or "")
            await esp.number("master_volume", 1.0)
            await esp.switch("auto_answer", True)
            if original_extension != args.destination:
                await esp.text("voip_extension", args.destination)
                await asyncio.sleep(args.registration_settle)
            try:
                for cycle in range(1, args.cycles + 1):
                    started = time.monotonic()
                    await asyncio.to_thread(xb.call_ha_route, args.destination)
                    # The route completes asynchronously after the final SIP
                    # INFO digit. Use persistent HA relay counters as the first
                    # oracle instead of a short ESPHome state edge.
                    audio = await wait_media(ws, video=False, timeout=20)
                    await wait_esp(esp, {"in_call"}, timeout=3)
                    if norm(esp.values.get(video_switch)) != "on":
                        await esp.switch(video_switch, True)
                    await asyncio.sleep(args.audio_hold)
                    await wait_esp(esp, {"in_call"}, timeout=2)
                    await asyncio.to_thread(xb.enable_video)
                    video = await wait_media(ws, video=True, timeout=20)
                    await asyncio.sleep(args.video_hold)
                    await wait_esp(esp, {"in_call"}, timeout=2)
                    terminal_side = "xbees" if cycle % 2 else "p4"
                    if terminal_side == "xbees":
                        await asyncio.to_thread(xbees_hangup)
                    else:
                        await esp.service("hangup_call")
                    quiescent = await wait_quiescent(ws, esp)
                    cycles.append(
                        {
                            "cycle": cycle,
                            "terminal_side": terminal_side,
                            "seconds": round(time.monotonic() - started, 3),
                            "audio": {
                                key: int(audio["audio"].get(key) or 0)
                                for key in (
                                    "left_rx_packets",
                                    "left_tx_packets",
                                    "right_rx_packets",
                                    "right_tx_packets",
                                )
                            },
                            "video": {
                                key: int(video["video"].get(key) or 0)
                                for key in (
                                    "left_rx_packets",
                                    "left_tx_packets",
                                    "right_rx_packets",
                                    "right_tx_packets",
                                )
                            },
                            "quiescent": (quiescent.get("media_debug") or {}).get(
                                "runtime_resources"
                            ),
                        }
                    )
                    await asyncio.sleep(1)
            finally:
                if norm(esp.values.get("voip_state")) != "idle":
                    with suppress(Exception):
                        await esp.service("hangup_call")
                with suppress(Exception):
                    await asyncio.to_thread(xbees_hangup)
                with suppress(Exception):
                    await asyncio.to_thread(xb.ensure_inbox)
                with suppress(Exception):
                    await esp.switch("auto_answer", original_auto)
                with suppress(Exception):
                    await esp.number("master_volume", original_volume)
                if original_extension and original_extension != args.destination:
                    with suppress(Exception):
                        await esp.text("voip_extension", original_extension)
    return {
        "status": "passed",
        "candidate": candidate_revision(),
        "cycles": cycles,
        "timestamp": datetime.now(UTC).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ha-url", default="http://192.168.1.10:8123")
    parser.add_argument("--token", default="")
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--auth-file", type=Path, default=DEFAULT_AUTH_FILE)
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--p4-host", default="192.168.1.57")
    parser.add_argument("--destination", default="1000")
    parser.add_argument("--cycles", type=int, default=2)
    parser.add_argument("--audio-hold", type=float, default=4)
    parser.add_argument("--video-hold", type=float, default=6)
    parser.add_argument("--registration-settle", type=float, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args))
    except BaseException as error:
        result = {
            "status": "failed",
            "error": f"{type(error).__name__}: {error}",
            "candidate": candidate_revision(),
            "timestamp": datetime.now(UTC).isoformat(),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
