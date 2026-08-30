#!/usr/bin/env python3
"""Qualify P4 to the registered X-Bees Wildix extension."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
import importlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT.parent / "android-voip-lab"
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(LAB))

import xbees_driver as xbees  # noqa: E402
import xbees_stress as stress  # noqa: E402
from live_voip_qualification import (  # noqa: E402
    DEFAULT_AUTH_FILE,
    DEFAULT_ESPS,
    DEFAULT_TOKEN_FILE,
    EspApi,
    HaWs,
    candidate_revision,
    norm,
    qualification_token,
    wait_new_relay_call_id,
)
from run_p4_wildix_hil import wait_media, wait_quiescent  # noqa: E402


async def run(args: argparse.Namespace) -> dict:
    xbees.preflight()
    await asyncio.to_thread(xbees.ensure_inbox)
    token = qualification_token(args)
    spec = replace(DEFAULT_ESPS["p4"], host=args.p4_host)
    async with HaWs(args.ha_url, token, insecure=args.insecure) as ws:
        async with EspApi(spec, capture_info_logs=True) as esp:
            original_volume = float(esp.values.get("master_volume") or 0)
            try:
                await esp.number("master_volume", 1.0)
                if norm(esp.values.get("send_video")) == "on":
                    raise AssertionError(
                        "P4 Send Video is unexpectedly active before the call"
                    )
                before = await ws.softphone_state()
                await esp.service("start_call", {"dest": args.extension})
                await esp.wait(
                    "voip_state", {"remote_ringing", "in_call"}, timeout=20
                )
                await asyncio.to_thread(stress.xbees_tap_matching, "answer")
                call_id = await wait_new_relay_call_id(
                    ws, set(before.get("rtp_relays") or {}), timeout=20
                )
                audio = await wait_media(ws, video=False, timeout=20, call_id=call_id)
                await esp.wait("voip_state", {"in_call"}, timeout=5)
                await esp.switch("send_video", True)
                await asyncio.sleep(1)
                await asyncio.to_thread(xbees.enable_video)
                video = await wait_media(ws, video=True, timeout=25, call_id=call_id)
                if args.screenshot is not None:
                    await asyncio.to_thread(xbees.screenshot, args.screenshot)
                await asyncio.sleep(args.video_hold)
                await esp.service("hangup_call")
                quiescent = await wait_quiescent(ws, esp)
                return {
                    "status": "passed",
                    "candidate": candidate_revision(),
                    "call_id": call_id,
                    "audio": audio.get("audio") or {},
                    "video": video.get("video") or {},
                    "quiescent": (quiescent.get("media_debug") or {}).get(
                        "runtime_resources"
                    ),
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            finally:
                if norm(esp.values.get("voip_state")) != "idle":
                    with suppress(Exception):
                        await esp.service("hangup_call")
                with suppress(Exception):
                    await asyncio.to_thread(stress.xbees_hangup)
                with suppress(Exception):
                    await asyncio.to_thread(xbees.ensure_inbox)
                with suppress(Exception):
                    await esp.number("master_volume", original_volume)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ha-url", default="http://192.168.1.10:8123")
    parser.add_argument("--token", default="")
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--auth-file", type=Path, default=DEFAULT_AUTH_FILE)
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--p4-host", required=True)
    parser.add_argument("--extension", default="426")
    parser.add_argument("--video-hold", type=float, default=8)
    parser.add_argument("--screenshot", type=Path)
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
