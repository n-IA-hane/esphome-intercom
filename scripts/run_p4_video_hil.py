#!/usr/bin/env python3
"""Qualify a real P4 audio call upgraded to bidirectional SIP video."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from live_voip_qualification import DEFAULT_ESPS, EspApi, norm  # noqa: E402


def validate_peer_result(result: object, codec: str) -> dict[str, object]:
    """Require signaling and media evidence from both directions."""

    if not isinstance(result, dict):
        raise AssertionError("video peer did not produce a JSON object")
    required = {
        "ok": result.get("ok") is True,
        "initial answer": 200 in result.get("sip_statuses", []),
        "re-INVITE answer": result.get("reinvite_status") == 200,
        "bidirectional video": result.get("reinvite_video_direction") == "sendrecv",
        "selected codec": codec.upper()
        in str(result.get("reinvite_negotiated_video") or "").upper(),
        "local BYE answer": result.get("bye_response_status") == 200,
        "audio transmit": int(result.get("audio_tx_packets") or 0) > 0,
        "audio receive": int(result.get("audio_rx_packets") or 0) > 0,
        "video transmit": int(result.get("video_tx_packets") or 0) > 0,
        "video receive": int(result.get("video_rx_packets") or 0) > 0,
        "video frame boundaries": int(result.get("video_rx_marker_packets") or 0) > 0,
        "valid received video": int(
            result.get("video_rx_capture_invalid_payloads") or 0
        )
        == 0,
    }
    failed = [name for name, passed in required.items() if not passed]
    if failed:
        raise AssertionError(f"P4 video evidence missing: {', '.join(failed)}")
    return {
        "audio_tx_packets": int(result["audio_tx_packets"]),
        "audio_rx_packets": int(result["audio_rx_packets"]),
        "video_tx_packets": int(result["video_tx_packets"]),
        "video_rx_packets": int(result["video_rx_packets"]),
        "video_rx_marker_packets": int(result["video_rx_marker_packets"]),
        "reinvite_status": int(result["reinvite_status"]),
        "bye_response_status": int(result["bye_response_status"]),
    }


async def wait_state(esp: EspApi, wanted: set[str], timeout: float) -> None:
    await esp.wait("voip_state", wanted, timeout=timeout)


async def run(args: argparse.Namespace) -> dict[str, object]:
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    peer_output = output.parent / "peer.json"
    spec = DEFAULT_ESPS["p4"]
    if args.esp_host:
        from dataclasses import replace

        spec = replace(spec, host=args.esp_host)

    async with EspApi(spec) as esp:
        maximum_volume = float(os.environ.get("VOIP_TEST_VOLUME_PERCENT", "1"))
        if "master_volume" not in esp.values:
            raise AssertionError("P4 does not expose the master volume state")
        current_volume = float(esp.values["master_volume"])
        if current_volume > maximum_volume:
            raise AssertionError(
                f"P4 volume is {current_volume:g} percent, limit is {maximum_volume:g}"
            )
        original_auto_answer = norm(esp.values.get("auto_answer")) == "on"
        await esp.switch("auto_answer", True)
        command = [
            str(ROOT / ".venv/bin/python"),
            str(ROOT / "tools/sip_video_peer.py"),
            "--host",
            args.sip_host,
            "--port",
            str(args.sip_port),
            "--target",
            args.target,
            "--local-ip",
            args.local_ip,
            "--codec",
            args.codec,
            "--direction",
            "sendrecv",
            "--add-video-after",
            "1",
            "--expect-reinvite-status",
            "200",
            "--duration",
            str(args.duration),
            "--out",
            str(peer_output),
        ]
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await wait_state(esp, {"ringing", "in_call"}, 15)
            await wait_state(esp, {"in_call"}, 15)
            _, stderr = await asyncio.wait_for(
                process.communicate(), args.duration + 25
            )
            if process.returncode:
                raise AssertionError(
                    f"video peer failed with {process.returncode}: "
                    f"{stderr.decode(errors='replace')[-1000:]}"
                )
            await wait_state(esp, {"idle"}, 15)
            evidence = validate_peer_result(
                json.loads(peer_output.read_text()), args.codec
            )
            result: dict[str, object] = {
                "schema_version": 1,
                "status": "passed",
                "duration_seconds": round(time.monotonic() - started, 3),
                "device": "p4",
                "codec": args.codec,
                "volume_percent": current_volume,
                "media": evidence,
            }
            output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            return result
        finally:
            if process.returncode is None:
                process.terminate()
                await process.wait()
            await esp.switch("auto_answer", original_auto_answer)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sip-host", required=True)
    parser.add_argument("--sip-port", type=int, default=5060)
    parser.add_argument("--local-ip", required=True)
    parser.add_argument("--codec", choices=("h264", "jpeg"), default="h264")
    parser.add_argument("--target", default="Waveshare P4 Touch")
    parser.add_argument("--esp-host")
    parser.add_argument("--duration", type=float, default=8)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except Exception as error:  # noqa: BLE001
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {"schema_version": 1, "status": "failed", "error": str(error)}, indent=2
            )
            + "\n",
            encoding="utf-8",
        )
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
