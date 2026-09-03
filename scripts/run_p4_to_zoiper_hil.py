#!/usr/bin/env python3
"""Qualify P4 to registered Zoiper audio, video and teardown."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parent / "android-voip-lab"))

import zoiper_driver as zoiper  # noqa: E402
from live_voip_qualification import (  # noqa: E402
    DEFAULT_AUTH_FILE,
    DEFAULT_ESPS,
    DEFAULT_TOKEN_FILE,
    EspApi,
    HaRest,
    HaWs,
    candidate_revision,
    norm,
    phonebook_contact,
    qualification_token,
    wait_new_relay_call_id,
)
from run_p4_wildix_hil import wait_media, wait_quiescent  # noqa: E402
from run_p4_zoiper_hil import wait_remote_video_offer  # noqa: E402


async def run(args: argparse.Namespace) -> dict:
    zoiper.preflight()
    await asyncio.to_thread(zoiper.ensure_dialpad)
    token = qualification_token(args)
    ha = HaRest(args.ha_url, token, insecure=args.insecure)
    deadline = asyncio.get_running_loop().time() + 12
    contact = None
    while asyncio.get_running_loop().time() < deadline:
        contact = phonebook_contact(
            await ha.state("sensor.voip_phonebook"), args.zoiper_contact
        )
        if contact is not None and (contact.get("metadata") or {}).get("registered"):
            break
        await asyncio.sleep(0.2)
    else:
        raise AssertionError(
            f"{args.zoiper_contact!r} is not an active registered SIP account"
        )

    spec = replace(DEFAULT_ESPS["p4"], host=args.p4_host)
    async with HaWs(args.ha_url, token, insecure=args.insecure) as ws:
        async with EspApi(spec, capture_info_logs=True) as esp:
            original_volume = float(esp.values.get("master_volume") or 0)
            original_video = norm(esp.values.get("send_video")) == "on"
            try:
                await esp.number("master_volume", 1.0)
                await esp.switch("send_video", False)
                before = await ws.softphone_state()
                await esp.service("start_call", {"dest": args.zoiper_contact})
                await esp.wait("voip_state", {"remote_ringing", "in_call"}, timeout=15)
                await asyncio.to_thread(zoiper.answer)
                call_id = await wait_new_relay_call_id(
                    ws, set(before.get("rtp_relays") or {})
                )
                audio = await wait_media(ws, video=False, timeout=20, call_id=call_id)
                await esp.wait("voip_state", {"in_call"}, timeout=5)
                await asyncio.to_thread(zoiper.enable_video)
                await wait_remote_video_offer(ws, call_id)
                if norm(esp.values.get("send_video")) != "on":
                    await esp.switch("send_video", True)
                video = await wait_media(ws, video=True, timeout=20, call_id=call_id)
                if args.screenshot is not None:
                    await asyncio.to_thread(zoiper.screenshot, args.screenshot)
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
                    await asyncio.to_thread(zoiper.hangup)
                with suppress(Exception):
                    await esp.number("master_volume", original_volume)
                with suppress(Exception):
                    await esp.switch("send_video", original_video)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ha-url", default="http://192.168.1.10:8123")
    parser.add_argument("--token", default="")
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--auth-file", type=Path, default=DEFAULT_AUTH_FILE)
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--p4-host", required=True)
    parser.add_argument("--zoiper-contact", default="Codex")
    parser.add_argument("--video-hold", type=float, default=6)
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
