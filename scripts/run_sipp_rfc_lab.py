#!/usr/bin/env python3
"""Exercise advanced SIP dialog behavior against the real HA lab runtime."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "scripts"))

from inbound_routing_qualification import FlowSnapshot, HomeAssistantApi  # noqa: E402
from live_voip_qualification import candidate_revision  # noqa: E402
from run_answered_sipp_lab import lab_token, runtime_quiescence  # noqa: E402
from run_ha_real_matrix import _registered_local_trunk  # noqa: E402


@dataclass(frozen=True, slots=True)
class Case:
    name: str
    scenario: str
    destination: str
    hangup_after: float | None


CASES = (
    Case("reliable_provisional", "reliable-provisional.xml", "5550101", 0.4),
    Case("session_refresh", "session-refresh.xml", "5550102", 2.8),
    Case("session_expiry", "session-expiry.xml", "5550103", None),
    Case("remote_fork_late_2xx", "remote-fork-late-2xx.xml", "5550104", 0.8),
)


def _run_case(
    case: Case,
    *,
    api: HomeAssistantApi,
    ha_url: str,
    token: str,
    host: str,
    port: int,
    out_dir: Path,
) -> dict[str, object]:
    scenario = ROOT / "tests" / "sipp" / case.scenario
    before = asyncio.run(runtime_quiescence(ha_url, token))
    started = time.monotonic()
    command = [
        "sipp",
        "-sf",
        str(scenario),
        "-i",
        host,
        "-p",
        str(port),
        "-m",
        "1",
        "-timeout",
        "12s",
        "-trace_msg",
        "-trace_err",
        "-trace_stat",
        "-nostdin",
    ]
    process = subprocess.Popen(
        command,
        cwd=out_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output = ""
    try:
        time.sleep(0.15)
        if process.poll() is not None:
            output, _ = process.communicate()
            raise RuntimeError(f"SIPp did not start: {output[-2000:]}")
        api.service("voip_stack", "call", {"destination": case.destination})
        if case.hangup_after is not None:
            time.sleep(case.hangup_after)
            api.service("voip_stack", "hangup")
        output, _ = process.communicate(timeout=10)
        if process.returncode:
            raise RuntimeError(f"SIPp exited {process.returncode}: {output[-3000:]}")
        after = asyncio.run(runtime_quiescence(ha_url, token))
        traces = sorted(
            out_dir.glob(f"{scenario.stem}_*_messages.log"),
            key=lambda path: path.stat().st_mtime_ns,
        )
        if not traces:
            raise RuntimeError(f"SIPp did not produce a trace for {case.name}")
        trace = traces[-1]
        trace_text = trace.read_text(encoding="utf-8", errors="replace")
        required_trace = {
            "reliable_provisional": ("PRACK sip:", "RAck: 41 1 INVITE"),
            "session_refresh": ("UPDATE sip:", "Session-Expires: 4;refresher=uac"),
            "session_expiry": ("Session-Expires: 4;refresher=uas", "BYE sip:"),
            "remote_fork_late_2xx": ("tag=fork-a", "tag=fork-b", "BYE sip:"),
        }[case.name]
        missing = [value for value in required_trace if value not in trace_text]
        if missing:
            raise RuntimeError(
                f"SIP trace for {case.name} lacks required evidence: {missing}"
            )
        return {
            "name": case.name,
            "status": "passed",
            "duration_s": round(time.monotonic() - started, 3),
            "before": before,
            "after": after,
            "trace": trace.name,
            "trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
            "messages": {
                "prack": trace_text.count("PRACK sip:"),
                "update": trace_text.count("UPDATE sip:"),
                "ack": trace_text.count("ACK sip:"),
                "bye": trace_text.count("BYE sip:"),
                "reliable_183": trace_text.count("183 Session Progress"),
            },
        }
    finally:
        if process.poll() is None:
            process.terminate()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ha-url", default="http://127.0.0.1:18123")
    parser.add_argument(
        "--credentials",
        type=Path,
        default=Path("/home/codex/ha-voip-lab/.credentials"),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19999)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "test_captures" / "sipp-rfc-lab",
    )
    parser.add_argument("--only", action="append", default=[])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_dir = args.out_dir / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=False)
    token = lab_token(args.ha_url, args.credentials)
    api = HomeAssistantApi(base_url=args.ha_url, token=token)
    ha_version = api.get("/api/config").get("version")
    snapshot = FlowSnapshot.capture(api)
    results: list[dict[str, object]] = []
    selected = set(args.only)
    trunk = {
        "trunk_transport": "udp",
        "trunk_server": args.host,
        "trunk_port": args.port,
        "trunk_domain": args.host,
        "trunk_username": "sipp",
        "trunk_auth_username": "sipp",
        "trunk_password": "qualification-only",
        "trunk_expires": 60,
        "trunk_outbound_proxy": "",
    }
    try:
        with _registered_local_trunk(run_dir, args.port) as registered_contact:
            snapshot.apply(
                api,
                mode="direct",
                automation=False,
                default_target="",
                trunk_override=trunk,
            )
            registered_contact()
        for case in CASES:
            if selected and case.name not in selected:
                continue
            try:
                result = _run_case(
                    case,
                    api=api,
                    ha_url=args.ha_url,
                    token=token,
                    host=args.host,
                    port=args.port,
                    out_dir=run_dir,
                )
            except Exception as error:  # noqa: BLE001 - retain complete evidence.
                result = {
                    "name": case.name,
                    "status": "failed",
                    "error": str(error),
                }
                with suppress(Exception):
                    api.service("voip_stack", "hangup")
                with suppress(Exception):
                    result["after_recovery"] = asyncio.run(
                        runtime_quiescence(args.ha_url, token)
                    )
            results.append(result)
            print(f"{result['status'].upper()} {case.name}")
    finally:
        with suppress(Exception):
            snapshot.apply(api)
        with suppress(Exception):
            asyncio.run(runtime_quiescence(args.ha_url, token))

    artifact = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "candidate": candidate_revision(),
        "home_assistant": ha_version,
        "results": results,
    }
    summary = run_dir / "summary.json"
    summary.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"artifact": str(summary), "results": results}, indent=2))
    return 1 if any(result["status"] != "passed" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
