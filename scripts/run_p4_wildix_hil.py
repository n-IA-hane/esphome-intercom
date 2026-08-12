#!/usr/bin/env python3
"""Qualify P4 audio, delayed video and teardown through a real Wildix peer."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import ha_softphone_matrix  # noqa: E402
from ha_softphone_matrix import BareSip  # noqa: E402
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


async def wait_peer(peer: BareSip, needle: str, timeout: float) -> str:
    return await asyncio.to_thread(peer.wait_for, needle, timeout)


async def wait_esp(esp: EspApi, wanted: set[str], timeout: float = 15) -> None:
    await esp.wait("voip_state", wanted, timeout=timeout)


async def wait_media(
    ws: HaWs,
    *,
    video: bool,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = await ws.softphone_state()
        relays = dict(last.get("rtp_relays") or {})
        for call_id, relay in relays.items():
            audio_ok = all(
                int(relay.get(counter) or 0) > 10
                for counter in (
                    "left_rx_packets",
                    "left_tx_packets",
                    "right_rx_packets",
                    "right_tx_packets",
                )
            )
            video_state = dict(relay.get("video") or {})
            video_ok = not video or all(
                int(video_state.get(counter) or 0) > 0
                for counter in (
                    "left_rx_packets",
                    "left_tx_packets",
                    "right_rx_packets",
                    "right_tx_packets",
                )
            )
            if audio_ok and video_ok:
                return {"call_id": call_id, "audio": relay, "video": video_state}
        await asyncio.sleep(0.2)
    raise AssertionError(f"media did not become bidirectional: {last}")


async def wait_quiescent(ws: HaWs, esp: EspApi, timeout: float = 15) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = await ws.softphone_state()
        resources = dict((last.get("media_debug") or {}).get("runtime_resources") or {})
        if (
            norm(esp.values.get("voip_state")) == "idle"
            and norm(last.get("state")) == "idle"
            and int(last.get("active_dialogs") or 0) == 0
            and resources.get("call_scoped_quiescent") is True
        ):
            return last
        await asyncio.sleep(0.2)
    raise AssertionError(f"call did not quiesce: {last}")


async def run(args: argparse.Namespace) -> dict[str, Any]:
    os.environ["TEST_CAPTURE_DIR"] = str(args.out_dir)
    ha_softphone_matrix.TEST_CAPTURE_DIR = args.out_dir
    token = qualification_token(args)
    spec = replace(DEFAULT_ESPS["p4"], host=args.p4_host)
    peer: BareSip | None = None
    async with HaWs(args.ha_url, token, insecure=args.insecure) as ws:
        async with EspApi(spec, capture_info_logs=True) as esp:
            video_switch = next(
                (
                    name
                    for name in esp.entities
                    if name in {"send_video", "video_send"}
                ),
                "",
            )
            if not video_switch:
                raise AssertionError("P4 firmware does not expose a video-send switch")
            original_volume = float(esp.values.get("master_volume") or 1)
            original_video = norm(esp.values.get(video_switch)) == "on"
            await esp.number("master_volume", 1.0)
            await esp.switch(video_switch, False)
            try:
                peer = await asyncio.to_thread(
                    BareSip,
                    args.wildix_config,
                    headless_audio=True,
                    video_codec="VP8",
                )
                # Preserve the user's dial string exactly. Numeric Wildix
                # extensions and explicit service codes have different
                # dial-plan semantics and must not be silently rewritten.
                await esp.service("start_call", {"dest": args.destination})
                await wait_peer(peer, "Incoming call", 18)
                peer.command("/accept")
                await wait_peer(peer, "Call established", 12)
                await wait_esp(esp, {"in_call"})
                # Exercise the standard established-dialog direction change:
                # remove local video, hold an audio-only interval, then add it
                # again through re-INVITE.
                await esp.switch(video_switch, False)
                audio = await wait_media(ws, video=False, timeout=12)

                # Stay beyond the firmware media-watchdog interval. A transport
                # that merely reached 200 OK but carries no RTP must fail here.
                await asyncio.sleep(args.audio_hold)
                await wait_esp(esp, {"in_call"}, timeout=2)

                video: dict[str, Any] = {"video": {}}
                if not args.audio_only:
                    await esp.switch(video_switch, True)
                    video = await wait_media(ws, video=True, timeout=18)
                    await asyncio.sleep(args.video_hold)
                    await wait_esp(esp, {"in_call"}, timeout=2)

                await esp.service("hangup_call")
                quiescent = await wait_quiescent(ws, esp)
                error_logs = [
                    item
                    for item in esp.logs
                    if any(
                        marker in str(item.get("message") or "")
                        for marker in (
                            "I2S error",
                            "completion event queue overflowed",
                            "completion record queue full",
                        )
                    )
                ]
                if error_logs:
                    raise AssertionError(f"P4 audio errors observed: {error_logs}")
                return {
                    "status": "passed",
                    "candidate": candidate_revision(),
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
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            finally:
                if norm(esp.values.get("voip_state")) != "idle":
                    with suppress(Exception):
                        await esp.service("hangup_call")
                if peer is not None:
                    await asyncio.to_thread(peer.close)
                with suppress(Exception):
                    await esp.switch(video_switch, original_video)
                with suppress(Exception):
                    await esp.number("master_volume", original_volume)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ha-url", default="http://192.168.1.10:8123")
    parser.add_argument("--token", default="")
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--auth-file", type=Path, default=DEFAULT_AUTH_FILE)
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--p4-host", default="192.168.1.57")
    parser.add_argument("--destination", default="426")
    parser.add_argument(
        "--wildix-config",
        type=Path,
        default=Path("/home/codex/.baresip-wildix-426-video"),
    )
    parser.add_argument("--audio-hold", type=float, default=17)
    parser.add_argument("--audio-only", action="store_true")
    parser.add_argument("--video-hold", type=float, default=5)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
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
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
