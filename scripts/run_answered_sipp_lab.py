#!/usr/bin/env python3
"""Run answered SIP calls through HA with caller and callee BYE ownership."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from ha_softphone_matrix import BareSip  # noqa: E402
from live_voip_qualification import HaWs, candidate_revision  # noqa: E402


def _credentials(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip():
            values[key.strip()] = value.strip()
    return values


def _post_json(url: str, data: dict[str, object], *, form: bool = False) -> dict:
    payload = urlencode(data).encode() if form else json.dumps(data).encode()
    request = Request(
        url,
        data=payload,
        headers={
            "Content-Type": (
                "application/x-www-form-urlencoded" if form else "application/json"
            )
        },
        method="POST",
    )
    with urlopen(request, timeout=8) as response:  # noqa: S310 - explicit lab URL.
        return json.load(response)


def lab_token(base_url: str, credentials_path: Path) -> str:
    credentials = _credentials(credentials_path)
    client_id = "https://home-assistant.io/iOS"
    flow = _post_json(
        f"{base_url}/auth/login_flow",
        {
            "client_id": client_id,
            "handler": ["homeassistant", None],
            "redirect_uri": client_id,
        },
    )
    result = _post_json(
        f"{base_url}/auth/login_flow/{flow['flow_id']}",
        {
            "client_id": client_id,
            "username": credentials["username"],
            "password": credentials["password"],
        },
    )
    token = _post_json(
        f"{base_url}/auth/token",
        {
            "grant_type": "authorization_code",
            "code": result["result"],
            "client_id": client_id,
        },
        form=True,
    )
    return str(token["access_token"])


async def runtime_quiescence(base_url: str, token: str) -> dict[str, object]:
    deadline = time.monotonic() + 5
    snapshot: dict = {}
    resources: dict = {}
    async with HaWs(base_url, token) as websocket:
        while time.monotonic() < deadline:
            snapshot = await websocket.softphone_state()
            resources = dict(
                snapshot.get("runtime_resources")
                or (snapshot.get("media_debug") or {}).get("runtime_resources")
                or {}
            )
            if (
                snapshot.get("state") == "idle"
                and int(snapshot.get("active_dialogs") or 0) == 0
                and not snapshot.get("pending_call_ids")
                and resources.get("call_scoped_quiescent") is True
            ):
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Home Assistant call resources did not quiesce")
    return {
        "state": snapshot.get("state"),
        "active_dialogs": int(snapshot.get("active_dialogs") or 0),
        "call_scoped_quiescent": resources.get("call_scoped_quiescent"),
        "resource_counts": resources.get("resource_counts") or {},
    }


def run_answered_case(
    name: str,
    scenario: Path,
    *,
    target_host: str,
    target_port: int,
    extension: str,
    local_port: int,
    callee_config: Path,
    capture_dir: Path,
    ha_url: str,
    token: str,
) -> dict[str, object]:
    callee = BareSip(callee_config, headless_audio=True)
    started = time.monotonic()
    command = [
        "sipp",
        f"{target_host}:{target_port}",
        "-sf",
        str(scenario),
        "-s",
        extension,
        "-i",
        "127.0.0.1",
        "-p",
        str(local_port),
        "-m",
        "1",
        "-timeout",
        "15s",
        "-trace_msg",
        "-trace_err",
        "-trace_stat",
        "-nostdin",
    ]
    process = subprocess.Popen(
        command,
        cwd=capture_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        state = callee.wait_for_any(("Incoming call", "Call established"), 8)
        if "call established" not in state.lower():
            callee.command("/accept")
            callee.wait_for("Call established", 8)
        if name == "callee_bye":
            time.sleep(0.25)
            callee.hangup()
        output, _ = process.communicate(timeout=12)
        if process.returncode != 0:
            raise RuntimeError(
                f"SIPp failed with {process.returncode}: {output[-2000:]}"
            )
        if name == "callee_bye":
            callee.wait_for("terminate call", 5)
        else:
            callee.wait_for_any(("call closed", "session closed"), 5)
        cleanup = asyncio.run(runtime_quiescence(ha_url, token))
        return {
            "scenario": name,
            "status": "passed",
            "duration_s": round(time.monotonic() - started, 3),
            "cleanup": cleanup,
        }
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)
        callee.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=15060)
    parser.add_argument("--extension", default="2502")
    parser.add_argument("--ha-url", default="http://127.0.0.1:18123")
    parser.add_argument(
        "--credentials",
        type=Path,
        default=Path("/home/codex/ha-voip-lab/.credentials"),
    )
    parser.add_argument(
        "--callee-config",
        type=Path,
        default=Path("/home/codex/ha-voip-lab/baresip-sink"),
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "test_captures" / "sipp-answered-lab",
    )
    parser.add_argument(
        "--quiescence-only",
        action="store_true",
        help="check that HA has no call-scoped resources and exit",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    token = lab_token(args.ha_url, args.credentials)
    if args.quiescence_only:
        print(json.dumps(asyncio.run(runtime_quiescence(args.ha_url, token))))
        return 0
    cases = (
        ("caller_bye", ROOT / "tests/sipp/answered-local-bye.xml", 16062),
        ("callee_bye", ROOT / "tests/sipp/answered-remote-bye.xml", 16064),
    )
    results: list[dict[str, object]] = []
    for name, scenario, local_port in cases:
        try:
            result = run_answered_case(
                name,
                scenario,
                target_host=args.host,
                target_port=args.port,
                extension=args.extension,
                local_port=local_port,
                callee_config=args.callee_config,
                capture_dir=args.out_dir,
                ha_url=args.ha_url,
                token=token,
            )
        except Exception as error:  # noqa: BLE001 - preserve all case evidence.
            result = {"scenario": name, "status": "failed", "error": str(error)}
        results.append(result)
        print(f"{result['status'].upper()} {name}")
    artifact = {
        "schema_version": 2,
        "created_at": datetime.now(UTC).isoformat(),
        "candidate": candidate_revision(),
        "results": results,
    }
    (args.out_dir / "summary.json").write_text(
        json.dumps(artifact, indent=2), encoding="utf-8"
    )
    return 1 if any(item["status"] != "passed" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
