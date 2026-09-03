#!/usr/bin/env python3
"""Qualify Zoiper to P4 audio, delayed video and teardown on real hardware."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
import importlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

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
    wait_phonebook_contains,
)
from run_p4_wildix_hil import wait_media, wait_quiescent  # noqa: E402


def load_zoiper(lab_root: Path) -> Any:
    lab_root = lab_root.expanduser().resolve()
    driver_path = lab_root / "zoiper_driver.py"
    if not driver_path.is_file():
        raise RuntimeError(f"Android Zoiper lab is incomplete: {driver_path}")
    sys.path.insert(0, str(lab_root))
    driver = importlib.import_module("zoiper_driver")
    driver.preflight()
    return driver


async def wait_remote_video_offer(
    ws: HaWs, call_id: str, timeout: float = 12
) -> dict[str, Any]:
    """Wait until the Zoiper re-INVITE creates the one-way video leg."""

    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = await ws.softphone_state()
        relay = dict((last.get("rtp_relays") or {}).get(call_id) or {})
        video = dict(relay.get("video") or {})
        if any(int(video.get(name) or 0) > 0 for name in video if name.endswith("_packets")):
            return video
        await asyncio.sleep(0.2)
    raise AssertionError(f"Zoiper video offer did not create a media leg: {last}")


async def run(args: argparse.Namespace) -> dict[str, Any]:
    zoiper = load_zoiper(args.android_lab)
    token = qualification_token(args)
    ha = HaRest(args.ha_url, token, insecure=args.insecure)
    spec = replace(DEFAULT_ESPS["p4"], host=args.p4_host)
    async with HaWs(args.ha_url, token, insecure=args.insecure) as ws:
        async with EspApi(spec, capture_info_logs=True) as esp:
            original_volume = float(esp.values.get("master_volume") or 0)
            original_auto = norm(esp.values.get("auto_answer")) == "on"
            original_video = norm(esp.values.get("send_video")) == "on"
            original_extension = str(esp.values.get("voip_extension") or "")
            try:
                existing = phonebook_contact(
                    await ha.state("sensor.voip_phonebook"), args.destination
                )
                if existing is not None and existing.get("id") != args.p4_endpoint_id:
                    raise AssertionError(
                        f"destination {args.destination!r} already belongs to "
                        f"{existing.get('id')!r}"
                    )
                await esp.number("master_volume", 1.0)
                await esp.switch("auto_answer", True)
                await esp.switch("send_video", False)
                await esp.text("voip_extension", args.destination)
                contact = await wait_phonebook_contains(
                    ha, args.destination, timeout=args.registration_settle
                )
                if contact.get("id") != args.p4_endpoint_id:
                    raise AssertionError(
                        f"destination {args.destination!r} resolves to "
                        f"{contact.get('id')!r}, not the P4"
                    )

                before = await ws.softphone_state()
                await asyncio.to_thread(zoiper.call, args.destination)
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
                await asyncio.to_thread(zoiper.hangup)
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
                    await esp.switch("auto_answer", original_auto)
                with suppress(Exception):
                    await esp.switch("send_video", original_video)
                with suppress(Exception):
                    await esp.number("master_volume", original_volume)
                if original_extension != args.destination:
                    with suppress(Exception):
                        await esp.text("voip_extension", original_extension)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ha-url", default="http://192.168.1.10:8123")
    parser.add_argument("--token", default="")
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--auth-file", type=Path, default=DEFAULT_AUTH_FILE)
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--p4-host", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--p4-endpoint-id", default="waveshare-p4-touch")
    parser.add_argument(
        "--android-lab",
        type=Path,
        default=Path(
            os.environ.get("ANDROID_VOIP_LAB", ROOT.parent / "android-voip-lab")
        ),
    )
    parser.add_argument("--registration-settle", type=float, default=12)
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
