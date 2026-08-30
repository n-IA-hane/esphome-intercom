#!/usr/bin/env python3
"""Qualify X-Bees to P4 audio, delayed video and teardown on real hardware."""

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
    active_call_tokens,
    candidate_revision,
    norm,
    phonebook_contact,
    qualification_token,
    wait_new_call_id,
    wait_phonebook_contains,
)
from run_p4_wildix_hil import (  # noqa: E402
    wait_media,
    wait_quiescent,
)


def load_xbees_modules(lab_root: Path) -> tuple[Any, Any]:
    """Load the preserved Android lab only after validating its location."""

    lab_root = lab_root.expanduser().resolve()
    required = (lab_root / "xbees_driver.py", lab_root / "xbees_stress.py")
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"Android X-Bees lab is incomplete: {missing}")
    sys.path.insert(0, str(lab_root))
    driver = importlib.import_module("xbees_driver")
    stress = importlib.import_module("xbees_stress")
    return driver, stress


async def wait_esp(esp: EspApi, wanted: set[str], timeout: float = 18) -> None:
    await esp.wait("voip_state", wanted, timeout=timeout)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    xb, stress = load_xbees_modules(args.android_lab)
    token = qualification_token(args)
    ha = HaRest(args.ha_url, token, insecure=args.insecure)
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
            raw_volume = esp.values.get("master_volume")
            original_volume = float(raw_volume) if raw_volume is not None else None
            original_auto = norm(esp.values.get("auto_answer")) == "on"
            original_extension = str(esp.values.get("voip_extension") or "")
            try:
                current_phonebook = await ha.state("sensor.voip_phonebook")
                existing_contact = phonebook_contact(
                    current_phonebook,
                    args.destination,
                )
                if existing_contact is not None and existing_contact.get("id") != args.p4_endpoint_id:
                    raise AssertionError(
                        f"destination {args.destination!r} already belongs to "
                        f"{existing_contact.get('id')!r}"
                    )
                await esp.number("master_volume", 1.0)
                await esp.switch("auto_answer", True)
                if original_extension != args.destination:
                    await esp.text("voip_extension", args.destination)
                contact = await wait_phonebook_contains(
                    ha,
                    args.destination,
                    timeout=args.registration_settle,
                )
                if contact.get("id") != args.p4_endpoint_id:
                    raise AssertionError(
                        f"destination {args.destination!r} resolves to "
                        f"{contact.get('id')!r}, not the P4"
                    )
                for cycle in range(1, args.cycles + 1):
                    started = time.monotonic()
                    before = await ws.softphone_state()
                    existing_call_ids = active_call_tokens(before)
                    await asyncio.to_thread(xb.call_ha_route, args.destination)
                    # The route completes asynchronously after the final SIP
                    # INFO digit. Use persistent HA relay counters as the first
                    # oracle instead of a short ESPHome state edge.
                    call_id = await wait_new_call_id(ws, existing_call_ids)
                    audio = await wait_media(
                        ws,
                        video=False,
                        timeout=20,
                        call_id=call_id,
                    )
                    await wait_esp(esp, {"in_call"}, timeout=3)
                    if norm(esp.values.get(video_switch)) != "on":
                        await esp.switch(video_switch, True)
                    await asyncio.sleep(args.audio_hold)
                    await wait_esp(esp, {"in_call"}, timeout=2)
                    await asyncio.to_thread(xb.enable_video)
                    video = await wait_media(
                        ws,
                        video=True,
                        timeout=20,
                        call_id=call_id,
                    )
                    await asyncio.sleep(args.video_hold)
                    await wait_esp(esp, {"in_call"}, timeout=2)
                    terminal_side = "xbees" if cycle % 2 else "p4"
                    if terminal_side == "xbees":
                        await asyncio.to_thread(stress.xbees_hangup)
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
                    await asyncio.to_thread(stress.xbees_hangup)
                with suppress(Exception):
                    await asyncio.to_thread(xb.ensure_inbox)
                with suppress(Exception):
                    await esp.switch("auto_answer", original_auto)
                if original_volume is not None:
                    with suppress(Exception):
                        await esp.number("master_volume", original_volume)
                if original_extension != args.destination:
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
    parser.add_argument("--cycles", type=int, default=2)
    parser.add_argument("--audio-hold", type=float, default=4)
    parser.add_argument("--video-hold", type=float, default=6)
    parser.add_argument("--registration-settle", type=float, default=12)
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
